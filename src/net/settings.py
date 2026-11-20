# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Module-local settings for `src.net` (TARGET_DESIGN section 1, prefix `NET_`).

The LLM base URL is shared with `src.llm`, so it is also read from the
un-prefixed `LLM_BASE_URL` env var. Only the *host* of it is used here; it
must be https on the default port, or construction fails (fail loud rather
than silently allowing plaintext egress).

Fixed vendor hosts (Telegram, Google, Notion) are not overridable: an env var
must not be able to point `Service.TELEGRAM` at an arbitrary host. Services
can only be *disabled* (`NET_ENABLED_SERVICES`). The three configurable hosts
(LLM, healthcheck, backup) are off unless set.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.net.errors import EgressConfigError

# Lowercase DNS name, >= 2 labels, alphabetic TLD. Rejects IP literals
# (v4 has a numeric TLD, v6 has ':'), 'localhost', and single-label names,
# so env misconfiguration cannot aim egress at link-local metadata or LAN.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

HARD_MAX_RESPONSE_BYTES = 25 * 1024 * 1024  # Telegram file cap is 20 MB
HARD_MAX_TOTAL_TIMEOUT_S = 300.0


def validate_hostname(value: str) -> str:
    host = value.strip().lower()
    if not _HOSTNAME_RE.fullmatch(host):
        raise EgressConfigError("invalid egress hostname")
    return host


def host_from_https_url(url: str) -> str:
    """Return the hostname of an https URL; reject anything else."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise EgressConfigError("LLM base URL must use https")
    if parts.username is not None or parts.password is not None:
        raise EgressConfigError("LLM base URL must not contain credentials")
    try:
        port = parts.port
    except ValueError:
        raise EgressConfigError("LLM base URL has an invalid port") from None
    if port not in (None, 443):
        raise EgressConfigError("LLM base URL must use the default https port")
    return validate_hostname(parts.hostname or "")


class NetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # INV-7: a ValidationError must never echo the offending value (a
        # mis-pasted URL can carry credentials).
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    llm_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NET_LLM_BASE_URL", "LLM_BASE_URL"),
    )
    healthcheck_host: str | None = None
    backup_host: str | None = None

    # Service names (see src.net.policy.Service). None => all services whose
    # host is known are enabled. An explicit list is the full enabled set.
    enabled_services: frozenset[str] | None = None

    connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    read_timeout_s: float = Field(default=30.0, gt=0, le=HARD_MAX_TOTAL_TIMEOUT_S)
    write_timeout_s: float = Field(default=30.0, gt=0, le=HARD_MAX_TOTAL_TIMEOUT_S)
    pool_timeout_s: float = Field(default=5.0, gt=0, le=60)
    total_timeout_s: float = Field(default=60.0, gt=0, le=HARD_MAX_TOTAL_TIMEOUT_S)

    max_response_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=HARD_MAX_RESPONSE_BYTES)
    max_redirects: int = Field(default=3, ge=0, le=5)

    @field_validator("llm_base_url")
    @classmethod
    def _llm_url_https(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        host_from_https_url(v)
        return v.strip()

    @field_validator("healthcheck_host", "backup_host")
    @classmethod
    def _host_ok(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_hostname(v)
