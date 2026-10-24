# Nimbus — Continuation Playbook (Steps 11–30)

**Covers:** Phase 1 through Phase 6 completion
**Prerequisite:** Steps 0–10 complete (183 tests, 5 eval harnesses, scrubber, memory, personality, agent runtime all built)
**State:** See current CLAUDE.md and MEMORY.md for exact status
1 ]
---

## Current gaps to close (from MEMORY.md tech debt + CLAUDE.md task table)

Before new features, these are the known structural gaps:

| Gap | Severity | Blocks |
|-----|----------|--------|
| AD-7: `DeterministicStubEmbedder` — no real embedding model | Critical | All memory quality, LoCoMo gate (6.4% vs 85% target) |
| No local LLM (vLLM) | Critical | All generation: extraction, friction, conversation quality, anti-sycophancy |
| Approver injection defense: 24/30 escalated | High | P4 release gate (0 escalations required) |
| T3.10: No network-layer egress proxy | Medium | SCR-5 (network-independent enforcement) |
| T4.2: Gmail/Drive tools missing | Medium | P4 tool breadth |
| T0.9: SBOM CI automation not built | Low | OSS compliance |
| PII detector false-positives on multi-word titles | Low | User-facing flow usability |
| No rate limiting on /auth/token | Medium | Production readiness |
| GET /memories has no test | Low | Regression coverage |

---

## Step 11 — Wire real embedding model

This unblocks everything. LoCoMo is at 6.4% because embeddings are sha256 noise.

```
Read CLAUDE.md and MEMORY.md. Note AD-7 (DeterministicStubEmbedder).

Task: Replace the stub embedder with a real sentence-transformer model.

1. Add `sentence-transformers>=3.0` to pyproject.toml
2. Create src/memory/embedder.py:
   - Implement the existing `Embedder` protocol with a real model
   - Use `all-MiniLM-L6-v2` (384-dim, ~80MB, fast, Apache 2.0) for dev
   - Make model name configurable via settings.py
   - Ensure it runs on CPU (no GPU dependency for embedding)

3. Update src/memory/manager.py:
   - Replace `DeterministicStubEmbedder` import with the real embedder
   - Keep the protocol-based swap so tests can still use a stub

4. Update Qdrant collection config:
   - Vector size from whatever the stub used to 384
   - May need to recreate the collection

5. Run the LoCoMo eval harness:
   uv run python tests/eval/locomo/harness.py
   Record the new score. It won't hit 85% yet (no generation model,
   retrieval-only scoring) but should jump significantly from 6.4%.

6. Update NOTICE file if the model has attribution requirements.

Use `python-patterns`. After: update CLAUDE.md (AD-7 resolved), MEMORY.md, commit.
```

---

## Step 12 — Wire local LLM via vLLM

This unblocks extraction, conversation quality, and all personality evals.

```
Read CLAUDE.md and MEMORY.md.

Task: Wire a local LLM for all generation tasks. This is the model
for conversation, extraction, friction checking, and the approver.

1. Add vLLM to docker-compose.yml (or document the standalone run command):
   - Image: vllm/vllm-openai or manual vllm serve
   - Model: Start with Qwen2.5-7B-Instruct (small, fast, good tool calling)
     for dev. The production model (Qwen3.8-27B) needs a bigger GPU.
   - Expose OpenAI-compatible API on localhost:8000

2. Create src/llm/client.py:
   - AsyncOpenAI client pointing at vLLM's local endpoint
   - NOT an egress point — this is local, not cloud (INV-1 unaffected)
   - Structured output support (for extraction workers)
   - Streaming support (for Mode A)
   - Configurable via settings.py

3. Wire into the orchestrator:
   - src/orchestrator/graph.py's `think` node: replace keyword heuristic
     with actual LLM call via the local client
   - src/orchestrator/graph.py's `respond` node: replace template with
     actual LLM generation using the assembled personality prompt

4. Wire into the approver:
   - src/orchestrator/approver.py: replace heuristic with LLM-based
     judgment (still no tools, still separate context — ACT-5 holds)

5. Wire into extraction (stubs become real):
   - src/memory/extractor.py: entity, relation extraction via local LLM
   - Structured output with Pydantic schemas for each extractor

6. Wire into personality:
   - src/personality/agent_character.py's check_friction: LLM-based
     conflict detection (still must be fast; use the local model)

7. Tests:
   - All existing tests must still pass (the LLM is an addition, not
     a replacement of tested interfaces)
   - New: test_llm_client.py (connection, streaming, structured output)
   - Re-run personality evals:
     uv run python tests/eval/personality/anti_sycophancy.py
     uv run python tests/eval/personality/stance_consistency.py
     uv run python tests/eval/personality/friction.py
   Record real numbers. These will likely still miss targets but should
   improve dramatically from 100%/0%/100%.

IMPORTANT: The local LLM client is NOT an egress point. It talks to
localhost, not a cloud provider. INV-1 is unaffected. Do not route it
through the scrubber.

Use `python-patterns`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

**This is the hardest session.** It touches the most files and requires vLLM actually running. If the session runs long, split: first session wires the client + orchestrator, second session wires extraction + personality.

---

## Step 13 — Wire Gemini for real tool execution

```
Read CLAUDE.md and MEMORY.md.

