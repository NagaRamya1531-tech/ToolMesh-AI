import asyncio
import os
from secrets import compare_digest
from uuid import UUID, uuid4

import grpc
from sqlalchemy.ext.asyncio import create_async_engine

from shared.database import execute, rows
from shared.protos import it_pb2 as pb
from shared.protos.it_pb2_grpc import (
    OperationsServicer,
    add_OperationsServicer_to_server,
)


class Operations(OperationsServicer):
    def __init__(self, db, secret):
        self.db = db
        self.secret = secret

    async def authorize(self, context):
        value = dict(context.invocation_metadata()).get("authorization", "")
        if not compare_digest(value.encode(), f"Bearer {self.secret}".encode()):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "AUTH_REQUIRED")

    async def CheckServiceHealth(self, request, context):
        await self.authorize(context)
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT id AS service_id,status FROM it.services WHERE id=:id AND customer_id=:customer
            """,
                id=request.service_id,
                customer=request.customer_id,
            )
        if not found:
            await context.abort(grpc.StatusCode.NOT_FOUND, "RECORD_NOT_AVAILABLE")
        return pb.ServiceHealth(**found[0])

    async def ListIncidents(self, request, context):
        await self.authorize(context)
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT id::text,service_id,title,status FROM it.incidents
                WHERE customer_id=:customer ORDER BY created_at DESC,id LIMIT 50
            """,
                customer=request.customer_id,
            )
        return pb.IncidentList(incidents=[pb.Incident(**row) for row in found])

    async def CreateIncident(self, request, context):
        await self.authorize(context)
        if not request.title.strip() or len(request.title) > 160:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_ARGUMENT")
        async with self.db.begin() as conn:
            found = await rows(
                conn,
                """
                INSERT INTO it.incidents (id,service_id,customer_id,title)
                SELECT :id,id,customer_id,:title FROM it.services WHERE id=:service AND customer_id=:customer
                RETURNING id::text,service_id,title,status
            """,
                id=uuid4(),
                service=request.service_id,
                customer=request.customer_id,
                title=request.title,
            )
        if not found:
            await context.abort(grpc.StatusCode.NOT_FOUND, "RECORD_NOT_AVAILABLE")
        return pb.Incident(**found[0])

    async def GetSystemLogs(self, request, context):
        await self.authorize(context)
        if (
            request.level not in {"INFO", "WARN", "ERROR"}
            or not 1 <= request.limit <= 50
        ):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "INVALID_ARGUMENT")
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT l.logged_at AS timestamp,l.level,l.message FROM it.logs l JOIN it.services s ON s.id=l.service_id
                WHERE s.id=:service AND s.customer_id=:customer AND l.level=:level
                ORDER BY logged_at DESC,l.id LIMIT :limit
            """,
                service=request.service_id,
                customer=request.customer_id,
                level=request.level,
                limit=request.limit,
            )
        return pb.LogList(
            logs=[
                pb.LogEntry(**(row | {"timestamp": row["timestamp"].isoformat()}))
                for row in found
            ]
        )

    async def GetDeploymentStatus(self, request, context):
        await self.authorize(context)
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT d.service_id,d.release,d.status FROM it.deployments d JOIN it.services s ON s.id=d.service_id
                WHERE d.service_id=:service AND s.customer_id=:customer
            """,
                service=request.service_id,
                customer=request.customer_id,
            )
        if not found:
            await context.abort(grpc.StatusCode.NOT_FOUND, "RECORD_NOT_AVAILABLE")
        return pb.Deployment(**found[0])

    async def GetRestartReceipt(self, request, context):
        await self.authorize(context)
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                "SELECT id::text,customer_id,service_id,status FROM it.restarts WHERE id=:id AND customer_id=:customer AND service_id=:service",
                id=UUID(request.operation_id),
                customer=request.customer_id,
                service=request.service_id,
            )
        if not found:
            await context.abort(grpc.StatusCode.NOT_FOUND, "RECORD_NOT_AVAILABLE")
        return pb.RestartReceipt(**found[0])

    async def RestartService(self, request, context):
        await self.authorize(context)
        try:
            operation = UUID(request.operation_id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "APPROVAL_REQUIRED")
            raise
        async with self.db.begin() as conn:
            scoped = await rows(
                conn,
                "SELECT id FROM it.services WHERE id=:service AND customer_id=:customer FOR UPDATE",
                service=request.service_id,
                customer=request.customer_id,
            )
            if not scoped:
                await context.abort(grpc.StatusCode.NOT_FOUND, "RECORD_NOT_AVAILABLE")
            inserted = await rows(
                conn,
                """
                INSERT INTO it.restarts VALUES(:id,:service,:customer,'SUCCEEDED')
                ON CONFLICT DO NOTHING RETURNING id::text,customer_id,service_id,status
            """,
                id=operation,
                service=request.service_id,
                customer=request.customer_id,
            )
            if inserted:
                await execute(
                    conn,
                    "UPDATE it.services SET status='healthy' WHERE id=:service",
                    service=request.service_id,
                )
                await execute(
                    conn,
                    "UPDATE it.deployments SET status='healthy' WHERE service_id=:service",
                    service=request.service_id,
                )
                await execute(
                    conn,
                    "INSERT INTO it.logs(service_id,level,message) VALUES(:service,'INFO','Simulated restart completed')",
                    service=request.service_id,
                )
            else:
                inserted = await rows(
                    conn,
                    "SELECT id::text,customer_id,service_id,status FROM it.restarts WHERE id=:id AND customer_id=:customer AND service_id=:service",
                    id=operation,
                    customer=request.customer_id,
                    service=request.service_id,
                )
                if not inserted:
                    await context.abort(
                        grpc.StatusCode.ALREADY_EXISTS, "RESTART_CONFLICT"
                    )
        return pb.RestartReceipt(**inserted[0])


async def serve():
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"], hide_parameters=True
    )
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", 32768),
            ("grpc.max_send_message_length", 65536),
        ]
    )
    add_OperationsServicer_to_server(
        Operations(db, os.environ["TOOLMESH_INTERNAL_SECRET"]), server
    )
    server.add_insecure_port("0.0.0.0:50051")
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(2)
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(serve())
