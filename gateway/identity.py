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
    profile: str = "human"

    @property
    def can_publish(self) -> bool:
        return self.id == "platform-admin"


PRINCIPALS = {
    "platform-admin": Principal(
        "platform-admin",
        "human",
        frozenset(
            {
                "payments",
                "support",
                "communication",
                "inventory",
                "it",
                "hr",
                "analytics",
            }
        ),
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
        "finance-agent",
        "agent",
        frozenset({"payments", "analytics"}),
        "C-102",
        can_refund=True,
    ),
    "refund-approver": Principal(
        "refund-approver", "human", frozenset(), "C-102", can_approve=True
    ),
    "it-agent": Principal(
        "it-agent", "agent", frozenset({"it", "inventory"}), "C-102", True
    ),
    "payments-agent": Principal(
        "payments-agent", "agent", frozenset({"payments"}), "C-102"
    ),
}

PROFILE_TEMPLATES = {
    "support": (frozenset({"payments", "support", "communication"}), True),
    "finance": (frozenset({"payments", "analytics"}), False),
    "it": (frozenset({"it", "inventory"}), True),
    "hr": (frozenset({"hr", "communication"}), True),
    "payments": (frozenset({"payments"}), False),
}


async def resolve_principal(conn, principal_id: str) -> Principal | None:
    known = PRINCIPALS.get(principal_id)
    if known and known.kind == "human":
        return known
    found = await rows(
        conn,
        "SELECT profile FROM agent_accounts WHERE id=:id AND enabled",
        id=principal_id,
    )
    if not found:
        return None
    profile = found[0]["profile"]
    template = PROFILE_TEMPLATES.get(profile)
    if template is None:
        return None
    return Principal(
        principal_id,
        "agent",
        template[0],
        "C-102",
        template[1],
        can_refund=principal_id == "finance-agent",
        profile=profile,
    )


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
        token = secrets.token_urlsafe(32)
        async with self.db.begin() as conn:
            accounts = await rows(
                conn,
                "SELECT credential_hash FROM agent_accounts WHERE id=:id AND enabled FOR SHARE",
                id=username,
            )
            principal = await resolve_principal(conn, username)
            stored = accounts[0]["credential_hash"] if accounts else None
            valid = (
                hmac.compare_digest(stored, digest(password))
                if stored
                else configured is not None
                and hmac.compare_digest(
                    configured.get_secret_value().encode(), password.encode()
                )
            )
            if not principal or not valid:
                raise HTTPException(401, "INVALID_CREDENTIALS")
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
            principal = (
                await resolve_principal(conn, users[0]["principal"]) if users else None
            )
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
