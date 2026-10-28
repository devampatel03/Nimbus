# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P3.2 pure primitives: canonical payload hash, the 46-byte HMAC token, RFC
6238 TOTP, the card text and the sealed `ConsumedApproval`. Table-driven; each
guard has a test that fails if the guard is removed (see the mutation notes)."""

from __future__ import annotations

import copy
import dataclasses
import hmac
import math
import pickle
import uuid
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest

from src.approvals import canonical, card, token, totp
from src.approvals import types as atypes
from src.approvals.types import ConsumedApproval, seal_consumed, thaw

KEY = b"k" * 32
AID = uuid.UUID(int=7)
SHA = "ab" * 32
NONCE = b"n" * 16
EXP = 1_800_000_000
USER = 4242

# ------------------------------------------------------------------ canonical


def test_hash_is_order_independent_and_stable() -> None:
    a = canonical.payload_sha256("t", "call", {"b": 1, "a": [1, {"y": 2, "x": None}]})
    b = canonical.payload_sha256("t", "call", {"a": [1, {"x": None, "y": 2}], "b": 1})
    assert a == b and len(a) == 64 and a == a.lower()


@pytest.mark.parametrize(
    ("tool", "action", "payload"),
    [
        ("t2", "call", {"a": 1}),
        ("t", "promote", {"a": 1}),
        ("t", "call", {"a": 2}),
        ("t", "call", {"a": "1"}),
        ("t", "call", {"a": 1, "b": None}),
        ("t", "call", {"A": 1}),
        ("t", "call", {"a": True}),
        ("t", "call", {"a": [1]}),
    ],
)
def test_hash_binds_tool_action_and_every_arg(tool: str, action: str, payload: Any) -> None:
    base = canonical.payload_sha256("t", "call", {"a": 1})
    assert canonical.payload_sha256(tool, action, payload) != base


@pytest.mark.parametrize(
    "payload",
    [
        {"a": math.nan},
        {"a": math.inf},
        {"a": "x\x00y"},
        {1: "x"},
        {"a\x00": 1},
        {"a": {1, 2}},
        {"a": b"bytes"},
        {"a": object()},
        {"a": "x" * 4000},
        "not a mapping",
    ],
)
def test_non_json_or_oversize_payload_is_refused(payload: Any) -> None:
    with pytest.raises(canonical.PayloadError):
        canonical.payload_sha256("t", "call", payload)


def test_too_deep_payload_is_refused() -> None:
    deep: Any = 1
    for _ in range(canonical.MAX_DEPTH + 2):
        deep = {"x": deep}
    with pytest.raises(canonical.PayloadError):
        canonical.plain_payload(deep)


@pytest.mark.parametrize(("tool", "action"), [("", "call"), ("t", ""), (None, "call")])
def test_empty_tool_or_action_is_refused(tool: Any, action: Any) -> None:
    with pytest.raises(canonical.PayloadError):
        canonical.canonical_bytes(tool, action, {})


def test_frozen_payload_hashes_like_the_plain_one() -> None:
    plain = {"a": [1, {"b": "c"}], "d": None}
    frozen = atypes._freeze(plain)
    assert isinstance(frozen, MappingProxyType)
    assert canonical.payload_sha256("t", "call", frozen) == canonical.payload_sha256(
        "t", "call", plain
    )
    assert thaw(frozen) == plain


# ---------------------------------------------------------------------- token


def mac(**over: Any) -> bytes:
    kw: dict[str, Any] = {
        "verb": token.Verb.APPROVE,
        "action_id": AID,
        "payload_sha256_hex": SHA,
        "nonce": NONCE,
        "expiry_epoch_s": EXP,
        "user_id": USER,
    }
    key = over.pop("key", KEY)
    kw.update(over)
    return token.compute_mac(key, **kw)


def test_token_round_trip_fits_telegram_callback_data() -> None:
    for verb in token.Verb:
        data = token.encode(verb, AID, mac(verb=verb))
        assert len(data.encode()) == 46 <= token.MAX_CALLBACK_BYTES
        parsed = token.parse(data)
        assert (parsed.verb, parsed.action_id, parsed.mac) == (verb, AID, mac(verb=verb))


@pytest.mark.parametrize(
    "change",
    [
        {"verb": token.Verb.REJECT},
        {"action_id": uuid.UUID(int=8)},
        {"payload_sha256_hex": "cd" * 32},
        {"nonce": b"m" * 16},
        {"expiry_epoch_s": EXP + 1},
        {"user_id": USER + 1},
        {"key": b"q" * 32},
    ],
    ids=["verb", "action_id", "payload_hash", "nonce", "expiry", "user", "key"],
)
def test_mac_binds_every_field(change: dict[str, Any]) -> None:
    """Mutation proof: dropping any field from the MAC input makes one row pass."""
    assert not token.mac_matches(mac(), mac(**change))


@pytest.mark.parametrize(
    ("over", "what"),
    [
        ({"key": b"short"}, "key"),
        ({"nonce": b"n" * 8}, "nonce"),
        ({"payload_sha256_hex": "zz"}, "hash"),
        ({"payload_sha256_hex": "ab" * 16}, "hash"),
        ({"expiry_epoch_s": -1}, "expiry"),
        ({"user_id": 1 << 64}, "user"),
        ({"user_id": True}, "user"),
    ],
)
def test_compute_mac_refuses_bad_inputs(over: dict[str, Any], what: str) -> None:
    with pytest.raises(token.TokenError):
        mac(**over)


GOOD = token.encode(token.Verb.APPROVE, AID, mac())


@pytest.mark.parametrize(
    "data",
    [
        None,
        42,
        "",
        "ap:",
        GOOD[:-1],
        GOOD + "A",
        GOOD + "=",
        "xx" + GOOD[2:],
        "AP" + GOOD[2:],
        "ap;" + GOOD[3:],
        GOOD[:10] + "+" + GOOD[11:],
        GOOD[:10] + "/" + GOOD[11:],
        GOOD[:10] + "\u0410" + GOOD[11:],  # Cyrillic A
        GOOD[:10] + "\uff21" + GOOD[11:],  # fullwidth A
        GOOD[:10] + " " + GOOD[11:],
    ],
)
def test_parse_is_strict(data: Any) -> None:
    with pytest.raises(token.TokenError):
        token.parse(data)


def test_non_canonical_base64_is_refused() -> None:
    raw = AID.bytes + mac()
    import base64

    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"  # pragma: allowlist secret
    idx = alphabet.index(body[-1])
    tweaked = body[:-1] + alphabet[idx ^ 1]  # flips an unused low bit
    with pytest.raises(token.TokenError):
        token.parse("ap:" + tweaked)


def test_encode_refuses_wrong_mac_length() -> None:
    with pytest.raises(token.TokenError):
        token.encode(token.Verb.APPROVE, AID, b"x" * 15)


def test_mac_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation proof: replacing compare_digest with `==` fails this test."""
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(token.hmac, "compare_digest", spy)
    assert token.mac_matches(b"a" * 16, b"a" * 16)
    assert not token.mac_matches(b"a" * 16, b"b" * 16)
    assert len(calls) == 2


