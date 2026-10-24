# 0002 — Invariant renumbering: the Brief's INV-1..7 are canonical

- Status: Accepted for design. Applying it to `tests/eval/invariants/**` needs the human's OK under CLAUDE.md rule 4 (open question Q1).

## Context
The repo's CLAUDE.md and `tests/eval/invariants/` use INV-1..5, and those numbers mean different things from the Brief's INV-1..7 (AUDIT_DISPOSITION §0.4–0.6):
- Repo INV-2 means "no memory in the egress payload" (a `ToolIntent` schema check). Brief INV-2 means "local-only inference, retired".
- Repo INV-5 means "provenance non-null". Brief INV-5 means "policy is deterministic".
- Repo INV-1 (one httpx call site) does not see the `openai` SDK path at all.
- Repo INV-4 is a permanent skip stub.

Keeping two numbering schemes would make every PR description ambiguous.

## Decision
The Brief's numbering is the only numbering. Mapping (TARGET_DESIGN §7):
- **INV-1:** rewrite the test as `test_inv1_single_llm_module.py` (LLM-SDK import scan + transport import scan + runtime guard checks). Delete `test_inv1_single_egress_point.py`.
- **INV-2:** the number is retired and never reused. Delete `test_inv2_no_sensitive_data_in_egress.py` together with `ToolIntent`. Its purpose (shrinking what crosses the boundary) is now served by INV-6 and INV-7.
- **INV-3:** keep the test. Add a DB-trigger test so append-only is enforced by Postgres too, not only by an AST scan of one file.
- **INV-4:** delete the stub.
- **INV-5:** rename the existing provenance test to `test_r7_memory_provenance.py` (R7 covers it). Add a new `test_inv5_policy_deterministic.py`.
- **INV-6, INV-7:** new tests.

CLAUDE.md's invariant section is rewritten by docs-memory at the W1 gate.

## Consequences
- One batch of protected-file edits, spread across tasks P1.2c, P1.3a, P1.4c, P1.5, P3.0 and P3.1.
- Old commit messages that cite "INV-2/INV-5" keep their historical meaning. MEMORY.md marks them `[SUPERSEDED by 0002]`.

## Alternatives
- **Keep repo numbers and append the Brief's new ones as INV-6..10.** Rejected: the Brief is normative and the reviewer checklists use its numbers.
- **Keep the repo INV-2 test as a regression guard.** Rejected: `ToolIntent` is deleted (ADR-0005), so the test would guard nothing.
