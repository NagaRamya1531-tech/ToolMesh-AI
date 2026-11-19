from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from services.domain_http import domain_app
from shared.database import execute, rows
from shared.domain_contracts import (
    Balance,
    CompanyPolicy,
    Department,
    DepartmentLookup,
    Employee,
    EmployeeLookup,
    HRNotice,
    LeaveReceipt,
    LeaveRequest,
    PolicyLookup,
)


def create_app():
    app, db, auth = domain_app("ToolMesh Human Resources")
    employee_fields = "id AS employee_id,department_id,display_name,title"

    async def read(model, statement, **values):
        async with db.connect() as conn:
            found = await rows(conn, statement, **values)
        if not found:
            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
        return model.model_validate(found[0])

    @app.post("/get_employee", dependencies=auth)
    async def employee(args: EmployeeLookup) -> Employee:
        return await read(
            Employee,
            f"SELECT {employee_fields} FROM hr.employees WHERE id=:employee_id AND customer_id=:customer_id",
            **args.model_dump(),
        )

    @app.post("/get_manager", dependencies=auth)
    async def manager(args: EmployeeLookup) -> Employee:
        return await read(
            Employee,
            f"""
            SELECT {employee_fields} FROM hr.employees WHERE customer_id=:customer_id AND id=(
              SELECT manager_id FROM hr.employees WHERE id=:employee_id AND customer_id=:customer_id)
        """,
            **args.model_dump(),
        )

    @app.post("/get_department", dependencies=auth)
    async def department(args: DepartmentLookup) -> Department:
        return await read(
            Department,
            "SELECT id AS department_id,name FROM hr.departments WHERE id=:department_id AND customer_id=:customer_id",
            **args.model_dump(),
        )

    @app.post("/get_company_policy", dependencies=auth)
    async def policy(args: PolicyLookup) -> CompanyPolicy:
        return await read(
            CompanyPolicy,
            "SELECT id AS policy_id,title,summary FROM hr.policies WHERE id=:policy_id AND customer_id=:customer_id",
            **args.model_dump(),
        )

    @app.post("/get_leave_balance", dependencies=auth)
    async def balance(args: EmployeeLookup) -> Balance:
        return await read(
            Balance,
            "SELECT id AS employee_id,available_days FROM hr.employees WHERE id=:employee_id AND customer_id=:customer_id",
            **args.model_dump(),
        )

    @app.post("/request_leave", dependencies=auth)
    async def leave(args: LeaveRequest) -> LeaveReceipt:
        span = (args.end_date - args.start_date).days + 1
        if not 1 <= span <= 30 or args.start_date < datetime.now(UTC).date():
            raise HTTPException(422, "INVALID_LEAVE_DATES")
        async with db.begin() as conn:
            found = await rows(
                conn,
                "SELECT available_days FROM hr.employees WHERE id=:id AND customer_id=:customer FOR UPDATE",
                id=args.employee_id,
                customer=args.customer_id,
            )
            if not found:
                raise HTTPException(404, "RECORD_NOT_AVAILABLE")
            overlap = await rows(
                conn,
                "SELECT id FROM hr.leave_requests WHERE employee_id=:employee AND start_date<=:last AND end_date>=:first",
                employee=args.employee_id,
                first=args.start_date,
                last=args.end_date,
            )
            remaining = found[0]["available_days"] - span
            if overlap or remaining < 0:
                raise HTTPException(409, "LEAVE_CONFLICT")
            receipt = uuid4()
            await execute(
                conn,
                "UPDATE hr.employees SET available_days=:days WHERE id=:id",
                days=remaining,
                id=args.employee_id,
            )
            await execute(
                conn,
                "INSERT INTO hr.leave_requests(id,employee_id,start_date,end_date,days) VALUES(:id,:employee,:first,:last,:days)",
                id=receipt,
                employee=args.employee_id,
                first=args.start_date,
                last=args.end_date,
                days=span,
            )
        return LeaveReceipt(
            id=receipt,
            employee_id=args.employee_id,
            start_date=args.start_date,
            end_date=args.end_date,
            available_days=remaining,
            status="SUBMITTED",
        )

    @app.post("/leave_status", dependencies=auth, include_in_schema=False)
    async def leave_status(args: HRNotice):
        async with db.connect() as conn:
            found = await rows(
                conn,
                """
                SELECT l.id,l.status,l.employee_id FROM hr.leave_requests l JOIN hr.employees e ON e.id=l.employee_id
                WHERE l.id=:leave_id AND l.employee_id=:employee_id AND e.customer_id=:customer_id
            """,
                **args.model_dump(),
            )
        if not found:
            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
        return found[0]

    return app
