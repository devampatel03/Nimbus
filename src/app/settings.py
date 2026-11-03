# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""App settings (env prefix `APP_`, docs/pivot/SETTINGS_PATTERN.md).

`env` defaults to `production`: the safe value is the default, so a missing
variable can never relax a gate. Only the character-card dev bypass is tied to
it (production refuses dev mode, see `src.app.card`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_heartbeat_path() -> Path:
    """A per-user state directory, never the shared temp dir: another local user
    could otherwise pre-create, replace or forge the file. `Heartbeat.prepare()`
    creates the directory 0700 and refuses an unsafe location."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nimbus" / "heartbeat.json"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    env: Literal["development", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Heartbeat file, written by every loop, read by `healthcheck`.
    heartbeat_path: Path = Field(default_factory=default_heartbeat_path)
    heartbeat_stale_s: float = Field(default=180.0, gt=0, le=3600)

    # Supervision: capped exponential backoff between restarts of a crashed loop.
    backoff_initial_s: float = Field(default=1.0, gt=0, le=60)
    backoff_max_s: float = Field(default=60.0, gt=0, le=600)
    backoff_reset_after_s: float = Field(default=300.0, gt=0)
    shutdown_grace_s: float = Field(default=10.0, gt=0, le=120)

    # Worker / scheduler cadence.
    recover_interval_s: float = Field(default=60.0, gt=0, le=3600)
    recover_limit: int = Field(default=20, ge=1, le=200)
    scheduler_tick_s: float = Field(default=30.0, gt=0, le=3600)
    # Job runner poll (ADR-0008: 5 s). Reminder delivery latency is this plus the
    # send time, so it must stay well inside R11's 60 s.
    job_poll_s: float = Field(default=5.0, gt=0, le=30)
    job_lease_s: float = Field(default=300.0, gt=10, le=3600)
    extract_pause_s: float = Field(default=2.0, gt=0, le=600)  # between memory_extract batches (R5)
    job_batch: int = Field(default=20, ge=1, le=200)

    # Turn pipeline.
    turn_queue_size: int = Field(default=8, ge=1, le=100)
    history_messages: int = Field(default=20, ge=0, le=100)
    # P3.0 MED-D: hard cap on quarantine-lane model calls per turn (memory and
    # tool output together); items past it are withheld. Literals (this module
    # stays import-light for `healthcheck`); a test pins them to
    # `src.lanes.quarantine` MAX_QUARANTINE_CALLS_PER_TURN / QUARANTINE_CALLS_RANGE.
    quarantine_calls_per_turn: int = Field(default=10, ge=0, le=25)

    # P5.5 cost levers (docs/pivot/reports/COST_MODEL.md). Defaults keep today's
    # behaviour; the cheap profile is documented there and in ADR-0025.
    memory_top_k: int = Field(default=5, ge=0, le=20)
    memory_item_chars: int = Field(default=1200, ge=100, le=8000)
    # memory_extract: minimum message length, and "all" (every non-trivial
    # message) or "cue" (only messages with a first-person fact/commitment cue).
    extract_min_chars: int = Field(default=40, ge=1, le=4000)
    extract_mode: Literal["all", "cue"] = "all"
    # Extraction pauses once the IST day's spend passes this share of the cap.
    extract_max_spend_share: float = Field(default=0.30, gt=0, le=1)

    # DB: no statement may run longer than this (a hung audit write must not
    # block a loop forever, P1.4b follow-up).
    db_statement_timeout_s: float = Field(default=30.0, gt=0, le=600)

    embedder: Literal["local", "api"] = "local"

    # In-process fakes for Telegram/LLM/embeddings: lets `python -m src.app` run
    # with no token and no network. Refused unless env=development.
    fake_backends: bool = False
