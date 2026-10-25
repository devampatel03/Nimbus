# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared application configuration (Postgres, API auth, CORS).

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

    # TODO(P2.1b): auth_password / jwt_secret_key should be SecretStr too; left
    # as str now because src/api and its tests (deleted/rewritten in P2.1b)
    # read them as plain strings.
    # API auth (Nimbus is single-user, self-hosted, PRD section 1 -- there is
    # no user table, just one shared login secret). Dev-only defaults below,
    # same pattern as `postgres_password` above: MUST be overridden via
    # `.env` before any real deployment.
    auth_password: str = "dev-insecure-change-me"
    # >=32 bytes: HS256's RFC 7518 3.2 minimum recommended key length --
    # PyJWT warns (InsecureKeyLengthWarning) below that, even for a
    # placeholder that's already flagged for override.
    jwt_secret_key: str = "dev-insecure-change-me-before-deploying-anywhere"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24h

    # CORS. "*" is a permissive dev default; scope down for real deployment.
    # `allow_credentials` stays False (see src/api/main.py) so this never
    # collides with the wildcard-origin+credentials restriction browsers
    # enforce -- auth here is a Bearer token, not a cookie, so credentialed
    # CORS was never needed.
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
