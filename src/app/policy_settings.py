# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Operator policy switches (env prefix `POLICY_`, docs/pivot/SETTINGS_PATTERN.md).

It lives in `src.app`, not `src.policy`: the policy package is pure (stdlib-only
imports, pinned by the INV-5 eval test), so the environment is read here and only
a plain bool crosses into `src.policy.config_with_creates`.

`POLICY_ENABLE_CREATES` (default false) lets the model's `tasks_create` and
`reminders_create` reach the ADR-0009 matrix instead of `deny.tool_disabled`.
It is human-edited: read once at boot from the environment / `.env`, frozen, and
no tool, prompt or model output can set it (INV-5). Strict: only `true` or
`false` (any case) or a real bool; `1`, `yes`, `on` and the like are refused, so
a typo can never switch it on.
"""

from __future__ import annotations

from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.policy import PolicyConfig, config_with_creates


class PolicySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLICY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    enable_creates: bool = False

    @field_validator("enable_creates", mode="before")
    @classmethod
    def _strict_bool(cls, v: Any) -> bool:
        if type(v) is bool:
            return v
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower() == "true"
        raise ValueError("expected true or false")

    def policy_config(self) -> PolicyConfig:
        return config_with_creates(self.enable_creates)
