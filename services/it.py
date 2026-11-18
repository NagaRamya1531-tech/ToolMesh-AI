import asyncio
import os
from secrets import compare_digest
from uuid import uuid4

import grpc
from sqlalchemy.ext.asyncio import create_async_engine

from shared.database import rows
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
