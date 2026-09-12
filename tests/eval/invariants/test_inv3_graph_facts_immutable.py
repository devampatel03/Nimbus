# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-3: Graph facts are never mutated in place — supersession only.

Acceptance: attempting to change an existing fact's object or valid_from
must never issue an UPDATE against the fact row; it must instead insert a
new fact row and mark the prior one superseded.

Stub: `src/memory` graph repository does not exist yet, so this fails by
default (ModuleNotFoundError), which is correct until P-MEMORY implements it.
"""

from src.memory.graph import (
    GraphRepository,  # noqa: F401 (intentional: import fails until implemented)
)


def test_updating_a_fact_creates_supersession_not_mutation():
    raise NotImplementedError(
        "INV-3 stub: write a fact, request a change to it via "
        "GraphRepository, and assert the original row is untouched while a "
        "new row with a supersedes pointer was created."
    )
