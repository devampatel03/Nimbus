# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared application configuration (Postgres connection parts only).

Module-owned settings live next to their module (`src/llm/settings.py`,
`src/net/settings.py`); see docs/pivot/SETTINGS_PATTERN.md. Do not add a
module's knobs here.
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # INV-7: a ValidationError must never echo the offending value.
        hide_input_in_errors=True,
    )

    # Postgres (semantic memory, bi-temporal graph tables, pgvector episodic memory)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "nimbus"
    postgres_password: SecretStr = Field(default=SecretStr("nimbus"))
    postgres_db: str = "nimbus"

    # P6.1 (INV-3/R7): optional least-privilege runtime role. Unset = the app runs
    # as `postgres_user` (the owner/migration role) exactly as before, with a
    # startup WARNING. Both set = the app connects as this role. Migrations and
    # `db-bootstrap` always use `postgres_dsn` (the owner).
    postgres_app_user: str | None = None
    postgres_app_password: SecretStr | None = None

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def app_role_split(self) -> bool:
        return bool(self.postgres_app_user) and self.postgres_app_password is not None

    @property
    def runtime_dsn(self) -> str:
        """DSN for the running app: the least-privilege role when configured."""
        if not self.app_role_split or self.postgres_app_password is None:
            return self.postgres_dsn
        return (
            f"postgresql+asyncpg://{self.postgres_app_user}:"
            f"{self.postgres_app_password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
