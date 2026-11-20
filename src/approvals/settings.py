# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Approval settings (env prefix `APPROVAL_`, docs/pivot/SETTINGS_PATTERN.md).

Both secrets are hidden `SecretStr` values; validation errors never echo them
(`hide_input_in_errors`) and the runtime adds them to the log scrubber (INV-7).

- `APPROVAL_HMAC_KEY`: at least 32 bytes. Unset = approvals are OFF and every
  `require_approval` verdict stays a denial (`approval_unavailable`).
- `APPROVAL_TOTP_SECRET`: base32, at least 128 bits. Unset = actions that need a
  step-up (destructive, external recipient) can never be approved.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.approvals.token import MIN_KEY_BYTES
from src.approvals.totp import TotpSecretError, decode_secret


class ApprovalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APPROVAL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    hmac_key: SecretStr | None = None
    totp_secret: SecretStr | None = None
    ttl_s: int = Field(default=600, ge=60, le=900)  # ADR-0009: 10 minutes
    step_up_window_s: int = Field(default=180, ge=30, le=600)
    max_open: int = Field(default=3, ge=1, le=10)  # open cards at once (spam bound)
    # Commitment cards (P3.9) are model-originated: their own smaller cap, so they
    # cannot starve tool approvals of the `max_open` slots.
    max_open_commitment: int = Field(default=1, ge=1, le=3)
    totp_attempts_per_action: int = Field(default=3, ge=1, le=5)
    totp_lockout_failures: int = Field(default=5, ge=1, le=20)
    totp_lockout_window_s: int = Field(default=900, ge=60, le=86_400)
    display_utc_offset_minutes: int = Field(default=330, ge=-720, le=840)

    @field_validator("hmac_key")
    @classmethod
    def _key_len(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value().encode("utf-8")) < MIN_KEY_BYTES:
            raise ValueError("hmac key too short")
        return v

    @field_validator("totp_secret")
    @classmethod
    def _totp_shape(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return v
        try:
            decode_secret(v.get_secret_value())
        except TotpSecretError:
            raise ValueError("invalid totp secret") from None
        return v

    @property
    def enabled(self) -> bool:
        return self.hmac_key is not None

    def key_bytes(self) -> bytes:
        if self.hmac_key is None:
            raise RuntimeError("approvals disabled")
        return self.hmac_key.get_secret_value().encode("utf-8")

    def totp_bytes(self) -> bytes | None:
        if self.totp_secret is None:
            return None
        return decode_secret(self.totp_secret.get_secret_value())

    def secret_values(self) -> tuple[str, ...]:
        """For the log scrubber only."""
        return tuple(
            s.get_secret_value() for s in (self.hmac_key, self.totp_secret) if s is not None
        )
