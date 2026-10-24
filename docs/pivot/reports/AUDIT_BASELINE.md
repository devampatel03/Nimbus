# AUDIT_BASELINE (P0.1) - measured 2026-10-24, branch `pivot`, HEAD cf8ec01

## 1. Measured results (own runs; Docker daemon down, no model loaded)

| Command | Result |
|---|---|
| `uv run pytest tests/unit -q` | 115 passed, 2 warnings, 18.39s. Clean. |
| `uv run pytest -q` (whole suite, 220s) | 178 passed, 1 skipped, 26 errors. |
| `uv run ruff check src/` | All checks passed. |
| `uv run mypy src/` | Success: no issues found in 58 source files. |

The 26 errors are all environmental. Every one is `ConnectionRefusedError: [WinError 1225]` (26 of 26 E-lines), i.e. Postgres/Redis/Qdrant are unreachable. There are no genuine assertion failures. Errors by file:

| File | Errors |
|---|---|
| tests/eval/invariants/test_inv5_memory_provenance.py | 7 |
| tests/integration/test_manager.py | 5 |
| tests/integration/test_stance_log.py | 4 |
| tests/integration/test_user_profile.py | 4 |
| tests/integration/test_contradiction.py | 3 |
| tests/integration/test_warm_context.py | 2 |
| tests/integration/test_supersession.py | 1 |

- The 1 skip is INV-4 (`tests/eval/invariants/test_inv4_mode_a_nonblocking.py`, `skipif`). It is a stub: the comment says "left as a stub", and it was never made concrete. INV-4 is therefore not enforced by any test.
- Not run, by instruction: `tests/eval/locomo/harness.py` and the model-dependent eval scripts. LoCoMo and personality scores are "not measured".
- ruff and mypy cover `src/` only. `tests/` is never linted or type-checked.
- `uv run pytest` collects `tests/eval/locomo/harness.py`'s sibling files. No pytest-timeout plugin is installed (`--timeout` is rejected).

## 2. CLAUDE.md claim check

| Claim | Reality |
|---|---|
| "204 passed / 1 skipped" | Total collected is 205 (178+26+1), so 204 plus 1 skip is consistent. 204 passed is only true with Postgres/Redis/Qdrant up. It is 178 passed here, and 115 unit-only. Not re-verifiable offline. |
| ruff and mypy clean | Confirmed (src/ only). |
| Integration and INV-5 tests "against real Postgres" | Confirmed by the code path. They cannot run without Docker. |
| INV-4 enforced | False. The test is a permanent skip. |
| "Perception: ASR + diarization in `src/perception/`" (architecture section) | `src/perception/` and `src/workers/` are empty directories. No `.py` files, not even `__init__.py`. Perception was never built. |
| Task table T0.9 "partial" | SBOM-in-CI not built (per the table). I did not verify CI. |

## 3. What exists in code (src/ is 58 py files, ~4466 LOC; commits from `git log`, 18 total)

| Item | Code | Commit |
|---|---|---|
| T0.1 CI/pre-commit | CI and hooks (not audited here) | 3385fbf |
| T0.2 (local-dev infra variant) | `src/db/{postgres,redis,qdrant}.py`, `src/api/health.py`, docker-compose | 843594a |
| T0.2 as in EXECUTION_PLAN (LUKS/WireGuard/egress proxy) | absent | - |
| T0.8 contracts | `src/contracts/*` (6 models) | a757108 |
| T0.9 | NOTICE only | a757108 |
| T0.10 invariants | `tests/eval/invariants/*` (INV-4 still a stub) | 6d9f5ef |
| T2.9 bi-temporal graph | `src/memory/{graph.py,schemas/*}` | 6d9f5ef |
| T1.6/T1.7 memory manager and warm context | `src/memory/{manager,retriever}.py` | 3429c16 |
| T3.1/3.4/3.5 personality | `src/personality/*` | 4723a87 |
| T3.9/11/12/13 scrubber | `src/security/{scrubber,pii_detector,audit_log,kill_switch}.py` | 23fe91f |
| T1.10/T4.1-4.4 orchestrator, approver, tools | `src/orchestrator/*`, `src/tools/*` | bd1fda5, 2a18d74 |
| T1.10 REST/WS | `src/api/*` | 747176c |
| T0.4 eval harness | `tests/eval/{locomo,personality,injection}` | 935218c |
| Step 11 embedder | `src/memory/embedder.py` (Qwen3-Embedding-0.6B) | db6ff6a |
| LLM wiring (vLLM) | `src/llm/client.py`, wired into graph/approver/extractor/agent_character | a5bb12c |
| Gemini egress and gmail/drive/reminders | `src/security/scrubber.py`, `src/tools/integrations/{gmail,drive,reminders}.py` | d33fe6d |
| Approver LLM judge, fail-closed | `src/orchestrator/approver.py` | 4bf040a |

Not built (per code): T3.2 trait workers, T3.3, T3.6 full eval, T3.10 network egress proxy, T4.5 graded red team, perception, workers, any Flutter client (no Flutter dir in src; not otherwise checked).

The "old Steps 1-13" PLAYBOOK is not in the repo (`docs/` holds only AGENT_OPERATING_MANUAL, EXECUTION_PLAN, PRD, TECHNICAL_ARCHITECTURE, pivot/). Only Step 11 is referenced in the notes. Steps 12 (local LLM serving) and 13 map per the Brief to the LLM-wiring and Gemini commits.

## 4. Flags

- **Secrets.** `git log -p --all -G` for Gemini-style (`AIza...`), `sk-...` and PEM private-key patterns returned no commits. No tracked `.env`, `.pem` or `.key` files. `.env` is gitignored and only `.env.example` exists. Residual risk: this was pattern-based only, and detect-secrets in pre-commit exists.
- **TODO/FIXME/XXX** in src/tests `.py`: 0.
- **Placeholder baselines and stubs.**
  - `DeterministicStubEmbedder` is still shipped (test-only injection).
  - `personality/character_card.md` is a placeholder.
  - `AlwaysAgreeBaseline` and `StonewallBaseline` are placeholder baselines in the personality evals.
  - `calendar.py` stubs the Google API.
  - `gmail.send` is dry-run only.
  - The LoCoMo 6.4% figure is stale (pre-embedder) and the post-embedder score was never measured.
  - The approver 0/30 and 30/30 red-team numbers reflect the fail-closed path with no LLM server, per CLAUDE.md's own note. They are not evidence of defence quality.
- **Flaky or env-sensitive tests.**
  - The integration tests and INV-5 hard-require live services and error rather than skip when Docker is down. Consider a skip marker.
  - `test_health_is_exempt_from_the_auth_guard` emits a Qdrant "Failed to obtain server version" warning, so it touches the network.
  - No intermittent failures were observed in my single run (not repeated).
- **Working tree.** Uncommitted modifications to `.claude/*`, `.serena/project.yml` and `docs/EXECUTION_PLAN.md` (+725/-290) pre-date this audit. `ENVCHECK.md`, `COUPLING.md` and `AUDIT_DISPOSITION.md` already existed as untracked files from other agents.
