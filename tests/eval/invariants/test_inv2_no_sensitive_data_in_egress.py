# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-2: No memory/transcript/reasoning content in any egress payload.

Acceptance: given a representative agent turn containing memory context,
raw transcript, and chain-of-thought, the payload built for
`execute_external` must contain none of those fields verbatim.

Stub: `src/security/scrubber.py` does not exist yet, so this fails by
default (ModuleNotFoundError), which is correct until P-SEC implements it.
"""

from src.security.scrubber import (
    build_egress_payload,  # noqa: F401 (intentional: import fails until implemented)
)


def test_egress_payload_excludes_memory_transcript_and_reasoning():
    raise NotImplementedError(
        "INV-2 stub: build an egress payload from a fixture turn containing "
        "memory/transcript/reasoning and assert none of it appears verbatim "
        "in the payload sent to build_egress_payload's output."
    )
