import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.database import execute, rows


@dataclass(frozen=True)
class Principal:
    id: str
    kind: str
    services: frozenset[str]
    customer: str
    can_write: bool = False
    can_refund: bool = False
    can_approve: bool = False

    @property
    def can_publish(self) -> bool:
        return self.id == "platform-admin"


PRINCIPALS = {
    "platform-admin": Principal(
        "platform-admin",
        "human",
        frozenset({"payments", "support", "communication"}),
        "C-102",
    ),
    "support-agent": Principal(
        "support-agent",
        "agent",
        frozenset({"payments", "support", "communication"}),
        "C-102",
        True,
    ),
    "finance-agent": Principal(
        "finance-agent", "agent", frozenset({"payments"}), "C-102", can_refund=True
    ),
    "refund-approver": Principal(
        "refund-approver", "human", frozenset(), "C-102", can_approve=True
    ),
    "payments-agent": Principal(
        "payments-agent", "agent", frozenset({"payments"}), "C-102"
    ),
}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Identity(TokenVerifier):
    def __init__(
        self, db: AsyncEngine, redis: Redis, passwords: dict[str, SecretStr | None]
    ):
        super().__init__()
        self.db = db
        self.redis = redis
        self.passwords = passwords

    async def login(self, username: str, password: str) -> str:
        try:
            async with asyncio.timeout(1):
                attempts = await self.redis.eval(
                    "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
                    1,
                    "toolmesh:login",
                )
        except (RedisError, TimeoutError):
            raise HTTPException(503, "AUTH_UNAVAILABLE") from None
        if attempts > 30:
            raise HTTPException(429, "RATE_LIMITED")
        configured = self.passwords.get(username)
        if configured is None or not hmac.compare_digest(
            configured.get_secret_value().encode(), password.encode()
        ):
            raise HTTPException(401, "INVALID_CREDENTIALS")
        token = secrets.token_urlsafe(32)
        async with self.db.begin() as conn:
            await execute(conn, "DELETE FROM sessions WHERE expires_at <= now()")
            await execute(
                conn,
                """
                INSERT INTO sessions VALUES (:digest, :principal, now() + interval '1 hour')
            """,
                digest=digest(token),
                principal=username,
            )
        return token

    async def principal(self, authorization: str) -> Principal:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or len(token) != 43:
            raise HTTPException(401, "AUTH_REQUIRED")
        async with self.db.connect() as conn:
            users = await rows(
                conn,
                """
                SELECT principal FROM sessions WHERE digest=:digest AND expires_at>now()
            """,
                digest=digest(token),
            )
        principal = PRINCIPALS.get(users[0]["principal"]) if users else None
        if principal is None:
            raise HTTPException(401, "AUTH_REQUIRED")
        return principal

    async def revoke(self, authorization: str) -> None:
        await self.principal(authorization)
        async with self.db.begin() as conn:
            await execute(
                conn,
                "DELETE FROM sessions WHERE digest=:digest",
                digest=digest(authorization.split(" ", 1)[1]),
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self.principal(f"Bearer {token}")
        except HTTPException:
            return None
        return AccessToken(token=token, client_id=principal.id, scopes=[])
