import asyncio
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.contracts import Refund, RefundIntent, RefundLookup
from shared.database import rows

REFUND_COLUMNS = (
    "id, transaction_id, customer_id, amount_minor, currency, 'SUCCEEDED' AS status"
)


def routes(db: AsyncEngine, authenticate) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authenticate)])

    @router.post("/get_refund_status")
    async def status(args: RefundLookup) -> Refund:
        async with db.connect() as conn:
            found = await rows(
                conn,
                f"""
                SELECT {REFUND_COLUMNS} FROM payments.refunds
                WHERE id=:refund_id AND customer_id=:customer_id
            """,
                **args.model_dump(),
            )
        if not found:
            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
        return Refund.model_validate(found[0])

    @router.post("/issue_refund")
    async def refund(
        args: RefundIntent, refund_id: Annotated[UUID, Header(alias="X-Refund-ID")]
    ) -> Refund:
        async with db.begin() as conn:
            captures = await rows(
                conn,
                """
                SELECT amount_minor,currency FROM payments.transactions
                WHERE id=:id AND customer_id=:customer AND status='captured' FOR UPDATE
            """,
                id=args.transaction_id,
                customer=args.customer_id,
            )
            if not captures:
                raise HTTPException(404, "RECORD_NOT_AVAILABLE")
            previous = await rows(
                conn,
                f"SELECT {REFUND_COLUMNS} FROM payments.refunds WHERE id=:id",
                id=refund_id,
            )
            if previous:
                refund_result = Refund.model_validate(previous[0])
                if any(
                    getattr(refund_result, key) != value
                    for key, value in args.model_dump().items()
                ):
                    raise HTTPException(409, "REFUND_CONFLICT")
                return refund_result
            totals = await rows(
                conn,
                """
                SELECT coalesce(sum(amount_minor),0) AS refunded FROM payments.refunds WHERE transaction_id=:id
            """,
                id=args.transaction_id,
            )
            capture = captures[0]
            if (
                args.currency != capture["currency"]
                or totals[0]["refunded"] + args.amount_minor > capture["amount_minor"]
            ):
                raise HTTPException(409, "REFUND_CONFLICT")
            inserted = await rows(
                conn,
                f"""
                INSERT INTO payments.refunds (id,transaction_id,customer_id,amount_minor,currency)
                VALUES (:id,:transaction_id,:customer_id,:amount_minor,:currency)
                ON CONFLICT (id) DO NOTHING RETURNING {REFUND_COLUMNS}
            """,
                id=refund_id,
                **args.model_dump(),
            )
            if not inserted:
                raise HTTPException(409, "REFUND_CONFLICT")
        if os.getenv("TOOLMESH_TEST_REFUND_DELAY") == "1":
            # Test-only container switch. The commit has already happened.
            await asyncio.sleep(6)
        return Refund.model_validate(inserted[0])

    return router
