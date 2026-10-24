#!/usr/bin/env python3
# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Environment check for the Nimbus pivot. Prints a table; never prints secret values.

Usage:
    python scripts/envcheck.py            # human table, exit 0 always
    python scripts/envcheck.py --json     # machine-readable
    python scripts/envcheck.py --strict   # exit 1 if any REQUIRED check fails
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass
class Check:
    name: str
    status: str  # OK | MISSING | WARN
    required: bool
    detail: str = ""
    blocks: str = ""


def run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def tool(name: str, required: bool, blocks: str, version_cmd: list[str] | None = None) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(name, "MISSING", required, "not on PATH", blocks)
    detail = path
    if version_cmd:
        rc, out = run(version_cmd)
        if rc == 0 and out:
            detail = out.splitlines()[0][:80]
    return Check(name, "OK", required, detail, blocks)


def env_var(name: str, required: bool, blocks: str) -> Check:
    # Presence only. The value is never read into output.
    present = bool(os.environ.get(name)) or _in_dotenv(name)
    return Check(
        f"env:{name}",
        "OK" if present else "MISSING",
        required,
        "set" if present else "not set",
        blocks,
    )


def _in_dotenv(name: str) -> bool:
    for fname in (".env", ".env.local"):
        try:
            with open(fname, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip() == name and val.strip().strip("'\""):
                        return True
        except OSError:
            continue
    return False


def git_checks() -> list[Check]:
    out: list[Check] = []
    rc, top = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        return [Check("git repo", "MISSING", True, "run from inside the Nimbus repo", "everything")]
    out.append(Check("git repo", "OK", True, top))
    _, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    out.append(Check("git branch", "OK", False, branch))
    _, dirty = run(["git", "status", "--porcelain"])
    n = len([ln for ln in dirty.splitlines() if ln.strip()])
    out.append(
        Check(
            "working tree",
            "OK" if n == 0 else "WARN",
            False,
            "clean"
            if n == 0
            else f"{n} uncommitted paths (orchestrator will leave unrelated ones alone)",
        )
    )
    rc, remote = run(["git", "remote", "get-url", "origin"])
    out.append(
        Check(
            "git remote origin",
            "OK" if rc == 0 else "MISSING",
            True,
            "configured" if rc == 0 else "no origin remote",
            "PRs/CI",
        )
    )
    return out


def gh_checks() -> list[Check]:
    if not shutil.which("gh"):
        return [Check("gh auth", "MISSING", True, "gh CLI not installed", "PRs/merge")]
    rc, out = run(["gh", "auth", "status"])
    return [
        Check(
            "gh auth",
            "OK" if rc == 0 else "MISSING",
            True,
            "authenticated" if rc == 0 else "run `gh auth login`",
            "PRs/merge",
        )
    ]


def main() -> int:
    checks: list[Check] = []
    checks += git_checks()
    checks += gh_checks()
    checks.append(tool("uv", True, "all tests", ["uv", "--version"]))
    checks.append(tool("python", True, "scripts", [sys.executable, "--version"]))
    checks.append(
        tool("docker", False, "integration tests (Postgres), deploy", ["docker", "--version"])
    )
    checks.append(tool("claude", False, "runner script", ["claude", "--version"]))
    checks.append(tool("bash", False, "hooks/checkpoint script"))

    # Live-integration secrets: NOT required for building; each unblocks live steps only.
    for var, blocks in [
        ("TELEGRAM_BOT_TOKEN", "P2.1 live, prod"),
        ("TELEGRAM_BOT_TOKEN_STAGING", "P6.5 staging"),
        ("TELEGRAM_ALLOWED_USER_ID", "P2.1 live"),
        ("LLM_API_KEY", "P5.4 bake-off, live replies"),
        ("LLM_BASE_URL", "P5.4"),
        ("LLM_MODEL", "P5.4"),
        ("GOOGLE_CLIENT_ID", "P3.4-3.8 live"),
        ("GOOGLE_CLIENT_SECRET", "P3.4-3.8 live"),
        ("NOTION_TOKEN", "P3.7 live"),
        ("HEALTHCHECK_PING_URL", "P6.4"),
    ]:
        checks.append(env_var(var, False, blocks))

    if "--json" in sys.argv:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        w = max(len(c.name) for c in checks)
        for c in checks:
            tag = {"OK": "[ ok ]", "MISSING": "[miss]", "WARN": "[warn]"}[c.status]
            req = "REQ" if c.required else "opt"
            extra = f"  blocks: {c.blocks}" if c.status != "OK" and c.blocks else ""
            print(f"{tag} {req} {c.name:<{w}}  {c.detail}{extra}")
        print(
            "\nNote: optional MISSING items are tracked in docs/pivot/HUMAN_TODO.md; "
            "agents build with fakes until you provide them."
        )

    if "--strict" in sys.argv and any(c.required and c.status == "MISSING" for c in checks):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