# ----------------------------------------------------------------------- totp

RFC_SECRET = b"12345678901234567890"
# RFC 6238 appendix B (SHA1). 6-digit codes are the last 6 of the 8-digit ones.
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("t", "code8"), RFC_VECTORS)
def test_totp_matches_rfc6238_vectors(t: int, code8: str) -> None:
    assert totp.code_at(RFC_SECRET, totp.step_of(t)) == code8[-6:]
    assert totp.verify(RFC_SECRET, code8[-6:], epoch_s=t, after_step=None) == totp.step_of(t)


@pytest.mark.parametrize(
    ("offset", "ok"), [(-60, False), (-30, True), (0, True), (30, True), (60, False)]
)
def test_totp_window_is_plus_minus_one_step(offset: int, ok: bool) -> None:
    t = 1_700_000_015.0
    code = totp.code_at(RFC_SECRET, totp.step_of(t + offset))
    assert (totp.verify(RFC_SECRET, code, epoch_s=t, after_step=None) is not None) is ok


def test_totp_code_cannot_be_replayed() -> None:
    t = 1_700_000_015.0
    step = totp.step_of(t)
    code = totp.code_at(RFC_SECRET, step)
    assert totp.verify(RFC_SECRET, code, epoch_s=t, after_step=step - 1) == step
    assert totp.verify(RFC_SECRET, code, epoch_s=t, after_step=step) is None
    assert totp.verify(RFC_SECRET, code, epoch_s=t, after_step=step + 5) is None


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        "12345a",
        " 12345",
        "\u0661\u0662\u0663\u0664\u0665\u0666",
        None,
        123456,
    ],
)
def test_totp_rejects_malformed_codes(code: Any) -> None:
    assert totp.verify(RFC_SECRET, code, epoch_s=1_700_000_000, after_step=None) is None


