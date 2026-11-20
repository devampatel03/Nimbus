# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""P4.4 detector: every obfuscation in the table is caught for every planted
site; ordinary text (including URLs, ids, base64 blobs and other secrets) is
not; oversize / too-complex input fails closed."""

from __future__ import annotations

import base64
import os

import pytest

from src.canary import detect as det
from src.canary.detect import CanaryDetector, CanaryHit, skeleton
from src.canary.tokens import Site, derive_canaries, generate_canaries
from tests._canary_helpers import ENCODINGS, canaries

CS = canaries()


def detector() -> CanaryDetector:
    return CanaryDetector([(c.site.value, c.value, True) for c in CS])


@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
@pytest.mark.parametrize("site", list(Site))
def test_every_obfuscation_of_every_site_is_detected(encoding: str, site: Site) -> None:
    text = f"Sure, here it is: {ENCODINGS[encoding](CS.value(site))} -- anything else?"
    hit = detector().detect(text)
    assert hit is not None and hit.site == site.value


BENIGN = [
    "Remind me to call Mum at 6pm tomorrow about the dentist.",
    "The quick brown fox jumps over the lazy dog. " * 80,
    "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/view?usp=sharing",
    "Meeting id 8e4f1c2a-9b7d-4e3f-a1c2-0d9e8f7a6b5c, room 4B, 2026-10-29T09:30:00+05:30",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    base64.b64encode(b"just an ordinary attachment body, nothing to see " * 20).decode(),
    "Ünïcödé façade naïve résumé — “quotes” and emoji 🎉🎉",
    "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14 apples",
    "",
]


@pytest.mark.parametrize("text", BENIGN, ids=[f"benign{i}" for i in range(len(BENIGN))])
def test_ordinary_text_is_not_flagged(text: str) -> None:
    assert detector().detect(text) is None


def test_random_base64_noise_does_not_trip() -> None:
    d = detector()
    for _ in range(20):
        assert d.detect(base64.b64encode(os.urandom(3000)).decode()) is None


def test_a_different_deployment_canary_is_not_flagged() -> None:
    other = generate_canaries()
    assert detector().detect(" ".join(other.values())) is None


def test_derivation_is_deterministic_and_seed_specific() -> None:
    assert derive_canaries("s" * 32).values() == derive_canaries("s" * 32).values()
    assert derive_canaries("s" * 32).values() != derive_canaries("t" * 32).values()


def test_nine_characters_are_below_the_fragment_threshold() -> None:
    v = CS.value(Site.SYSTEM)
    assert detector().detect(v[:9]) is None
    assert detector().detect(v[:10]) is not None


def test_oversize_input_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(det, "MAX_SCAN_CHARS", 100)
    assert detector().detect("a" * 101) == CanaryHit("unknown", "oversize")


def test_too_many_candidate_runs_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = " , ".join(["abcdefghijklmnopqrstuvwx"] * 10)
    nested = base64.b64encode(inner.encode()).decode()
    assert detector().detect(nested) is None
    monkeypatch.setattr(det, "MAX_RUNS", 3)
    assert detector().detect(inner) is None  # top level is not run-capped
    assert detector().detect(nested) == CanaryHit("unknown", "too_complex")
    monkeypatch.setattr(det, "MAX_RUNS", 10_000)
    monkeypatch.setattr(det, "MAX_NESTED_CHARS", 10)
    assert detector().detect(nested) == CanaryHit("unknown", "too_complex")


def test_configured_literal_matches_whole_value_only() -> None:
    d = CanaryDetector([("configured", "CANARY-STRING-1", False), ("configured", "short", False)])
    assert d.detect("leak c.a.n.a.r.y s t r i n g 1 here") is not None
    assert d.detect("a canary string") is None  # partial: not a hit
    assert d.detect("short") is None  # < 8 skeleton chars are ignored
    assert CanaryDetector([]).empty and CanaryDetector([]).detect("anything") is None


def test_non_string_input_is_stringified() -> None:
    v = CS.value(Site.MEMORY)
    assert detector().detect({"x": v}) is not None  # type: ignore[arg-type]


def test_skeleton_folds_case_space_zero_width_homoglyph_and_leet() -> None:
    assert skeleton("A\u200bb-C d") == "abcd"
    assert skeleton("сорех") == skeleton("copex")  # Cyrillic look-alikes
    assert skeleton("h3ll0") == skeleton("hello")


@pytest.mark.parametrize("n", range(1, 26))
def test_every_rot_n_and_reversed_rot_n_is_detected(n: int) -> None:
    from tests._canary_helpers import _rot

    v = CS.value(Site.TOOL_FRAME)
    assert detector().detect(f"x {_rot(v, n)} y") is not None
    assert detector().detect(f"x {_rot(v[::-1], n)} y") is not None


def test_benign_corpus_stays_clean_with_all_forms() -> None:
    """False-positive recheck after widening (ROT-N/Atbash forms on the canary side)."""
    import random

    rng = random.Random(7)
    words = [
        "remind",
        "call",
        "mum",
        "dentist",
        "tomorrow",
        "meeting",
        "budget",
        "review",
        "project",
        "alpha",
        "notes",
        "lunch",
        "asha",
        "train",
        "ticket",
        "bangalore",
        "pune",
        "invoice",
        "deadline",
        "friday",
        "report",
        "draft",
    ]
    d = detector()
    for _ in range(300):
        text = " ".join(rng.choice(words) for _ in range(600))
        assert d.detect(text) is None
    for _ in range(50):
        assert d.detect(" ".join(f"{rng.getrandbits(8):08b}" for _ in range(200))) is None
