import os
from contextlib import asynccontextmanager
from secrets import compare_digest
from uuid import UUID, uuid4

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.contracts import Identifier, ResolvedTicket, Subject, Ticket, Tickets
from shared.database import rows


class InternalVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if compare_digest(
            token.encode(), os.environ["TOOLMESH_INTERNAL_SECRET"].encode()
        ):
            return AccessToken(token=token, client_id="gateway", scopes=[])
        return None


def create_app():
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"],
        pool_size=4,
        max_overflow=0,
        pool_timeout=3,
        hide_parameters=True,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield {}
        finally:
            await db.dispose()

    server = FastMCP(
        "ToolMesh Support",
        auth=InternalVerifier(),
        lifespan=lifespan,
        mask_error_details=True,
    )

    @server.tool
    async def create_ticket(
        customer_id: Identifier, order_id: Identifier, subject: Subject
    ) -> Ticket:
        async with db.begin() as conn:
            values = await rows(
                conn,
                """
                INSERT INTO support.tickets (id, customer_id, order_id, subject)
                VALUES (:id, :customer_id, :order_id, :subject)
                RETURNING id, customer_id, order_id, subject, status
            """,
                id=uuid4(),
                customer_id=customer_id,
                order_id=order_id,
                subject=subject,
            )
        return Ticket.model_validate(values[0])

    @server.tool
    async def get_ticket(customer_id: Identifier, ticket_id: UUID) -> Ticket:
        async with db.connect() as conn:
            values = await rows(
                conn,
                """
                SELECT id, customer_id, order_id, subject, status FROM support.tickets
                WHERE id=:ticket_id AND customer_id=:customer_id
            """,
                customer_id=customer_id,
                ticket_id=ticket_id,
            )
        if not values:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Ticket.model_validate(values[0])

    @server.tool
    async def search_tickets(customer_id: Identifier) -> Tickets:
        async with db.connect() as conn:
            values = await rows(
                conn,
                """
                SELECT id, customer_id, order_id, subject, status FROM support.tickets
                WHERE customer_id=:customer_id ORDER BY created_at DESC, id LIMIT 50
            """,
                customer_id=customer_id,
            )
        return Tickets(tickets=[Ticket.model_validate(value) for value in values])

    @server.tool
    async def open_duplicate_ticket(
        customer_id: Identifier,
        order_id: Identifier,
        ticket_id: UUID,
        transaction_ids: list[Identifier],
    ) -> Ticket:
        if not 2 <= len(transaction_ids) <= 10 or len(set(transaction_ids)) != len(
            transaction_ids
        ):
            raise ToolError("INVALID_EVIDENCE")
        async with db.begin() as conn:
            await rows(
                conn,
                """
                INSERT INTO support.tickets (id,customer_id,order_id,subject,duplicate_transactions)
                VALUES (:id,:customer,:order,'Duplicate capture investigation',:transactions)
                ON CONFLICT (id) DO NOTHING RETURNING id
            """,
                id=ticket_id,
                customer=customer_id,
                order=order_id,
                transactions=transaction_ids,
            )
            found = await rows(
                conn,
                """
                SELECT id,customer_id,order_id,subject,status FROM support.tickets
                WHERE id=:id AND customer_id=:customer AND order_id=:order AND duplicate_transactions=:transactions
            """,
                id=ticket_id,
                customer=customer_id,
                order=order_id,
                transactions=transaction_ids,
            )
        if not found:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return Ticket.model_validate(found[0])

    @server.tool
    async def resolve_ticket(
        customer_id: Identifier, ticket_id: UUID, refund_id: UUID
    ) -> ResolvedTicket:
        async with db.begin() as conn:
            found = await rows(
                conn,
                """
                UPDATE support.tickets SET status='resolved',refund_id=:refund
                WHERE id=:id AND customer_id=:customer
                  AND (status='open' OR (status='resolved' AND refund_id=:refund))
                RETURNING id AS ticket_id, customer_id,refund_id,status
            """,
                id=ticket_id,
                customer=customer_id,
                refund=refund_id,
            )
        if not found:
            raise ToolError("RECORD_NOT_AVAILABLE")
        return ResolvedTicket.model_validate(found[0])

    async def health(_):
        async with db.connect() as conn:
            await rows(conn, "SELECT id FROM support.tickets LIMIT 1")
        return JSONResponse({"status": "alive"})

    app = server.http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        allowed_hosts=["support:*"],
    )
    app.routes.append(Route("/healthz", health))
    return app
