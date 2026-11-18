import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.support import InternalVerifier
from shared.contracts import Identifier, Notification
from shared.database import rows


def create_app():
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"],
        pool_size=4,
        max_overflow=0,
        hide_parameters=True,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield {}
        finally:
            await db.dispose()

    server = FastMCP(
        "ToolMesh Communication",
        auth=InternalVerifier(),
        lifespan=lifespan,
        mask_error_details=True,
    )

    async def find(conn, customer_id: str, refund_id: UUID):
        return await rows(
            conn,
            """
            SELECT id,customer_id,refund_id,status FROM communication.messages
            WHERE customer_id=:customer AND refund_id=:refund
        """,
            customer=customer_id,
            refund=refund_id,
        )

    @server.tool
    async def send_notification(
        customer_id: Identifier, refund_id: UUID
    ) -> Notification:
        async with db.begin() as conn:
            await rows(
                conn,
                """
                INSERT INTO communication.messages(id,customer_id,refund_id,body)
                VALUES (:id,:customer,:refund,'Your sandbox refund has been confirmed.')
                ON CONFLICT(customer_id,refund_id) DO NOTHING RETURNING id
            """,
                id=uuid4(),
                customer=customer_id,
                refund=refund_id,
            )
            result = await find(conn, customer_id, refund_id)
        return Notification.model_validate(result[0])

    @server.tool
    async def get_notification(
        customer_id: Identifier, refund_id: UUID
    ) -> Notification:
        async with db.connect() as conn:
            result = await find(conn, customer_id, refund_id)
        if not result:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Notification.model_validate(result[0])

    async def health(_):
        async with db.connect() as conn:
            await rows(conn, "SELECT id FROM communication.messages LIMIT 1")
        return JSONResponse({"status": "alive"})

    app = server.http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        allowed_hosts=["communication:*"],
    )
    app.routes.append(Route("/healthz", health))
    return app
