# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.1 red team, category email_instruction_injection: DEFERRED.

There is no Gmail read tool yet: P3.6 (Gmail, drafts only) is blocked on human
gate H16 (ADR-0007 Proposed), so no email can reach a turn today. The quarantine
side already exists (`SourceKind.EMAIL` -> `EmailDigest`) and is exercised by
`tests/adversarial/lanes/test_lanes_injection.py`; the instruction-injection
vectors themselves run in the other categories here (`instruction_override`,
`send_and_share`).

When P3.6 lands, replace the skip with the `_qrt_harness.VECTORS` matrix over
the real Gmail read tool (shape: From / Subject / body, plus HTML parts and
attachments' file names), as in `test_qrt_doc.py`.
"""

from __future__ import annotations

import pytest

from src.policy import TOOL_RULES

DEFERRED_REASON = (
    "email category deferred: P3.6 Gmail read tool is blocked on H16 (ADR-0007 "
    "Proposed); no email source can reach a turn yet"
)


@pytest.mark.skip(reason=DEFERRED_REASON)
def test_email_category_placeholder() -> None:
    raise AssertionError("placeholder: implement with P3.6")


def test_no_gmail_tool_exists_while_the_category_is_deferred() -> None:
    """Fails the day a Gmail tool gets a policy row, so this file cannot stay
    a silent skip once email can actually reach a turn."""
    assert not [name for name in TOOL_RULES if "mail" in name.lower()]
