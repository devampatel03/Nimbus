# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

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

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
