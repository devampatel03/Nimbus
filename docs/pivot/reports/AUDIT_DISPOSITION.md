# AUDIT_DISPOSITION (P0.2)

Method: static analysis only (grep import graph, reading code, `git log`). No tests run here. Line counts are `wc -l` of the file at HEAD `cf8ec01` on branch `pivot`. "Dependent tasks" use the P-ids of `docs/pivot/01_SDLC_PLAN.md` where P1.2/P1.3 are quoted there; later ids are proposed by this audit as `P?.x` and need the architect (P0.5) to map them.

Verdict key: KEEP / MODIFY / REMOVE / CREATE. Risk: L / M / H.

## 0. Findings that overturn or sharpen the Brief's hypotheses

1. **Apache AGE is not used by any code.** `grep -rniE 'cypher|ag_catalog|create_graph'` over `src/ migrations/ tests/` returns nothing. The only AGE references are `infra/docker/postgres/Dockerfile:5` (`FROM apache/age:release_PG16_1.5.0`), `infra/docker/postgres/init.sql:4,8` (`CREATE EXTENSION age`; `SET search_path = ag_catalog`) and a comment in `config/settings.py:17`. The bi-temporal "graph" is plain relational tables (`src/memory/schemas/graph.py` `graph_node`/`graph_edge` with FKs and btree indexes at lines 48-54; Alembic revision `0dc85954088a`). AD-1 (MEMORY.md:7) was never implemented. Brief's "plain Postgres + pgvector" is correct; the work is to swap the base image, not rewrite graph code. Risk to flag: `apache/age` ARM64 availability was not verified; `pgvector/pgvector:pg16` is multi-arch (not verified here either, no network check).
2. **pgvector is installed but no vector column exists.** `grep -n 'vector\|Vector'` over `migrations/versions/*.py` and `src/memory/schemas/*.py` returns nothing. Episodic vectors live only in Qdrant (`src/memory/manager.py:168-173,235-237`). `pgvector` python package is not in `pyproject.toml`. P1.3 is a build, not a swap.
3. **Tests bypass Alembic.** `tests/conftest.py` `pg_engine` runs `Base.metadata.create_all`; the migration is only round-tripped by hand per CLAUDE.md. Nothing in the suite verifies that `alembic upgrade head` equals the ORM. P1.2 (migrations from empty) needs its own test.
4. **Brief INV-2 is mislabeled relative to the repo.** Brief §4 says INV-2 = "local-only inference, remove tests that assert it". Repo INV-2 (CLAUDE.md "Invariants") = "No memory/transcript/reasoning in any egress payload", tested by `tests/eval/invariants/test_inv2_no_sensitive_data_in_egress.py` as a schema check on `ToolIntent`/`ToolResult` only (lines 1-60). That test does not assert local-only inference at all. Under the pivot the model legitimately receives memory in context, so the repo INV-2 as worded is dead for the LLM path, but the test still passes as long as `ToolIntent` exists. Decision needed: delete it with `ToolIntent`, or keep it as a regression guard on the Gemini-style intent contract if tools keep `ToolIntent`.
5. **Brief INV-5 number collides with repo INV-5.** Repo INV-5 = provenance non-null (`tests/eval/invariants/test_inv5_memory_provenance.py`); Brief new INV-5 = deterministic policy. Brief R7 covers provenance. Rename the existing file to `test_r7_memory_provenance.py` (or keep the name and add `test_inv5_policy_*` under a different path) to avoid two meanings.
6. **Repo INV-1 test does not cover the LLM path.** `tests/eval/invariants/test_inv1_single_egress_point.py` is an AST scan for `httpx`/`requests`/`aiohttp` call sites and expects exactly one, inside `scrubber.execute_external` (CLAUDE.md T0.10 row, `src/security/scrubber.py:65,262`). `VLLMClient` uses the `openai` SDK (`src/llm/client.py`), invisible to that scan (CLAUDE.md row "Local LLM wired"). Brief INV-1 ("exactly one module may call an LLM") needs a new test, and the old scan will go red the moment Google/Notion/Telegram clients (httpx or SDK) are added. Rewrite, not just retire.
7. **`src/orchestrator` and `src/llm` are less local-model-shaped than hypothesized.** `src/llm/client.py` is already an OpenAI-SDK client (`from openai import AsyncOpenAI`, `LLMClient` Protocol with `complete/stream/structured`). Only vLLM-specific bit is the `guided_json` extra_body. The graph (`src/orchestrator/graph.py`) depends on the `LLMClient` Protocol via `GraphDependencies.llm_client`, so the adapter swap is small. The Qdrant/Redis coupling in the orchestrator is only through `MemoryManager` (`graph.py:55,117`).
8. **`src/perception/` and `mobile/` contain only `.gitkeep`.** `find src/perception mobile -type f` -> `.gitkeep` and `mobile/lib/.gitkeep` only. No diarization/STT/Flutter code exists. Removal is directory deletion. BUT `DiarizedTranscript` contract is live: used by `src/memory/extractor.py` (4 extractors, lines 34,82-218), `src/contracts/__init__.py`, `tests/unit/test_contracts.py`, `tests/fixtures/diarized_transcript.json`. Extractors have no importer (`grep` of `extractor` in src/ tests/ returns nothing): dead code today, but it is the planned ingest path.
9. **`arq` is declared (`pyproject.toml:16`) and imported nowhere** (`grep -rn arq src tests` empty). Dead dependency. `src/workers/` is `.gitkeep` only.
10. **`langchain_core` is imported (`src/orchestrator/graph.py`: `from langchain_core.runnables import RunnableConfig`) but is not a declared dep** (transitive via langgraph). Fragile on dependency changes.
11. **Gmail `send` exists** (`src/tools/integrations/gmail.py:30` `"send": RiskClass.WRITE_EXTERNAL`; `src/security/scrubber.py:85` `ALLOWED_TOOLS["gmail"]` includes `"send"`; docstring line 11-17 says route through `execute_external` once signed off). R6 ("no send, ever") means the action and its allowlist entry must be deleted, and `tests/unit/test_gmail_drive_reminders.py` currently asserts send dry-run behavior, so that test must be replaced by a "no send action/scope" test.
12. **Reminders and tasks are in-memory dicts** (`src/tools/integrations/reminders.py:58` `self._reminders: dict`, `:69` `AsyncIOScheduler()` with default memory jobstore; tasks.py similarly). They do not survive restart, violating R1/R11. They are MODIFY to Postgres-backed state, not KEEP.
13. **Calendar/Gmail/Drive tools are intent-builders with no Google client** (AD-25, MEMORY.md:31; `calendar.py` imports `execute_external`). The pivot needs real Google clients and OAuth; these three are rewrite-in-place, and the Gemini `generateContent` call in `execute_external` has no role in the new design.
14. **Hook-enforced protection of `tests/eval/` is absent.** CLAUDE.md rule 4 is prose. `grep 'tests/eval' .claude/settings.json .claude/hooks/*` finds nothing; `protect-files.sh` only blocks secret-like paths. Protected files are protected by convention only; changing them still needs the human's explicit OK per rule 4 (Brief §0 says CLAUDE.md wins on process).
15. **The injection red-team and approver tests currently measure a fail-closed artefact, not defense.** CLAUDE.md last task row: 0/30 and 30/30 come from the LLM judge being unreachable (no vLLM). Retiring them in favour of the architectural gate loses nothing real.
16. **Placeholder baselines remain placeholders.** `personality/character_card.md:1,8` is "PLACEHOLDER" with `TODO(owner)`; R9 is blocked on the human. `tests/eval/personality/anti_sycophancy.py` and `stance_consistency.py` default to `AlwaysAgreeBaseline`/`StonewallBaseline` (CLAUDE.md T0.4 row). LoCoMo and embedder re-run never recorded (CLAUDE.md "Step 11" row).

