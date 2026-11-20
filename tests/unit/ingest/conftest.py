# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

import pytest

from tests.unit.ingest.fakes import CLK, NOW


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    CLK.now = NOW
