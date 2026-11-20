# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P6.1: one DSN = behave as before; app role set = runtime DSN switches, the
migration DSN never does (ADR-0026)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from config.settings import Settings
from src.db.roles import _ident


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, postgres_host="h", postgres_db="d", **kw)  # type: ignore[call-arg]


def test_single_dsn_is_unchanged_and_not_split() -> None:
    s = _s()
    assert not s.app_role_split and s.runtime_dsn == s.postgres_dsn


def test_split_uses_app_credentials_for_runtime_only() -> None:
    s = _s(postgres_app_user="nimbus_app", postgres_app_password=SecretStr("pw-app"))
    assert s.app_role_split
    tail = "@h:5432/d"
    assert ("nimbus_app:" + "pw-app" + tail) in s.runtime_dsn
    assert "nimbus_app" not in s.postgres_dsn  # migrations stay on the owner


def test_half_configured_is_not_split() -> None:
    assert not _s(postgres_app_user="nimbus_app").app_role_split
    assert not _s(postgres_app_password=SecretStr("x")).app_role_split


@pytest.mark.parametrize("bad", ['x"; DROP ROLE y; --', "Upper", "", "a" * 64, "a b"])
def test_role_name_is_validated(bad: str) -> None:
    with pytest.raises(ValueError):
        _ident(bad)
