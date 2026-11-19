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

from benchmarks.api import build_evaluation_api
from benchmarks.evaluation import Evaluations
from control_plane.catalog import Catalog
from control_plane.crawler import Crawler
from control_plane.governance import build_governance
from control_plane.sources import Target
from gateway.agent_runs import AgentRuns, build_agent_api
from gateway.config import Settings
from gateway.discovery import Discovery
from gateway.embeddings import Embeddings
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
            "it-agent": config.it_password,
            "hr-agent": config.hr_password,
        },
    )

    def secret(value):
        return value.get_secret_value() if value else ""

    embeddings = Embeddings(config.embedding_path)
    discovery = Discovery(engine, embeddings)
    evaluations = Evaluations(engine, embeddings)
    runtime = Runtime(
        engine,
        redis,
        secret(config.payments_secret),
        secret(config.support_secret),
        secret(config.communication_secret),
        secret(config.inventory_secret),
        secret(config.it_secret),
        discovery=discovery,
        hr_secret=secret(config.hr_secret),
        analytics_secret=secret(config.analytics_secret),
    )
    agent_runs = AgentRuns(runtime)
    crawler = Crawler(
        engine,
        {
            "payments": Target("REST", 8000, secret(config.payments_secret)),
            "support": Target("MCP", 8000, secret(config.support_secret)),
            "communication": Target("MCP", 8000, secret(config.communication_secret)),
            "inventory": Target("REST", 8000, secret(config.inventory_secret)),
            "it": Target("gRPC", 50051, secret(config.it_secret)),
            "hr": Target("REST", 8000, secret(config.hr_secret)),
            "analytics": Target("REST", 8000, secret(config.analytics_secret)),
        },
    )
    mcp = build_mcp(identity, runtime, ["localhost:*", "127.0.0.1:*", "api:*"])

    async def postgres_ready() -> bool:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = 'default'), "
                    "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'), "
                    "(SELECT array_agg(version_num ORDER BY version_num) "
                    "FROM alembic_version) = ARRAY['0008']::varchar[]"
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
                await embeddings.load()
                workers = [
                    asyncio.create_task(runtime.approvals.worker()),
                    asyncio.create_task(crawler.worker()),
                    asyncio.create_task(discovery.worker()),
                    asyncio.create_task(evaluations.worker()),
                    asyncio.create_task(agent_runs.worker()),
                ]
                try:
                    yield
                finally:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
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

    app.include_router(build_agent_api(identity, agent_runs))
    app.include_router(build_governance(identity, runtime))
    app.include_router(build_evaluation_api(identity, evaluations))
    app.include_router(build_api(identity, runtime, Catalog(engine), crawler))
    app.mount("/mcp", mcp)
    return app
