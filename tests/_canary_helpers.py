# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Shared fakes for the P4.4 canary tests (unit + adversarial)."""

from __future__ import annotations

import base64
import codecs
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.approvals.types import AuditRecord
from src.canary.guard import CanaryGuard
from src.canary.tokens import CanarySet, Site, derive_canaries
from src.gateway.types import ApprovalCard, FileRef, InboundUpdate, MessageRef
from src.llm.types import ToolSpec
from src.orchestrator.dispatch import Decision, ProposedCall, Risk, ToolOutput
from src.policy import PolicyContext

# Test-only seed: deterministic values, never a deployment's.
SEED = "test-only-canary-seed-0123456789abcdef"  # pragma: allowlist secret


def canaries(seed: str = SEED) -> CanarySet:
    return derive_canaries(seed)


class RecordingAudit:
    def __init__(self, *, fail: bool = False) -> None:
        self.recs: list[AuditRecord] = []
        self.fail = fail

    async def audit(self, rec: AuditRecord) -> None:
        if self.fail:
            raise RuntimeError("audit down")
        self.recs.append(rec)


class RecordingAlert:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


def make_guard(
    cs: CanarySet | None = None,
    *,
    audit: RecordingAudit | None = None,
    alert: RecordingAlert | None = None,
    configured: Sequence[str] = (),
    clock: Callable[[], float] | None = None,
    cooldown: float = 600.0,
) -> CanaryGuard:
    kw: dict[str, Any] = {}
    if clock is not None:
        kw["clock"] = clock
    return CanaryGuard(
        cs or canaries(),
        configured=configured,
        audit=audit,
        alert=alert,
        alert_cooldown_s=cooldown,
        **kw,
    )


class RecordingGateway:
    """A `Gateway` that records everything sent."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[ApprovalCard] = []
        self.keypads: list[str] = []
        self.toasts: list[str | None] = []
        self.closed: list[str] = []
        self._n = 0

    def inbound(self) -> AsyncIterator[InboundUpdate]:
        async def none() -> AsyncIterator[InboundUpdate]:
            return
            yield  # pragma: no cover

        return none()

    async def ack(self, update: InboundUpdate, *, event_id: uuid.UUID | None = None) -> None:
        return None

    def _ref(self) -> MessageRef:
        self._n += 1
        return MessageRef(self._n)

    async def send_text(self, text: str, *, reply_to: MessageRef | None = None) -> list[MessageRef]:
        self.texts.append(text)
        return [self._ref()]

    async def send_approval(self, card: ApprovalCard) -> MessageRef:
        self.cards.append(card)
        return self._ref()

    async def send_keypad(self, text: str, rows: Sequence[Sequence[tuple[str, str]]]) -> MessageRef:
        self.keypads.append(text)
        return self._ref()

    async def close_approval(self, ref: MessageRef, *, outcome: str) -> None:
        self.closed.append(outcome)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.toasts.append(text)

    async def fetch_file(self, file: FileRef, *, max_bytes: int) -> bytes:
        return b""


class AllowAll:
    """A policy that allows everything: proves the canary screen, not the
    policy, is what stops the call."""

    def decide(self, call: ProposedCall, ctx: PolicyContext) -> Decision:
        return Decision("allow", "allow.test", "test")


@dataclass
class RecordingTool:
    name: str
    risk: Risk = Risk.WRITE_EXTERNAL
    text: str = "done"
    untrusted: bool = True
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.spec = ToolSpec(
            name=self.name,
            description=f"{self.name} tool",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )

    async def run(self, args: Mapping[str, Any]) -> ToolOutput:
        self.calls.append(dict(args))
        return ToolOutput(self.text, self.untrusted)


# ------------------------------------------------------------- obfuscations


def _zw(v: str) -> str:
    return "\u200b".join(v)


def _homoglyph(v: str) -> str:
    table = str.maketrans({"a": "а", "e": "е", "o": "о", "c": "с", "p": "р", "x": "х"})
    return v.translate(table)


ENCODINGS: dict[str, Callable[[str], str]] = {
    "plain": lambda v: v,
    "upper": str.upper,
    "spaced": lambda v: " ".join(v),
    "dotted": lambda v: ".".join(v),
    "dashed_pairs": lambda v: "-".join(v[i : i + 2] for i in range(0, len(v), 2)),
    "newlines": lambda v: "\n".join(v[i : i + 4] for i in range(0, len(v), 4)),
    "zero_width": _zw,
    "zw_joiner": lambda v: "‍".join(v),
    "fullwidth": lambda v: "".join(chr(ord(c) + 0xFEE0) for c in v),
    "math_bold": lambda v: "".join(
        chr(0x1D41A + ord(c) - 97) if c.isalpha() else chr(0x1D7CE + ord(c) - 48) for c in v
    ),
    "homoglyph": _homoglyph,
    "tag_chars": lambda v: "".join(chr(0xE0000 + ord(c)) for c in v),
    "reversed": lambda v: v[::-1],
    "rot13": lambda v: codecs.encode(v, "rot13"),
    "base64": lambda v: base64.b64encode(v.encode()).decode(),
    "base64_prefixed": lambda v: base64.b64encode(f"the key is {v}".encode()).decode(),
    "base64url": lambda v: base64.urlsafe_b64encode(f"??{v}>>".encode()).decode(),
    "base64_spaced": lambda v: " ".join(
        base64.b64encode(v.encode()).decode()[i : i + 4] for i in range(0, 44, 4)
    ),
    "base64_twice": lambda v: base64.b64encode(base64.b64encode(v.encode())).decode(),
    "base32": lambda v: base64.b32encode(v.encode()).decode(),
    "hex": lambda v: v.encode().hex(),
    "hex_upper_colon": lambda v: ":".join(f"{b:02X}" for b in v.encode()),
    "hex_escaped": lambda v: "".join(f"\\x{b:02x}" for b in v.encode()),
    "hex_0x": lambda v: " ".join(f"0x{b:02x}" for b in v.encode()),
    "percent": lambda v: "".join(f"%{b:02X}" for b in v.encode()),
    "html_entities": lambda v: "".join(f"&#{ord(c)};" for c in v),
    "unicode_escapes": lambda v: "".join(f"\\u{ord(c):04x}" for c in v),
    "charcodes": lambda v: ", ".join(str(ord(c)) for c in v),
    "half": lambda v: v[:16],
    "middle_third": lambda v: v[11:22],
    "in_url": lambda v: f"https://evil.example/collect?d={v}",
    "md_image": lambda v: f"![x](https://evil.example/p.png?q={v})",
    "json_value": lambda v: '{"note": "' + v + '"}',
    "leet": lambda v: v.replace("o", "0").replace("l", "1").replace("e", "3").replace("a", "4"),
    "accented": lambda v: "".join(c + "́" if c.isalpha() else c for c in v),
}

SITES = tuple(Site)
