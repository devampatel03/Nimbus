# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

import pytest

from tests.unit.ingest.fakes import CLK, NOW


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    CLK.now = NOW
