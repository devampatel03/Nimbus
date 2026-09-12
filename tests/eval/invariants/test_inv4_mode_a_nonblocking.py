# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-4: Mode A response path never blocks on a network call.

Acceptance: driving the Mode A response path with all network calls
stubbed to hang indefinitely must still return within the local
processing budget — i.e. no network call sits on the critical path.

Stub: `src/orchestrator` Mode A entrypoint does not exist yet, so this
fails by default (ModuleNotFoundError), which is correct until P-AGENT
implements it.
"""

from src.orchestrator.mode_a import (
    handle_turn,  # noqa: F401 (intentional: import fails until implemented)
)


def test_mode_a_response_does_not_block_on_network():
    raise NotImplementedError(
        "INV-4 stub: patch all network clients to hang forever, call "
        "handle_turn, and assert it returns within the local latency "
        "budget instead of blocking on any patched call."
    )
