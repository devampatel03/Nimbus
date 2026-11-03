# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Monthly model-cost projection for Nimbus Lite (P5.5, COST_MODEL.md).

Stdlib only. Reproduces every number in docs/pivot/reports/COST_MODEL.md:

    uv run python scripts/cost_projection.py                      # all tables
    uv run python scripts/cost_projection.py --profile gpt-5-nano --msgs 100
    uv run python scripts/cost_projection.py --price-in 9.5 --price-out 38 \
        --price-cached 0.95 --config cheap --msgs 50 100 200
    uv run python scripts/cost_projection.py --levers --profile gpt-5-nano

The token model is derived from the call sites (see COST_MODEL.md section 2):
chat turn (src/orchestrator/loop.py + context.py), quarantine calls
(src/lanes/quarantine.py), the combined memory_extract call
(src/memory/extractor.py via src/scheduler/memory_extract.py). Commitments
reuse the extraction call; proactive jobs (reminders) make no model call.
Prices are INR per million tokens. All workload numbers are ASSUMPTIONS until
a live bake-off (P5.4) replaces them with llm_audit measurements.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace

USD_INR = 95.0  # 2026-11-02 close ~94.6-95.1 (COST_MODEL.md section 1)
DAYS = 30
CHARS_PER_TOKEN = 4.0  # English; Indic or Hinglish text is closer to 2-3


@dataclass(frozen=True)
class Price:
    """INR per Mtok. `cached_in` is the provider's cache-hit input price."""

    name: str
    inp: float
    out: float
    cached_in: float


def usd(name: str, inp: float, out: float, cached: float) -> Price:
    return Price(name, inp * USD_INR, out * USD_INR, cached * USD_INR)


# Verified 2026-11-03; see COST_MODEL.md section 1 for sources.
PROFILES: dict[str, Price] = {
    "placeholder": Price("placeholder (src/llm/settings.py '*')", 250.0, 1000.0, 250.0),
    "free-tier": Price("free tier (priced 0.0001, rule: 0 rejected)", 0.0001, 0.0001, 0.0001),
    "gpt-5-nano": usd("OpenAI gpt-5-nano", 0.05, 0.40, 0.005),
    "gemini-2.5-flash-lite": usd("Gemini 2.5 Flash-Lite (paid)", 0.10, 0.40, 0.01),
    "deepseek-flash-peak": usd("DeepSeek V4.1 Flash (peak)", 0.30, 1.20, 0.006),
    "gemini-3.1-flash-lite": usd("Gemini 3.1 Flash-Lite (paid)", 0.25, 1.50, 0.025),
}


@dataclass(frozen=True)
class Config:
    """One configuration of the levers. Token figures are per call."""

    # chat: static prefix
    card_tokens: int = 1000  # real card (H1) assumed ~4,000 chars; placeholder is 453
    tools_exposed: int = 12  # LoopConfig.max_tools
    tokens_per_tool: int = 90  # name + description + JSON schema
    canary_tokens: int = 60  # P4.4 system + memory plant lines
    # chat: per-turn variable part
    memory_top_k: int = 5  # APP_MEMORY_TOP_K
    memory_item_chars: int = 1200  # APP_MEMORY_ITEM_CHARS (cap)
    avg_memory_chars: int = 300  # typical recalled chunk before the cap
    data_block_overhead_tokens: int = 60  # nonce delimiters + DATA header (183 chars)
    history_messages: int = 20  # APP_HISTORY_MESSAGES
    tokens_per_history_msg: int = 60
    user_tokens: int = 40
    reply_tokens: int = 150  # final answer, bounded by LLM_MAX_OUTPUT_TOKENS_TRUSTED
    max_output_trusted: int = 800
    tool_turn_share: float = 0.30  # turns with at least one tool call
    extra_calls_per_tool_turn: float = 1.2
    tool_call_out_tokens: int = 40
    tool_result_tokens: int = 300
    reasoning_tokens_per_call: int = 0  # LLM_REASONING_EFFORT none/minimal => ~0
    # caching
    caching: bool = False  # provider applies its cache-hit price to the stable prefix
    cache_hit_rate: float = 0.8  # share of calls whose prefix is still warm
    # quarantine lane
    quarantine_turn_share: float = 0.15  # turns that read mail/calendar/drive/notion
    quarantine_calls_per_q_turn: float = 3.0  # bounded by the per-turn budget below
    quarantine_budget: int = 10  # APP_QUARANTINE_CALLS_PER_TURN
    quarantine_prompt_tokens: int = 350  # system prompt + schema instruction
    quarantine_data_tokens: int = 400  # typical item (cap 12,000 chars ~ 3,000 tokens)
    quarantine_out_tokens: int = 150  # cap LLM_MAX_OUTPUT_TOKENS_QUARANTINE
    quarantine_cheap: bool = False  # LLM_MODEL_CHEAP serves the quarantine lane
    # memory_extract (one combined call; commitments reuse it)
    extract_share: float = 0.50  # messages that pass MIN_CHARS + ack filter (mode=all)
    extract_prompt_tokens: int = 950  # system prompt 205 + JSON schema ~690 + framing
    extract_body_tokens: int = 40
    extract_out_tokens: int = 120  # cap 300 (COMBINED_MAX_OUTPUT_TOKENS)
    extract_repair_rate: float = 0.05  # schema repair retry = a second full call
    extract_cheap: bool = False  # LLM_MODEL_CHEAP serves extraction


