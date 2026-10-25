# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Gateway settings (env prefix `TELEGRAM_`, TARGET_DESIGN section 1).

The bot token is a `SecretStr` and is format-checked: it is interpolated into
the Bot API URL path, so anything outside `<digits>:<url-safe chars>` is refused
at construction. Validation errors never echo the offending value (INV-7).
"""

from __future__ import annotations

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TELEGRAM_FILE_HARD_CAP = 20 * 1024 * 1024  # Bot API getFile limit
_TOKEN_RE = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{20,128}$")


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    bot_token: SecretStr
    allowed_user_id: int = Field(gt=0)

    poll_timeout_s: int = Field(default=20, ge=0, le=50)
    # Our own caps, deliberately below Telegram's 20 MB.
    max_voice_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=TELEGRAM_FILE_HARD_CAP)
    max_document_bytes: int = Field(default=1 * 1024 * 1024, gt=0, le=TELEGRAM_FILE_HARD_CAP)
    per_chat_interval_s: float = Field(default=1.0, ge=0)
    global_interval_s: float = Field(default=1 / 30, ge=0)
    max_429_retries: int = Field(default=3, ge=0, le=10)
    max_retry_after_s: int = Field(default=120, ge=1, le=3600)

    @field_validator("bot_token")
    @classmethod
    def _token_shape(cls, v: SecretStr) -> SecretStr:
        if not _TOKEN_RE.fullmatch(v.get_secret_value()):
            raise ValueError("invalid bot token format")
        return v
