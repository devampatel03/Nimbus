# COUPLING (P0.3)

Static analysis at HEAD `cf8ec01` (branch `pivot`): `grep -rnE '^\s*(from|import) '` over `src config tests migrations scripts`, plus reading call sites. Nothing was executed. "Breaks" means import error or assertion failure on removal, inferred from the import graph, not observed.

## 1. Import graph by technology

### Qdrant (`qdrant-client`)
| Importer | How |
|---|---|
| `src/db/qdrant.py` | creates `AsyncQdrantClient` singleton (`qdrant_client`), `check_health` |
| `src/memory/manager.py:45,127,134,141-173,235-265` | constructor param `qdrant`; episodic write/query; `EPISODIC_COLLECTION`; provenance string `episodic_qdrant_point:` |
| `src/api/main.py:37,46` | imports `qdrant`; closes client in lifespan |
| `src/api/health.py` | `qdrant.check_health` |
| `src/api/routes/memory.py:25,41` | passes `qdrant.qdrant_client` to `MemoryManager` |
| `config/settings.py:25-26` | `qdrant_host/port` |
| `docker-compose.yml:30`, volume `qdrant_data` | service |
| `tests/conftest.py` | `qdrant_client` fixture (deletes all collections on teardown) |
| `tests/integration/test_manager.py`, `test_warm_context.py` | construct `MemoryManager` with it |
| `tests/unit/test_health.py:26,45` | patches `src.api.health.qdrant.check_health` |
| `tests/eval/locomo/harness.py` (protected) | imports `AsyncQdrantClient`, `EPISODIC_COLLECTION`; drops/recreates collection per sample (AD-20) |
| `.pre-commit-config.yaml`, `pyproject.toml`, `.env.example` (`QDRANT_URL`) | deps/config |

### Redis (`redis`)
| Importer | How |
|---|---|
| `src/db/redis.py` | client singleton |
| `src/memory/manager.py:46,135,327-362` | warm-context cache (`warm_context:` key, 300 s TTL), `get_warm_context`/`refresh_warm_context`, `SessionState` |
| `src/orchestrator/graph.py:117` | `retrieve_memory` node calls `deps.memory_manager.get_warm_context` (the only production reader) |
| `src/api/main.py`, `health.py`, `routes/memory.py` | as Qdrant |
| `config/settings.py:22-24,~75` (`redis_url`) | |
| `docker-compose.yml:19` | service |
| `tests/conftest.py` (`redis_client` fixture), `test_manager.py`, `test_warm_context.py`, `tests/unit/test_health.py:25,44`, `tests/eval/locomo/harness.py` (protected) | |
| `arq` (declared, never imported) | would have been the Redis queue; dead |

### Local-LLM serving (vLLM) and the `openai` SDK
| Importer | How |
|---|---|
| `src/llm/client.py` | only module importing `openai`; `VLLMClient`; vLLM `guided_json` extra_body |
| `src/memory/extractor.py`, `src/orchestrator/approver.py`, `src/orchestrator/graph.py`, `src/personality/agent_character.py` | import `LLMClient`, `VLLMClient` (default constructor args instantiate `VLLMClient()`), `ChatMessage` |
| `config/settings.py:28-37` | `llm_base_url`, `llm_api_key`, `llm_model`, `llm_timeout_seconds` |
| `docker-compose.yml:53` | `vllm` service (needs NVIDIA GPU per file comment) |
| `tests/conftest.py` (`FakeLLMClient`), `tests/unit/test_graph.py`, `test_risk_gating.py`, `test_llm_client.py` (9 refs, mocks `AsyncOpenAI`) | |
| `.env.example` `VLLM_BASE_URL` | |
No runtime import of vLLM itself exists; the coupling is the compose service, settings names, the `VLLMClient` class name and the `guided_json` hint. Nothing breaks if the compose service is removed.

### sentence-transformers / embedder
`src/memory/embedder.py` (lazy import) -> sole importer `src/memory/manager.py:52,136` (default `Qwen3Embedder()` when none injected). `DeterministicStubEmbedder` is defined in `manager.py:67`, imported by `tests/integration/test_manager.py`, `test_warm_context.py`. Embed dim (`DEFAULT_EMBED_DIM`=1024) is baked into the Qdrant collection definition.

### Perception / STT / diarization
`src/perception/` has only `.gitkeep`. No import anywhere. Residual contract coupling: `DiarizedTranscript` (`src/contracts/diarized_transcript.py`) -> `src/contracts/__init__.py`, `src/memory/extractor.py` (4 extractors), `tests/unit/test_contracts.py`, `tests/fixtures/diarized_transcript.json`. `SessionBundle` -> `contracts/__init__.py`, `test_contracts.py`, fixture; `src/memory/schemas/session.py` is documented as its persisted counterpart (docstring only, no import).

