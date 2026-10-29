# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Canary settings (env prefix `CANARY_`, docs/pivot/SETTINGS_PATTERN.md).

- `CANARY_SEED`: hidden `SecretStr`, at least 32 characters. The per-deployment
  canary values are derived from it (HMAC-SHA256), so they are stable across
  restarts and the database sweep can look for leaks from any earlier run.
  Unset = fresh random canaries per process (the outbound check still works;
  the sweep refuses to run, it would only know this process's values).
  Never committed: it lives in the env/secret store with the other secrets.
- `CANARY_ALERT_TELEGRAM`: send the allowlisted user a fixed-text notice when a
  canary is blocked (default on).
- `CANARY_ALERT_COOLDOWN_S`: at most one notice per window (default 10 min).

There is deliberately no "disable" switch: the check always runs (fail closed).
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SEED_CHARS = 32


class CanarySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANARY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    seed: SecretStr | None = None
    alert_telegram: bool = True
    alert_cooldown_s: float = Field(default=600.0, ge=0, le=86_400)

    @field_validator("seed")
    @classmethod
    def _seed_len(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < MIN_SEED_CHARS:
            raise ValueError("canary seed too short")
        return v

    def secret_values(self) -> tuple[str, ...]:
        """For the log scrubber only."""
        return (self.seed.get_secret_value(),) if self.seed is not None else ()