Task: Connect Gemini 3.8 Flash through the scrubber for real tool execution.

1. Update src/security/scrubber.py:
   - execute_external now makes a real httpx call to Gemini's API
   - Use the google-genai SDK or raw REST — whichever is simpler
   - All existing scrubber guards (PII, schema, entity sub) stay as-is
   - Add response parsing and error handling

2. Update src/tools/integrations/calendar.py:
   - Replace the Google Calendar API stub with real calls
   - Needs OAuth2 credentials (from .env)
   - CRUD: create, list, update, delete events

3. Create src/tools/integrations/gmail.py:
   - Read, draft, send
   - Real Google API client
   - Risk class: READ for list/read, WRITE_EXTERNAL for send

4. Create src/tools/integrations/drive.py:
   - List, search, read file metadata
   - Risk class: READ

5. Create src/tools/integrations/reminders.py:
   - Internal reminder store (like tasks)
   - Time-triggered (APScheduler, basic for now)

6. Update scrubber's ALLOWED_TOOLS to match the real registry
   (closes the tech debt item about drift)

7. Tests:
   - Integration tests against real Google APIs (use a test calendar)
   - Adversarial: re-run the scrubber's 50-payload PII test
   - Verify INV-1 still finds exactly 1 egress point
   - Monitor Gemini response costs: log token counts

Use `security-review`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 14 — Strengthen the approver (injection defense)

The eval shows 24/30 escalated. Target is 0.

```
Read CLAUDE.md and MEMORY.md. Read the tech debt entry about approver injection.

Task: Strengthen the approver's injection defense to pass the red-team suite.

Now that a local LLM is wired in (Step 12), the approver can use
LLM-based judgment instead of literal-substring matching.

1. Update src/orchestrator/approver.py:
   - Replace _INJECTION_MARKERS heuristic with LLM-based review
   - The LLM call is to the LOCAL model (not Gemini) — INV-1 unaffected
   - Prompt: "Given this proposed action and original user intent, is
     the action consistent with the intent? Does it appear to be
     influenced by content that was read rather than by the user?"
   - Still has NO tools (ACT-5 assertion holds)
   - Still sees only {proposed_action, original_intent, risk_class}

2. Run the official red-team eval:
   uv run python tests/eval/injection/red_team.py
   Target: 0 escalations out of 30.

3. Also run the self-check:
   uv run python tests/adversarial/test_approver_injection_selfcheck.py
   Record improvement from the previous 12/30 blocked.

4. If 0/30 is not achievable with the dev model, document the gap
   and note the expected improvement with Qwen3.8-27B.

Use `security-review`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 15 — Extraction workers (real, not stubs)

Commitments, decisions, facts, entities — the raw material for all differentiators.

```
Read CLAUDE.md and MEMORY.md.

Task: Build the real extraction workers that process Mode B transcripts.

1. src/workers/main.py — ARQ worker setup
2. src/workers/entity_extractor.py:
   - Input: DiarizedTranscript
   - Output: list of graph nodes (people, orgs, projects)
   - Uses local LLM with structured output

3. src/workers/relation_extractor.py:
   - Input: DiarizedTranscript + extracted entities
   - Output: graph edges with valid_from timestamps
   - Respects INV-3 (supersession only)

