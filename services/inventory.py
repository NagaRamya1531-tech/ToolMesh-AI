"""Allowlisted local OpenAPI fixture with real inventory and controlled schema evolution."""

import os
from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from shared.contracts import Customer, Identifier
from shared.database import rows


class InventoryLookup(Customer):
    item_id: Identifier


class LocatedLookup(InventoryLookup):
    location: Identifier


class Item(Customer):
    id: Identifier
    quantity: int
    location: Identifier


def create_app():
    db = create_async_engine(
        os.environ["TOOLMESH_DOMAIN_DATABASE_URL"], hide_parameters=True
    )

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            await db.dispose()

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    secret = os.environ["TOOLMESH_INTERNAL_SECRET"]

    async def auth(authorization: Annotated[str, Header()] = ""):
        if not compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(401)

    async def current(conn):
        state = await rows(conn, "SELECT require_location FROM inventory.schema_state")
        return LocatedLookup if state[0]["require_location"] else InventoryLookup

    @app.get("/healthz")
    async def health():
        async with db.connect() as conn:
            await current(conn)
        return {"status": "alive"}

    @app.get("/openapi.json", dependencies=[Depends(auth)])
    async def openapi():
        async with db.connect() as conn:
            lookup = await current(conn)
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "Sandbox Inventory",
                "version": "2" if lookup is LocatedLookup else "1",
            },
            "paths": {
                "/get_inventory": {
                    "post": {
                        "operationId": "get_inventory",
                        "summary": "Read the quantity for a sandbox inventory item.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": lookup.model_json_schema()
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Inventory record",
                                "content": {
                                    "application/json": {
                                        "schema": Item.model_json_schema()
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }

    @app.post("/get_inventory", dependencies=[Depends(auth)])
    async def inventory(body: dict):
        async with db.connect() as conn:
            lookup = await current(conn)
            try:
                args = lookup.model_validate(body).model_dump()
            except ValidationError:
                raise HTTPException(422, "INVALID_ARGUMENT") from None
            values = await rows(
                conn,
                """
                SELECT id,customer_id,quantity,location FROM inventory.items
                WHERE id=:item_id AND customer_id=:customer_id
                  AND (CAST(:location AS text) IS NULL OR location=:location)
            """,
                location=args.get("location"),
                item_id=args["item_id"],
                customer_id=args["customer_id"],
            )
        if not values:
            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
        return Item.model_validate(values[0])

    return app
