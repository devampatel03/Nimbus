# TARGET_DESIGN (P0.4 + P0.5) — Nimbus Lite

Author: architect. Date: 2026-10-24. Branch `pivot` @ `cf8ec01`.
Inputs: `00_PIVOT_BRIEF.md` (normative), `01_SDLC_PLAN.md`, `reports/AUDIT_BASELINE.md`, `reports/AUDIT_DISPOSITION.md` (cited as **AD§n**), `reports/COUPLING.md` (cited as **CP§n**).
ADRs: `docs/decisions/0001`–`0015`. Each deviation from the Brief has one. This file only covers interfaces and ordering. Implementations come from the specialist tasks in §9.

## 0. External facts this design relies on (searched 2026-10-24)

| Fact | Consequence here | Source |
|---|---|---|
| Oracle Always Free A1 = **2 OCPU / 12 GB** (halved from 4/24, docs changed ~2026-06-15, enforced ~2026-08-18) | Brief's assumption holds. Size for 2 vCPU. | [terminalbytes](https://terminalbytes.com/oracle-cloud-free-tier-changes-2026/), [linuxiac](https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/), [Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm) |
| `pgvector/pgvector:pg16` is multi-arch (amd64 + arm64) | Fixes AD§0.1's open ARM risk. ADR-0001. | [Docker Hub tags](https://hub.docker.com/r/pgvector/pgvector/tags) |
| Telegram bots can download files of at most **20 MB** (`getFile`). `callback_data` is **1–64 bytes**. Message length is 4096 chars. An update is confirmed by calling `getUpdates` with a higher offset. | Voice and `.txt` caps are ≤ 20 MB. The approval token has to fit in 64 bytes (ADR-0009). Offset handling in §4.1. | [Bot API](https://core.telegram.org/bots/api), [PTB InlineKeyboardButton](https://docs.python-telegram-bot.org/telegram.inlinekeyboardbutton.html), [conferbot limits](https://www.conferbot.com/limits/telegram) |
| **`gmail.compose` allows `drafts.send`. Gmail has no drafts-only scope.** | R6 cannot be enforced by scope alone. ADR-0007 (needs a human decision). | [Google scopes](https://developers.google.com/identity/protocols/oauth2/scopes), [Zenn write-up](https://zenn.dev/alma65/articles/gmail-draft-only-scope?locale=en) |
| OAuth apps in "Testing" get refresh tokens that expire after 7 days. "In production" tokens live until revoked or unused for about 6 months. Unverified apps show a warning and are capped at 100 users. | R15 holds. The one-time warning click is a human step (H4). | [unipile](https://www.unipile.com/google-oauth-refresh-token/), [dev.to](https://dev.to/ko-hi/googles-oauth-testing-mode-expires-refresh-tokens-in-7-days-publish-the-consent-screen-before-24hm) |
| Gemini's OpenAI-compat endpoint takes `input_audio` in chat (ogg reported to work, unverified). OpenAI `/audio/transcriptions` caps files at 25 MB, and its ogg support is documented inconsistently. | Voice transcription is an adapter capability, checked at the bake-off. ffmpeg transcode is the fallback (ADR-0003). | [Gemini OpenAI compat](https://ai.google.dev/gemini-api/docs/openai), [OpenAI STT guide](https://developers.openai.com/api/docs/guides/speech-to-text) |

## 1. Target `src/` layout

One process: `python -m src.app`. It runs three asyncio tasks (gateway poller, job worker, scheduler tick) and talks to one Postgres.

```
src/
  app/            __main__.py (wire + run loops, signal handling), container.py (DI: builds real or fake impls)
  core/           clock.py (Clock protocol, IST helpers), ids.py, logging.py (scrubbing log filter, INV-7), errors.py
  net/            http.py  — THE ONLY importer of httpx. Service enum -> host allowlist (R13). [new]
  llm/            THE ONLY importer of an LLM SDK (INV-1/R4). [MODIFY src/llm/client.py]
                  adapter.py (OpenAI-compatible), types.py, guard.py (kill switch + spend cap + scrub + audit),
                  scrub.py (moved from security/pii_detector.py + secret/canary patterns), pricing.py,
                  embeddings_api.py (API embedder fallback), fake.py (recorded-fixture FakeLLM, compromised mode)
  gateway/        telegram.py (Gateway impl over net), render.py (escaping, chunking, no previews), commands.py (/panic /resume /export ...) [new]
  ingest/         classify.py (source -> lane), text.py, voice.py, document.py -> contracts.Event [new]
  lanes/          data.py (UntrustedText, DataBlock, render), quarantine.py (schema extraction via llm, no tools/memory), schemas.py [new]
  orchestrator/   loop.py (plain async tool loop, ADR-0006), context.py (was context_builder.py), exposure.py (<=12 tools), dispatch.py (policy -> approvals -> tool) [MODIFY]
  policy/         engine.py (pure decide()), rules.py (table), counters.py [new; replaces orchestrator/approver.py]
  approvals/      service.py (HMAC token, expiry, single-use, TOTP), cards.py (exact-payload render) [new]
  tools/          base.py (Tool protocol + ActionSpec), registry.py (KEEP discovery), integrations/{calendar,gmail_draft,drive_read,notion,reminders,tasks,memory_recall}.py
  integrations/   google/{auth,calendar_api,gmail_api,drive_api}.py, notion/api.py — thin REST clients over net (no Google/Notion SDKs, ADR-0004) [new]
  memory/         store.py (MemoryStore over Postgres+pgvector), graph.py (KEEP GraphRepository), retriever.py (KEEP scoring),
                  embedder.py (KEEP Qwen3Embedder, now batch/async), extractor.py (KEEP, retyped to Event), schemas/ (KEEP + event, chunk)
  personality/    KEEP agent_character.py, user_profile.py, prompt_assembler.py
  scheduler/      jobs.py (Postgres SKIP LOCKED queue), runner.py, budget.py (interruption budget, quiet hours), proactive.py (W5) [new]
  contracts/      event.py, transcript.py (speaker-optional), tool_io.py (RiskClass kept), memory_query.py (KEEP), user_profile_slice.py (KEEP), proactive_candidate.py (KEEP)
  db/             postgres.py (KEEP), crypto.py (column encryption helper) [new]
REMOVED: src/api/ (ADR-0012), src/db/{redis,qdrant}.py, src/security/scrubber.py, src/orchestrator/approver.py, src/perception/, src/workers/ (folded into scheduler/), src/personality/models/
```

The `src/security/` contents end up as follows. `pii_detector.py` moves to `llm/scrub.py`. `kill_switch.py` and `audit_log.py` move to `llm/guard.py` and become Postgres-backed. The package is then deleted, so there is one obvious place for each control.

**Settings.** Each package defines its own `BaseSettings` subclass with an env prefix (`src/gateway/settings.py` uses `TELEGRAM_`, and so on). `config/settings.py` keeps only DB settings. This lets parallel tasks avoid sharing one settings file (§9 ownership).

**Dependencies.**
- Final runtime deps: `openai`, `httpx`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pgvector`, `pydantic(-settings)`, `cryptography`, `pyotp`, `python-dateutil` (RRULE).
- `sentence-transformers` becomes an optional `[local-embed]` extra.
- Dropped: `redis`, `qdrant-client`, `arq`, `apscheduler`, `langgraph`, `fastapi`, `uvicorn`, `pyjwt`.
- No Telegram, Google or Notion SDKs (ADR-0004).

## 2. Protocols (stubs for `src/**/protocols.py`; signatures are normative, bodies are not)

```python
# ---------- src/llm/types.py + src/llm/protocols.py ----------
class Lane(StrEnum):
    TRUSTED = "trusted"; QUARANTINE = "quarantine"; SYSTEM = "system"   # SYSTEM = scheduler/extraction, no user turn

class Purpose(StrEnum):
    CHAT = "chat"; QUARANTINE_EXTRACT = "quarantine_extract"; TRANSCRIBE = "transcribe"
    MEMORY_EXTRACT = "memory_extract"; FRICTION = "friction"; EMBED = "embed"; EVAL = "eval"

class LLMRefused(Exception):
    """Raised BEFORE any network I/O. reason in {kill_switch, spend_cap, scrub_blocked, lane_violation}."""
    reason: str

class LLMClient(Protocol):
    """The only path to a model (INV-1, R4). Every call, in order: kill-switch check -> lane check
    (QUARANTINE => tools == [] and no memory-tagged messages) -> scrub (secrets/canaries hard-fail,
    INV-7) -> spend-cap pre-check on estimated cost (R5) -> audit row written -> provider call
    (via net.Service.LLM only) -> audit row completed with usage+cost. Fails closed: any guard error
    raises LLMRefused and makes no provider call. Retries 429/5xx with bounded backoff (<=3) inside one
    audited call."""

    async def chat(self, req: "ChatRequest") -> "ChatResult":
        """One completion. `req.tools` -> OpenAI tool-calling; `req.response_schema` -> JSON mode with
        client-side Pydantic validation (one repair retry, then error). Never streams (Telegram
        sends whole messages)."""

    async def transcribe(self, audio: bytes, *, mime_type: str, language_hint: str | None = None) -> "Transcription":
        """Speech->text for user voice notes (<=20 MB, Telegram cap). Provider-specific path is hidden
        behind the adapter (chat input_audio or /audio/transcriptions); transcodes with ffmpeg only if the
        chosen provider rejects audio/ogg (ADR-0003). Audited and spend-capped like chat()."""

    async def mode(self) -> "LLMMode":
        """NORMAL | READ_ONLY (spend cap hit today) | KILLED (/panic). Read from Postgres (survives reboot)."""

class ChatRequest(BaseModel):          # frozen, extra=forbid
    purpose: Purpose; lane: Lane
    messages: list["ChatMessage"]      # content is str; DataBlock-rendered untrusted text only (INV-6)
    tools: list["ToolSpec"] = []       # <= 12 (R16), enforced here as well as in orchestrator.exposure
    response_schema: type[BaseModel] | None = None
    max_output_tokens: int = 800; temperature: float = 0.3
    turn_id: UUID | None = None        # audit correlation

class ChatResult(BaseModel):
    text: str | None; tool_calls: list["ToolCallRequest"]; parsed: BaseModel | None
    usage: "Usage"; cost_inr: Decimal; audit_id: UUID

# ---------- src/memory/protocols.py ----------
class Embedder(Protocol):
    """Text -> vectors. Local (Qwen3-Embedding-0.6B, CPU, batch, run in a thread) or API (lives in
    src/llm/embeddings_api.py so it is audited/spend-capped). `model_id` and `dim` are persisted on every
    chunk row so a model change is a detectable re-embed migration, never a silent mismatch."""
    model_id: str
    dim: int
    async def embed(self, texts: Sequence[str], *, kind: Literal["query", "document"]) -> list[list[float]]:
        """Batch embed. `kind` selects Qwen3's query instruction prefix. Len(out) == len(texts); unit-norm."""

class MemoryStore(Protocol):
    """Postgres+pgvector. Append-only: content columns are immutable (DB trigger + AST test, INV-3).
    Every row carries provenance: source_event_id, lane, untrusted, confidence (R7, provenance_coverage=1.0)."""
    async def record_event(self, event: "Event") -> UUID:
        """Idempotent on event.source_key (e.g. 'tg:update:123'); returns existing id on replay."""
    async def add_chunks(self, event_id: UUID, chunks: Sequence["ChunkIn"]) -> list[UUID]:
        """Embeds (via injected Embedder) and stores episodic chunks; inherits lane/untrusted from the event."""
    async def assert_fact(self, fact: "FactIn") -> UUID:
        """Insert; if a current fact exists for (subject, predicate), supersede it (valid_to/invalidated_at
        on the old row only). Untrusted facts are stored with promoted=false and are NOT returned by
        search() unless include_unpromoted=True (R8)."""
    async def retract(self, fact_id: UUID, *, reason: str, source_event_id: UUID) -> None:
        """'Forget that' = supersede with a tombstone row; evidence retained (UAT 19)."""
    async def search(self, q: "MemoryQuery") -> list["MemoryHit"]:
        """Hybrid: pgvector cosine + recency/importance rerank (retriever.py). q.as_of enables bi-temporal
        reads. Each hit carries untrusted: bool; callers must render untrusted hits as DataBlocks."""
    async def facts_as_of(self, subject: str, at: datetime) -> list["Fact"]: ...
    async def review_queue(self, *, since: datetime) -> list["ReviewItem"]:
        """Untrusted/unpromoted facts, commitment candidates, contradictions — weekly review input."""
    async def promote(self, item_id: UUID, *, approval_id: UUID) -> None:
        """Only callable with a consumed user approval id (never from model output, INV-5)."""

# ---------- src/gateway/protocols.py ----------
class Gateway(Protocol):
    """User channel. Telegram today; PWA later implements the same protocol. Single user: no chat ids
    in the API — the impl is bound to the one allowlisted user (R2). Non-allowlisted updates are dropped
    silently and logged (R2) before they are yielded."""
    def inbound(self) -> AsyncIterator["InboundUpdate"]:
        """Long-poll. Each update is persisted (unique update_id) BEFORE the offset is advanced in Postgres,
        so restart neither loses nor duplicates (R1). Yields messages, voice, documents, callbacks."""
    async def send_text(self, text: str, *, reply_to: "MessageRef | None" = None) -> list["MessageRef"]:
        """Escapes, strips markdown images/non-allowlisted URLs (P4.3), chunks to 4096, previews off,
        respects ~1 msg/s and 429 retry_after."""
    async def send_approval(self, card: "ApprovalCard") -> "MessageRef":
        """Message with exact payload text + Approve/Reject buttons whose callback_data is the 64-byte token."""
    async def close_approval(self, ref: "MessageRef", *, outcome: str) -> None: ...
    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...
    async def fetch_file(self, file: "FileRef", *, max_bytes: int) -> bytes:
        """Refuses before download if file.size > max_bytes (<= 20 MB, Telegram cap)."""

# ---------- src/tools/base.py ----------
class ActionSpec(BaseModel):
    name: str; description: str
    args_model: type[BaseModel]              # extra=forbid; no free 'url' args anywhere (R13)
    risk: "RiskClass"                         # READ | WRITE_INTERNAL | WRITE_EXTERNAL | DESTRUCTIVE
    returns_untrusted: bool                   # email bodies, docs, invite descriptions -> True
    idempotent: bool                          # create ops take ToolContext.idempotency_key
    recipients: Callable[[BaseModel], list[str]] | None = None   # external recipients -> TOTP step-up

class Tool(Protocol):
    """A capability. Tools never call the LLM, never read memory, never import httpx (they get a
    service-scoped net client in ToolContext). That kills the tools->execute_external cycle (ADR-0005)."""
    name: str
    actions: Mapping[str, ActionSpec]
    async def preview(self, action: str, args: BaseModel, ctx: "ToolContext") -> "ActionPreview":
        """Resolve to the exact effect (absolute Asia/Kolkata datetimes, recipients, body) with no side
        effect. The approval card shows this and the executed payload is this — byte-identical."""
    async def run(self, action: str, args: BaseModel, ctx: "ToolContext") -> "ToolOutput":
        """Execute. ToolOutput = data (system-generated structured fields: ids, times) + untrusted
        (list[UntrustedText], external free text) + user_summary. Raises ToolError (sanitised, no tokens)."""

# ---------- src/policy/protocols.py ----------
class Decision(BaseModel):
    verdict: Literal["allow", "require_approval", "deny"]
    step_up_totp: bool = False; rule_id: str; reason: str

class Policy(Protocol):
    """Deterministic, pure, table-driven (INV-5). No I/O, no LLM; counters/flags come in via ctx.
    Rules (ADR-0009): mode KILLED -> deny all effects; READ_ONLY -> deny non-READ; READ -> allow;
    WRITE_INTERNAL -> allow if not tainted else require_approval; WRITE_EXTERNAL -> require_approval
    (always); DESTRUCTIVE -> require_approval + TOTP; external recipient not in allowlist -> + TOTP;
    rate limit exceeded -> deny; unknown tool/action -> deny."""
    def decide(self, call: "ProposedCall", ctx: "PolicyContext") -> Decision: ...

# ---------- src/approvals/protocols.py ----------
class Approvals(Protocol):
    """User-only approval of an exact frozen payload. Token = base64url(action_id[12B] || HMAC-SHA256(key,
    action_id || sha256(payload) || expiry)[:16B]) -> 'ap:' + 38 chars <= 64 bytes. Key from env (R12)."""
    async def request(self, call: "ProposedCall", preview: "ActionPreview", *, step_up: bool,
                      ttl: timedelta = timedelta(minutes=10)) -> "PendingAction": ...
    async def verify_callback(self, *, from_user_id: int, data: str, now: datetime) -> "CallbackVerdict":
        """Rejects: wrong user, bad MAC, expired, already used, payload hash mismatch. Constant-time compare."""
    async def submit_totp(self, action_id: UUID, code: str, *, now: datetime) -> "CallbackVerdict": ...
    async def consume(self, action_id: UUID) -> "ApprovedAction":
        """Atomic single-use (UPDATE ... WHERE status='pending' RETURNING). Returns the frozen payload that
        dispatch executes WITHOUT re-entering the model."""

# ---------- src/scheduler/protocols.py ----------
class Scheduler(Protocol):
    """Postgres jobs table, claimed with FOR UPDATE SKIP LOCKED (ADR-0008). One runner loop ticks every
    5 s (R11 <= 60 s). Missed jobs (downtime) run on start-up; recurring jobs reschedule from RRULE."""
    def register(self, kind: str, handler: "JobHandler") -> None: ...
    async def schedule(self, job: "JobSpec") -> UUID:
        """JobSpec: kind, run_at (aware UTC), payload (JSON), dedupe_key (unique), rrule | None,
        proactive: bool (counts toward the <=6/day budget + quiet hours; user-set reminders do not, ADR-0011)."""
    async def cancel(self, job_id: UUID) -> bool: ...
    async def run_due(self, now: datetime, *, limit: int = 20) -> int:
        """Claim due jobs, run handlers, mark done/failed (bounded retries), return count. Fake-clock testable."""
```

Supporting protocols (small, also stubbed): `core.clock.Clock.now() -> datetime`, `net.http.EgressClient.request(service, method, path, ...)` (host comes from `Service`, never from the caller), `lanes.quarantine.Quarantine.extract(text: UntrustedText, schema: type[T]) -> T`.

## 3. `llm` module absorbs `scrubber.execute_external` (ADR-0003) and the INV-1 test rewrite

| Today (`src/security/scrubber.py`) | Target |
|---|---|
| `execute_external` making a Gemini `generateContent` call for tool "execution" | **Deleted.** The model never executes tools. Tools call their APIs directly via `net` (ADR-0005). |
| `ALLOWED_TOOLS` with `gmail.send` | Deleted. The tool registry and `ActionSpec` are the single source. Gmail has no send action (R6). |
| PII hard-fail (`pii_detector`) | Moves to `llm/scrub.py`. The new posture blocks secrets, tokens and canaries (INV-7). Card, Aadhaar and OTP-like numbers are redacted. Names and emails are **not** blocked, because the Brief accepts the provider seeing plaintext. |
| Entity substitution (`_SessionRefStore`, AD-12) | Deleted (ADR-0003). It breaks the model's reasoning and gives no real privacy under the Brief's stance. |
| Audit JSONL before send | `llm_audit` Postgres table, append-only. It stores metadata and token cost always, plus the redacted payload in an encrypted column with 14-day retention. |
| In-memory kill switch | `system_flag` row. `/panic` and `/resume` are handled in `gateway/commands.py` before any LLM call (UAT 13). |
| (none) | Spend ledger: today's sum over `llm_audit.cost_inr` plus a pre-call estimate, checked against the cap. The cap moves the mode to READ_ONLY and sends a Telegram alert (R5). Reminders keep firing because they need no LLM. |

**New INV-1 test** (`tests/eval/invariants/test_inv1_single_llm_module.py`, which replaces `test_inv1_single_egress_point.py`). It has five parts:
1. AST import scan. `openai|anthropic|google.genai|google.generativeai|litellm|mistralai|cohere|groq|together|huggingface_hub.inference` may only be imported under `src/llm/`.
2. Transport scan. `httpx|requests|aiohttp|urllib3|urllib.request|http.client|httplib2|websockets|socket` may only be imported under `src/net/`. This closes the `openai` and `httplib2` blind spots from AD§0.6 and AD-25.
3. `Service.LLM` may only be referenced under `src/llm/`.
4. Runtime check with a fake transport: the kill switch, the spend cap and a scrub hit each cause **zero** transport calls. Every successful call has a completed audit row.
5. Sentence-transformers runs locally and is allowed in `src/memory/embedder.py`. Its HF downloads happen at image build, and at runtime `HF_HUB_OFFLINE=1` is set and asserted.

The **R13 test** (`tests/unit/net/test_r13_egress_allowlist.py`) covers three things:
- `Service` maps to exactly this set: `api.telegram.org`, the LLM host from settings, `oauth2.googleapis.com`, `www.googleapis.com`, `gmail.googleapis.com`, `api.notion.com`, the healthcheck host and the backup host.
- Any other host raises.
- No `ActionSpec.args_model` has a URL-typed field (no generic fetch tool).

## 4. Data flow per use case

Notation: `[T]` = trusted lane, `[Q]` = quarantine, `[D]` = data block. `taint` is the per-turn flag (ADR-0010).

### 4.1 Text message (dictated or typed)
1. `gateway.inbound`: `getUpdates(offset)`, allowlist check (anything else is dropped and logged), then `INSERT inbound_update ON CONFLICT DO NOTHING`, then the offset is stored in `system_flag`.
2. `ingest.classify`: user-authored text goes to `[T]`. A message with `forward_origin` goes to `[Q]` (someone else wrote it).
3. `memory.record_event`, then a `turn` job is enqueued.
4. `orchestrator.loop`:
   - Context: character card, stance log, traits with confidence ≥ 0.6, `memory.search`.
   - Untrusted hits are rendered as `[D]` and set `taint`.
   - `exposure.select` picks ≤ 12 tools.
   - `llm.chat` with tools, iterating up to N=5 within the token budget.
5. Each tool call goes through `dispatch`, which runs `policy.decide`:
   - `allow`: `tool.run`.
   - `require_approval`: §4.5.
   - `deny`: a tool-error message goes back to the model.
6. The reply goes out through `gateway.send_text`. The update is then marked done.
7. Post-turn, a `memory_extract` job is enqueued (SYSTEM lane). It produces facts and commitment candidates with provenance.

A crash between steps 1 and 6 re-runs the turn from the persisted update. Tool effects are idempotent by key, so the worst case is a duplicate reply, never a duplicate effect.

### 4.2 Voice note (user speaking to the bot)
1. Steps 1–2 as in 4.1. The `voice` (ogg/opus) message is classified `[T]`, because the user is the author.
2. `gateway.fetch_file(max_bytes=20 MB)`, then `llm.transcribe` (audited and spend-capped).
3. The transcript event is stored with `kind=voice` and the original `file_unique_id`. The audio is not retained.
4. Continue as 4.1 step 4. The bot echoes a short "Heard: …" line so mis-transcription is visible.

### 4.3 Pixel Recorder transcript (`.txt` document)
1. The document is classified `[Q]`, because it records third-party speech.
2. `fetch_file` is capped at 1 MB (configurable, ≤ 20 MB). Decoding tries utf-8, then utf-8-sig, utf-16 (BOM), then cp1252, with NUL and control characters stripped. Oversized files are refused politely.
3. Chunk at about 2k tokens. For each chunk, `quarantine.extract(UntrustedText, TranscriptDigest)` makes an LLM call with `lane=QUARANTINE`, no tools, no memory, and a fixed system prompt. The schema fields are a summary (≤ 600 chars), candidate commitments (≤ 10 × 200 chars) and decisions.
4. Results are stored with `untrusted=true, promoted=false`. The chunks are embedded with `untrusted=true`.
5. The `[T]` reply shows the summary as `[D]`. Each candidate commitment gets its own Confirm button, which goes through the WRITE_INTERNAL path under taint, so approval is required. A confirmed commitment is stored with `lane=untrusted` and `approval_id`.

### 4.4 Email read ("what did Priya send about the lease?")
1. `[T]` turn. The model calls `gmail_draft.search` (READ, `returns_untrusted=True`).
2. `ToolOutput.data` holds only system-generated fields: message id, thread id, internal date and label ids. Everything the sender controls goes in `ToolOutput.untrusted`, with HTML and hidden text stripped. That includes the `From` display name **and** address (both spoofable), the subject and the body.
3. `dispatch` routes the untrusted output under the ADR-0010 rule (Proposed, Q7):
   - **Every email body**, whatever its length, goes to `quarantine.extract(EmailDigest)`.
   - Short header fields (`from`, `subject`) are validated against the deterministic `EmailHeaderFields` schema (see §5 for what that does), then rendered as `[D]`.
   - `taint=True` for the rest of the turn and for the next 3 turns (window in policy table).
4. The model sees only `[D]` blocks. If injected text makes it propose `calendar.create` or `gmail_draft.create`, the call is WRITE_EXTERNAL and needs approval; a tainted WRITE_INTERNAL also needs approval. The approval card shows the exact payload, so the user sees the attack (UAT 8).

### 4.5 Calendar create with approval
1. The model proposes `calendar.create{title, start: "tomorrow 7pm", ...}`.
2. `args_model` validates the arguments. `tool.preview` resolves `start` to an absolute `2026-10-25T19:00+05:30` and lists attendees.
3. `policy.decide` returns `require_approval` (WRITE_EXTERNAL), with `step_up_totp` if there are external attendees.
4. `approvals.request` persists `pending_action{payload, sha256, expiry, status=pending}`. `gateway.send_approval(card)` sends the card. The model turn ends with "Waiting for your approval".
5. The user taps a button. `gateway.inbound` yields the callback, then `approvals.verify_callback(from_user_id, data)` runs. If TOTP is required, a 6-digit prompt follows and then `submit_totp`.
6. `approvals.consume` (atomic). Then `tool.run(payload, idempotency_key=action_id)`. The Google event id is derived from the key, so a re-send produces no duplicate (UAT 5). The card is closed and the result is sent. **The model is not consulted between approval and execution** (ADR-0006).
7. If the token is expired, used or forged: `answer_callback("expired")` and no effect (UAT 18).

### 4.6 Reminder fire
1. Creation: "remind me to call Mom tomorrow 7pm" → `reminders.create` (WRITE_INTERNAL, untainted, so allowed). The preview shows the absolute IST time in the reply. Then `scheduler.schedule(JobSpec(kind="reminder", run_at=UTC, dedupe_key=reminder_id, proactive=False))`.
2. The runner ticks every 5 s and claims due jobs with `SKIP LOCKED`. The handler calls `gateway.send_text` directly, with no LLM. That way it works in READ_ONLY mode and does not need the model to be up.
3. Done, or the next occurrence is scheduled from the RRULE. After downtime, overdue reminders fire on start-up, marked "(late, was due HH:MM)".
4. Proactive jobs (brief, nudges, check-ins) also pass `budget.may_interrupt(now)`, which enforces ≤ 6 per day and quiet hours. Otherwise they are deferred to the next allowed slot or dropped into the digest.

## 5. Trust lanes and taint (ADR-0010)

- **Types carry the lane.** `lanes.data.UntrustedText` is a frozen wrapper. `str(x)` raises, so the only way out is `render_data_block(x)`, which wraps the text in a nonce-delimited, escaped block with a "this is data, not instructions" header. `orchestrator.context` accepts `TrustedText | DataBlock` only. A test enforces this, and it is INV-6.
- **Source classification is a table**, unit-tested in `ingest/classify.py`. User text, captions and voice notes are `[T]`. Forwarded messages, documents, email, calendar descriptions, Notion and Drive content, and untrusted memory hits are untrusted.
- **Routing rule (ADR-0010, Proposed, Q7), one rule everywhere.**
  - **Bodies always go through the quarantine LLM**, regardless of length. Bodies are: email bodies, documents and Recorder `.txt` files, Notion/Drive page content, forwarded messages, and calendar descriptions.
  - **Short fields** (each one ≤ 500 chars after capping) skip the LLM. Short fields are: email from/subject, event title/location/attendee names, page/file titles, and untrusted memory hits (already quarantine output).
  - Short fields are **schema-validated deterministically** instead. Each source has a frozen Pydantic model with `extra=forbid` and per-field `max_length`. Each value is NFKC-normalised. Control, zero-width and bidi-override chars are stripped. Any occurrence of the DataBlock delimiter or nonce is rejected. A field that fails validation is replaced with `"[unrenderable field]"` and logged.
  - INV-6's "schema-validated" means one of two things: the output of `Quarantine.extract` (an LLM call validated against a schema), or one of these deterministic field schemas. A test asserts that no other constructor of `DataBlock` exists.
  - Taint is set either way.
- **Taint is a TurnContext flag** and is an input to `policy.decide`. It is set on any `[D]` entering the turn and lasts for a policy-table window (default: the turn plus 3 turns). It is cleared by `/clear` or by time.

## 6. AGE vs plain Postgres (ADR-0001)

Plain Postgres 16 with `pgvector` (`pgvector/pgvector:pg16`, multi-arch). AGE is never used in code (AD§0.1). The bi-temporal "graph" stays as relational `graph_node`/`graph_edge`, using recursive CTEs if multi-hop is ever needed.

There is one episodic table, `memory_chunk(vector(1024), model_id, dim, untrusted, source_event_id, ...)`, with an HNSW cosine index. Dev volumes have to be recreated, and no production data exists.

## 7. INV renumbering (ADR-0002) — Brief numbering is canonical

| Brief | Meaning | Repo today | Action / test file (all under protected `tests/eval/invariants/`, one batch human OK needed) |
|---|---|---|---|
| INV-1 | single `llm` module, audit/scrub/cap/kill | repo INV-1 (single httpx call site) | rewrite → `test_inv1_single_llm_module.py` (§3); delete old (P1.4c) |
| INV-2 | retired (local-only inference) | repo INV-2 = "no memory in egress payload" (`ToolIntent` schema) | delete `test_inv2_*` along with `ToolIntent` (P1.4c/P2.5b). Number stays **retired**, never reused. |
| INV-3 | append-only, supersession | repo INV-3 (AST scan of `graph.py`) | keep; extend with DB trigger test on `graph_edge`/`memory_chunk`/`commitment` content columns (P1.2c) |
| INV-4 | moot | repo INV-4 skip-stub | delete `test_inv4_*` (P1.3a) |
| INV-5 | policy deterministic, no LLM grants permission | repo INV-5 = provenance | rename existing → `test_r7_memory_provenance.py` (P1.2c); new `test_inv5_policy_deterministic.py`: AST (no `src.llm` import in `src/policy`, `src/approvals`), table tests, compromised-FakeLLM cannot cause an unapproved effect (P3.1) |
| INV-6 | untrusted only as validated DATA | — | new `test_inv6_untrusted_as_data.py` (P3.0) |
| INV-7 | no secrets in logs/prompts/memory/errors | — | new `test_inv7_no_secrets.py`: log filter, llm scrub, ToolError sanitisation, canary sweep of DB (P1.5, extended P4.4) |

CLAUDE.md "Invariants" gets rewritten by docs-memory at the W1 gate to match this table.

## 8. Migration path (ordered; each step leaves `uv run pytest` green)

The principle is **add, migrate callers, delete**. This follows CP§3 with three changes. First, the tools cycle is broken by deleting the intent-builder tools rather than adding a shim (ADR-0005). Second, `net` comes before `llm`. Third, the API goes after the gateway.

| # | Step | Task |
|---|---|---|
| M0 | Delete dead weight: `arq`, `mobile/`, `src/perception/`, `src/personality/models/`, `infra/{nginx,scripts}`, compose `vllm` | P1.0 |
| M1 | CI on `pivot` with a pgvector service | P1.1 |
| M2 | AGE → pgvector image | P1.2a |
| M3 | Alembic baseline: add missing personality tables, plus a migration==ORM test | P1.2b |
| M4 | New tables (`event`, `memory_chunk`, `job`, `llm_audit`, `system_flag`, `pending_action`, `inbound_update`), provenance columns (lane/untrusted/promoted), append-only triggers, `db/crypto.py` | P1.2c |
| M5 | Redis out (warm context deleted, INV-4 deleted) | P1.3a |
| M6 | pgvector `MemoryStore` episodic path, then Qdrant out, LoCoMo harness ported | P1.3b |
| M7 | `src/net` egress client + R13 test (only importer of httpx; scrubber temporarily allow-listed in the transport scan) | P1.4a |
| M8 | `src/llm` guard + adapter + FakeLLM. `VLLMClient` kept as a deprecated alias so the 4 callers stay green. | P1.4b |
| M9 | Delete `execute_external`, `ALLOWED_TOOLS`, calendar/gmail/drive intent-builders, Gemini/entity-sub tests. INV-1 rewritten, INV-2 deleted. `security/` emptied into `llm/`. | P1.4c |
| M10 | Module settings, `.env.example`, log scrubber, INV-7 | P1.5 |
| M11 | Gateway, then app entrypoint, then `src/api` deleted | P2.1a, P2.1b |
| M12 | Plain loop orchestrator with interim `DenyAllWritesPolicy`, then Approver + LangGraph + `ToolIntent` deleted, then real Policy/Approvals swapped in | P2.5a, P2.5b, P3.1, P3.2 |
| M13 | Tools rebuilt on the new protocol over `integrations/*` | P3.3–P3.8 |

Each REMOVE step is its own PR, so it can be reverted cleanly.

## 9. Proposed STATE.md task table (P0.5) — orchestrator copies into `docs/pivot/STATE.md`

Ownership notes:
- These files are **orchestrator-only** (O): `pyproject.toml`, `uv.lock`, `docker-compose.yml`, `CLAUDE.md`, `MEMORY.md`, `STATE.md`, `.claude/**`, `NOTICE`, `.pre-commit-config.yaml`. Tasks request changes to them in their report.
- `tests/conftest.py` is shared, so tasks marked `cf` must not run concurrently with each other.
- `tests/eval/**` is protected. Tasks marked `PROT` need the human's batch OK (open question Q1).

**Diff vs `01_SDLC_PLAN.md`:**
- Done: P0.1–P0.3 and P0.6. P0.4 and P0.5 are delivered by this file.
- Split: P1.2 → a/b/c, P1.3 → a/b, P1.4 → a/b/c, P2.1 → a/b, P2.5 → a/b.
- P1.6 is absorbed into P1.0, P1.4c, P2.1b and P2.5b.
- The quarantine/taint core (old P4.1+P4.2) moves earlier to **P3.0**, because Gmail read (P3.6) and Policy (P3.1) depend on it. P4.1 becomes the red-team expansion of it.
- P3.3 builds the job runner. P5.1 adds proactive jobs on top of it.

Additional ownership rules:
- **Shared-file sequencing.** These files have more than one owning task. The tasks are ordered by deps, so no two of them share a batch. `config/settings.py` goes P1.3a → P1.3b → P1.4c → P1.5. `tests/unit/test_contracts.py` and `src/contracts/__init__.py` go P2.2 → P2.4 → P2.5b → P5.1 → P5.2 (P2.5b removes the `ToolIntent`/`ToolResult` imports and golden-contract cases).
- **P1.4b does not touch `src/security/**`.** P1.4b *copies* the `pii_detector` patterns and the audit and kill-switch logic into `src/llm/{scrub,guard}.py`, Postgres-backed. `scrubber.py` and its tests keep importing the untouched `src/security` modules. P1.4c then deletes `src/security/**` as a unit. The duplication lasts exactly one PR, and no re-export shim is needed. The `src/llm` settings live in `src/llm/settings.py` (module-local, prefix `LLM_`, same env names as today), so P1.4b does not edit `config/settings.py`.

| ID | Title | Agent | Deps | Owns (globs) | Acceptance (SDLC + additions) | Notes |
|---|---|---|---|---|---|---|
| P0.1–P0.3, P0.6 | Audit, coupling, envcheck | repo-auditor/devops | – | docs/pivot/reports/** | per SDLC | done |
| P0.4/P0.5 | Target design + task table | architect | P0.3 | docs/pivot/reports/TARGET_DESIGN.md, docs/decisions/** | ADRs for every deviation; ownership globs disjoint per batch | this file |
| P1.0 | Delete dead weight (M0) | backend | – | mobile/**, src/perception/**, src/personality/models/**, infra/nginx/**, infra/scripts/** (+O: pyproject arq, compose vllm, .env.example VLLM_*) | dirs gone; `arq` gone from lock; suite green | pure deletion |
| P1.1 | CI on pivot: ruff/mypy/pytest + pgvector svc + red-team job + secret scan; **fix the 3 existing `ruff check .` errors** | devops | – | .github/**, scripts/envcheck.py, tests/unit/test_gmail_drive_reminders.py (import order only) | CI green on empty change; `uv run ruff check .` = 0 errors (today 3: envcheck.py:100,116 RUF059; test_gmail_drive_reminders.py:10 I001) | test file is deleted later by P1.4c |
| P1.2a | Postgres image AGE→pgvector | devops | – | infra/docker/postgres/** (+O compose) | `CREATE EXTENSION vector` works; image runs on arm64 (buildx) | recreate dev volume |
| P1.2b | Alembic baseline: personality tables revision + upgrade==ORM test | backend | P1.2a | migrations/**, tests/integration/test_migrations.py | migrations apply from empty; autogenerate diff vs ORM empty | |
| P1.2c | Pivot schema: event/memory_chunk/job/llm_audit/system_flag/pending_action/inbound_update; provenance cols; append-only triggers; db/crypto.py | backend+memory | P1.2b | migrations/**, src/memory/schemas/**, src/db/postgres.py, src/db/crypto.py, tests/integration/test_schema_*.py, tests/eval/invariants/test_inv3*, test_inv5*→test_r7* | INV-3 test incl. DB trigger; provenance_coverage = 1.0; downgrade round-trip | PROT |
| P1.3a | Redis out (M5) | memory | P1.2c | src/memory/manager.py, src/db/redis.py, src/orchestrator/graph.py, src/api/{main,health}.py, src/api/routes/memory.py, config/settings.py (redis_* only), tests/integration/{test_warm_context,test_manager}.py, tests/unit/{test_health,test_graph}.py, tests/conftest.py, tests/eval/locomo/harness.py, tests/eval/invariants/test_inv4* | no `redis` import anywhere; suite green; LoCoMo harness still runs | PROT, cf |
| P1.3b | pgvector MemoryStore episodic + Qdrant out (M6) | memory | P1.3a | src/memory/** (excl. extractor.py, embedder.py), src/db/qdrant.py, **src/api/{main,health}.py, src/api/routes/memory.py, config/settings.py (qdrant_* only), tests/unit/test_health.py**, tests/integration/{test_manager,test_pgvector_store}.py, tests/conftest.py, tests/eval/locomo/** | no `qdrant_client` import (import-lint); suite green; **LoCoMo harness still runs (score may be low; recorded, not gated)** | PROT, cf |
| P1.4a | `src/net` egress client + R13 test | security | P1.1 | src/net/**, tests/unit/net/** | R13 test: non-allowlisted host raises; transport import scan passes (scrubber.py temporarily allow-listed) | parallel with P1.2*/P1.3* |
| P1.4b | `src/llm` guard/adapter/pricing/FakeLLM (R4, R5) | backend | P1.4a, P1.2c | src/llm/**, tests/unit/llm/**, **tests/unit/test_llm_client.py** (moved to tests/unit/llm/), tests/fakes/** | R4, R5 tests (kill switch/spend cap/scrub hit → zero transport calls; audit row per call); **import-lint test: no module outside src/llm imports an LLM SDK** | copies, does not edit, src/security (see rule above) |
| P1.4c | Delete execute_external + src/security/** + intent-builder tools; INV-1 rewrite; INV-2 delete (M9) | security | P1.4b, P1.3b | src/security/**, src/tools/integrations/{calendar,gmail,drive}.py, config/settings.py (gemini_*/cloud_egress_* only), tests/unit/{test_scrubber_gemini,test_entity_substitution,test_kill_switch,test_gmail_drive_reminders,test_tool_discovery}.py, tests/adversarial/test_scrubber.py (retarget → tests/adversarial/llm/test_scrub.py), tests/eval/invariants/test_inv1*, test_inv2* | new INV-1 test green; `src/security` gone; no `send` anywhere in gmail code; 50 PII/secret adversarial cases retargeted at llm.scrub | PROT |
| P1.5 | Module settings pattern, .env.example, scrubbing log filter, INV-7 test | backend | P1.4c | config/**, src/core/**, tests/unit/core/**, tests/eval/invariants/test_inv7* (+O .env.example) | R12 test; INV-7 log-scrubber test | PROT |
| P2.1a | Telegram Gateway (long poll, allowlist, persisted offset/updates, rate limit, chunk/escape, no previews) | bot | P1.4a, P1.2c | src/gateway/**, tests/unit/gateway/**, tests/integration/gateway/** | R1, R2, R3 tests; replay-after-restart test (no lost/duplicate updates) | |
| P2.1b | App entrypoint (one process, 3 loops) + healthcheck CLI + delete src/api + FastAPI/JWT deps | bot | P2.1a, P1.5 | src/app/**, src/api/**, config/settings.py (jwt_*/auth_* only; runs after P1.5), tests/unit/{test_auth,test_chat,test_ws,test_health}.py (+O deps) | `python -m src.app` starts against fakes; healthcheck exits non-zero on stale heartbeat; no fastapi import | |
| P2.2 | Ingest + Event/Transcript contracts; retire SessionBundle/DiarizedTranscript; extractor retyped | bot | P2.1a, P1.4b | src/ingest/**, src/contracts/{event,transcript,session_bundle,diarized_transcript,__init__}.py, src/memory/extractor.py, tests/unit/ingest/**, tests/unit/test_contracts.py, tests/fixtures/{event,transcript,session_bundle,diarized_transcript}* | fixtures for text/voice/recorder; oversized/hostile file tests; golden-contract tests | |
| P2.3 | Embedder: batch/async Qwen3 local + API fallback in llm; measure on arm64 | memory | P1.3b, P1.4b | src/memory/embedder.py, src/llm/embeddings_api.py, tests/unit/test_embedder*.py | embedding dim/migration test; latency measured on target VM class (or flagged "not measured") | ADR-0013 flip criterion; H6 |
| P2.4 | Retrieval rerank + bi-temporal as_of + untrusted filters; LoCoMo re-baseline + provisional-gate ADR | memory | P2.3, P2.2 | src/memory/{store,retriever}.py, **src/contracts/memory_query.py**, src/contracts/__init__.py, tests/fixtures/memory_*.json, tests/unit/test_contracts.py, tests/unit/test_retriever.py, tests/integration/test_retrieval*.py, tests/eval/locomo/** | LoCoMo re-baseline recorded in reports/; provisional gate ADR | PROT |
| P2.5a | Orchestrator plain tool loop + context + exposure (≤12) + interim DenyAllWritesPolicy | backend | P1.4b, P1.3b | src/orchestrator/{loop,context,exposure,dispatch}.py, **src/personality/prompt_assembler.py**, tests/unit/orchestrator/**, tests/unit/test_prompt_assembler.py | transcripts in tests; budget exceeded → graceful message; tool exposure ≤ 12 asserted | |
| P2.5b | Delete Approver, LangGraph, ToolIntent/ToolResult; Tool protocol v2 for tasks/reminders | backend | P2.5a, P1.4c, P2.4 | src/orchestrator/{graph,approver,state,context_builder}.py, src/contracts/{tool_intent,__init__}.py, tests/unit/test_contracts.py (ToolIntent/ToolResult cases), tests/fixtures/{tool_intent,tool_result}*.json, src/tools/{base,registry}.py, src/tools/integrations/{tasks,reminders}.py, tests/unit/{test_approver,test_risk_gating,test_graph}.py, tests/adversarial/test_approver_injection_selfcheck.py, tests/eval/injection/red_team.py, tests/conftest.py | no `langgraph`/`Approver`/`ToolIntent` import; suite green | PROT, cf; red team returns in P4.7 |
| P2.6 | Character card loader + R9 guard | personality | P1.5 | src/personality/agent_character.py, tests/unit/test_agent_character.py, tests/unit/test_r9_character_card.py (+O .claude/hooks/protect-files.sh) | R9 test (marker + not-placeholder + sha256) | card text = H1 |
| P3.0 | Lanes core: UntrustedText/DataBlock, Quarantine.extract, deterministic field schemas, taint in TurnContext, INV-6 | security | P2.5b, P2.2 | src/lanes/**, tests/unit/lanes/**, tests/eval/invariants/test_inv6* | INV-6 test (DataBlock only built by quarantine or field schemas); taint tests | PROT; ADR-0010/Q7 |
| P3.1 | Policy engine + wire into dispatch; INV-5 | security | P3.0 | src/policy/**, src/orchestrator/dispatch.py, tests/unit/policy/**, tests/eval/invariants/test_inv5_policy* | INV-5; ≥ 95% coverage; table-driven tests | PROT |
| P3.2 | Approvals: HMAC token, expiry, single-use, user-only, TOTP, cards | security+bot | P3.1 | src/approvals/**, src/gateway/callbacks.py, tests/unit/approvals/** | forgery/replay/expiry tests; callback from other user ignored; token ≤ 64 bytes | |
| P3.3 | Scheduler job runner + reminders/tasks persisted + IST resolution + catch-up | backend | P2.5b, P2.1b | src/scheduler/{jobs,runner}.py, src/core/timeparse.py, src/tools/integrations/{reminders,tasks}.py, tests/unit/scheduler/** | R11 tests with fake clock; catch-up after downtime; no apscheduler import | |
| P3.4 | Google OAuth one-time loopback flow, encrypted token store, scope-lint | tools | P1.4a, P1.2c | src/integrations/google/auth.py, scripts/google_oauth.py, docs/runbooks/google_oauth.md, tests/unit/integrations/google/test_auth*.py | R15 documented; scope-lint test (exact scope set, no gmail.send) | H4 live |
| P3.5 | Calendar tool | tools | P3.4, P3.2 | src/integrations/google/calendar_api.py, src/tools/integrations/calendar.py, tests/**/calendar* | recorded tests; confirmation shows absolute datetimes; idempotent re-send | sandbox UAT manual |
| P3.6 | Gmail tool: search/read (untrusted) + draft create only; send-path denylist | tools+security | P3.4, P3.0 | src/integrations/google/gmail_api.py, src/tools/integrations/gmail_draft.py, tests/**/gmail* | R6 test: no send scope, no send method, net denies send paths; HTML/hidden-text stripping; from/subject in `untrusted` | ADR-0007/Q2 |
| P3.7 | Notion tool: search/read (untrusted)/append | tools | P3.0, P1.4a | src/integrations/notion/**, src/tools/integrations/notion.py, tests/**/notion* | recorded tests; chunking/retries/size limits | H5 live |
| P3.8 | Drive read-only tool | tools | P3.4, P3.0 | src/integrations/google/drive_api.py, src/tools/integrations/drive_read.py, tests/**/drive* | scope-lint | Q4 |
| P3.9 | Commitment ledger (candidate → confirm) | memory | P3.2, P2.4 | src/memory/commitments.py, src/tools/integrations/memory_recall.py, tests/eval/fixtures/commitments/** | eval fixtures | PROT (fixtures dir) |
| P4.1 | Quarantine red-team: email/invite/doc/recorded speech | security | P3.6, P3.7 | tests/adversarial/lanes/** | red-team categories email/invite/doc/recorded speech, incl. short-field payloads | |
| P4.3 | Exfil: markdown image/URL stripping, previews off, reply URL allowlist | security | P2.1a | src/gateway/render.py, tests/adversarial/exfil/** | markdown_image_exfil, link_preview_exfil | |
| P4.4 | Canary strings + outbound blocking + alert | security | P1.4b, P2.1a | src/llm/scrub.py, src/core/canary.py, tests/adversarial/canary/** | canary_exfil | |
| P4.5 | Container egress rules; honest-limit doc | devops+security | P2.1b | deploy/network/** (+O compose) | R13 (app-level test + container rules verified from inside container); registry has no generic fetch | ADR-0004/Q3 |
| P4.6 | Memory poisoning: untrusted never auto-promoted; review queue | memory+security | P2.4, P3.0 | src/memory/review.py, tests/adversarial/memory/** | untrusted_auto_promotion = 0 | |
| P4.7 | Red-team corpus ≥100 cases, 12 categories, compromised-FakeLLM, CI | eval-qa | P4.1–P4.6 | tests/adversarial/redteam/**, tests/eval/injection/** | thresholds.security all pass | PROT |
| P4.8 | Security review pass | security+reviewer | P4.7 | docs/pivot/reports/SECURITY_REVIEW.md | no HIGH open | |
| P5.1 | Proactive jobs + interruption budget | backend | P3.3, P3.9 | src/scheduler/{budget,proactive}.py, **src/contracts/proactive_candidate.py**, src/contracts/__init__.py, tests/fixtures/proactive_candidate*.json, tests/unit/scheduler/test_budget*.py, tests/unit/test_contracts.py | R11 tests, fake clock | ADR-0011/Q5 |
| P5.2 | Trait model + "forget that about me" veto | personality | P2.4, P5.1 | src/personality/user_profile.py, **src/contracts/user_profile_slice.py**, src/contracts/__init__.py, tests/fixtures/user_profile_slice*.json, tests/**/test_user_profile*.py, tests/unit/test_contracts.py | confidence ≥ 0.6 gate test | |
| P5.3 | Friction/stance live with real model; personality evals | personality+eval-qa | P5.4 | src/personality/agent_character.py, tests/eval/personality/** | thresholds.personality | PROT, H3 |
| P5.4 | Bake-off harness (incl. ogg transcription) | eval-qa | P4.7 | tests/eval/bakeoff/**, docs/pivot/reports/BAKEOFF.md | reports/BAKEOFF.md; FakeLLM keeps pipeline green until H3 | H3 |
| P5.5 | Model ADR + pricing + spend cap + LoCoMo gate | architect | P5.4 | docs/decisions/**, src/llm/pricing.py (+O quality/thresholds.yaml) | ADR merged; thresholds updated by PR | |
| P6.1–P6.8 | Unchanged from SDLC | devops/eval-qa/reviewer | W5 | Dockerfile, deploy/**, docs/runbooks/**, tests/e2e/** | per SDLC W6 (arm64 build, R14 restore drill, ops thresholds, 72 h soak, UAT, PRR) | H6–H9 |
| P7.1–P7.5 | Unchanged | docs/architect | W6 | docs/**, README* | per SDLC W7 | H10 |

Every file under `src/` and `tests/` that exists today now has an owner, either in a task above or as a KEEP that no task touches. The untouched KEEPs are `src/memory/graph.py`, `src/db/postgres.py` (owned by P1.2c only; other tasks do not touch it), `src/personality/user_profile.py` until P5.2, and the integration tests `test_supersession`, `test_contradiction`, `test_stance_log` and `test_user_profile`. Remaining assignments: `test_retriever.py` → P2.4, `test_agent_character.py` → P2.6, contract fixtures → P2.5b/P5.1/P5.2.

**Parallel batches** (cap 3, disjoint owners):
- W1: {P1.0, P1.1, P1.2a}, then {P1.2b, P1.4a}, then P1.2c, then {P1.3a, P1.4b}, then P1.3b, then P1.4c, then P1.5. P1.3b and P1.4c are serialized because both edit `config/settings.py`.
- W2: {P2.1a, P2.3, P2.5a}, then {P2.1b, P2.2, P2.6}, then P2.4, then P2.5b. P2.2, P2.4 and P2.5b all edit `test_contracts.py` and `src/contracts/__init__.py`, so they are strictly serialized (P2.2 → P2.4 → P2.5b).
- W3: {P3.0, P3.3, P3.4}, then {P3.1, P3.7}, then P3.2, then {P3.5, P3.6, P3.8}, then P3.9.

## 10. Open questions needing human decisions

| # | Question | Default until answered | Blocks |
|---|---|---|---|
| Q1 | One batch approval to edit protected `tests/eval/**`. Affects: INV-1 rewrite, delete INV-2 and INV-4, rename INV-5 to R7, new INV-5/6/7, LoCoMo harness port, `red_team.py` replacement. | Those PROT tasks wait. Everything else proceeds. | P1.2c, P1.3a/b, P1.4c, P1.5, P2.4, P2.5b, P3.0, P3.1, P3.9, P4.7, P5.3 |
| Q2 | Gmail: no drafts-only scope exists, so `gmail.compose` also permits sending (ADR-0007). Accept compose with a code and network send-block, or go readonly-only? | compose + block + disclosure | P3.4, P3.6 |
| Q3 | Egress: hostnames are allowlisted at app level, with only coarse port rules at network level (ADR-0004). Is that acceptable vs. adding a squid sidecar? | app-level + nftables | P4.5 |
| Q4 | Drive scope: `drive.readonly` (restricted, whole Drive) vs. `drive.file` (only files the app created or opened, so users would need a picker UI we don't have). | `drive.readonly` | P3.8 |
| Q5 | Should reminders bypass quiet hours (ADR-0011)? | yes | P5.1 |
| Q7 | Quarantine routing (ADR-0010, Proposed). Bodies always go to the quarantine LLM; short fields get deterministic schema validation and become a DataBlock, with no LLM call. Accept, or quarantine every untrusted field (the Brief, read literally: more LLM calls per calendar/email list)? | **quarantine every untrusted field** until the human approves the bypass (matches HUMAN_TODO H18; §5's bypass is the proposal, not the default) | P3.0, P3.5–P3.8, P4.1 |
| Q6 | Fix HUMAN_TODO H1: the card path should be `personality/character_card.md` (ADR-0015). | orchestrator edits H1 | P2.6 |