### Egress / "proxy"
There is no network-layer egress proxy (T3.10 was never built: CLAUDE.md T3.9 row). App-layer egress is `src/security/scrubber.py:213 execute_external` (httpx at `:65,262`), importers: `src/tools/integrations/{calendar,drive,gmail}.py`, `src/contracts/tool_intent.py` (types), tests `tests/adversarial/test_scrubber.py`, `tests/unit/test_scrubber_gemini.py`, `test_entity_substitution.py`, `test_kill_switch.py`, `test_gmail_drive_reminders.py`, `tests/eval/invariants/test_inv1*`, `test_inv2*`. `src/security/{audit_log,kill_switch,pii_detector}.py` are imported only by `scrubber.py` (plus tests). `ALLOWED_TOOLS` (`scrubber.py:83`) is checked against the live `ToolRegistry` by `test_gmail_drive_reminders.py::test_allowed_tools_matches_the_real_tool_registry`, so adding or removing a tool breaks that test until both change together.

### Flutter / mobile
`mobile/lib/.gitkeep` only. No importers. `grep -ri 'flutter' src tests docker-compose.yml .gitignore` -> nothing.

### Apache AGE
Image + init.sql only (see AUDIT_DISPOSITION finding 1). No Python importer; removal changes Docker image only. Risk: persisted `postgres_data` volume holds an AGE catalogue schema (`ag_catalog`) that the new image lacks, so existing dev volumes must be recreated (no production data exists on this branch: not verified, dev-only per CLAUDE.md).

### FastAPI / JWT / CORS
`src/api/{main,auth,health}.py`, `middleware/auth.py`, `routes/{chat,memory,tasks}.py`; `config/settings.py:49-65`; tests `test_auth.py`, `test_chat.py`, `test_ws.py`, `test_health.py`. `src/api/main.py` is the only production caller of `build_graph`; nothing else instantiates the orchestrator, so removing the API strands the orchestrator until a gateway exists (tests still call `build_graph` directly, so the suite is not affected).

### APScheduler
`src/tools/integrations/reminders.py` only; test `tests/unit/test_gmail_drive_reminders.py` (job firing). mypy override in `pyproject.toml`.

### langgraph / langchain_core
`src/orchestrator/graph.py` only. `langchain_core` undeclared transitive.

## 2. Internal coupling hot spots
- `MemoryManager` constructor takes `(pg_session, qdrant, redis, embedder)` (`manager.py:127-135`). Every consumer constructs it with all three stores: `api/routes/memory.py:41`, tests, harness. Changing the constructor is the pivot point of P1.3.
- `GraphDependencies` (orchestrator) holds `memory_manager`, `approver`, `tool_registry`, `llm_client` (`graph.py`); tests inject fakes.
- `Approver` is called from `graph.py` act node and from three test files; two of them are protected/eval.
- `FakeLLMClient` (conftest) keys behaviour off schema field names (`approved`, `tool`); changing the approver schema silently changes the fake's output.
- `personality/*` ORM models inherit `src.memory.schemas.base.Base` (`agent_character.py`, `user_profile.py`), so tables are created by `Base.metadata.create_all` in conftest; they have **no Alembic revision**: `migrations/versions/0dc85954088a_*.py` creates only graph_node, session, person, graph_edge, commitment, decision, while `agent_stance`, `user_trait`, `trait_evidence` (`src/personality/*.py` `__tablename__`) exist only via `create_all`. P1.2 must add them.

## 3. Removal plan (one PR per step, each leaves suite green)

Principle: add the replacement, migrate callers, then delete. Steps are acyclic: each depends only on earlier ones. "T" = test files touched. Protected = `tests/eval/**` (CLAUDE.md rule 4; needs human OK; recommend that approval be gathered once now).

