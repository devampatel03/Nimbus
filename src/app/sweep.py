# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`python -m src.app canary-sweep` (P4.4): scan the database for planted
canaries (any obfuscation the detector knows) and the process's literal
secrets (bot token, model key, DB password, approval keys, ... exact match).

Exit 0 clean, 1 findings, 2 configuration or database error. Output is one
line of counts per table/column/category; never a value or a row id (INV-7).
Needs the same environment as `run`, plus `CANARY_SEED` (without it the
canaries are random per process, so a sweep could only find its own).
"""

from __future__ import annotations

from collections.abc import Callable

EXIT_CLEAN = 0
EXIT_FOUND = 1
EXIT_CONFIG = 2


async def canary_sweep(out: Callable[[str], None] = print) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.app.db import build_engine
    from src.app.runtime import AppConfig, build_canary_guard
    from src.canary.sweep import sweep_database
    from src.core.logging import install_log_scrubbing
    from src.memory.schemas import Base

    try:
        cfg = AppConfig.from_env()
    except Exception as exc:  # noqa: BLE001 - startup boundary: type name only
        out(f"canary sweep: invalid settings err={type(exc).__name__}")
        return EXIT_CONFIG
    if cfg.canary is None or cfg.canary.seed is None:
        out("canary sweep: CANARY_SEED is not set; nothing stable to look for")
        return EXIT_CONFIG
    guard = build_canary_guard(cfg)
    restore = install_log_scrubbing(cfg.secrets, matchers=(guard.log_matcher,))
    planted = set(cfg.canaries.values())
    literals = tuple(s for s in cfg.secrets if s not in planted)
    engine = build_engine(cfg.dsn, statement_timeout_s=600.0)
    try:
        async with AsyncSession(engine) as session:
            report = await sweep_database(
                session, guard.detector, metadata=Base.metadata, literals=literals
            )
    except Exception as exc:  # noqa: BLE001 - type name only
        out(f"canary sweep: failed err={type(exc).__name__}")
        return EXIT_CONFIG
    finally:
        await engine.dispose()
        restore()
    out(report.summary())
    return EXIT_CLEAN if report.clean else EXIT_FOUND
