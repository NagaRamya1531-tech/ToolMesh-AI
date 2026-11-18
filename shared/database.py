from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def rows(
    connection: AsyncConnection, query: str, /, **parameters: Any
) -> list[dict]:
    result = await connection.execute(text(query), parameters)
    return [dict(row) for row in result.mappings()]


async def execute(
    connection: AsyncConnection, query: str, /, **parameters: Any
) -> None:
    await connection.execute(text(query), parameters)
