# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Application configuration, loaded from environment / .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres (semantic memory + bi-temporal graph via Apache AGE, pgvector)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "nimbus"
    postgres_password: str = Field(default="nimbus")
    postgres_db: str = "nimbus"

    # Redis (working memory)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # Qdrant (episodic vectors)
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Cloud egress (scrubber -> Gemini 3.8 Flash, AD-2). Must be https.
    cloud_egress_url: str = "https://generativelanguage.googleapis.com/v1/tool-execute"
    cloud_egress_api_key: str = ""
    cloud_egress_timeout_seconds: float = 20.0  # AD-2: 13.3s TTFT + headroom
    cloud_egress_enabled_default: bool = True  # kill switch initial state

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
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


settings = Settings()
