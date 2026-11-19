import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.support import InternalVerifier
from shared.contracts import Identifier, Notification, Subject
from shared.database import rows
from shared.domain_contracts import Channel, Message, Messages, ShortText


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

    async def store(
        customer_id: str,
        recipient: str,
        kind: str,
        subject: str,
        body: str,
        leave: UUID | None = None,
    ) -> Message:
        async with db.begin() as conn:
            found = await rows(
                conn,
                """
                INSERT INTO communication.messages(id,customer_id,kind,subject,body,recipient_id,leave_id)
                VALUES(:id,:customer,:kind,:subject,:body,:recipient,:leave)
                ON CONFLICT(leave_id) DO NOTHING RETURNING id,kind,subject,body,status
            """,
                id=uuid4(),
                customer=customer_id,
                kind=kind,
                subject=subject,
                body=body,
                recipient=recipient,
                leave=leave,
            )
            if not found and leave:
                found = await rows(
                    conn,
                    "SELECT id,kind,subject,body,status FROM communication.messages WHERE leave_id=:leave AND customer_id=:customer AND recipient_id=:recipient",
                    leave=leave,
                    customer=customer_id,
                    recipient=recipient,
                )
        if not found:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Message.model_validate(found[0])

    @server.tool
    async def send_email(
        customer_id: Identifier,
        recipient_id: Identifier,
        subject: Subject,
        body: ShortText,
    ) -> Message:
        if customer_id != recipient_id:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return await store(customer_id, recipient_id, "email", subject, body)

    @server.tool
    async def notify_leave(
        customer_id: Identifier, employee_id: Identifier, leave_id: UUID
    ) -> Message:
        return await store(
            customer_id,
            employee_id,
            "leave_notification",
            "Sandbox leave request",
            "Your leave request was submitted.",
            leave_id,
        )

    @server.tool
    async def search_messages(
        customer_id: Identifier,
        query: Annotated[str, Field(max_length=100)],
        limit: Annotated[int, Field(ge=1, le=25)],
    ) -> Messages:
        async with db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT id,kind,subject,body,status FROM communication.messages WHERE customer_id=:customer
                AND kind<>'leave_notification' AND strpos(lower(subject||' '||body),lower(:query))>0 ORDER BY created_at DESC,id LIMIT :limit
            """,
                customer=customer_id,
                query=query[:100],
                limit=max(1, min(25, limit)),
            )
        return Messages(messages=[Message.model_validate(row) for row in found])

    @server.tool
    async def get_message(customer_id: Identifier, message_id: UUID) -> Message:
        async with db.connect() as conn:
            found = await rows(
                conn,
                "SELECT id,kind,subject,body,status FROM communication.messages WHERE customer_id=:customer AND id=:id AND kind<>'leave_notification'",
                customer=customer_id,
                id=message_id,
            )
        if not found:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Message.model_validate(found[0])

    @server.tool
    async def create_channel(
        customer_id: Identifier, channel_name: Identifier
    ) -> Channel:
        async with db.begin() as conn:
            found = await rows(
                conn,
                """
                INSERT INTO communication.channels VALUES(:customer,:name,ARRAY[CAST(:customer AS text)])
                ON CONFLICT DO NOTHING RETURNING channel_name,members
            """,
                customer=customer_id,
                name=channel_name,
            )
        if not found:
            raise ToolError("CHANNEL_EXISTS")
        return Channel.model_validate(found[0])

    @server.tool
    async def get_channel_members(
        customer_id: Identifier, channel_name: Identifier
    ) -> Channel:
        async with db.connect() as conn:
            found = await rows(
                conn,
                "SELECT channel_name,members FROM communication.channels WHERE customer_id=:customer AND channel_name=:name",
                customer=customer_id,
                name=channel_name,
            )
        if not found:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Channel.model_validate(found[0])

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
