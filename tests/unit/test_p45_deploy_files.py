# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.5 / R13: compose network lint and nftables ruleset checks. Static; no
Docker daemon and no root needed. The live syntax check runs only if `nft`
exists (it does not on dev boxes; CI on ubuntu may install it)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
OVERRIDE = ROOT / "docker-compose.override.yml"
NFT = ROOT / "deploy" / "nftables" / "nimbus-egress.nft"


def _load(p: Path) -> dict[str, Any]:
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _nets(svc: dict[str, Any]) -> list[str]:
    n = svc.get("networks", [])
    return list(n) if isinstance(n, (list, dict)) else []


def test_base_postgres_internal_only_and_unpublished() -> None:
    cfg = _load(BASE)
    pg = cfg["services"]["postgres"]
    assert not pg.get("ports")
    assert _nets(pg) == ["db"]
    assert cfg["networks"]["db"]["internal"] is True


def test_no_service_in_base_publishes_ports() -> None:
    for name, svc in _load(BASE)["services"].items():
        assert not svc.get("ports"), name
        assert svc.get("network_mode") not in ("host",), name


def test_override_binds_loopback_only() -> None:
    cfg = _load(OVERRIDE)
    for svc in cfg["services"].values():
        for port in svc.get("ports", []):
            text = str(port)
            assert text.startswith("127.0.0.1:"), text
            assert "0.0.0.0" not in text


def test_app_stub_is_profile_gated_and_not_on_internal_only_net() -> None:
    cfg = _load(BASE)
    app = cfg["services"]["app"]
    assert app["profiles"] == ["app"]
    assert "egress" in app["networks"]
    assert cfg["networks"]["egress"].get("internal") is not True
    assert "environment" not in app  # secrets come from the 0600 env file only
    assert str(app["env_file"]).startswith("/etc/")


def test_postgres_never_joins_egress_network() -> None:
    cfg = _load(BASE)
    assert "egress" not in _nets(cfg["services"]["postgres"])


def test_no_secret_values_in_compose() -> None:
    text = BASE.read_text(encoding="utf-8")
    for needle in ("TELEGRAM_BOT_TOKEN", "API_KEY", "SECRET", "sk-"):
        assert needle not in text


def test_nft_bridge_name_matches_compose() -> None:
    bridge = _load(BASE)["networks"]["egress"]["driver_opts"]["com.docker.network.bridge.name"]
    assert f'"{bridge}"' in NFT.read_text(encoding="utf-8")


def test_nft_policy_shape() -> None:
    t = NFT.read_text(encoding="utf-8")
    body = "\n".join(line for line in t.splitlines() if not line.lstrip().startswith("#"))
    assert "tcp dport 443 accept" in body
    assert "169.254.0.0/16" in body and "10.0.0.0/8" in body
    assert "counter drop" in body
    # No other TCP port is ever accepted.
    accepts = [ln.strip() for ln in body.splitlines() if "tcp dport" in ln and "accept" in ln]
    assert all(("443" in ln) or ("53" in ln) for ln in accepts), accepts
    assert "dport 123" not in body and "NTP" not in body
    assert "dport 22" not in body and "dport 80 " not in body


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft binary not installed on this host")
def test_nft_syntax_check() -> None:
    # `nft -c` needs CAP_NET_ADMIN on some builds; skip rather than fail then.
    r = subprocess.run(["nft", "-c", "-f", str(NFT)], capture_output=True, text=True, check=False)
    if "Operation not permitted" in r.stderr:
        pytest.skip("nft -c requires privileges here")
    assert r.returncode == 0, r.stderr
