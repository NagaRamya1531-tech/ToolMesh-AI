"""Compatibility helpers for legacy writers on both sides of revision 0011."""

from sqlalchemy.ext.asyncio import AsyncConnection

from shared.database import execute, rows


async def guard_authorization_write(conn: AsyncConnection) -> None:
    """Lock before catalog row locks. Statement triggers bump on mutation."""
    [installed] = await rows(
        conn,
        "SELECT to_regprocedure('public.lock_execution_authorization()') IS NOT NULL AS enabled",
    )
    if installed["enabled"]:
        await execute(conn, "SELECT public.lock_execution_authorization()")


async def legacy_approval_only(conn: AsyncConnection) -> str:
    """SQL predicate for the fixed approval alias, compatible with older schemas."""
    [installed] = await rows(
        conn,
        "SELECT to_regprocedure('public.is_durable_approval(uuid)') IS NOT NULL AS enabled",
    )
    if installed["enabled"]:
        return " AND NOT public.is_durable_approval(a.id) "
    return " "
