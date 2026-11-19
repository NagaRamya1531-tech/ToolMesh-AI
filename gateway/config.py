from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOOLMESH_")

    database_url: SecretStr
    redis_url: SecretStr
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
