# COST_MODEL: monthly model-API cost projection (P5.5)

Date: 2026-11-03. Owner: P5.5. Decision record: ADR-0025 (Proposed).
To reproduce every number below, run `uv run python scripts/cost_projection.py` (stdlib only;
`--help` lists the options). The workload figures are **assumptions** until the bake-off
(P5.4) measures real `llm_audit` rows. They are not measurements.

## 1. Prices (verified 2026-11-03; INR at 95/USD)

| Profile | USD/Mtok in / cached in / out | INR/Mtok in / cached / out | Source (checked 2026-11-03) |
|---|---|---|---|
| placeholder `*` (today's default) | n/a | 250 / 250 / 1000 | `src/llm/settings.py` |
| OpenAI gpt-5-nano | 0.05 / 0.005 / 0.40 | 4.75 / 0.48 / 38 | [Glamdring, 2026-09-28 check of OpenAI pricing](https://www.glamdringresearch.com/post/gpt-5-api-pricing), [costperprompt](https://costperprompt.com/models/openai-gpt-5-nano) |
| Gemini 2.5 Flash-Lite (paid) | 0.10 / 0.01 / 0.40 | 9.5 / 0.95 / 38 | [Google AI pricing page](https://aistudio.google.com/docs/pricing) |
| Gemini 3.1 Flash-Lite (paid) | 0.25 / 0.025 / 1.50 | 23.75 / 2.4 / 142.5 | same |
| DeepSeek V4.1 Flash (peak; off-peak is half) | 0.30 / 0.006 / 1.20 | 28.5 / 0.57 / 114 | [apidog](https://apidog.com/blog/deepseek-v4-1-flash-pricing/), [costgoat Nov 2026](https://costgoat.com/pricing/deepseek-api) |
| Free tier (any) | 0 | 0.0001 (0 is rejected by settings) | quota-bound, see section 5 |

Exchange rate: 1 USD was about 94.6 to 95.1 INR on 2026-11-02 ([Trading Economics](https://tradingeconomics.com/india/currency), [OFX](https://www.ofx.com/en-au/exchange-rates/usd-to-inr/)).

Caveats:
- The Gemini page says Gemini 2.5 Flash requests route to and bill as 3.5 Flash-Lite
  from 2026-12-01. Treat the 2.5 generation as short-lived.
- The Gemini free tier is marked "used to improve our products". See section 6.
- Free-tier RPM/RPD are not published on the rate-limits page. They are shown per
  project in AI Studio ([rate limits](https://aistudio.google.com/docs/rate-limits)), and
  third-party figures for Flash-Lite range from 5 to 30 RPM and 1,000 to 1,500 RPD.
  Daily quotas reset at midnight Pacific.

## 2. Token model, derived from the call sites

Every user message (one Telegram text) causes the following calls.

| Call | Where | Calls per message | Input tokens per call (current) | Output tokens |
|---|---|---|---|---|
| Chat, first iteration | `src/orchestrator/loop.py` `run_turn` | 1 | card 1,000 (H1 card assumed ~4K chars; placeholder is 453) + 12 tools x 90 = 1,080 + canaries 60 + memory 5 x (75 + 60 wrapper) = 675 + history 20 x 60 = 1,200 + user 40, about **4,055** | reply about 150 (cap 800) |
| Chat, tool iterations | same, `max_iterations` 5 | 0.30 x 1.2 = 0.36 | previous prompt + call 40 + tool result 300 | about 115 |
| Quarantine | `src/lanes/quarantine.py`, budget `APP_QUARANTINE_CALLS_PER_TURN`=10 | 0.15 x min(3, budget) = 0.45 | system + schema 350 + item about 400 (cap 12,000 chars, about 3,000 tokens) | about 150 (cap 1000; guard cap 800) |
| memory_extract (combined) | `src/scheduler/memory_extract.py` with `CombinedExtractor` | 0.50 (passes MIN_CHARS + ack filter) x 1.05 (schema repair) | prompt 205 + JSON schema 2,754 chars, about 690 + framing + body 40, about **990** | about 120 (cap 300) |
| Commitments | reuse the extraction output (P3.9) | 0 | - | - |
| Reminders and proactive jobs | `src/scheduler/reminders.py` | 0 (no model call by default) | - | - |
| Embeddings | local Qwen3 (ADR-0013) | 0 INR (API embedder optional) | - | - |

That is about 2.3 model requests per user message, or about 230 requests/day at 100
msgs/day. Measured from code: the combined schema is 2,754 chars, its prompt 718 chars,
the quarantine prompt 433 chars, the data-block header 183 chars.

## 3. Monthly projection (INR/month, 30 days; `cost_projection.py` default run)

"current" is today's defaults. "cheap" is the profile in section 4.

| config | model | 50/day | 100/day | 200/day |
|---|---|---:|---:|---:|
| current | placeholder `*` | 2,918 | 5,837 | 11,673 |
| current | gpt-5-nano | 65 | 129 | 258 |
| current | Gemini 2.5 Flash-Lite | 111 | 222 | 444 |
| current | DeepSeek V4.1 Flash (peak) | 333 | 665 | 1,331 |
| current | Gemini 3.1 Flash-Lite | 300 | 600 | 1,201 |
| cheap | placeholder `*` | 2,038 | 4,076 | 8,152 |
| cheap | gpt-5-nano | **31** | **63** | **126** |
| cheap | Gemini 2.5 Flash-Lite | 47 | 95 | 189 |
| cheap | DeepSeek V4.1 Flash (peak) | 134 | 268 | 536 |
| cheap | Gemini 3.1 Flash-Lite | 138 | 276 | 551 |
| either | free tier | ~0 | ~0 | ~0 (quota-bound) |

The current/placeholder row (5,837 at 100/day) is consistent with the W3 figure of
4,400 to 5,550. The difference is the history and tool sizes assumed here.

Split at 100/day, current, placeholder: chat 82%, extraction 10%, quarantine 8%. With
the cheap config on gpt-5-nano: chat 73%, extraction 14%, quarantine 13%.

**Worst case per turn** (every cap hit: 5 iterations, 10 quarantine calls at 12K chars,
full memory and history): 39 INR at the placeholder price, 0.97 INR on gpt-5-nano, and
4.4 INR on DeepSeek peak. With the new default cap of 50 INR/day (bounded at 100), a
month can never exceed 1,500 INR (or 3,000 at the 100 cap), whatever the workload.

## 4. Levers (100 msgs/day, current config, one lever at a time)

| Lever | Setting | gpt-5-nano | Gemini 2.5 Flash-Lite | placeholder |
|---|---|---:|---:|---:|
| base | - | 129 | 222 | 5,837 |
| Prompt caching of the static prefix (tools + card) | automatic; tools now sorted (ADR-0025 item 10) | **-31%** | **-36%** | 0% (no cache price) |
| History 20 -> 8 messages | `APP_HISTORY_MESSAGES=8` | -11% | -13% | -13% |
| Tools exposed 12 -> 8 | `LoopConfig.max_tools` (selector) | -5% | -6% | -6% |
| Extraction `all` -> `cue` | `APP_EXTRACT_MODE=cue`, `APP_EXTRACT_MIN_CHARS=60` | -5% | -4% | -4% |
| Memory top-k 5 -> 3, item cap 1200 -> 400 | `APP_MEMORY_TOP_K=3`, `APP_MEMORY_ITEM_CHARS=400` | -4% | -5% | -5% |
| Quarantine budget 10 -> 4 | `APP_QUARANTINE_CALLS_PER_TURN=4` | -3% | -3% | -3% |
| Output cap 800 -> 400 | `LLM_MAX_OUTPUT_TOKENS_TRUSTED=400` | 0% average (bounds the worst case and the pre-call reservation) | 0% | 0% |
| A "thinking" model (+300 reasoning tokens/call) | `LLM_REASONING_EFFORT=minimal` or `none` avoids it | **+31%** | +18% | +18% |
| Cheap model for quarantine + extraction | `LLM_MODEL_CHEAP` (same host) | -10 INR when chat is on a pricier model | | |

What matters most, in order:
1. **Model tier.** Moving off the placeholder to a real nano or flash-lite price is a 20 to
   45x cut.
2. **Thinking tokens off.** These bill as output, at 4 to 8x the input price.
3. **Prompt caching.** It needs a stable prefix: tools (sorted) and then the card first in
   the system message.
4. **History length**, which dominates the variable part of the prompt.

Everything else is under 6% each. Output caps matter for the worst case and for the
pre-call reservation (`estimate_inr` counts the full `max_output_tokens`), not for the
average.

**Cheap profile (env):**
```
LLM_DAILY_SPEND_CAP_INR=50            # default; <= 100 enforced
LLM_REASONING_EFFORT=minimal          # only if the model supports it
LLM_MAX_OUTPUT_TOKENS_TRUSTED=400
LLM_MAX_OUTPUT_TOKENS_QUARANTINE=400
LLM_MAX_OUTPUT_TOKENS_SYSTEM=300
APP_HISTORY_MESSAGES=8
APP_MEMORY_TOP_K=3
APP_MEMORY_ITEM_CHARS=400
APP_QUARANTINE_CALLS_PER_TURN=4
APP_EXTRACT_MODE=cue
APP_EXTRACT_MIN_CHARS=60
APP_EXTRACT_MAX_SPEND_SHARE=0.3
```
`max_tools` 8 is a `LoopConfig` field with no env setting yet (proposed follow-up).

**Proposed, not built (follow-ups):**
- Move the recalled-memory DATA blocks out of the system message into a message after the
  history. Each block's random nonce currently ends the cacheable prefix right after the
  card, so history is never cached across turns. The estimated further saving is about
  10 to 15%. This touches INV-6 framing and needs review (P3.0 owner).
- An env setting for `max_tools`.
- Batching extraction over a time window (one call for N messages). The ~950-token schema
  prompt is the fixed part, so batching 3 messages would save about 60% of extraction cost
  (about 8% of the total). It needs a provenance design, because one call would yield items
  for several `event_id`s (R7). Deferred.

## 5. Free-tier operation

The request limiter (ADR-0025, `src/llm/ratelimit.py`) keeps a free key from being burned:

```
LLM_PRICE_IN_PER_MTOK_INR=0.0001
LLM_PRICE_OUT_PER_MTOK_INR=0.0001
LLM_RPM_LIMIT=10                 # set below the key's AI Studio RPM
LLM_DAILY_REQUEST_LIMIT=900      # below the key's RPD; ~2.3 requests per message
LLM_BACKGROUND_SHARE=0.3         # extraction gets 3 RPM / 270 a day, chat the rest
LLM_RATE_LIMIT_MAX_WAIT_S=30
```
At 100 msgs/day that is about 230 requests/day, which fits typical Flash-Lite free RPD.
A 429 cools everything down for the provider's Retry-After. Chat waits up to 30 s, then
replies "I'm getting a lot of requests right now. Give me a minute and ask again."
Extraction defers and retries later.

## 6. Privacy note (Brief section 8)
Google's pricing page marks free-tier content as used to improve Google products. A free
key is for synthetic or test data unless the human accepts that exposure explicitly (H3).
The paid tiers listed above say no.

## 7. Proposed `quality/thresholds.yaml` changes (not applied; ADR-0025)
- Add `cost.projected_monthly_inr_target: 300` (cheap profile, 100 msgs/day).
- Add `cost.default_daily_cap_inr: 50`.
- Keep `projected_monthly_inr_max: 1500` and `hard_daily_cap_inr: 100`. The settings now
  enforce the latter.