def test_totp_compare_is_constant_time_and_scans_whole_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(totp.hmac, "compare_digest", spy)
    t = 1_700_000_015.0
    totp.verify(
        RFC_SECRET, totp.code_at(RFC_SECRET, totp.step_of(t) - 1), epoch_s=t, after_step=None
    )
    assert len(calls) == 2 * totp.WINDOW + 1


@pytest.mark.parametrize("bad", ["not base32!", "AAAA", "", "MFRGGZDF"])
def test_totp_secret_must_be_base32_and_long_enough(bad: str) -> None:
    with pytest.raises(totp.TotpSecretError):
        totp.decode_secret(bad)


def test_totp_secret_accepts_spaces_and_lowercase() -> None:
    assert len(totp.decode_secret("jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp")) == 20


# ----------------------------------------------------------------------- card

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


def render(**over: Any) -> str:
    kw: dict[str, Any] = {
        "tool": "tasks_delete",
        "action": "call",
        "payload": {"task_id": "42"},
        "expires_at": NOW + timedelta(minutes=10),
        "now": NOW,
        "offset_minutes": 330,
        "step_up": True,
        "rule_id": "approval.destructive",
    }
    kw.update(over)
    return card.render_card(**kw)


def test_card_shows_tool_args_absolute_expiry_and_step_up() -> None:
    text = render()
    assert "Tool: tasks_delete" in text and '"task_id": "42"' in text
    assert "28 Oct 2026, 13:40:00 IST" in text and "in about 10 min" in text
    assert "authenticator code" in text and "approval.destructive" in text


def test_card_without_step_up_and_other_offset() -> None:
    text = render(step_up=False, offset_minutes=-90)
    assert "Step-up: not required." in text and "UTC-01:30" in text


@pytest.mark.parametrize(
    "hidden",
    ["\u202e", "\u200b", "\u2066", "\ufeff", "\u00a0", "\u2028", "\ue000", "\x7f", "\U000e0041"],
)
def test_card_reveals_invisible_and_bidi_characters(hidden: str) -> None:
    text = render(payload={"title": f"pay{hidden}rent"})
    assert hidden not in text
    assert "\\u" in text or "\\U" in text


def test_reveal_keeps_ordinary_text() -> None:
    assert card.reveal("Dinner with Asha \u0905\u0936\u093e at 7\n") == (
        "Dinner with Asha \u0905\u0936\u093e at 7\n"
    )


def test_step_up_prompt_masks_digits() -> None:
    text = card.render_step_up_prompt(
        tool="tasks_delete", entered=2, expires_at=NOW, offset_minutes=330
    )
    assert "**____" in text and "13:30:00 IST" in text


# ----------------------------------------------------------- ConsumedApproval


def sealed(**over: Any) -> ConsumedApproval:
    kw: dict[str, Any] = {
        "action_id": AID,
        "tool": "tasks_update",
        "action": "call",
        "payload": {"a": [1]},
        "payload_sha256": SHA,
        "step_up_verified": False,
        "source_event_id": None,
    }
    kw.update(over)
    return seal_consumed(**kw)


@pytest.mark.parametrize("seal", [None, object(), "seal", True])
def test_consumed_approval_cannot_be_forged(seal: Any) -> None:
    with pytest.raises(TypeError):
        ConsumedApproval(AID, "t", "call", {}, SHA, True, None, seal)


def test_consumed_approval_is_immutable_and_uncopyable() -> None:
    c = sealed()
    with pytest.raises(AttributeError):
        c.step_up_verified = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del c.tool
    with pytest.raises(TypeError):
        dataclasses.replace(c, step_up_verified=True)  # type: ignore[type-var]
    for fn in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            fn(c)
    with pytest.raises(TypeError):
        c.payload["a"] = 2  # type: ignore[index]
    assert "tasks_update" in repr(c) and "payload" not in repr(c)


def test_consumed_approval_step_up_flag_must_be_bool() -> None:
    with pytest.raises(TypeError):
        sealed(step_up_verified=1)