## 1. Top-level directories and files

| Path | Purpose today | Verdict | Reason | Evidence | Risk | Dependent tasks |
|---|---|---|---|---|---|---|
| `src/` | Python package (see section 2) | MODIFY | Core reused, backends swapped | section 2 | M | all |
| `tests/` | pytest suite | MODIFY | Keep structure, replace infra fixtures and retired-invariant tests | section 3 | M | P1.3, P1.1 |
| `config/` | Pydantic settings, licence header | MODIFY | Drop redis/qdrant/vllm/JWT/CORS settings; add telegram/allowlist/spend-cap/model settings | `config/settings.py:22-26` (redis, qdrant), `:28-37` (vLLM), `:39-47` (Gemini egress), `:49-65` (JWT/CORS) | L | P1.3, P1.4+ |
| `infra/` | Postgres image (AGE+pgvector), nginx/scripts placeholders | MODIFY | Replace AGE base image with pgvector; remove empty `nginx/` and `scripts/` | `infra/docker/postgres/Dockerfile:5,8`; `init.sql:4-8`; `infra/nginx/.gitkeep`, `infra/scripts/.gitkeep` | M (ARM64 image, unverified) | P1.2 |
| `docker-compose.yml` | Dev stack: postgres, redis, qdrant, vllm | MODIFY | Keep postgres only; add nimbus-app; drop redis/qdrant/vllm; add internal-network egress controls (R3, R13) | `docker-compose.yml` services at lines 2,19,30,53; volumes 75-77; publishes 5432/6379/6333/8000 to host (R3-adjacent: bind to 127.0.0.1 or no ports on VM) | M | P1.2, P1.3, W6 deploy |
| `migrations/` + `alembic.ini` | Alembic async env + one revision | MODIFY | Add pgvector/extension, events, jobs, audit, encrypted columns revisions; AGE-free already | `migrations/versions/0dc85954088a_bitemporal_graph_schema.py` (170 lines, plain tables) | M | P1.2 |
| `mobile/` | Flutter placeholder | REMOVE | Out of scope; no code | `mobile/lib/.gitkeep` only | L | none |
| `personality/` | `character_card.md` (hand-authored target) | KEEP | R9 differentiator; still PLACEHOLDER, human-blocked | `personality/character_card.md:1,8` | M (human dependency) | HUMAN_TODO, R9 CI check |
| `quality/` | Machine-checkable gates (pivot kit) | KEEP | Pivot artefact, already encodes R5/R6/R13 gates | `quality/thresholds.yaml` | L | P1.1, reviewer |
| `scripts/` | envcheck + pivot runner | KEEP | Pivot kit | `scripts/envcheck.py`, `pivot-*.sh/.ps1` | L | P0.6 |
| `data/` | Runtime audit log dir | KEEP | Gitignored runtime output (`data/audit/egress.jsonl`); path must move into volume on VM | CLAUDE.md T3.9 row; `src/security/audit_log.py` | L | audit/llm module |
| `docs/` | Specs + pivot docs | KEEP | `docs/pivot/00_PIVOT_BRIEF.md` wins; PRD/TECH_ARCH/EXECUTION_PLAN marked superseded where in conflict | Brief header | L | docs-memory |
| `.github/workflows/ci.yml` | lint, mypy, pytest, detect-secrets, sbom | MODIFY | Triggers only on `main` (`ci.yml:3-6`); pivot branch gets no CI. `test` job has no Postgres/Qdrant/Redis services, so integration tests cannot pass in CI as written (`ci.yml:20-26`). Add services (pgvector only), add red-team job, `pivot` branch trigger. Fixes the CLAUDE.md-noted SBOM gap partially (sbom job exists now). | `ci.yml` lines 3-6, 20-26, 38-50 | M | P1.1 |
| `.pre-commit-config.yaml` | ruff, mypy, detect-secrets, licence header | MODIFY | Remove `redis`, `qdrant-client`, `sentence-transformers` etc. from mypy `additional_dependencies` as they drop | `.pre-commit-config.yaml` mypy hook list | L | P1.3 |
| `.secrets.baseline` | detect-secrets baseline (137 lines) | KEEP | Needed for secret-scan gate; history scan is AUDIT_BASELINE's job | `.github/workflows/ci.yml:35` | L | P1.1 |
| `.env.example` | Env template | MODIFY | Contains `REDIS_URL`, `QDRANT_URL`, `VLLM_BASE_URL`, `MASTER_KEY_DERIVATION_SALT`, `JWT_SECRET`, Google credential *paths*; rewrite for Telegram token, allowed user id, model key, spend cap, encryption key, backup target. Keys only, no values reviewed. | `.env.example` lines 3-4,7,14,17-18 | L | P1.4, R12 |
| `pyproject.toml` | deps | MODIFY | see section 4 | `pyproject.toml:5-21` | M | P1.3 |
| `uv.lock` | lockfile | MODIFY | regenerate after dep drops (torch etc. leave if embedder goes API) | n/a | L | P1.3 |
| `NOTICE` | licence attributions | MODIFY | Remove entries for dropped deps (qdrant, redis, fastapi?, etc.); add new | CLAUDE.md T0.9 row | L | last step of each removal |
| `CLAUDE.md` | project rules | MODIFY (human-approved) | Architecture/invariants/phase sections stale after pivot; Brief says CLAUDE.md wins on process only | CLAUDE.md "Architecture", "Invariants" | L | docs-memory |
| `MEMORY.md` | AD log | MODIFY | Append ADRs superseding AD-1 (AGE), AD-2/3 (Gemini-only egress), AD-4, AD-14, AD-15..17 (API), AD-22/27 (approver) | `MEMORY.md:7-33` | L | docs-memory |
| `REVIEW.md`, `README_FIRST.md` | pivot-kit guidance | KEEP | | root listing | L | none |
| `.claude/` | Harness | see section 5 | | | | |
| `.serena/`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.venv` | local tooling | KEEP (untracked/ignored) | Not product code; `.serena/project.yml` shows modified in git status | gitStatus | L | none |

## 2. `src/` modules

| Path | Purpose today | Verdict | Reason | Evidence | Risk | Dependent tasks |
|---|---|---|---|---|---|---|
| `src/__init__.py`, `src/.gitkeep` | package marker | KEEP | | | L | none |
| `src/api/main.py` (74) | FastAPI app factory, CORS + JWT middleware, builds graph | REMOVE (replace with tiny health) | Telegram long polling needs no HTTP surface (R3). Only consumer of `build_graph` besides tests. | `main.py:38,52-54`; importers: `tests/unit/test_auth.py:14`, `test_chat.py:10`, `test_ws.py` | L | API-removal PR; gateway CREATE must exist first as the orchestrator entry |
| `src/api/auth.py` (78) | PyJWT login route, shared password | REMOVE | Single-password JWT for a web client; not in pivot. Brief INV-7 / R12 gain nothing from it. | `auth.py` imports `jwt`; importers `src/api/main.py`, `src/api/middleware/auth.py`; `tests/unit/test_auth.py` | L | API-removal PR |
| `src/api/middleware/auth.py` (78) | ASGI JWT middleware | REMOVE | as above | imports `src.api.auth` | L | API-removal PR |
| `src/api/routes/chat.py` (82) | `POST /chat`, `WS /chat/ws` | REMOVE | Replaced by Telegram gateway. Logic worth porting: per-turn `AgentState` construction (`chat.py` imports `AgentState`, `ConversationTurn`) | importers `main.py`; `tests/unit/test_chat.py`, `test_ws.py` | L | gateway CREATE (P?.gateway) |
| `src/api/routes/tasks.py` (112) | REST CRUD over `TasksTool` | REMOVE | Only exposes task CRUD to a web client | imports `src.tools.integrations.tasks.TasksTool` | L | API-removal PR |
| `src/api/routes/memory.py` (43) | `GET /memories` debug | REMOVE | Uses `qdrant.qdrant_client` and `redis.redis_client` (lines 25,41); no test | `memory.py:25,41` | L | Redis/Qdrant PRs hit this first |
| `src/api/health.py` (26) | `/health` returning postgres/redis/qdrant status | MODIFY | Keep as a minimal localhost-only health (Brief §5), drop redis/qdrant checks | `health.py` lines 20-26; `tests/unit/test_health.py:25-54` patches `src.api.health.redis.check_health` / `qdrant.check_health` | L | Redis and Qdrant PRs |
| `src/contracts/tool_intent.py` | `ToolIntent`, `ToolResult`, `RiskClass`, `Primitive` | MODIFY | `RiskClass` and `Primitive` still useful for policy; `ToolIntent`'s cloud-egress role goes away. Imported by scrubber, approver, graph, base tool, all tools, `tests/eval/invariants/test_inv2*` | grep of `contracts.tool_intent` | M | policy CREATE, tools rewrite |
| `src/contracts/memory_query.py` | `MemoryQuery/Result` | KEEP | Interface between orchestrator and memory; storage-agnostic | `src/memory/manager.py`, `src/orchestrator/state.py`, `tests/fixtures/memory_*.json` | L | P1.3 |
| `src/contracts/user_profile_slice.py` | trait slice | KEEP | personality wiring | `context_builder.py` | L | none |
| `src/contracts/session_bundle.py` | voice-session bundle | MODIFY | Shaped for diarized voice sessions (`Session` ORM mirrors it, `src/memory/schemas/session.py:4`). Replace with Event/Ingest contract; only referenced by `test_contracts.py` and fixture. | `tests/unit/test_contracts.py:38,109`; no src importer other than `__init__` | L | ingest CREATE |
| `src/contracts/diarized_transcript.py` | diarization output | MODIFY (rename to transcript/event) | Brief: Pixel Recorder `.txt` and voice notes become plain transcripts; keep a speaker-optional transcript type. Live importer is the extractor. | `src/memory/extractor.py:34,82-218`; `tests/unit/test_contracts.py:22,51,110`; `tests/fixtures/diarized_transcript.json` | M | ingest CREATE, extractor MODIFY |
| `src/contracts/proactive_candidate.py` | proactive item | KEEP | scheduler/interruption budget (R11) | `tests/unit/test_contracts.py:25,96` | L | scheduler |
| `src/contracts/__init__.py` | re-exports | MODIFY | follows above | | L | |
| `src/db/postgres.py` (36) | async engine/session | KEEP | | importers `src/api/*` | L | none |
| `src/db/redis.py` (17) | Redis client | REMOVE | | importers: `src/api/main.py:37,45`, `health.py`, `routes/memory.py:25,41`, `src/memory/manager.py:46` (type) via param | M | Redis-removal PR |
| `src/db/qdrant.py` (18) | Qdrant client | REMOVE | | importers: same set as redis | M | Qdrant-removal PR |
| `src/llm/client.py` (142) | `LLMClient` Protocol + `VLLMClient` (OpenAI SDK, `guided_json`) | MODIFY | Becomes the single `llm` module (R4): rename to OpenAI-compatible adapter, add audit log + scrubber + spend cap + kill switch inside it. Protocol stays. | `client.py:36-62`; importers: `src/memory/extractor.py`, `src/orchestrator/approver.py`, `graph.py`, `src/personality/agent_character.py`, `tests/conftest.py`, `tests/unit/test_graph.py`, `test_risk_gating.py`, `test_llm_client.py` (9 refs) | M | P?.llm-adapter |
| `src/memory/manager.py` (362) | Redis + Qdrant + Postgres unified manager, `DeterministicStubEmbedder`, `SessionState` warm context | MODIFY | Keep API shape (`write_episodic`, `write_semantic`, `query`); re-back episodic on pgvector; drop warm-context Redis code (Mode A concept, INV-4 retired) | `manager.py:45-46` imports, `:59-60` warm key/ttl, `:141-173,235-265` Qdrant, `:327-362` Redis; importers: `orchestrator/graph.py:55,117`, `api/routes/memory.py`, `tests/integration/test_manager.py`, `test_warm_context.py`, `tests/eval/locomo/harness.py` | H | P1.3 |
| `src/memory/embedder.py` (51) | `Qwen3Embedder` via sentence-transformers (CPU) | MODIFY | Brief: local only if >=3 GB headroom, else API. Make behind `Embedder` protocol; torch/sentence-transformers become optional extra. Re-run on A1 ARM unmeasured. | `embedder.py` lazy import of `sentence_transformers`; sole importer `manager.py:52` | M | W2 embedder decision |
| `src/memory/graph.py` (111) | `GraphRepository` insert/supersede/find_contradictions | KEEP | INV-3 / R7 core, plain SQLAlchemy | importers `manager.py`; tests `test_supersession.py`, `test_contradiction.py`, `test_inv3` | L | none |
| `src/memory/retriever.py` (92) | hybrid scoring (pure functions) | KEEP | storage-agnostic | importers `manager.py:54`, `tests/unit/test_retriever.py` | L | none |
| `src/memory/extractor.py` (226) | 4 LLM-based extractors | KEEP (MODIFY inputs) | Real, well-formed, but no importer today (dead code) and typed to `DiarizedTranscript`. Wire to ingest + workers. Untrusted-lane tagging (R8) not present. | `grep extractor src tests` empty; `extractor.py:34` | M | ingest, R8 |
| `src/memory/schemas/{base,graph}.py` | Declarative base; `graph_node`, `graph_edge` | KEEP | bi-temporal, provenance CHECKs | `schemas/graph.py:48-65` | L | P1.2 |
| `src/memory/schemas/{commitment,decision,person}.py` | ORM | KEEP | R7; `commitment` needed for reminders | | L | P1.2 |
| `src/memory/schemas/session.py` | persisted voice-session | MODIFY | Becomes `event` table (lane, untrusted flag, source) | `session.py:4-9` | M | P1.2 |
| `src/orchestrator/graph.py` (319) | LangGraph route/retrieve/think/act/respond | MODIFY (**superseded by ADR-0006: replaced by plain async loop**) | Keep (langgraph is already used and tested via `tests/unit/test_graph.py`, `test_risk_gating.py`); replace `Approver` with policy + approvals, add lane/taint to `AgentState`, drop `get_warm_context` | `graph.py:55 (MemoryManager)`, `:117 (get_warm_context)`, imports `Approver`, `VLLMClient` | H | policy, lanes |
| `src/orchestrator/state.py` (39) | `AgentState` | MODIFY | add `lane`, `tainted` | imports contracts | L | policy |
| `src/orchestrator/context_builder.py` (94) | assemble personality prompt | KEEP | | imports personality only | L | none |
| `src/orchestrator/approver.py` (251) | heuristic markers + LLM judge, fail-closed | REMOVE | Brief §2.4: replaced by deterministic policy + user approval buttons. Its destructive-confirmation concept moves to policy. | importers: `orchestrator/graph.py`, `tests/unit/test_approver.py`, `test_risk_gating.py:23`, `tests/adversarial/test_approver_injection_selfcheck.py:40`, `tests/eval/injection/red_team.py:43` | M | policy CREATE first |
| `src/personality/agent_character.py` (257) | character card loader, stance log ORM, friction heuristic + LLM | KEEP | Differentiator; `check_friction_llm` exists; sync heuristic retained for protected eval | importers `context_builder`, `prompt_assembler`, `tests/eval/personality/friction.py`, `tests/integration/test_stance_log.py`, `tests/unit/test_agent_character.py` | L | evals W5 |
| `src/personality/user_profile.py` (152) | trait model with evidence | KEEP | | `tests/integration/test_user_profile.py` | L | none |
| `src/personality/prompt_assembler.py` (52) | pure prompt assembly | KEEP | | `tests/unit/test_prompt_assembler.py` | L | none |
| `src/personality/models/` | `.gitkeep` | REMOVE | empty | | L | none |
| `src/perception/` | `.gitkeep` only | REMOVE | no code to remove; do not build | find output | L | none |
| `src/security/scrubber.py` (365) | `execute_external` Gemini egress, tool/action allowlist, entity substitution, PII hard-fail | MODIFY (split) | Reusable: PII/secret scrubbing and audit-before-send move into the `llm` module (R4/INV-7). REMOVE: Gemini `generateContent` call, `ALLOWED_TOOLS` incl. gmail `send`, `_SessionRefStore` entity substitution if prompts carry names to the provider (Brief accepts provider sees plaintext). Decide whether substitution stays. | `scrubber.py:65,83-91,213-283`; importers: `calendar.py`, `drive.py`, `gmail.py`, `tests/adversarial/test_scrubber.py`, `tests/unit/test_scrubber_gemini.py`, `test_entity_substitution.py`, `test_kill_switch.py`, `test_gmail_drive_reminders.py`, `test_inv1`, `test_inv2` | H | llm-adapter, tools rewrite |
| `src/security/pii_detector.py` (154) | regex PII | KEEP | Brief: scrubber reusable; heuristics are high-recall; used for log/prompt scrubbing (R12/INV-7) | importers `scrubber.py`, `tests/unit/test_kill_switch.py` | L | llm-adapter |
| `src/security/audit_log.py` (54) | JSONL audit | KEEP | Brief `llm` module requires it; move to Postgres `audit` table per P1.2 | importer `scrubber.py` | L | P1.2 |
| `src/security/kill_switch.py` (39) | in-memory toggle | KEEP (MODIFY) | R5 needs persistence + spend-cap integration; in-memory resets on reboot (R1) | importers `scrubber.py`, `tests/unit/test_kill_switch.py`, `tests/adversarial/test_scrubber.py`, `tests/unit/test_scrubber_gemini.py` | L | llm-adapter |
| `src/tools/base.py`, `registry.py` | `BaseTool` ABC, pkgutil discovery | KEEP | R16 tool exposure can be built on the registry | `tests/unit/test_tool_discovery.py` (hardcodes 5 tools) | L | tools |
| `src/tools/integrations/calendar.py` (62) | intent builder -> `execute_external` | MODIFY (rewrite) | needs real Google Calendar client + OAuth (R15) | imports `execute_external` | M | W3 tools |
| `src/tools/integrations/gmail.py` (72) | intent builder; `send` dry-run | MODIFY (rewrite) | drafts only; delete `send` (R6) | `gmail.py:30,36` | M | W3 tools, R6 test |
| `src/tools/integrations/drive.py` (52) | intent builder | MODIFY (rewrite) | read-only least-privilege scope | | M | W3 tools |
| `src/tools/integrations/reminders.py` (152) | in-memory APScheduler | MODIFY | persistent, catch-up on reboot (R1, R11) | `reminders.py:58,69` | M | scheduler |
| `src/tools/integrations/tasks.py` (120) | in-memory tasks | MODIFY | persist to Postgres | | L | P1.2 |
| `src/tools/integrations/` Notion | absent | CREATE | Brief tools list | no file | M | W3 |
| `src/workers/` | `.gitkeep`; no arq usage | CREATE | Postgres `SKIP LOCKED` jobs (Brief §7) | `arq` unused | M | P1.2 |
| gateway (`src/gateway/telegram.py`) | absent | CREATE | allowlist, offset store, rate limit (R2) | no file | M | W2 |
| ingest, policy, approvals, quarantine lane, scheduler, llm spend cap | absent | CREATE | Brief §2 | no files; `grep -ri 'untrusted\|taint\|lane' src` finds nothing | H | W2-W4 |

## 3. `tests/`

| Path | Purpose | Verdict | Reason | Evidence | Risk | Dependent tasks |
|---|---|---|---|---|---|---|
| `tests/conftest.py` (123) | `FakeLLMClient`, `pg_engine`, `db_session`, `qdrant_client`, `redis_client` | MODIFY | Delete `qdrant_client`/`redis_client` fixtures and their imports (lines 17-18,102-123); keep the rest. Note `FakeLLMClient.structured` pattern-matches schema field names `approved`/`tool` (lines 55-70): breaks silently when approver schema goes. | file | M | Redis & Qdrant PRs |
| `tests/unit/test_health.py` | patches `src.api.health.redis/qdrant` | MODIFY | follows health change | `test_health.py:25-54` | L | |
| `tests/unit/test_auth.py`, `test_chat.py`, `test_ws.py` | JWT/REST/WS | REMOVE | with `src/api` | import `src.api.main.create_app` | L | API-removal PR |
| `tests/unit/test_approver.py` | Approver | REMOVE | with approver; replaced by policy tests | `test_approver.py:10-12` | L | policy PR |
| `tests/unit/test_risk_gating.py` | `_make_act` risk gating with `Approver` | MODIFY | rewrite against policy (concept retained) | `:21-26` | M | policy PR |
| `tests/unit/test_graph.py` | graph e2e with fakes | KEEP/MODIFY | adjust to new deps | `:27-30` | L | |
| `tests/unit/test_llm_client.py` (9 refs) | OpenAI client w/ mock | MODIFY | rename with adapter | | L | llm-adapter |
| `tests/unit/test_scrubber_gemini.py`, `test_gmail_drive_reminders.py`, `test_tool_discovery.py` | Gemini request shape, tool drift | REMOVE/REPLACE | Gemini-specific; replace with llm-module and per-tool tests | | M | tools, llm |
| `tests/unit/test_entity_substitution.py`, `test_kill_switch.py` | | KEEP if features kept | decide with scrubber split | | L | |
| `tests/unit/test_contracts.py` + `tests/fixtures/*.json` | golden contract fixtures | MODIFY | `DiarizedTranscript`/`SessionBundle` fixtures change; golden-file compat test is a good pattern, keep | `test_contracts.py:22-38,109-116` | L | ingest |
| `tests/unit/test_agent_character.py`, `test_prompt_assembler.py`, `test_retriever.py` | | KEEP | | | L | |
| `tests/integration/test_manager.py`, `test_warm_context.py` | real Qdrant/Redis/PG | MODIFY / REMOVE | `test_warm_context.py` is Redis-only (`:79` lines): REMOVE; `test_manager.py` rewrite fixtures to pgvector | imports `qdrant_client`, `redis.asyncio` | H | P1.3 |
| `tests/integration/test_supersession.py`, `test_contradiction.py`, `test_stance_log.py`, `test_user_profile.py` | PG only | KEEP | storage unchanged | | L | |
| `tests/adversarial/test_scrubber.py` (50 PII payloads) | scrubber hard-fail | MODIFY | keep as PII/secret scrubbing test against the `llm` module | `:38` etc. | M | llm-adapter |
| `tests/adversarial/test_approver_injection_selfcheck.py` | 30 payloads vs Approver | REMOVE | replaced by red-team vs policy | `:39-40` | L | policy PR |
| `tests/eval/invariants/test_inv1_single_egress_point.py` | AST: exactly 1 httpx call site | MODIFY (protected) | rewrite to "exactly one module imports an LLM SDK/endpoint" | file docstring | M | llm-adapter; needs human OK |
| `tests/eval/invariants/test_inv2_no_sensitive_data_in_egress.py` | schema of `ToolIntent` | REMOVE or KEEP (protected) | see finding 4 | | L | decision |
| `tests/eval/invariants/test_inv3_graph_facts_immutable.py` | AST scan of `src/memory/graph.py` | KEEP (protected) | INV-3 kept | | L | |
| `tests/eval/invariants/test_inv4_mode_a_nonblocking.py` | skipif stub, `mode_a.py` never existed | REMOVE (protected) | Brief INV-4 moot; this is the 1 skipped test | `:21-25` skipif on missing module | L | needs human OK |
| `tests/eval/invariants/test_inv5_memory_provenance.py` | NULL provenance + IntegrityError | KEEP (protected), rename | R7; numbering collision | | L | |
| `tests/eval/locomo/harness.py` + `data/locomo10.json` | LoCoMo recall over Qdrant+Redis via MemoryManager | MODIFY (protected) | imports `qdrant_client`, `Redis`, `EPISODIC_COLLECTION` (harness imports); must follow P1.3 (P1.3 acceptance: "LoCoMo harness still runs") | `harness.py` imports | H | P1.3; needs human OK |
| `tests/eval/personality/{friction,anti_sycophancy,stance_consistency}.py` | personality evals | KEEP (protected) | placeholder baselines; real-model runs in W5 | | L | |
| `tests/eval/injection/red_team.py` | 30 payloads vs sync `Approver.review` | REMOVE/REPLACE (protected) | Brief: >=100 cases, 0 successful attacks vs policy (`quality/thresholds.yaml` security block) | `red_team.py:42-43` | M | policy PR; human OK |
| `tests/eval/longmemeval/` | `.gitkeep` | KEEP | | | L | |

## 4. `pyproject.toml` dependencies

| Dep | Verdict | Evidence |
|---|---|---|
| `redis` | REMOVE | importers: `src/db/redis.py`, `src/memory/manager.py:46`, tests above |
| `qdrant-client` | REMOVE | `src/db/qdrant.py`, `manager.py:45`, tests, harness |
| `arq` | REMOVE now | zero imports |
| `fastapi`, `uvicorn[standard]`, `pyjwt` | REMOVE (after API PR) | only `src/api/*`; test_health imports fastapi |
| `sentence-transformers` | MOVE to optional extra | sole importer `src/memory/embedder.py` (lazy) |
| `openai` | KEEP | `src/llm/client.py` |
| `httpx` | KEEP | scrubber now; Google/Notion/Telegram later; also dev dep for tests |
| `apscheduler` | **SUPERSEDED by ADR-0008 (REMOVE)**; was KEEP-pending | reminders; if used, needs SQLAlchemy jobstore (sync driver) or replace by Postgres jobs per Brief §7 |
| `langgraph` | **SUPERSEDED by ADR-0006 (REMOVE)**; was KEEP | orchestrator; `langchain_core` should be declared explicitly |
| `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic`, `pydantic-settings` | KEEP | |
| `pgvector`, a Telegram lib (aiogram/python-telegram-bot or raw httpx), Google API libs, `notion-client`, `cryptography`, `pyotp` | ADD | none declared |
| `.pre-commit-config.yaml` mypy deps | MODIFY | lists redis, qdrant-client, fastapi, pyjwt, sentence-transformers |

## 5. Docker/compose, CI, `.claude/`

| Path | Verdict | Reason / evidence | Risk |
|---|---|---|---|
| compose `postgres` service | MODIFY | replace AGE image; remove host port publish on VM | M |
| compose `redis`, `qdrant`, `vllm` services and volumes `qdrant_data`, `vllm_cache` | REMOVE | `docker-compose.yml:19,30,53,76,77`; vllm service needs NVIDIA GPU (comment in file), unusable on A1 | L |
| compose `nimbus-app`, egress-restricted network | CREATE | R3, R13 | M |
| `.github/workflows/ci.yml` | MODIFY | see section 1 | M |
| `.claude/agents/{architect,backend-engineer,bot-engineer,devops-engineer,docs-memory,eval-qa,memory-engineer,repo-auditor,reviewer,security-engineer,tools-engineer}.md` | KEEP | pivot kit | L |
| `.claude/agents/{bug-hunter,intent-checker}.md` | KEEP | Brief §5 | L |
| `.claude/commands/{pivot,pivot-resume,init,security-review}.md` | KEEP | | L |
| `.claude/hooks/*.sh` | KEEP, MODIFY `enforce-boundary.sh`? | CLAUDE.md T-last row reports it misfires on legitimate in-repo paths; `stop-verify.sh` already fixed to `uv run pytest`. Add protection for `tests/eval/` and `personality/character_card.md` if wanted (not present, finding 14) | M |
| `.claude/settings.json`, `settings.local.json`, `.headroom_*`, `logs/decisions.log` | KEEP, merge not clobber | all show modified in `git status` at session start; `settings.json.graphify-bak` is a stale backup (REMOVE candidate) | L |
| `.claude/skills/graphify/` | KEEP | `.claude/CLAUDE.md` references it | L |
