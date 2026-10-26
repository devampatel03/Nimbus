# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Command line: `run` (default) and `healthcheck`.

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
    try:
        return asyncio.run(run_app(cfg, handle_signals=True))
    except KeyboardInterrupt:  # Windows: Ctrl-C cancels the main task
        return 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.app")
    sub = parser.add_subparsers(dest="cmd")
    hc = sub.add_parser("healthcheck", help="exit 1 if the heartbeat is stale")
    hc.add_argument("--heartbeat-path", type=Path, default=None)
    hc.add_argument("--stale-s", type=float, default=None)
    args = parser.parse_args(list(argv))
    if args.cmd == "healthcheck":
        return healthcheck(args.heartbeat_path, args.stale_s)
    return run()