CURRENT = Config()
CHEAP = Config(
    tools_exposed=8,
    memory_top_k=3,
    memory_item_chars=400,
    history_messages=8,
    max_output_trusted=400,
    caching=True,
    quarantine_budget=4,
    quarantine_calls_per_q_turn=2.0,
    quarantine_cheap=True,
    extract_share=0.30,  # APP_EXTRACT_MODE=cue, APP_EXTRACT_MIN_CHARS=60
    extract_cheap=True,
)
CONFIGS = {"current": CURRENT, "cheap": CHEAP}


@dataclass
class Breakdown:
    chat: float = 0.0
    quarantine: float = 0.0
    extract: float = 0.0

    @property
    def total(self) -> float:
        return self.chat + self.quarantine + self.extract


def _inr(tokens_in: float, tokens_cached: float, tokens_out: float, p: Price) -> float:
    return (tokens_in * p.inp + tokens_cached * p.cached_in + tokens_out * p.out) / 1e6


def per_turn(c: Config, main: Price, cheap: Price) -> Breakdown:
    """INR for one user message (all calls it causes)."""
    static = c.card_tokens + c.tools_exposed * c.tokens_per_tool
    mem_item = min(c.avg_memory_chars, c.memory_item_chars) / CHARS_PER_TOKEN
    memory = c.memory_top_k * (mem_item + c.data_block_overhead_tokens)
    variable = (
        c.canary_tokens + memory + c.history_messages * c.tokens_per_history_msg + c.user_tokens
    )
    reply = min(c.reply_tokens, c.max_output_trusted) + c.reasoning_tokens_per_call

    # First call: static prefix may be cached; the rest is fresh.
    hit = c.cache_hit_rate if c.caching else 0.0
    cost = _inr(static * (1 - hit) + variable, static * hit, reply, main)
    # Tool iterations resend the whole previous prompt (cacheable within the turn)
    # plus the call and its result.
    extra = c.tool_turn_share * c.extra_calls_per_tool_turn
    prev = static + variable
    grow = c.tool_call_out_tokens + c.tool_result_tokens
    if c.caching:
        cost += extra * _inr(grow, prev, c.tool_call_out_tokens + reply / 2, main)
    else:
        cost += extra * _inr(prev + grow, 0, c.tool_call_out_tokens + reply / 2, main)

    qp = cheap if c.quarantine_cheap else main
    q_calls = c.quarantine_turn_share * min(c.quarantine_calls_per_q_turn, c.quarantine_budget)
    q_in = c.quarantine_prompt_tokens + c.quarantine_data_tokens
    q_cost = q_calls * _inr(q_in, 0, c.quarantine_out_tokens, qp)

    ep = cheap if c.extract_cheap else main
    e_calls = c.extract_share * (1 + c.extract_repair_rate)
    e_in = c.extract_prompt_tokens + c.extract_body_tokens
    e_cost = e_calls * _inr(e_in, 0, c.extract_out_tokens, ep)
    return Breakdown(cost, q_cost, e_cost)


def monthly(c: Config, msgs_per_day: int, main: Price, cheap: Price | None = None) -> Breakdown:
    t = per_turn(c, main, cheap or main)
    k = msgs_per_day * DAYS
    return Breakdown(t.chat * k, t.quarantine * k, t.extract * k)


def worst_case_turn(c: Config, p: Price) -> float:
    """Every cap hit at once: the bound the daily spend cap must absorb."""
    static = c.card_tokens + c.tools_exposed * c.tokens_per_tool
    memory = c.memory_top_k * (c.memory_item_chars / CHARS_PER_TOKEN + c.data_block_overhead_tokens)
    hist = c.history_messages * 200
    first_in = static + c.canary_tokens + memory + hist + 2000
    chat = 5 * _inr(first_in + 4 * (c.tool_result_tokens * 4), 0, c.max_output_trusted, p)
    q = c.quarantine_budget * _inr(c.quarantine_prompt_tokens + 3000, 0, 800, p)
    return chat + q


