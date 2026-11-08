# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1/P6.2 static lint of the Dockerfile, compose `app`/`migrate` services,
init and bootstrap scripts. No Docker daemon needed. The scripts have never run
on a VM (H6); these tests pin the properties that matter for safety."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
BOOTSTRAP = ROOT / "deploy" / "bootstrap-vm.sh"
INIT_SH = ROOT / "infra" / "docker" / "postgres" / "20-app-role.sh"
UNIT = ROOT / "deploy" / "nimbus.service"
SERVICES = ("app", "migrate")


def _svc(name: str) -> dict[str, Any]:
    cfg = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    out = cfg["services"][name]
    assert isinstance(out, dict)
    return out


def _docker_lines() -> list[str]:
    return [
        ln.strip()
        for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_dockerfile_bases_are_digest_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    args = re.findall(r"^ARG (?:PYTHON|UV)_IMAGE=(\S+)$", text, re.MULTILINE)
    assert len(args) == 2 and all(re.search(r"@sha256:[0-9a-f]{64}$", a) for a in args)


def test_dockerfile_nonroot_frozen_nodev_healthcheck() -> None:
    lines = _docker_lines()
    user = [ln for ln in lines if ln.startswith("USER ")]
    assert user and user[-1] != "USER root" and not user[-1].split()[1].startswith("0")
    sync = " ".join(ln for ln in lines if "uv sync" in ln)
    assert "--frozen" in sync and "--no-dev" in sync
    joined = " ".join(lines)
    assert re.search(r"HEALTHCHECK .*CMD \[[^\]]*src\.app[^\]]*healthcheck", joined)
    assert sum(1 for ln in lines if ln.startswith("FROM ")) >= 3  # multi-stage


def test_dockerfile_bakes_no_secrets_or_env_files() -> None:
    text = "\n".join(_docker_lines())
    assert not re.search(r"COPY\s+.*\.env", text)
    assert not re.search(r"\bENV\b[^\n]*(TOKEN|PASSWORD|API_KEY|SECRET)", text, re.IGNORECASE)
    ign = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in ign and ".git" in ign and "tests" in ign


@pytest.mark.parametrize("name", SERVICES)
def test_app_services_are_hardened(name: str) -> None:
    s = _svc(name)
    assert s["read_only"] is True
    assert s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]
    assert "environment" not in s  # secrets only via the 0600 env file
    assert str(s["env_file"]).startswith("/etc/nimbus/")
    assert not s.get("ports")
    assert s.get("mem_limit")


def test_app_restart_grace_and_networks() -> None:
    s = _svc("app")
    assert s["restart"] in ("unless-stopped", "always")
    grace = str(s["stop_grace_period"])
    assert grace.endswith("s") and int(grace[:-1]) >= 20
    assert set(s["networks"]) == {"db", "egress"}
    assert s["env_file"] == "/etc/nimbus/app.env"
    assert s["profiles"] == ["app"]


def test_migrate_has_owner_creds_file_and_no_internet() -> None:
    m = _svc("migrate")
    assert m["env_file"] == "/etc/nimbus/migrate.env" and m["profiles"] == ["ops"]
    assert m["networks"] == ["db"]  # internal only: no route out
    assert "db-bootstrap" in " ".join(m["entrypoint"])
    assert _svc("app")["env_file"] != m["env_file"]  # owner creds never in the app env


def test_init_script_and_bootstrap_safety() -> None:
    init = INIT_SH.read_text(encoding="utf-8")
    assert "NOSUPERUSER" in init and "%L" in init and "set -eu" in init
    code = chr(10).join(ln for ln in init.splitlines() if not ln.lstrip().startswith("#"))
    assert "ALTER DEFAULT PRIVILEGES" not in code  # default-deny: db-bootstrap grants
    b = BOOTSTRAP.read_text(encoding="utf-8")
    assert b.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in b
    for needle in (
        "PasswordAuthentication no",
        "default deny incoming",
        "allow 22/tcp",
        "unattended-upgrades",
        "chmod 0600",
        "nft -c -f",
        "sshd -t",
    ):
        assert needle in b, needle
    # no inbound port other than SSH is ever opened
    assert [m for m in re.findall(r"ufw allow (\S+)", b)] == ["22/tcp"]
    # never reads or prints env-file contents
    assert not re.search(r"\b(cat|source|\.)\s+\"?\$(APP_ENV|MIGRATE_ENV)", b)


def test_unit_orders_after_docker_and_loads_ruleset() -> None:
    u = UNIT.read_text(encoding="utf-8")
    assert "After=docker.service" in u and "nft -f /etc/nimbus-egress.nft" in u
    assert "--profile app up -d" in u


@pytest.mark.parametrize("path", [DOCKERFILE, BOOTSTRAP, INIT_SH, UNIT])
def test_lf_line_endings(path: Path) -> None:
    assert b"\r\n" not in path.read_bytes(), path.name
    assert "eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    r = subprocess.run(
        ["shellcheck", str(BOOTSTRAP), str(INIT_SH)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stdout