| # | PR | Prerequisite | Changes | Tests touched | Suite stays green because |
|---|---|---|---|---|---|
| R0 | Drop dead deps and dirs | none | remove `arq` from `pyproject.toml`; delete `mobile/`, `src/perception/`, `src/personality/models/`, `infra/nginx`, `infra/scripts`, `.claude/settings.json.graphify-bak` (empty/dead); declare `langchain-core` | none | zero importers |
| R1 | Compose: drop `vllm` service/volume | none | `docker-compose.yml`; `.env.example` `VLLM_*`; keep `llm_*` settings | none | tests mock the client (`test_llm_client.py`) or use fakes |
| R2 | CI repair | none | `.github/workflows/ci.yml`: trigger `pivot`, add pgvector service (after R5 for the DB image), keep Redis/Qdrant services until R4/R6 | none | CI only |
| R3 | Add pgvector storage (additive) | R2 | new Alembic revision (extension + episodic table with `vector(1024)` + provenance), swap postgres image to pgvector, add `pgvector` dep, new `PgVectorEpisodicStore` behind an interface; `MemoryManager` accepts either store | new `tests/integration/test_pgvector_store.py` | nothing removed |
| R4 | Redis out of `MemoryManager` | R3 not needed | delete warm-context methods and `SessionState` (INV-4/Mode A retired); `retrieve_memory` node queries directly or via an in-process LRU; constructor drops `redis` param; remove `src/db/redis.py`, health check, `routes/memory.py`/`main.py` wiring, `redis` dep, compose service, `redis_*` settings | delete `tests/integration/test_warm_context.py`, `tests/conftest.py` (`redis_client`), edit `test_manager.py`, `tests/unit/test_health.py`, `tests/unit/test_graph.py`; **protected**: `tests/eval/locomo/harness.py` (drop `Redis`), `tests/eval/invariants/test_inv4_*.py` (delete, optional here) | `get_warm_context` has exactly one production caller (`graph.py:117`) |
| R5 | Qdrant out of `MemoryManager` | R3 | episodic write/query use pgvector store; constructor drops `qdrant`; remove `src/db/qdrant.py`, `qdrant-client`, compose service+volume, `qdrant_*` settings, `EPISODIC_COLLECTION` | `tests/conftest.py` (`qdrant_client`), `test_manager.py`; **protected**: `tests/eval/locomo/harness.py` (collection-per-sample isolation (AD-20) must become a filter or per-sample temp table) | P1.3 acceptance says harness still runs (score may be low) |
| R6 | Compose/image: AGE -> pgvector image | R3 | `infra/docker/postgres/Dockerfile`, `init.sql` (drop `age`, `ag_catalog`); recreate dev volume | none | no Python uses AGE |
| R7 | New `Policy`/`Approvals` (additive) | none | `src/policy/`, `src/approvals/`, typed tool risk metadata; orchestrator `act` node switches from `Approver` to policy | new policy unit tests; edit `tests/unit/test_risk_gating.py`, `test_graph.py` | Approver still present but unused |
| R8 | Delete Approver | R7 | remove `src/orchestrator/approver.py`; `FakeLLMClient` `approved` branch | delete `tests/unit/test_approver.py`, `tests/adversarial/test_approver_injection_selfcheck.py`; **protected**: delete/replace `tests/eval/injection/red_team.py` with >=100-case policy red team | graph no longer imports Approver |
| R9 | `llm` module consolidation | R7 | rename `VLLMClient` -> OpenAI-compatible adapter; add audit/scrub/spend-cap/kill switch; remove `guided_json`; move scrubber PII + audit + kill switch under it; delete Gemini `generateContent` in `execute_external`, `ALLOWED_TOOLS`, `gmail send` | `tests/unit/test_llm_client.py`, `test_scrubber_gemini.py`, `test_gmail_drive_reminders.py`, `test_kill_switch.py`, `tests/adversarial/test_scrubber.py`, `test_entity_substitution.py`; **protected**: `tests/eval/invariants/test_inv1*` (rewrite to "one module owns LLM calls"), `test_inv2*` (delete or retain) | tools stop importing `execute_external` in R10 first, or R9 and R10 merge (see ordering note) |
| R10 | Tool rewrite | R9 interface only | calendar/gmail(drafts only)/drive/notion on real clients; reminders/tasks persisted | `test_tool_discovery.py`, `test_gmail_drive_reminders.py` | tools stop importing `execute_external` |
| R11 | Remove HTTP API | gateway CREATE merged (orchestrator entry exists) | delete `src/api/{auth,main}.py`, `middleware/`, `routes/`; keep `health.py` minimal or replace; drop `fastapi`, `uvicorn`, `pyjwt`, JWT/CORS settings | delete `test_auth.py`, `test_chat.py`, `test_ws.py`; edit `test_health.py` | the tests deleted are the only importers |
| R12 | Embedder optionality | R5 | `sentence-transformers` to optional extra, default behind `Embedder` protocol | `test_manager.py` stubs only | lazy import already |
| R13 | Diarized transcript -> transcript/event | ingest CREATE | rename/replace `DiarizedTranscript`, `SessionBundle`; update extractor, `session` schema/migration | `test_contracts.py`, fixtures | golden-file test updated in same PR |
| R14 | Final cleanup | all | `NOTICE`, pre-commit mypy deps, `CLAUDE.md`/`MEMORY.md` sync, rename INV-5 test | none | docs only |

Ordering notes:
- R9 vs R10 cycle risk: tools import `execute_external`; the `llm` module cannot drop it until tools stop calling it, and tools cannot be rewritten against real clients without the R13/R4 allowlist. Resolution: in R9 keep `execute_external` as a thin deprecated shim (raises unless `LEGACY_EGRESS=1`) until R10 deletes it; or merge R9+R10. This keeps acyclic order R9 -> R10 -> R9b (delete shim).
- R4 and R5 are independent after R3 and can be reordered. Doing R4 first is lower risk (single production caller).
- R11 must come after a Telegram gateway exists, or the repo temporarily has no way to run the agent, though no test fails.
- Protected-file edits (R4, R5, R8, R9) all need one batch human approval.

## 4. Risks
- Embedding dimension/model choice affects the pgvector column type (R3); a later change needs a re-embed migration.
- LoCoMo harness semantic change when dropping the collection-per-sample isolation (AD-20) may change baseline numbers; do not compare to the stale 6.4%.
- `uv.lock` regeneration will drop torch and friends; verify nothing else needs numpy transitively.
- Not measured here: whether the A1 (ARM64) VM can run the AGE or pgvector images and the 0.6B embedder within 12 GB.
