import json
from uuid import uuid4

from fastapi import HTTPException

from services.domain_http import domain_app
from shared.database import execute, rows
from shared.domain_contracts import (
    CompareWindow,
    Comparison,
    CustomerMetrics,
    DailyTotal,
    Report,
    ReportingWindow,
    Revenue,
    Totals,
)


def window(start, end):
    if not 0 <= (end - start).days < 366:
        raise HTTPException(422, "INVALID_REPORTING_WINDOW")


async def totals(conn, customer, start, end, refunds=False) -> Totals:
    window(start, end)
    relation, column = (
        ("analytics.refunds", "day")
        if refunds
        else ("analytics.captures", "captured_on")
    )
    found = await rows(
        conn,
        f"""
        SELECT count(*) AS count,coalesce(sum(amount_minor),0) AS total_minor,'USD' AS currency
        FROM {relation} WHERE customer_id=:customer AND {column} BETWEEN :first AND :last AND currency='USD'
    """,
        customer=customer,
        first=start,
        last=end,
    )
    return Totals.model_validate(found[0])


def create_app():
    app, db, auth = domain_app("ToolMesh Analytics")

    @app.post("/get_daily_revenue", dependencies=auth)
    async def revenue(args: ReportingWindow) -> Revenue:
        window(args.start_date, args.end_date)
        async with db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT captured_on AS day,count(*) AS count,sum(amount_minor) AS total_minor,'USD' AS currency
                FROM analytics.captures WHERE customer_id=:customer_id AND captured_on BETWEEN :start_date AND :end_date AND currency='USD'
                GROUP BY captured_on ORDER BY captured_on
            """,
                **args.model_dump(),
            )
        return Revenue(days=[DailyTotal.model_validate(value) for value in found])

    @app.post("/get_sales_metrics", dependencies=auth)
    async def sales(args: ReportingWindow) -> Totals:
        async with db.connect() as conn:
            return await totals(conn, args.customer_id, args.start_date, args.end_date)

    @app.post("/get_customer_metrics", dependencies=auth)
    async def customers(args: ReportingWindow) -> CustomerMetrics:
        window(args.start_date, args.end_date)
        async with db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT count(*) AS customers,count(*) FILTER (WHERE captures>1) AS repeat_customers
                FROM (SELECT count(*) AS captures FROM analytics.captures
                  WHERE customer_id=:customer_id AND captured_on BETWEEN :start_date AND :end_date
                  GROUP BY customer_id) scoped
            """,
                **args.model_dump(),
            )
        return CustomerMetrics.model_validate(found[0])

    @app.post("/get_refund_metrics", dependencies=auth)
    async def refunds(args: ReportingWindow) -> Totals:
        async with db.connect() as conn:
            return await totals(
                conn, args.customer_id, args.start_date, args.end_date, refunds=True
            )

    @app.post("/compare_periods", dependencies=auth)
    async def compare(args: CompareWindow) -> Comparison:
        async with db.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            current = await totals(
                conn, args.customer_id, args.start_date, args.end_date
            )
            previous = await totals(
                conn, args.customer_id, args.previous_start_date, args.previous_end_date
            )
        return Comparison(
            current=current,
            previous=previous,
            delta_minor=current.total_minor - previous.total_minor,
        )

    @app.post("/generate_report", dependencies=auth)
    async def report(args: ReportingWindow) -> Report:
        async with db.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                sales_total = await totals(
                    conn, args.customer_id, args.start_date, args.end_date
                )
                refund_total = await totals(
                    conn, args.customer_id, args.start_date, args.end_date, refunds=True
                )
                report_id = uuid4()
                await execute(
                    conn,
                    """
                    INSERT INTO analytics.reports(id,customer_id,start_date,end_date,sales,refunds)
                    VALUES(:id,:customer_id,:start_date,:end_date,CAST(:sales AS jsonb),CAST(:refunds AS jsonb))
                """,
                    id=report_id,
                    sales=json.dumps(sales_total.model_dump()),
                    refunds=json.dumps(refund_total.model_dump()),
                    **args.model_dump(),
                )
        return Report(id=report_id, sales=sales_total, refunds=refund_total)

    return app
