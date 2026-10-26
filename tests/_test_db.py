# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Name rules for the per-session throwaway test databases (INFRA-1).

Every name is validated against one strict pattern before it is interpolated
into CREATE/DROP DATABASE, so a hostile or accidental name (wildcard
look-alikes, quotes, other projects' databases) can never be touched.
"""

import re

PREFIX = "nimbus_test_"
STALE_AFTER_SECONDS = 6 * 3600
_NAME_RE = re.compile(r"nimbus_test_([0-9]{10})_([0-9]{1,10})_([0-9a-f]{8})", re.ASCII)


def is_valid_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None


def require_valid_name(name: str) -> str:
    if not is_valid_name(name):
        raise ValueError("refusing DDL on a database name outside the nimbus_test namespace")
    return name


def is_stale(name: str, now: float) -> bool:
    """True only for in-namespace names whose epoch is in the past by > 6h.

    Future, zero or otherwise implausible epochs are rejected (not stale).
    Caveat: a live session older than 6h, or >6h of clock skew between hosts,
    can have its database dropped by another run's cleanup.
    """
    m = _NAME_RE.fullmatch(name)
    if m is None:
        return False
    age = now - int(m.group(1))
    return age > STALE_AFTER_SECONDS
