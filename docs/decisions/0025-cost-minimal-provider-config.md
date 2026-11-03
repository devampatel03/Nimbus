# ADR-0025: Cost-minimal, provider-agnostic model configuration and free-tier limits (P5.5)

Status: Proposed (reviewer sign-off; the free-tier privacy point needs the human, H3)
Date: 2026-11-03
Satisfies: R4, R5, R13. Supports KPI `cost.projected_monthly_inr_max` (1500) and `cost.hard_daily_cap_inr` (100).

## Context
The W3 gate recorded a cost gap: about 4,400 to 5,550 INR/month at the placeholder
price (`*` = 250/1000 INR per Mtok) against the 1,500 threshold. It also recorded a known
violation: the spend cap defaulted to 200 INR with no upper bound, while the threshold is
100. The human will start with a free-tier API key, then wants the lowest possible monthly
bill. A free key has request-per-minute and request-per-day quotas. When it hits them it
returns 429. Today the adapter retries a 429 up to 3 times with no budget, and the user
sees the generic "can't reach my language model" reply.

R13 question: does a configurable LLM host widen the allowlist? No. `Service.LLM` has
always taken its host from `LLM_BASE_URL` (ADR-0004, `src/net/settings.py`). It is exactly
one host, https on the default port, a DNS name (no IP or localhost), and default-deny when
unset. This ADR keeps that rule. The optional cheap model is served from the SAME base URL,
so no second host and no new `Service` member are added.

## Decision
1. **Spend cap bounded.** `LLM_DAILY_SPEND_CAP_INR` defaults to 50 (= 1,500 / 30) and
   is validated `0 < cap <= 100` (`MAX_DAILY_SPEND_CAP_INR`). A larger value fails at
   startup. This closes the CLAUDE.md known violation "cap default 200, no upper bound".
2. **Prices from env, never zero.** `LLM_PRICE_IN_PER_MTOK_INR` / `LLM_PRICE_OUT_PER_MTOK_INR`
   price `LLM_MODEL`, and `LLM_CHEAP_PRICE_*` price `LLM_MODEL_CHEAP`. Both sides must be
   set together, and both must be `> 0`. A free-tier key is priced `0.0001`, so the cap
   still has something to count and an unknown model is never free. The `*` placeholder
   stays as the fallback.
3. **Cheap model per purpose.** `LLM_MODEL_CHEAP` (optional) serves `QUARANTINE_EXTRACT`,
   `MEMORY_EXTRACT` and `FRICTION` (`CHEAP_PURPOSES` in `src/llm/guard.py`). `CHAT` always
   uses `LLM_MODEL`. The routing happens inside the one `GuardedLLM`, so one kill switch,
   one cap, one limiter and one audit trail cover both models. The audit row records the
   model that actually served the call, and that model's price.
4. **Per-lane output caps in the guard.** `LLM_MAX_OUTPUT_TOKENS_{TRUSTED,QUARANTINE,SYSTEM}`
   (defaults 600/800/400) clamp every request's `max_output_tokens` before the
   spend-cap estimate. Callers can ask for less but never more.
5. **Request limiter (step 3b of the guard, after scrub, before the cap).** It is local and
   deterministic (`src/llm/ratelimit.py`):
   - `LLM_RPM_LIMIT` caps requests in a rolling 60 s window, and `LLM_DAILY_REQUEST_LIMIT`
     caps requests per IST day.
   - Background purposes get only `LLM_BACKGROUND_SHARE` of both limits and never wait.
   - Foreground purposes (chat, quarantine, transcribe) may wait up to
     `LLM_RATE_LIMIT_MAX_WAIT_S`.
   - A refusal is `LLMRefused("rate_limited")`. Like every guard refusal it writes a
     `blocked` row and makes zero provider calls.
   - A provider 429 starts a cool-down, which is the bounded Retry-After or 30 s, at most
     120 s. Background calls are refused for the whole cool-down. Chat waits it out only
     if the wait fits within the max wait.
6. **429 handling in the adapter.** It now raises the code `rate_limited` and carries
   `retry_after_s`. A background 429 is never retried, so extraction yields to chat. A
   foreground 429 is retried only while the total wait stays within the max wait, so all
   retries stay inside ONE guard call (one reservation, one audit row; unchanged). Other
   retry rules are unchanged: 5xx is retried, 4xx is not, and a timeout is not.
