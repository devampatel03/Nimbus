# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Cloud egress kill switch (SCR-6).

A single in-memory, thread-safe toggle that `scrubber.execute_external`
checks before doing anything else — including before validating the
intent or touching the network. Everything else in Nimbus (memory,
personality, local generation, tool *planning*) must keep working with
the switch engaged; only `execute_external` is gated by it.
"""

import threading

_lock = threading.Lock()
_engaged = False


class KillSwitchEngagedError(RuntimeError):
    """Raised by execute_external when the kill switch is engaged."""


def engage() -> None:
    """Disable all cloud egress. Idempotent."""
    global _engaged
    with _lock:
        _engaged = True


def disengage() -> None:
    """Re-enable cloud egress. Idempotent."""
    global _engaged
    with _lock:
        _engaged = False


def is_engaged() -> bool:
    with _lock:
        return _engaged
