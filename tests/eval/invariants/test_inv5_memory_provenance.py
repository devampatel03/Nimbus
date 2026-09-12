# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-5: Every derived memory row has non-null provenance.

Acceptance: any memory row written by an extraction worker must carry a
non-null provenance reference (source turn/document id) — rows with null
provenance must be rejected at the write boundary.

Stub: `src/memory` write path does not exist yet, so this fails by
default (ModuleNotFoundError), which is correct until P-MEMORY implements it.
"""

from src.memory.store import (
    write_derived_memory,  # noqa: F401 (intentional: import fails until implemented)
)


def test_derived_memory_write_rejects_null_provenance():
    raise NotImplementedError(
        "INV-5 stub: attempt to write a derived memory row with "
        "provenance=None via write_derived_memory and assert it is "
        "rejected rather than persisted."
    )
