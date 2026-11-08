# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""One-shot connectivity probes for `preflight --online`.

Both go through the normal guard (kill switch, scrub, spend cap, request limit,
audit row), so a probe is an ordinary, accounted call. Results carry fixed text,
codes and numbers only (INV-7): never a prompt, key, URL or provider body.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.llm.factory import build_api_embedder, build_guarded_llm
from src.llm.protocols import AuditWriter, FlagStore, SpendLedger
from src.llm.settings import LLMSettings
from src.llm.types import (
    ChatMessage,
    ChatRequest,
    Lane,
    LLMError,
    LLMProviderError,
    LLMRefused,
    Purpose,
)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    status: int | None = None
    code: str = ""


async def ping(
    settings: LLMSettings,
    *,
    flags: FlagStore,
    audit: AuditWriter,
    ledger: SpendLedger,
    embed_model: str | None = None,
) -> tuple[ProbeResult, ProbeResult | None]:
    """One tiny chat call; plus one embedding call when `embed_model` is given."""
    llm = build_guarded_llm(settings, flags=flags, audit=audit, ledger=ledger)
    try:
        res = await llm.chat(
            ChatRequest(
                purpose=Purpose.CHAT,
                lane=Lane.TRUSTED,
                messages=(ChatMessage(role="user", content="Reply with the single word: ok"),),
                max_output_tokens=32,
                temperature=0,
            )
        )
        chat = ProbeResult(
            True,
            f"model replied (tokens in={res.usage.tokens_in} out={res.usage.tokens_out}, "
            f"cost {res.cost_inr} INR, audit row written)",
        )
    except LLMProviderError as exc:
        chat = ProbeResult(
            False, f"provider error code={exc.code} status={exc.status}", exc.status, exc.code
        )
    except LLMRefused as exc:
        chat = ProbeResult(False, f"refused by the guard: {exc.reason}", code=exc.reason)
    except LLMError as exc:
        chat = ProbeResult(False, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - type name only
        chat = ProbeResult(False, f"failed ({type(exc).__name__})")
    if embed_model is None:
        return chat, None
    try:
        emb = build_api_embedder(llm, settings, model=embed_model)
        vec = await emb.aembed("preflight")
        return chat, ProbeResult(True, f"model {embed_model} returned {len(vec)} dims")
    except Exception as exc:  # noqa: BLE001 - type name only
        return chat, ProbeResult(False, f"failed ({type(exc).__name__})")
