import os
from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine

from shared.database import rows


def domain_app(title: str):
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"],
        pool_size=4,
        max_overflow=0,
        hide_parameters=True,
    )
    secret = os.environ["TOOLMESH_INTERNAL_SECRET"]

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            await db.dispose()

    async def authenticate(authorization: Annotated[str, Header()] = ""):
        if not compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(401, "AUTH_REQUIRED")

    app = FastAPI(
        title=title, lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None
    )

    @app.get(
        "/openapi.json", dependencies=[Depends(authenticate)], include_in_schema=False
    )
    async def openapi():
        return app.openapi()

    @app.get("/healthz", include_in_schema=False)
    async def health():
        async with db.connect() as conn:
            await rows(conn, "SELECT 1")
        return {"status": "alive"}

    return app, db, [Depends(authenticate)]
