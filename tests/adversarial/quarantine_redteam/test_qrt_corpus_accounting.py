# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Corpus accounting for the P4.1 quarantine red team (input to P4.7).

The end-to-end matrix is `VECTORS` x entry path, one case per pair:
invite (1 path), doc (2: Drive, Notion), transcript (2: Drive export, recalled
memory), forwarded (1: recalled memory), web (2: external item, local text
field). Email is DEFERRED (`test_qrt_email_deferred.py`).
"""

from __future__ import annotations

from tests.adversarial.quarantine_redteam._qrt_harness import ALL_TAGS, VECTORS

ENTRY_PATHS = {"invite": 1, "doc": 2, "transcript": 2, "forwarded": 1, "web": 2}
REQUIRED = {
    "calendar_invite_injection",
    "shared_doc_injection",
    "recorded_third_party_speech",
    "markdown_image_exfil",
    "link_preview_exfil",
    "tool_arg_smuggling",
    "memory_poisoning",
    "approval_spoofing",
    "callback_forgery",
    "canary_exfil",
    "encoding_obfuscation",
}
SOURCE_TAGS = {"calendar_invite_injection", "shared_doc_injection", "recorded_third_party_speech"}


def test_matrix_size_and_categories() -> None:
    ids = [v.vid for v in VECTORS]
    assert len(ids) == len(set(ids))
    end_to_end = len(VECTORS) * sum(ENTRY_PATHS.values())
    assert end_to_end >= 60, end_to_end
    # Source categories come from the entry path; attack categories from the vectors.
    assert REQUIRED - SOURCE_TAGS <= ALL_TAGS
    for v in VECTORS:
        assert v.attempts or v.withheld or v.echo or v.canary, f"{v.vid} has no liveness check"