LEVERS: list[tuple[str, dict[str, object]]] = [
    ("history 20 -> 8 messages", {"history_messages": 8}),
    ("memory top-k 5 -> 3, item cap 1200 -> 400", {"memory_top_k": 3, "memory_item_chars": 400}),
    ("tools exposed 12 -> 8", {"tools_exposed": 8}),
    ("prompt caching of static prefix", {"caching": True}),
    ("extraction mode all -> cue (share 0.5 -> 0.3)", {"extract_share": 0.30}),
    ("quarantine budget 10 -> 4", {"quarantine_budget": 4, "quarantine_calls_per_q_turn": 2.0}),
    ("max_output_tokens trusted 800 -> 400", {"max_output_trusted": 400}),
    ("reasoning tokens 0 -> 300/call (a 'thinking' model)", {"reasoning_tokens_per_call": 300}),
]


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 10 else f"{x:,.2f}"


def table(configs: list[str], profiles: list[str], msgs: list[int], cheap: str | None) -> None:
    print(f"| config | model | {' | '.join(f'{m}/day' for m in msgs)} |")
    print("|---|---|" + "---:|" * len(msgs))
    for cname in configs:
        for pname in profiles:
            p = PROFILES[pname]
            cp = PROFILES[cheap] if cheap else p
            cells = [_fmt(monthly(CONFIGS[cname], m, p, cp).total) for m in msgs]
            label = p.name + (f" + cheap {cp.name}" if cheap and cp is not p else "")
            print(f"| {cname} | {label} | {' | '.join(cells)} |")


def breakdown(cname: str, pname: str, msgs: int, cheap: str | None = None) -> None:
    cp = PROFILES[cheap] if cheap else None
    b = monthly(CONFIGS[cname], msgs, PROFILES[pname], cp)
    extra = f" + cheap {cp.name}" if cp is not None else ""
    print(f"\n{cname} / {PROFILES[pname].name}{extra} / {msgs} msgs/day (INR/month)")
    for f in ("chat", "quarantine", "extract"):
        v = getattr(b, f)
        print(f"  {f:<11} {_fmt(v):>10}  ({100 * v / b.total:4.1f}%)")
    print(f"  {'total':<11} {_fmt(b.total):>10}")


def levers(pname: str, msgs: int) -> None:
    p = PROFILES[pname]
    base = monthly(CURRENT, msgs, p).total
    print(f"\nLevers vs 'current', {p.name}, {msgs} msgs/day: base {_fmt(base)} INR/month")
    print("| lever | INR/month | change |")
    print("|---|---:|---:|")
    for label, kw in LEVERS:
        v = monthly(replace(CURRENT, **kw), msgs, p).total  # type: ignore[arg-type]
        print(f"| {label} | {_fmt(v)} | {100 * (v - base) / base:+.0f}% |")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--msgs", type=int, nargs="+", default=[50, 100, 200])
    ap.add_argument("--profile", choices=sorted(PROFILES), action="append")
    ap.add_argument("--cheap-profile", choices=sorted(PROFILES), default=None)
    ap.add_argument("--config", choices=sorted(CONFIGS), action="append")
    ap.add_argument("--price-in", type=float, help="INR per Mtok input (custom profile)")
    ap.add_argument("--price-out", type=float, help="INR per Mtok output (custom profile)")
    ap.add_argument("--price-cached", type=float, help="INR per Mtok cached input")
    ap.add_argument("--levers", action="store_true")
    ap.add_argument("--worst-case", action="store_true")
    a = ap.parse_args(argv)

    if a.price_in is not None or a.price_out is not None:
        if a.price_in is None or a.price_out is None:
            ap.error("--price-in and --price-out go together")
        cached = a.price_cached if a.price_cached is not None else a.price_in
        PROFILES["custom"] = Price("custom", a.price_in, a.price_out, cached)
        a.profile = ["custom"]

    profiles = a.profile or list(PROFILES)
    configs = a.config or ["current", "cheap"]
    table(configs, profiles, a.msgs, a.cheap_profile)
    for pname in profiles:
        if a.levers:
            levers(pname, a.msgs[len(a.msgs) // 2])
    if a.worst_case:
        for cname in configs:
            for pname in profiles:
                w = worst_case_turn(CONFIGS[cname], PROFILES[pname])
                print(f"worst-case turn {cname}/{pname}: {_fmt(w)} INR")
    if not a.levers and not a.worst_case and len(profiles) == 1:
        for cname in configs:
            breakdown(cname, profiles[0], a.msgs[len(a.msgs) // 2], a.cheap_profile)


if __name__ == "__main__":
    main()
