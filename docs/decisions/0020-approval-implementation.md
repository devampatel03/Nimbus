# 0020 — Approval implementation: token, step-up keypad, policy_audit

- Status: Proposed (P3.2, 2026-10-28). Implements ADR-0009; refines its token layout and TOTP library.

## Context
ADR-0009 fixes the matrix and a 64-byte approval token (12-byte id, HMAC over id, payload hash and expiry, `pyotp`). Building it showed four gaps: `pending_action.id` is a 16-byte UUID; the token did not bind the user or say which button it is; a TOTP code typed as a chat message would be stored as an event and reach memory and the model; and policy decisions were logged only (P3.1 review LOW-6). The P3.1 review MED also asked that a confirmation be bound to the payload hash and consumed atomically.

## Decision
1. **Token (46 bytes):** `verb ":" base64url(action_id[16] || mac[16])`, where verb is `ap` or `rj` and
   `mac = HMAC-SHA256(K, domain || verb || action_id || sha256(payload) || len(nonce) || nonce || expiry_s || user_id)[:16]`.
   The MAC is recomputed from the stored row and compared with `hmac.compare_digest`. The parse is strict (canonical base64url only).
2. **Payload hash:** sha256 over canonical JSON of `{v, tool, action, payload}`. The JSON is sorted, compact and UTF-8, with no NaN. Anything that is not plain JSON is refused, as is anything over 3000 bytes. The stored payload is re-hashed on every callback, and again in `Dispatcher.execute_approved`.
3. **Single use:** one `UPDATE ... WHERE status='pending' AND expires_at > now AND payload_sha256 = h [AND requires_totp = false unless the step-up was verified] RETURNING`. It commits before the tool runs (at most once). The existing `nimbus_single_use_guard` trigger backs it.
4. **Confirmation source:** the dispatcher sets `user_confirmed` / `step_up_confirmed` only from a sealed `ConsumedApproval`. That object is not a dataclass and cannot be copied. Only `ApprovalStore.consume` mints it. The policy still decides again: the kill switch, spend cap, rate limit and disabled tools all apply. Pinned by `tests/eval/invariants/test_inv5_approvals.py`.
5. **TOTP step-up:** RFC 6238 on the standard library (SHA1, 6 digits, 30 s, ±1 step), checked against the RFC vectors. It is not `pyotp`: the algorithm is the same and no dependency is added. Digits are entered on an inline keypad (callbacks), so a code is never a message, an event or memory. Limits:
   - a code is accepted only for a step after the last accepted one;
   - 3 wrong codes **per action** reject the action. The count comes from `policy_audit` rows (`totp_failed` with that `action_id`), so tapping Approve again (a new keypad) or restarting does not reset it;
   - 5 wrong codes in 15 minutes, across all actions, lock step-up. This is also counted durably in `policy_audit`.
8. **Deviations from ADR-0009 (line 31: "TOTP is `pyotp`, ±1 step, and the secret is stored encrypted"):**
   - *Library:* stdlib RFC 6238 instead of `pyotp`. The algorithm, window and constant-time compare are the same and are checked against the RFC 6238 appendix B vectors. This avoids a new dependency for about 20 lines of code.
   - *Secret storage:* `APPROVAL_TOTP_SECRET` is a hidden `SecretStr` read from the env (and scrubbed from logs), not a value encrypted in the database. Rationale: the only available key (`DataCipher`, `src/db/crypto.py`) lives in the same env on the same VM. Encrypting the secret would add nothing against a VM compromise, which exposes both the key and the ciphertext; such an attacker can run tools directly anyway. The step-up control is aimed at a different threat: a compromised or unattended Telegram account. A Telegram attacker cannot read the VM env.
6. **`policy_audit`** (migration `f3a9c1d7e5b2`, append-only, truncate-guarded): one row per policy decision and per approval event. It holds names, rule ids and fixed reason codes only, never values. A decision whose audit write fails does not run.
7. **R8:** `ApprovalService.request_memory_promotion` writes tool `memory`, action `promote`, payload `{"item_id"}`. This is exactly what `PgVectorMemoryStore.promote` accepts.

## Consequences
- Approvals are off unless `APPROVAL_HMAC_KEY` is set. Step-up actions (destructive, external recipient) cannot be approved unless `APPROVAL_TOTP_SECRET` is set. In both cases the result is a denial.
- Residual: a compromised model can still ask the user to approve a harmful but plausible action. The card shows the true stored payload and escapes invisible characters, but it does not normalise look-alike letters. An approved action runs outside the turn, and the model does not see the result.
- In-process state: the step-up challenge and the rate counter are lost on restart. The user taps Approve again; the lockout survives.

## Alternatives
- **TOTP as a typed message.** Rejected: it would be stored as an event and fed to the model (INV-7).
- **`pyotp`.** It is equivalent. It was not added to avoid a dependency for about 20 lines; it can be swapped in if review prefers.
- **12-byte id as in ADR-0009.** It would need a second id column; the UUID primary key is already random (122 bits).
