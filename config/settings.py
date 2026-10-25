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

    # Qdrant (episodic vectors)
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333

    # Local LLM (vLLM, OpenAI-compatible, this session). NOT cloud egress --
    # `llm_base_url` defaults to loopback and `src/llm/client.py` never
    # routes through the scrubber (INV-1 unaffected). Dev default model is
    # the small Qwen2.5-7B-Instruct; production's Qwen3.8-27B needs a
    # bigger GPU and is set via `.env`, not this default.
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"  # vLLM's OpenAI-compatible server ignores this by default
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_timeout_seconds: float = 60.0

    # Cloud egress (scrubber -> Gemini 3.8 Flash, AD-2). Must be https.
    # `cloud_egress_url` is the Gemini generateContent API base; the
    # per-request URL is built as `{cloud_egress_url}/{gemini_model}:generateContent`
    # in src/security/scrubber.py -- see that module for the real call.
    cloud_egress_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    cloud_egress_api_key: str = ""
    cloud_egress_timeout_seconds: float = 20.0  # AD-2: 13.3s TTFT + headroom
    cloud_egress_enabled_default: bool = True  # kill switch initial state
    gemini_model: str = "gemini-3.8-flash"  # AD-2: the tool-execution model

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


settings = Settings()
