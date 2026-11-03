# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Proactive-message settings (P5.1; env prefix `PROACTIVE_`,
docs/pivot/SETTINGS_PATTERN.md). All times are Asia/Kolkata wall-clock "HH:MM"
(India has no DST). There is NO model call in the proactive jobs: every message is a template
built in code (a model "polish" would need an INV-6 prompt-site allow-list entry, rule 4)."""

from __future__ import annotations

import re
from datetime import time

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_BUDGET_PER_DAY = 6  # R11 / ADR-0011: the hard ceiling


def parse_hhmm(raw: str) -> time:
    m = _HHMM.fullmatch(raw.strip())
    if m is None:
        raise ValueError("expected HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


class ProactiveSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROACTIVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    enabled: bool = True
    # Interruption budget across ALL kinds, per IST calendar day (ADR-0011). The
    # ceiling is 6: a larger value is refused, not clamped.
    budget_per_day: int = Field(default=MAX_BUDGET_PER_DAY, ge=0, le=MAX_BUDGET_PER_DAY)
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"

    morning_enabled: bool = True
    morning_at: str = "08:30"
    morning_valid_h: float = Field(default=6.0, gt=0, le=18)  # too late after this: skipped
    evening_enabled: bool = True
    evening_at: str = "20:30"
    evening_valid_h: float = Field(default=1.5, gt=0, le=18)
    weekly_enabled: bool = True
    weekly_weekday: int = Field(default=6, ge=0, le=6)  # Monday=0 .. Sunday=6
    weekly_at: str = "18:00"
    weekly_valid_h: float = Field(default=4.0, gt=0, le=18)
    weekly_cards_max: int = Field(default=3, ge=0, le=10)  # promotion cards per review

    nudges_enabled: bool = True
    nudge_lead_h: float = Field(default=24.0, gt=0, le=168)  # "approaching": due within this
    nudge_overdue_days_max: int = Field(default=3, ge=0, le=30)  # then the brief only
    nudge_max_per_day: int = Field(default=3, ge=0, le=MAX_BUDGET_PER_DAY)
    nudge_scan_s: float = Field(default=600.0, ge=1, le=86_400)
    nudge_valid_h: float = Field(default=4.0, gt=0, le=18)

    items_max: int = Field(default=5, ge=1, le=10)  # lines per section in a message
    skip_empty_brief: bool = True
    snooze_max_h: float = Field(default=72.0, gt=0, le=720)

    @field_validator("quiet_start", "quiet_end", "morning_at", "evening_at", "weekly_at")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v.strip()

    def times(self) -> dict[str, time]:
        return {
            "quiet_start": parse_hhmm(self.quiet_start),
            "quiet_end": parse_hhmm(self.quiet_end),
            "morning": parse_hhmm(self.morning_at),
            "evening": parse_hhmm(self.evening_at),
            "weekly": parse_hhmm(self.weekly_at),
        }