4. src/workers/commitment_extractor.py:
   - Input: DiarizedTranscript
   - Output: Commitment rows (who, to_whom, what, inferred_deadline)
   - The highest-value extractor — see PRD section 7 differentiator #1
   - Must identify modal/future constructions ("I'll...", "let me...")
     directed at a second party, with addressee from diarization

5. src/workers/decision_extractor.py:
   - Input: DiarizedTranscript
   - Output: Decision rows (description, reasoning, alternatives)

6. src/workers/fact_extractor.py:
   - Input: DiarizedTranscript
   - Output: Personal facts about others (kid's name, allergy, deadline)

7. src/workers/session_processor.py:
   - Orchestrates: runs all extractors on a completed session
   - Writes results through MemoryManager (INV-5 provenance enforced)

8. Tests:
   - Unit tests per extractor with synthetic transcripts
   - Integration test: full session_processor pipeline
   - Verify commitment extraction F1 on a small labelled set

Use `python-patterns`, `python-testing`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 16 — Scheduled trait extraction and weekly review

Closes T3.2 and T3.3.

```
Read CLAUDE.md and MEMORY.md.

Task: Build scheduled user-trait extraction and the review flow.

1. src/workers/trait_extractor.py:
   - Runs on a schedule (daily or after N sessions)
   - Analyses recent conversations for user traits
   - Uses the existing UserTrait + TraitEvidence schema
   - Enforces PSU-1 (every trait has evidence), PSU-2 (confidence)
   - Must NOT run inline in Mode A (PSU-5, INV-4)

2. src/api/routes/review.py:
   - GET /review/traits — returns unreviewed traits with evidence
   - POST /review/traits/{id}/approve
   - POST /review/traits/{id}/veto
   - POST /review/traits/{id}/edit
   - Vetoed traits feed back into extraction prompt (PSU-4)

3. src/api/routes/review.py (profile diff):
   - GET /review/diff — shows what changed since last review (PSU-6)

4. Track veto rate as a metric (target: ≤10%)

5. Tests:
   - test_trait_extractor.py: extraction produces valid traits with evidence
   - test_review.py: approve/veto/edit flows; vetoed trait excluded from
     next 10 prompt assemblies (PSU-4)

Use `python-patterns`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 17 — Proactive engine

The Jarvis feeling — it does things without being asked.

```
Read CLAUDE.md and MEMORY.md. Read docs/TECHNICAL_ARCHITECTURE.md section 8.4.

Task: Build the proactive engine with hard interruption budget.

1. src/workers/proactive/engine.py:
   - APScheduler-based, runs scanners on configurable intervals
   - Hard daily budget: 6 max (enforced in code, not configuration)
   - Expected-value scoring on candidates
   - Losers stay queued and decay

2. src/workers/proactive/scanners/:
   - pending_commitments.py — commitments past inferred deadline
   - goal_tracker.py — stated goals vs current behaviour
   - relationship_cadence.py — "You haven't talked to X in N weeks"
   - event_prep.py — upcoming calendar events → prep nudges

3. src/workers/proactive/nudge.py:
   - Generates contextual nudge text via local LLM
   - Routes to the user via API notification endpoint

4. src/api/routes/notifications.py:
   - GET /notifications — poll for pending nudges
   - POST /notifications/{id}/rate — thumb up/down (KPI tracking)
   - WebSocket push for real-time delivery

5. Tests:
   - test_budget.py: assert budget enforced, 7th candidate rejected
   - test_scanners.py: each scanner produces candidates from test data
   - test_nudge.py: nudge generated, rated, tracked

Use `python-patterns`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 18 — Mode A voice pipeline (server side)

Everything needed for sub-second voice conversation, server side.

```
Read CLAUDE.md and MEMORY.md. Read docs/TECHNICAL_ARCHITECTURE.md section 4.

Task: Build the Mode A voice pipeline on the server.

1. src/perception/stt_streaming.py:
   - WebSocket endpoint that receives audio chunks
   - Streaming ASR via faster-whisper (small model for latency)
   - Partial hypotheses within 300ms
   - VAD-based endpointing

2. src/perception/tts.py:
   - Streaming TTS via Kokoro (or piper for initial dev)
   - Must produce first audio chunk within 120ms of first text token
   - WebSocket audio output stream

3. src/api/routes/voice.py:
   - WS /voice — bidirectional audio streaming
   - Receives: audio chunks from client
   - Sends: audio chunks back (TTS output)
   - Internally: STT → orchestrator → TTS pipeline

4. Wire the latency budget:
   - VAD endpoint: 120ms
   - Audio transport: 30ms
   - ASR finalisation: 150ms
   - Memory retrieval: 100ms (warm context, already built)
   - LLM TTFT: 250ms
   - TTS first audio: 120ms
   - Total: 800ms p50

5. Tests:
   - test_stt.py: audio file → transcript, latency measured
   - test_tts.py: text → audio, first-chunk latency measured
   - test_voice_pipeline.py: end-to-end latency harness
   - INV-4 test: now concrete (was a stub)

Add `faster-whisper`, `kokoro` (or `piper-tts`) to pyproject.toml.
These are LOCAL — not cloud, not egress. INV-1 unaffected.

Use `python-patterns`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

**Note:** This will likely need 2 sessions. Split at STT (session 1) + TTS + pipeline (session 2) if needed.

---

## Step 19 — Mode B recording pipeline (server side)

```
Read CLAUDE.md and MEMORY.md.

Task: Build the Mode B batch processing pipeline on the server.

1. src/perception/stt_batch.py:
   - Parakeet-TDT 0.6B v2 for English (primary)
   - faster-whisper large-v3 for Hindi / code-switching (fallback)
   - Language routing: detect language, route to appropriate model
   - Word-level timestamps via forced alignment

2. src/perception/diarization.py:
   - pyannote-segmentation-3.0 for turn boundaries
   - CAM++ embeddings (192-dim) for voice prints
   - VBx clustering (no fixed speaker cap)
   - Over-segmentation consolidation pass (PER-6)

3. src/perception/speaker_id.py:
   - Voice-print store (encrypted, in Postgres)
   - Enrollment flow: name unnamed clusters
   - Cosine matching against enrolled speakers (threshold 0.65)
   - Unenrolled speakers labelled "Unknown", never guessed

4. src/perception/session_pipeline.py:
   - Receives encrypted SessionBundle from client
   - Decrypts in tmpfs (never disk)
   - Runs: ASR → diarization → speaker ID → alignment
   - Outputs: DiarizedTranscript (frozen contract)
   - Triggers extraction workers (Step 15)
   - Shreds plaintext from tmpfs

5. src/api/routes/upload.py:
   - POST /sessions/upload — receives encrypted session bundles
   - Triggers async processing via ARQ

6. Tests:
   - test_stt_batch.py: WER measured on labelled eval set
   - test_diarization.py: DER measured at 2, 4, 6 speakers
   - test_speaker_id.py: accuracy on enrolled set
   - test_session_pipeline.py: full pipeline integration

Add `pyannote.audio`, `nemo-toolkit[asr]` (for Parakeet) to pyproject.toml.
Update NOTICE for Parakeet CC-BY attribution.

Use `python-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

**This is the highest-risk step.** Speaker ID quality determines whether differentiators work. Budget 2–3 sessions. If DER/speaker-ID targets miss, iterate before moving forward.

---

## Step 20 — Network-layer egress proxy (T3.10)

```
Read CLAUDE.md and MEMORY.md. Read the tech debt entry about T3.10.

Task: Build the network-layer egress proxy, independent of the application.

1. Add a Squid or mitmproxy container to docker-compose.yml:
   - Allowlist: only Google APIs, Gemini API
   - All other outbound traffic blocked
   - All application containers route through it

2. Configure all services to use the proxy as their HTTP proxy

3. Tests:
   - test_egress_proxy.py: attempt to reach a non-allowlisted host
     from inside the app container — verify it's blocked
   - Verify this is independent of the scrubber (app-layer bypass
     attempt blocked at proxy level)

Use `security-review`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 21 — Flutter mobile app (Mode A)

The client side of the voice assistant.

```
Read CLAUDE.md and MEMORY.md.

Task: Build the Flutter mobile app with Mode A (conversation) support.

1. mobile/lib/core/api_client.dart — HTTP + WS client to your server
2. mobile/lib/core/auth.dart — JWT login flow
3. mobile/lib/features/chat/ — text chat UI (simpler than voice, build first)
4. mobile/lib/features/voice/ — push-to-talk voice UI:
   - Record audio, stream to /voice WS
   - Receive audio response, play back
   - Barge-in: stop playback on user speech
5. mobile/lib/main.dart — routing, theme, state management

6. Mode state machine (IDLE ↔ CONVERSING):
   - PTT press → CONVERSING
   - PTT release → IDLE
   - State visible in UI at all times

7. Tests:
   - Widget tests for state machine transitions
   - Integration test: login → chat → voice round trip

Note: if you're running agents in Claude Code (not Flutter-native),
this step may need to be done partially manually or with a
Flutter-capable agent. The prompts are provided for completeness.
```

---

## Step 22 — Flutter Mode B (recording)

```
Read CLAUDE.md and MEMORY.md. Read docs/TECHNICAL_ARCHITECTURE.md section 3.

Task: Add Mode B (recording) to the Flutter app.

1. Mode state machine extension (IDLE ↔ CONVERSING ↔ ARMING ↔ RECORDING):
   - "Record this" voice command → ARMING → chime → RECORDING
   - "Stop listening" → chime → IDLE
   - UI stop button → IDLE
   - Hard cap at 2h with 5-min warning

2. On-device stop-phrase detection:
   - Runs continuously during RECORDING
   - Must work with network disabled
   - 99% detection target on adversarial set

3. Recording state indicators:
   - Persistent non-dismissible notification
   - Distinct entry and exit chimes
   - Spoken confirmation on "are you recording?"

4. Encrypted local buffer:
   - Opus encode → AES-GCM → local storage
   - 72h ring buffer
   - Chunked upload on connectivity

5. CVB-7 enforcement:
   - No code path enters RECORDING without explicit user action
   - Test asserting no auto-trigger path exists

6. Tests:
   - State machine 100% branch coverage
   - Airplane-mode: 0 loss over 90 min
   - 3h soak: no crash, no memory growth, no thermal throttle
```

---

## Step 23 — Commitment ledger and contradiction detection (live)

The first differentiators go live.

```
Read CLAUDE.md and MEMORY.md.

Task: Wire commitment tracking and contradiction detection end-to-end.

1. Wire commitment_extractor (Step 15) into session_processor:
   - Every Mode B session processed → commitments extracted
   - Commitments stored in the ledger with speaker attribution

2. Commitment closure detection:
   - When a new session mentions a topic matching an open commitment,
     check whether the commitment was fulfilled
   - Close commitments with evidence (the session that confirmed closure)

3. Contradiction detection (already SQL-based, AD-6):
   - Wire into session_processor: after new facts are extracted,
     check for contradictions against existing graph
   - Surface contradictions via the proactive engine (Step 17)
   - Rank by: recency of older belief, confidence, user-verified status

4. Relationship intelligence layer:
   - Per-person view: interaction cadence, topics, facts, open commitments
   - GET /people — list known people with stats
   - GET /people/{id} — full relationship view

5. Tests:
   - test_commitment_lifecycle.py: extract → track → close
   - test_contradiction_live.py: new fact contradicts old → surfaced
   - test_relationship.py: per-person aggregation

6. Run commitment extraction eval:
   Target: F1 ≥ 0.90 on labelled set

Use `python-patterns`, `backend-patterns`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 24 — Pre-meeting briefing

```
Read CLAUDE.md and MEMORY.md.

Task: Build automatic pre-meeting briefings.

1. src/workers/proactive/scanners/meeting_briefing.py:
   - 30 min before any calendar event with known attendees:
   - Retrieve all prior interactions with those people
   - Open commitments both ways
   - Unresolved threads
   - Personal context (facts from fact_extractor)
   - Last discussion summary

2. Generate a brief via local LLM, push as a proactive notification

3. Tests:
   - test_meeting_briefing.py: synthetic calendar + history → brief generated
   - Usefulness rating target: ≥75%
```

---

## Step 25 — Decision journal and prospective memory

```
Read CLAUDE.md and MEMORY.md.

Task: Build decision journal automation and prospective memory triggers.

1. Decision journal:
   - Decisions captured by decision_extractor (Step 15)
   - Automatic retrospective at review_date horizon
   - Compare expected outcome with actual (from later conversations)

2. Prospective memory:
   - "Remind me when I talk to Sarah next" → triggers on speaker ID match
   - "Remind me when I'm near a pharmacy" → triggers on location
   - Person-triggered reminders depend on voice-print matching
   - Location-triggered via phone's geofence API

3. Idea resurrection:
   - When conversation enters semantic territory near an old abandoned idea,
     surface it: "You mentioned this approach in March"
   - Requires episodic memory search on topic adjacency

4. Tests:
   - test_decision_retrospective.py: decision stored → horizon reached → review triggered
   - test_prospective_memory.py: person-triggered reminder fires on voice match
   - test_idea_resurrection.py: adjacent topic surfaces old idea
```

---

## Step 26 — Memory consolidation and scale testing

```
Read CLAUDE.md and MEMORY.md.

Task: Ensure the system performs at scale and doesn't grow unboundedly.

1. src/workers/consolidation.py:
   - Merge near-duplicate episodic memories into semantic facts
   - Preserve provenance chain (INV-5)
   - Run nightly

2. Graph performance test:
   - Generate synthetic 2-year history
   - Verify retrieval ≤100ms at that volume
   - Verify graph queries (contradictions, relationships) perform

3. Retention policy engine:
   - Raw audio: configurable TTL (default 7 days)
   - Transcripts: default 90 days
   - Derived memory: indefinite
   - Nightly enforcement job

4. Storage growth monitoring:
   - Track DB sizes over time
   - Alert on unexpected growth

5. Tests:
   - test_consolidation.py: duplicates merged, provenance preserved
   - test_scale.py: retrieval ≤100ms at 2-year volume
   - test_retention.py: expired data actually deleted
```

---

## Step 27 — Cryptographic erasure

```
Read CLAUDE.md and MEMORY.md. Read docs/TECHNICAL_ARCHITECTURE.md section 9.3.

Task: Build per-person and per-timeframe cryptographic erasure.

1. src/security/erasure.py:
   - erase_person(person_id): the full pipeline from TECH ARCH section 9.3
     1. Resolve all sessions where person_id in speaker_set
     2. Destroy audio_dek and transcript_dek for those sessions
     3. Hard-delete graph edges with source_speaker = person_id
     4. Delete Qdrant points referencing person_id
     5. Regenerate derived summaries without the erased content
     6. Delete voice-print enrollment
     7. Write tombstone to retention ledger

   - erase_timeframe(start, end): same but scoped by time

2. src/api/routes/erasure.py:
   - POST /erasure/person/{id} — DESTRUCTIVE risk class
   - POST /erasure/timeframe — DESTRUCTIVE risk class

3. Verification:
   - After erasure, search for known content across ALL stores
   - Must find nothing — including in derived summaries (Step 5 is
     the one everyone skips)

4. Tests:
   - test_erasure.py: erase person → search returns nothing
   - test_erasure_derived.py: summary that referenced erased person
     is regenerated without them
   - test_tombstone.py: retention ledger records the erasure

Use `security-review`. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 28 — Red-team, backup drill, network audit

```
Read CLAUDE.md and MEMORY.md.

Task: Hardening — the final security pass.

1. Red-team the full system:
   - Run tests/eval/injection/red_team.py — target: 0 escalations
   - Run tests/adversarial/test_scrubber.py — target: 0 PII leaks
   - New: craft 20 additional injection payloads specifically
     targeting Nimbus's actual tool set (calendar invite bodies,
     email content, Drive document text)

2. Backup restore drill:
   - pg_dump + Qdrant snapshot + DEK store → encrypted → off-site
   - Restore to a clean machine
   - Verify data integrity post-restore

3. 24h network capture audit:
   - Run tcpdump/Wireshark on the server for 24h of normal use
   - Verify 0 unexpected egress events
   - Document every outbound connection

4. Rate limiting on /auth/token:
   - Add rate limiting (e.g. slowapi or manual token bucket)
   - 5 attempts per minute per IP

5. Tests:
   - test_backup_restore.py: automated backup + restore + verify
   - test_rate_limit.py: 6th login attempt within 1 min rejected
```

---

## Step 29 — Open-source releases

```
Read CLAUDE.md and MEMORY.md. Read docs/PRD.md section 10.

Task: Package and release the planned OSS components.

1. Bi-temporal memory layer:
   - Extract into a standalone package
   - Strip all Nimbus-specific code
   - Add docs, benchmarks, examples
   - NOTICE with all attributions
   - No real data in any fixture

2. The scrubber:
   - Standalone package
   - Include threat model documentation
   - Example integration guide

3. Local memory eval harness:
   - Standalone LoCoMo/LongMemEval runner
   - Reproducible from clean clone
   - Document methodology

4. Commitment extraction (prompt + schema + synthetic eval set):
   - Package the prompt template, Pydantic schema, and eval harness
   - Synthetic data only — no real conversations

5. Verify for each release:
   - [ ] No real capture data
   - [ ] No key material
   - [ ] NOTICE correct (Parakeet CC-BY!)
   - [ ] Reproduces from clean clone
   - [ ] Licence headers applied

6. Create GitHub repos, publish, announce.

Use `security-review` for the data-leak scan. After: update CLAUDE.md, MEMORY.md, commit.
```

---

## Step 30 — Final documentation and Phase 6 gate

```
Read CLAUDE.md and MEMORY.md.

Task: Final documentation pass and Phase 6 completion.

1. Every module has a README
2. Architecture doc is current with actual code
3. API documented (OpenAPI spec is auto-generated by FastAPI)
4. Deployment guide: how to set up from scratch
5. Character card guide: how to write and iterate on personality
6. All eval results documented with dates

7. Phase 6 gate checklist:
   - [ ] Red-team clean on criticals
   - [ ] Restore drill successful
   - [ ] 24h network audit clean
   - [ ] All planned OSS components released
   - [ ] Parakeet CC-BY attribution correct everywhere
   - [ ] SBOM current
   - [ ] Full test suite green
   - [ ] All 5 invariants passing
   - [ ] All eval harnesses recorded and documented
   - [ ] CLAUDE.md and MEMORY.md fully current
```

---

## Session management (unchanged from original playbook)

**Start of every session:**
```
Read CLAUDE.md and MEMORY.md. Check the task tracking table for current state. Your task is [Step N].
```

**End of every session:**
```
Before we end: update CLAUDE.md task table. Append to MEMORY.md "What the agent learned this session" with today's date. Commit all changes.
```

**If running long (~70% budget):**
```
Commit everything with [WIP] prefix. Update CLAUDE.md with exactly where you stopped and what remains. We'll continue next session.
```

---

## Expected session count

| Steps | Sessions | Phase |
|-------|----------|-------|
| 11-12 (models) | 2-3 | P1 |
| 13-14 (Gemini + approver) | 2 | P3/P4 |
| 15-16 (extraction + review) | 2-3 | P3/P4 |
| 17 (proactive) | 1-2 | P4 |
| 18 (Mode A voice) | 2-3 | P1 |
| 19 (Mode B perception) | 2-3 | P2 |
| 20 (egress proxy) | 1 | P3 |
| 21-22 (Flutter) | 3-4 | P1/P2 |
| 23-25 (differentiators) | 3-4 | P4/P5 |
| 26 (scale) | 1-2 | P5 |
| 27 (erasure) | 1-2 | P6 |
| 28 (hardening) | 1-2 | P6 |
| 29 (OSS) | 2-3 | P6 |
| 30 (docs + gate) | 1 | P6 |
| **Total** | **~25-35** | |

---

## Your homework between sessions

1. **Write the character card** (personality/character_card.md) — still a placeholder. Do this before Step 12 so the personality evals have something real to test against.

2. **Enroll your top 20 speakers** after Step 19 — record short clips of the people you talk to most. This is the single highest-leverage quality investment.

3. **Label a code-switched eval set** — 30 min of real EN/HI conversation with word-level language tags. No public benchmark covers this.

4. **Label a commitment eval set** — 50 real commitments from your conversations with ground truth (who, to whom, what, deadline). This is what F1 ≥ 0.90 is measured against.

5. **Set up the GPU** — vLLM needs a real GPU. Qwen2.5-7B for dev, Qwen3.8-27B for production. Budget accordingly.