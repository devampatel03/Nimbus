# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tests.conftest as cf
from tests._test_db import is_stale, is_valid_name, require_valid_name

NOW = 1_800_000_000.0
OLD = f"nimbus_test_{int(NOW) - 7 * 3600}_123_abcdef01"


@pytest.mark.parametrize(
    "name",
    [OLD, "nimbus_test_1792972405_3728_78dc4095"],
)
def test_accepts_real_names(name: str) -> None:
    assert is_valid_name(name)
    assert require_valid_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "nimbusXtestY1000_x",
        "nimbusXtestY1000_poc",
        "nimbus",
        "nimbus_test_-5_x",
        "nimbus_test_-5_1_abcdef01",
        "nimbusXtestY1792972405_1_abcdef01",
        "nimbus_test_1792972405_12345678901_abcdef01",
        "nimbus_test_١792972405_1_abcdef01",
        'nimbus_test_1792972405_1_abcdef01"; DROP DATABASE x; --',
        "nimbus_test_1792972405_1_ABCDEF01",
        "nimbus_test_1792972405_1_abcdef01_extra",
    ],
)
def test_rejects_foreign_names(name: str) -> None:
    assert not is_valid_name(name)
    assert not is_stale(name, NOW)
    with pytest.raises(ValueError):
        require_valid_name(name)


def test_stale_only_when_older_than_six_hours() -> None:
    assert is_stale(OLD, NOW)
    assert not is_stale(f"nimbus_test_{int(NOW) - 3600}_1_abcdef01", NOW)
    assert not is_stale(f"nimbus_test_{int(NOW) + 10_000}_1_abcdef01", NOW)  # future
    assert not is_stale("nimbus_test_0000000000_1_abcdef01", 5.0)


def test_unreachable_postgres_does_not_crash_unit_tests() -> None:
    """M2: with Postgres unreachable, a pure unit test file must still pass."""
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "POSTGRES_PORT": "1"}
    env.pop("NIMBUS_TEST_SESSION_DB", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/unit/test_prompt_assembler.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-800:]
    assert "could not create throwaway test database" in proc.stdout + proc.stderr


def test_far_future_name_is_valid_but_never_stale() -> None:
    name = f"nimbus_test_{int(time.time()) + 7 * 3600}_1_abcdef01"
    assert is_valid_name(name)
    assert not is_stale(name, time.time())


@pytest.mark.parametrize("fn", [cf._drop_db, cf._create_db])
def test_ddl_helpers_validate_before_connecting(fn, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise AssertionError("connected before validating the name")

    monkeypatch.setattr(cf, "_admin_connect", boom)
    with pytest.raises(ValueError):
        asyncio.run(fn("nimbus"))


class _FakeConn:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.fetch_args: tuple[object, ...] = ()
        self.executed: list[str] = []

    async def fetch(self, *args: object) -> list[dict[str, str]]:
        self.fetch_args = args
        return [{"datname": n} for n in self.names]

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def close(self) -> None:
        pass


def test_drop_stale_only_drops_stale_valid_names(monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    stale = f"nimbus_test_{now - 7 * 3600}_5_abcdef01"
    conn = _FakeConn(
        [
            stale,
            f"nimbus_test_{now - 60}_5_abcdef02",  # fresh
            f"nimbus_test_{now + 7 * 3600}_5_abcdef03",  # future
            "nimbusXtestY1000_poc",
            "nimbus",
            "postgres",
        ]
    )

    async def fake_connect() -> _FakeConn:
        return conn

    monkeypatch.setattr(cf, "_admin_connect", fake_connect)
    asyncio.run(cf._drop_stale())
    assert conn.executed == [f'DROP DATABASE IF EXISTS "{stale}" WITH (FORCE)']
    query, pattern = conn.fetch_args
    assert "ESCAPE" in str(query)
    assert pattern == r"nimbus\_test\_%"


def test_env_value_outside_namespace_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    async def fake_create(name: str) -> None:
        created.append(name)

    async def fake_stale() -> None:
        return None

    bogus = f"nimbus_{os.getpid()}_x"
    monkeypatch.setenv(cf._ENV_VAR, bogus)
    monkeypatch.setattr(cf, "_session_db", None)
    monkeypatch.setattr(cf.settings, "postgres_db", cf.settings.postgres_db)
    monkeypatch.setattr(cf, "_create_db", fake_create)
    monkeypatch.setattr(cf, "_drop_stale", fake_stale)
    cf._activate_session_db()
    assert cf.settings.postgres_db != bogus
    assert is_valid_name(cf.settings.postgres_db)
    assert created == [cf.settings.postgres_db]
