# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Command line: `run` (default), `healthcheck` and `canary-sweep` (P4.4,
`src.app.sweep`: exit 0 clean, 1 findings, 2 config/database error).

`healthcheck` imports only settings and the heartbeat reader: no database, no
Telegram, no model. It prints one line with no secrets and exits 0 (healthy) or
1 (stale, missing or unreadable heartbeat).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from src.app.heartbeat import check_heartbeat
from src.app.settings import AppSettings


def healthcheck(path: Path | None = None, stale_s: float | None = None) -> int:
    try:
        cfg = AppSettings()
    except ValidationError:
        print("unhealthy: invalid APP_* settings")
        return 1
    report = check_heartbeat(
        path or cfg.heartbeat_path,
        stale_s=stale_s if stale_s is not None else cfg.heartbeat_stale_s,
    )
    print(("healthy: " if report.ok else "unhealthy: ") + report.detail)
    return 0 if report.ok else 1


def run() -> int:
    from src.app.runtime import AppConfig, describe_validation_error, run_app

    try:
        cfg = AppConfig.from_env()
    except ValidationError as exc:
        print(describe_validation_error(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - startup boundary: type name only
        print(f"startup failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_app(cfg, handle_signals=True))
    except KeyboardInterrupt:  # Windows: Ctrl-C cancels the main task
        return 0
    except Exception as exc:  # noqa: BLE001 - never 1 (= unhealthy); type name only
        print(f"startup failed: {type(exc).__name__}", file=sys.stderr)
        return 2


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.app")
    sub = parser.add_subparsers(dest="cmd")
    hc = sub.add_parser("healthcheck", help="exit 1 if the heartbeat is stale")
    hc.add_argument("--heartbeat-path", type=Path, default=None)
    hc.add_argument("--stale-s", type=float, default=None)
    sub.add_parser("canary-sweep", help="scan the database for canaries and secrets (P4.4)")
    args = parser.parse_args(list(argv))
    if args.cmd == "healthcheck":
        return healthcheck(args.heartbeat_path, args.stale_s)
    if args.cmd == "canary-sweep":
        from src.app.sweep import canary_sweep

        return asyncio.run(canary_sweep())
    return run()
