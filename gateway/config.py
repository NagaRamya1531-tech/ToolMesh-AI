from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOOLMESH_")

    database_url: SecretStr
    redis_url: SecretStr
    openai_api_key: SecretStr | None = None
    otlp_traces_endpoint: str | None = None
    probe_timeout: float = Field(default=2, gt=0, le=10)

    payments_secret: SecretStr | None = None
    support_secret: SecretStr | None = None
    admin_password: SecretStr | None = None
    support_password: SecretStr | None = None
    payments_password: SecretStr | None = None

    communication_secret: SecretStr | None = None
    finance_password: SecretStr | None = None
    approver_password: SecretStr | None = None

    it_password: SecretStr | None = None
    it_secret: SecretStr | None = None
    inventory_secret: SecretStr | None = None

    embedding_path: Path = Path(".models/discovery")

    hr_password: SecretStr | None = None
    hr_secret: SecretStr | None = None
    analytics_secret: SecretStr | None = None


class SandboxWorkerSettings(BaseSettings):
    """Independent configuration for the disposable ownership exercise."""

    model_config = SettingsConfigDict(env_prefix="TOOLMESH_SANDBOX_WORKER_")

    enabled: bool = False
    database_url: SecretStr
    lease_seconds: float = Field(default=20, ge=0.1, le=300)
    renew_seconds: float = Field(default=5, gt=0, le=60)
    poll_seconds: float = Field(default=0.25, gt=0, le=5)
    max_poll_seconds: float = Field(default=2, gt=0, le=30)
    drain_seconds: float = Field(default=4, gt=0, le=30)
    refund_verification_keys: SecretStr | None = None
