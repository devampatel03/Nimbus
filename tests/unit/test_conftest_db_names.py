# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

import os
import subprocess
import sys
from pathlib import Path

import pytest

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
