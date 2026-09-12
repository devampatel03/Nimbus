# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-1: Exactly one function performs cloud model egress.

Acceptance: `src/security/scrubber.py:execute_external` is the sole call site
that sends data to an external/cloud model. This test statically scans
`src/` for outbound HTTP client usage and asserts it only appears inside
that function.

Stub: `src/security/scrubber.py` does not exist yet, so this fails by
default (ModuleNotFoundError), which is correct until P-SEC implements it.
"""

from src.security.scrubber import (
    execute_external,  # noqa: F401 (intentional: import fails until implemented)
)


def test_only_scrubber_execute_external_calls_cloud_model():
    raise NotImplementedError(
        "INV-1 stub: implement a static scan of src/ asserting outbound "
        "cloud-model HTTP calls occur only within execute_external."
    )
