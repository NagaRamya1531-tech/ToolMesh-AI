import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.exceptions import HTTPException

from control_plane.catalog import Catalog
from gateway.config import Settings
from gateway.identity import Identity
from gateway.interfaces import build_api, build_mcp
from gateway.limits import LimitBody
from gateway.runtime import Runtime

Check = Literal["available", "unavailable"]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class Liveness(BaseModel):
    status: Literal["alive"] = "alive"


class Readiness(BaseModel):
    status: Literal["ready", "unready"]
    checked_at: datetime
    checks: dict[str, Check]


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()  # type: ignore[call-arg]
    engine = create_async_engine(
        config.database_url.get_secret_value(),
        pool_size=4,
        max_overflow=0,
        pool_timeout=3,
        hide_parameters=True,
        connect_args={"timeout": 3, "command_timeout": 3},
    )
    redis = Redis.from_url(config.redis_url.get_secret_value(), max_connections=8)

    identity = Identity(
        engine,
        redis,
        {
            "platform-admin": config.admin_password,
            "support-agent": config.support_password,
            "payments-agent": config.payments_password,
            "finance-agent": config.finance_password,
            "refund-approver": config.approver_password,
        },
    )
    runtime = Runtime(
        engine,
        redis,
        config.payments_secret.get_secret_value() if config.payments_secret else "",
        config.support_secret.get_secret_value() if config.support_secret else "",
        config.communication_secret.get_secret_value()
        if config.communication_secret
        else "",
    )
    mcp = build_mcp(identity, runtime, ["localhost:*", "127.0.0.1:*", "api:*"])

    async def postgres_ready() -> bool:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = 'default'), "
                    "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'), "
                    "(SELECT array_agg(version_num ORDER BY version_num) "
                    "FROM alembic_version) = ARRAY['0004']::varchar[]"
                )
            )
            return all(result.one())

    async def redis_ready() -> bool:
        return bool(await redis.ping())

    async def probe(check: Awaitable[bool]) -> Check:
        try:
            async with asyncio.timeout(config.probe_timeout):
                return "available" if await check else "unavailable"
        except (TimeoutError, OSError, SQLAlchemyError, RedisError):
            return "unavailable"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            async with mcp.lifespan(mcp):
                worker = asyncio.create_task(runtime.approvals.worker())
                try:
                    yield
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="ToolMesh AI", version="0.1.0", lifespan=lifespan)

    def error_response(
        request: Request, status: int, code: str, message: str
    ) -> JSONResponse:
        body = ErrorEnvelope(
            error=ErrorDetail(
                code=code,
                message=message,
                request_id=request.state.request_id,
            )
        )
        return JSONResponse(body.model_dump(), status_code=status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = str(exc.detail)
        code = (
            detail
            if detail.isupper() and detail.replace("_", "").isalnum()
            else "HTTP_ERROR"
        )
        response = error_response(request, exc.status_code, code, detail)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, _: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            request, 422, "INVALID_ARGUMENT", "Request validation failed."
        )

    app.add_middleware(LimitBody)

    @app.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def health() -> Liveness:
        return Liveness()

    @app.get("/readyz", responses={503: {"model": Readiness}})
    async def readiness(response: Response) -> Readiness:
        postgres, redis_status = await asyncio.gather(
            probe(postgres_ready()), probe(redis_ready())
        )
        ready = postgres == redis_status == "available"
        if not ready:
            response.status_code = 503
        return Readiness(
            status="ready" if ready else "unready",
            checked_at=datetime.now(UTC),
            checks={"postgres": postgres, "redis": redis_status},
        )

    app.include_router(build_api(identity, runtime, Catalog(engine)))
    app.mount("/mcp", mcp)
    return app
