# 0009 — Deterministic policy matrix and a 64-byte approval token

- Status: Accepted (P0.4, 2026-10-24). Supersedes MEMORY.md AD-22, AD-27 (`Approver`).

## Context
Brief §2.4 replaces the LLM approver with deterministic policy and user approval. Brief §2 "Approvals" specifies an HMAC over (payload, nonce, expiry), user-only, single-use, 10-minute expiry and TOTP step-up. Telegram `callback_data` is limited to 1–64 bytes ([Bot API](https://core.telegram.org/bots/api), verified 2026-10-24), so the payload cannot ride in the button. The existing `RiskClass` enum has `WRITE_REVERSIBLE`, while the Brief says "write-internal".

## Decision
**Risk classes:** READ, WRITE_INTERNAL (renamed from WRITE_REVERSIBLE), WRITE_EXTERNAL, DESTRUCTIVE.

`Policy.decide(call, ctx)` is pure. The first matching row wins:

| # | Condition | Verdict |
|---|---|---|
| 1 | Unknown tool/action, or args fail validation | deny |
| 2 | mode = KILLED | deny every non-READ, and READ too if the tool is external |
| 3 | mode = READ_ONLY (spend cap) | deny non-READ |
| 4 | Rate limit exceeded (per tool per hour, from the `ctx` counters) | deny |
| 5 | READ | allow |
| 6 | WRITE_INTERNAL, not tainted | allow |
| 7 | WRITE_INTERNAL, tainted | require_approval |
| 8 | WRITE_EXTERNAL | require_approval, always |
| 9 | Any external recipient not in the user-configured contact allowlist, or DESTRUCTIVE | + TOTP |

Only a human editing config can relax row 8 per action (`approval_exempt_when_untainted`). The model never can.

**Approval token:** `ap:` + base64url(`action_id` 12 random bytes ‖ HMAC-SHA256(K, action_id ‖ sha256(payload) ‖ expiry)[:16]`). That is 3 + 38 = 41 bytes, under the 64-byte limit.
- The payload, its hash, the expiry and the status live in `pending_action`.
- Verification checks the `from.id` allowlist, compares the MAC in constant time, checks expiry and the hash, then runs an atomic `pending → consumed` update.
- K comes from env (R12).
- TOTP is `pyotp`, ±1 step, and the secret is stored encrypted.

## Consequences
- 95% coverage on a small table-driven module is achievable (thresholds).
- The red team runs with a compromised FakeLLM. A pass means no effect happened without a consumed approval, regardless of what the model says.
- Every external write costs one tap. The UX trade-off is accepted. Row 8's exemption is a config knob, not code.

## Alternatives
- **Keep the LLM judge as an extra layer.** Rejected: INV-5. Its measured 0/30 was a fail-closed artefact (AUDIT_DISPOSITION §0.15).
- **Signed payload in callback_data.** Impossible in 64 bytes.
- **Approval only when tainted.** Rejected: UAT 5 expects approval for calendar create, and an untainted model can still be wrong about time zones.
