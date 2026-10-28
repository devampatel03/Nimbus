# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""User approvals (P3.2, ADR-0009, INV-5, R8).

- `canonical`: payload canonicalisation and hash (pure).
- `token`: the 46-byte HMAC callback token (pure).
- `totp`: RFC 6238 step-up (pure).
- `card`: card text from the stored payload, never model prose (pure).
- `types`: `ConsumedApproval` (sealed), `ApprovalAsk`, `AskOutcome`, `AuditRecord`.
- `store`: `pending_action` / `policy_audit` access; the atomic consume.
- `service`: request -> card -> callback -> verify -> step-up -> consume -> execute.
- `settings`: `APPROVAL_*` (hidden secrets).

Deliberately import-light: `src.orchestrator.dispatch` imports the pure
modules only, so this package must not import the store or service here.
"""
