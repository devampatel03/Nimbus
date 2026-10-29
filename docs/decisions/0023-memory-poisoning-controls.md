# ADR-0023: Memory poisoning controls (P4.6)

Status: Proposed (reviewer sign-off)
Date: 2026-10-30
Satisfies: R7, R8, INV-3, INV-5, INV-6. KPI `untrusted_auto_promotion = 0`.

## Context
R8 was enforced for chunks only, in Python SQL plus a CHECK (a promoted untrusted row
needs *some* approval id) and a single-use unique index. Nothing checked that the
approval was consumed or named this item at the database, and graph edges had no
promotion path at all. P2.4 follow-ups: edge promotion, same `source_key` with a
different source/confidence merged silently.

## Decision
1. Trigger `nimbus_promotion_guard()` (migration e5b1c7a3d902) on `memory_chunk` and
   `graph_edge`, BEFORE INSERT OR UPDATE: untrusted rows cannot be born promoted;
   they become promoted only with a consumed, in-time `memory`/`promote` pending_action
   whose payload `item_id` equals the row id; promoted rows cannot be demoted; a
   `trusted` row cannot cite a non-trusted source event. Single use stays in the partial
   unique indexes (c4e8a1d92b37). Defence in depth: the store SQL keeps its own check.
2. `GraphRepository.promote_edge` reuses the chunk binding (same approval shape).
3. `src/memory/review.py` `ReviewQueue`: read the pending untrusted queue, ask one
   approval card per item (`ApprovalService.request_memory_promotion`, bounded drain),
   `promote` as the approval promoter for both tables, reject-by-card retracts via
   `invalidated_at` (never a delete). No bulk approve; no proactive messaging: the
   weekly review (P5.1) calls `drain`.
4. `record_event` refuses a replayed `source_key` whose lane, kind, source or confidence
   differs (`EventConflictError`) instead of merging.

## Consequences
- Trusted retrieval already filters `promoted`; unchanged and now tested for edges.
- A superuser (the app role today, HIGH in CLAUDE.md) can still disable triggers; the
  fix remains the non-superuser role (P6.1/P6.2).
- Out of scope: commitments/decisions tables (their own guard, ADR-0022), quarantine
  extraction route (P3.0 MED-D), retract of a successor reopening the old fact (P5.2).