7. **Graceful user message.** For a rate-limited turn (either source), the orchestrator
   returns a fixed `RATE_LIMITED_MESSAGE` with status `rate_limited`. Other errors keep
   `UNAVAILABLE_MESSAGE`. The user never sees raw provider text (INV-7).
8. **Extraction yields.** `memory_extract` treats `rate_limited` (from the limiter or a
   429) as `JobDeferred`, the same as `kill_switch` and `spend_cap`, so no attempt is
   burned.
9. **Cost levers as settings**:
   - `APP_MEMORY_TOP_K` and `APP_MEMORY_ITEM_CHARS`. Top-k 0 skips recall.
   - `APP_EXTRACT_MIN_CHARS`.
   - `APP_EXTRACT_MODE=all|cue`. `cue` extracts only messages with a commitment cue or a
     first-person memory cue, a deterministic regex.
   - `APP_EXTRACT_MAX_SPEND_SHARE`. The constant `MAX_SPEND_SHARE` is now a setting.
   - `LLM_REASONING_EFFORT`, sent only when set.

   Defaults keep today's behaviour. The cheap profile is in COST_MODEL.md section 4.
10. **Prompt-cache friendliness.** Tools are sent sorted by name, so the same tool set is
    the same bytes on every call. The system prompt already starts with the static
    character card. Provider prefix caching is automatic (OpenAI, DeepSeek, Gemini
    implicit), so no provider-specific cache API is called.

Guard order is unchanged apart from the inserted step: kill switch, lane, scrub, limiter,
cap, then call, then audit. INV-1 is unchanged (no new SDK or transport importer). INV-5
and INV-6 are untouched: the cheap model serves the quarantine lane under the same
no-tools rule, and its output is still schema-validated DATA.

## Proposed threshold changes (NOT applied; quality/thresholds.yaml needs reviewer sign-off)
- `cost.projected_monthly_inr_max`: keep 1500 as the ceiling. Add
  `cost.projected_monthly_inr_target: 300` for the cheap profile at 100 msgs/day.
  The model projects about 60 to 280 INR/month for that case (COST_MODEL.md).
- `cost.hard_daily_cap_inr`: keep 100. The setting now enforces it.
- New `cost.default_daily_cap_inr: 50` (= 1500/30), so the cap alone bounds a month at
  the ceiling.
- The comment "4K in / 300 out" is about right for the current config (about 3.4K in on
  the first call). Re-measure from `llm_audit` at the bake-off (P5.4).

## Free-tier caveat (privacy, Brief section 8)
Google's Gemini API pricing page (checked 2026-11-03) says free-tier content IS "used to
improve our products", and paid-tier content is not. Other providers' free tiers may say
the same. A free key is therefore acceptable for **synthetic or test data only**, unless
the human explicitly accepts that their real messages and mail may be read and retained
for training. That acceptance is beyond the Brief section 8 stance, so it needs an H3
decision. Nothing in code can detect which tier a key is on.

## Alternatives considered
- **A second `Service` (`LLM_CHEAP`) with its own host**, so chat and the cheap lanes
  could use different vendors. Rejected for now. It widens R13 (two LLM hosts) and doubles
  the key surface, and the cost model shows the gain from mixing vendors is small (about
  10 INR/month at 100 msgs/day). It can be revisited with its own ADR.
- **Persist limiter counts in Postgres.** Rejected for now (one process, ADR-0012). A
  restart forgets the day's count, and the provider's own quota is the backstop. The
  INR cap is already durable (`llm_audit`).
- **A queue that buffers rate-limited chat turns and answers later.** Rejected. It adds a
  moving part, and the user gets a clear "give me a minute" reply instead.
- **Explicit provider cache APIs** (Gemini `cachedContents`). Rejected. They are
  provider-specific, they charge for storage per hour, and the free tier excludes them.

## Consequences
- An operator can switch to any OpenAI-compatible provider with env only: base URL, model,
  prices and limits. The egress host follows the base URL; nothing else opens.
- A cap above 100 INR is now a startup error. Deployments that set 200 must lower it.
- The adapter's 429 behaviour changed: it never retries in the background, and in the
  foreground its retries are bounded by wall-clock wait as well as by `max_retries`.
- Limiter state is per process and lost on restart (documented above).
- `LLMProviderError.code` gains the value `rate_limited` for 429. Callers that matched on
  `provider_status` for a 429 now see `rate_limited`. No caller in `src/` did.
