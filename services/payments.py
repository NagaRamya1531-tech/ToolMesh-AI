import os
from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from services.refunds import routes
from shared.contracts import (
    Customer,
    DuplicateEvidence,
    OrderLookup,
    Transaction,
    TransactionLookup,
    Transactions,
)
from shared.database import rows


def create_app() -> FastAPI:
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"],
        pool_size=4,
        max_overflow=0,
        pool_timeout=3,
        hide_parameters=True,
    )
    secret = os.environ["TOOLMESH_INTERNAL_SECRET"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await db.dispose()

    async def authenticate(authorization: Annotated[str, Header()] = ""):
        if not compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(401)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        async with db.connect() as conn:
            await rows(conn, "SELECT id FROM payments.transactions LIMIT 1")
        return {"status": "alive"}

    @app.post("/get_transaction", dependencies=[Depends(authenticate)])
    async def transaction(args: TransactionLookup) -> Transaction:
        async with db.connect() as conn:
            values = await rows(
                conn,
                """
                SELECT * FROM payments.transactions WHERE id=:transaction_id AND customer_id=:customer_id
            """,
                **args.model_dump(),
            )
        if not values:
            raise HTTPException(404)
        return Transaction.model_validate(values[0])

    @app.post("/list_transactions", dependencies=[Depends(authenticate)])
    async def transactions(args: Customer) -> Transactions:
        async with db.connect() as conn:
            values = await rows(
                conn,
                """
                SELECT * FROM payments.transactions WHERE customer_id=:customer_id ORDER BY id LIMIT 50
            """,
                **args.model_dump(),
            )
        return Transactions(
            transactions=[Transaction.model_validate(value) for value in values]
        )

    @app.post("/check_duplicate_payment", dependencies=[Depends(authenticate)])
    async def duplicates(args: OrderLookup) -> DuplicateEvidence:
        async with db.connect() as conn:
            values = await rows(
                conn,
                """
                SELECT array_agg(id ORDER BY id) AS ids FROM payments.transactions
                WHERE customer_id=:customer_id AND order_id=:order_id AND installment IS NULL
                  AND status='captured'
                GROUP BY amount_minor, currency HAVING count(*) > 1 ORDER BY amount_minor, currency
            """,
                **args.model_dump(),
            )
        return DuplicateEvidence(
            duplicate_capture_detected=bool(values),
            groups=[value["ids"] for value in values],
        )

    app.include_router(routes(db, authenticate))
    return app
