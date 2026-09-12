# Project Nimbus — Technical Architecture

**Version:** 2.0
**Date:** 10 September 2026
**Supersedes:** v1.0
**Audience:** Build agents and human owner
**Companion docs:** `PRD.md`, `EXECUTION_PLAN.md`, `AGENT_OPERATING_MANUAL.md`

---

## 0. How to read this document

This is a normative specification. Where it says MUST, deviation is a defect requiring an explicit written exception. Where it says SHOULD, deviation requires a recorded rationale in the commit message.

Build agents: this document is the source of truth for interfaces. If your implementation and this document disagree, this document wins and your implementation is wrong — unless you have first amended this document through a reviewed change.

---

## 1. Architectural invariants

These five statements must remain true at every commit. Each has an automated test that runs in CI. If a change breaks one, the change is reverted, not debated.

| ID | Invariant | Enforced by |
|---|---|---|
| INV-1 | Exactly one function performs cloud model egress | `test_single_egress_point` — AST scan for outbound HTTP to model hosts |
| INV-2 | No memory content, transcript text or reasoning chain appears in any egress payload | `test_egress_payload_schema` — whitelist validation plus PII detector |
| INV-3 | Graph facts are never mutated in place; supersession only | `test_graph_append_only` — asserts no UPDATE on object columns |
| INV-4 | Mode A response path never blocks on a network call | `test_mode_a_no_network_block` — latency assertion with cloud stubbed to 30s |
| INV-5 | Every derived memory row has non-null provenance | Database NOT NULL constraints plus `test_provenance_completeness` |

---

## 2. Topology

```
┌──────────────────────────────────────────────────────────────────┐
│ TIER 1 — PHONE (Flutter + platform-native audio)                 │
│                                                                   │
│  Mode A path (latency-critical, local only)                      │
│    mic → VAD → streaming ASR → WS → server → TTS → speaker       │
│    target: 800ms p50 mic-release to first audio                  │
│                                                                   │
│  Mode B path (quality-critical, latency-free)                    │
│    mic → VAD gate → Opus encode → AES-GCM → local ring buffer    │
│    → chunked upload on connectivity                              │
│                                                                   │
│  Always: mode state machine, stop-phrase detector (on-device),   │
│  persistent notification, encrypted buffer                       │
└────────────────────────────┬─────────────────────────────────────┘
                             │ WireGuard, mTLS. No public ingress.
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ TIER 2 — PRIVATE SERVER                                          │
│                                                                   │
│  ┌─ PERCEPTION ────────────────────────────────────────────┐     │
│  │ Mode A: streaming ASR (small, latency-tuned)            │     │
│  │ Mode B: Parakeet-TDT batch + faster-whisper (EN/HI)     │     │
│  │         pyannote-3.0 + CAM++ + VBx, consolidation pass  │     │
│  └──────────────────────────────────────────────────────────┘    │
│                            ▼                                     │
│  ┌─ MEMORY ────────────────────────────────────────────────┐     │
│  │ Redis: working set    Qdrant: episodic vectors          │     │
│  │ Postgres: semantic    Postgres+AGE: bi-temporal graph   │     │
│  └──────────────────────────────────────────────────────────┘    │
│                            ▼                                     │
│  ┌─ PERSONALITY ───────────────────────────────────────────┐     │
│  │ User model: traits + evidence + confidence + veto state │     │
│  │ Agent model: character card + stance log + friction     │     │
│  └──────────────────────────────────────────────────────────┘    │
│                            ▼                                     │
│  ┌─ REASONING (local, vLLM) ───────────────────────────────┐     │
│  │ Conversation, memory reasoning, extraction, personality │     │
│  │ Decides WHAT to do. Never delegates reasoning to cloud. │     │
│  └──────────────────────────────────────────────────────────┘    │
│                            ▼                                     │
│  ┌─ SCRUBBER (INV-1, INV-2) ───────────────────────────────┐     │
│  │ The ONLY egress point. Intent in, sanitised out.        │     │
│  └──────────────────────────────────────────────────────────┘    │
│                            ▼                                     │
│  ┌─ EGRESS PROXY (network-layer allowlist) ────────────────┐     │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
                    Gemini 3.8 Flash — execution only
                    Google APIs — calendar, gmail, drive
```

---

## 3. Mode state machine

The single most important correctness surface in the client. Ambiguity here is a legal and trust failure, not a UX annoyance.

```
                  ┌──────┐
       ┌─────────►│ IDLE │◄────────────┐
       │          └──┬───┘             │
       │             │ PTT press       │ session end
       │             ▼                 │
       │      ┌─────────────┐          │
       │      │ CONVERSING  │          │
       │      │  (Mode A)   │          │
       │      └──┬───────┬──┘          │
       │  PTT    │       │ "record this"│
       │  release│       ▼             │
       └─────────┘  ┌──────────────┐   │
                    │  ARMING      │   │
                    │  chime+notif │   │
                    └──────┬───────┘   │
                           │ confirmed │
                           ▼           │
                    ┌──────────────┐   │
                    │  RECORDING   │───┘
                    │  (Mode B)    │ "stop listening"
                    └──────────────┘ or UI stop
                                     or hard cap
```

**Rules (all MUST):**

- No transition into RECORDING without explicit user action. There is no timer, heuristic or context trigger that enters Mode B. (CVB-7, and the legal enforcement of Telecom Act s.20.)
- RECORDING state has a persistent, non-dismissible notification.
- Entry and exit both play distinct audible chimes.
- Querying state ("are you recording?") returns a spoken answer in every state.
- Stop-phrase detection runs **on-device** and continuously during RECORDING. It MUST NOT depend on server connectivity.
- Hard cap (default 2h) forces exit with warning at T-5min.
- App crash or force-quit during RECORDING MUST end the session and preserve the buffer, never silently continue.

The stop phrase is a safety control. Treat its detection rate (99% target, CVB-2) as a P0 metric, and test it against adversarial audio: crosstalk, music, the phrase spoken by someone other than the user, and the phrase embedded in a longer sentence.

---

## 4. Mode A — the latency budget

800ms p50 end to end. Allocate it explicitly; unallocated budget disappears.

| Stage | Budget | Notes |
|---|---|---|
| VAD endpoint detection | 120ms | Aggressive endpointing trades against CVA-4 false cutoffs |
| Audio upload (WS, already streaming) | 30ms | Stream during speech, not after |
| ASR finalisation | 150ms | Partial hypotheses already sent; only final alignment |
| Memory retrieval | 100ms | **Precomputed context, see below** |
| LLM time-to-first-token | 250ms | The hard constraint on model choice |
| TTS time-to-first-audio | 120ms | Streaming TTS mandatory; non-streaming disqualified |
| Network + jitter buffer | 30ms | LAN or WireGuard on local network |
| **Total** | **800ms** | |

**Retrieval must be precomputed.** Running vector search plus graph traversal inside the turn will not fit 100ms. Instead: maintain a warm context slice in Redis, refreshed on session start and after each turn, containing the user profile summary, active stance entries, recent session context, and the top-K memories for the current conversational topic. The turn reads from Redis; the refresh happens after the response is streaming.

**Tool-requiring turns (CVA-6):** the model emits an acknowledgement token stream immediately and the tool executes asynchronously. Under no circumstances does the user wait on Gemini's time-to-first-token, which is documented at 13.30s. Result arrives as a follow-up utterance or notification.

**INV-4 test:** stub the cloud endpoint to 30s latency and assert Mode A p95 is unchanged.

---

## 5. Perception

### 5.1 Mode A — streaming

Small, latency-tuned streaming ASR. Requirements: partial hypotheses within 300ms, incremental correction, no full-utterance buffering. Accuracy target is looser than Mode B because conversational turns are short and the model has context to disambiguate.

### 5.2 Mode B — batch

```
encrypted chunks → decrypt in tmpfs → Opus decode → 16kHz PCM
  ├─ language ID → route:
  │    English-dominant  → Parakeet-TDT 0.6B v2
  │    Hindi / mixed     → faster-whisper large-v3
  ├─ pyannote-segmentation-3.0 → turn boundaries
  │    → CAM++ embeddings (192-dim) → VBx clustering
  │    → consolidation pass (PER-6)
  ├─ wav2vec2 forced alignment → word-level timestamps
  ├─ voice-print match vs enrolled speakers (cosine, tau=0.65)
  └─ shred plaintext from tmpfs
```

**Code-switching (PER-3) is the hardest perception requirement.** Parakeet does not cover Hindi. Language ID at the utterance level is insufficient because switching happens mid-sentence. Approach: run both engines on segments flagged as mixed, then merge on confidence per word span. Build the code-switched eval set from real recordings in P0 — this cannot be evaluated on public benchmarks.

**Over-segmentation (PER-6)** is a known pyannote failure: one person is split across multiple clusters, producing duplicate speakers in the transcript. The fix is a consolidation pass after speaker naming that merges clusters whose centroids fall within threshold. This is also a good upstream contribution candidate.

### 5.3 Speaker enrollment

The highest-leverage quality investment in the system. Incumbent products fail here because they attempt zero-shot identification and users report being labelled "Unknown" in their own recordings.

Flow: after each Mode B session, unnamed clusters are surfaced for naming. Named clusters store a centroid voice print. Target: top 20 speakers enrolled by end of P2, covering the majority of conversational volume. Re-enroll on drift (centroid similarity degrading over time).

Every downstream differentiator depends on this. Commitment attribution, relationship intelligence, speaker-weighted retrieval and pre-meeting briefing all collapse to generic transcript search if speaker ID fails.

---

## 6. Memory

### 6.1 Bi-temporal schema

```sql
CREATE TABLE graph_edge (
    id              UUID PRIMARY KEY,
    subject_id      UUID NOT NULL,
    predicate       TEXT NOT NULL,
    object_id       UUID,
    object_literal  TEXT,

    valid_from      TIMESTAMPTZ NOT NULL,   -- when the fact became true
    valid_to        TIMESTAMPTZ,            -- NULL = currently true
    ingested_at     TIMESTAMPTZ NOT NULL,   -- when we learned it
    invalidated_at  TIMESTAMPTZ,            -- when we learned it was wrong

    source_session  UUID NOT NULL,          -- INV-5
    source_speaker  UUID,
    confidence      REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    superseded_by   UUID REFERENCES graph_edge(id),
    user_verified   BOOLEAN DEFAULT NULL
);

CREATE INDEX idx_edge_current ON graph_edge (subject_id, predicate)
    WHERE valid_to IS NULL AND invalidated_at IS NULL;
CREATE INDEX idx_edge_temporal ON graph_edge (subject_id, valid_from, valid_to);
```

Separating valid time from ingestion time is what makes four things possible that a flat store cannot do: contradiction detection, "what did I believe on date X", correct handling of late-arriving information, and full provenance.

**INV-3 is enforced here.** No UPDATE ever touches `object_id`, `object_literal` or `valid_from`. Supersession sets `valid_to` and `superseded_by` on the old row and inserts a new one.

### 6.2 Contradiction detection (MEM-3)

A new edge contradicts an existing one when: same `subject_id`, same `predicate`, different object, and overlapping valid-time intervals. This is a cheap SQL predicate, not an LLM call — the LLM is only invoked to judge whether the contradiction is *interesting* enough to surface.

Rank surfaced contradictions by: recency of the older belief, confidence of both, and whether the user previously verified the older one. A contradiction against a user-verified belief is high signal.

### 6.3 Retrieval scoring

```
score = w_sim   * cosine(query, memory)
      + w_graph * graph_proximity(memory, active_entities)
      + w_rec   * exp(-lambda * age_days)
      + w_imp   * importance
      + w_spk   * speaker_relevance(memory, present_speakers)
      + w_ver   * user_verified_bonus
```

`speaker_relevance` is the term competitors structurally cannot compute — it requires diarized, speaker-identified history. When talking to a known person, memories involving them weight up sharply. This is why pre-meeting briefing is nearly free for Nimbus and expensive for everyone else.

Weights MUST be tuned against the eval harness, not hand-picked. MEM-6 requires an ablation showing each term contributes; a term that does not earn its place gets removed.

### 6.4 Eval harness (MEM-7)

Reproduce LoCoMo (1,540 questions across single-hop, multi-hop, open-domain and temporal) and LongMemEval (500 questions including knowledge updates) locally.

Published 2026 figures for orientation only: Omi reports 86.6 / 83.3; Mem0's token-efficient algorithm reports around 91.6 / 94.8; Zep reports 94.7 / 90.2. **These are self-reported, vendors publicly dispute each other's methodology, and at least one independent audit has raised LoCoMo methodological concerns. Treat every published number as unverified.** Our gate is our own run.

Third eval set: built from the owner's real capture data, hand-labelled. This is the only benchmark that actually predicts product quality.

CI gate on every memory-layer commit.

---

## 7. Personality subsystem

Two models, different machinery, one prompt assembly.

### 7.1 User model

```sql
CREATE TABLE user_trait (
    id              UUID PRIMARY KEY,
    category        TEXT NOT NULL,   -- communication | decision | energy
                                     -- value | pattern | preference
    statement       TEXT NOT NULL,   -- "prefers being told the conclusion first"
    confidence      REAL NOT NULL,
    observation_count INT NOT NULL DEFAULT 1,
    first_observed  TIMESTAMPTZ NOT NULL,
    last_reinforced TIMESTAMPTZ NOT NULL,
    user_status     TEXT DEFAULT 'unreviewed',
                    -- unreviewed | approved | edited | vetoed
    superseded_by   UUID REFERENCES user_trait(id)
);

CREATE TABLE trait_evidence (
    trait_id        UUID REFERENCES user_trait(id),
    session_id      UUID NOT NULL,
    excerpt         TEXT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL
);
```

**Rules:**

- Every trait MUST have at least one evidence row (PSU-1). A trait without evidence is a hallucination and is deleted by a nightly integrity job.
- Traits below confidence 0.6 are stored but MUST NOT be asserted in prompt assembly (PSU-2). They are candidates, not beliefs.
- Vetoed traits are never re-derived from the same evidence and the veto feeds extraction prompt tuning (PSU-4).
- Trait extraction runs on a schedule, never inline in a Mode A turn (PSU-5, protects INV-4).

The weekly review surfaces new and changed traits with their evidence. The user approves, edits or vetoes. **Veto rate above 10% means extraction is broken** and is a P1 defect, not a tuning opportunity.

### 7.2 Assistant model

Three components. The first is hand-written and never generated.

**Character card** — `personality/character_card.md`, version controlled, authored by the owner. Defines voice, temperament, how it handles bad ideas, what it refuses to do, its standing opinions. PSA-1 requires a CI check that this file is not written by any code path.

**Stance log** — the assistant's own prior positions:

```sql
CREATE TABLE agent_stance (
    id              UUID PRIMARY KEY,
    topic           TEXT NOT NULL,
    position        TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    taken_at        TIMESTAMPTZ NOT NULL,
    session_id      UUID NOT NULL,
    strength        TEXT NOT NULL,  -- tentative | held | firm
    revised_from    UUID REFERENCES agent_stance(id),
    revision_reason TEXT
);
```

This is the component nobody builds and it is what makes something feel like a continuous entity rather than a fresh instance in a costume. Before responding on a topic with prior stance entries, those entries enter the prompt. Reversal without acknowledgement is a PSA-3 failure.

**Friction mandate** — a standing instruction, evaluated as a hard gate. When the user's current request conflicts with a stated goal, a prior decision, or an observed pattern, the assistant names the conflict before complying. Not refusal — naming.

### 7.3 Anti-sycophancy (PSA-5)

The default failure of every assistant with a personality layer is agreeing with everything, which makes it useless as a thinking partner.

Eval design: a set of 40 user assertions, 20 factually or logically wrong. Measure agreement rate. Target: 60% maximum overall agreement, and specifically 20% maximum agreement with the incorrect subset. Run this on every personality-layer change.

This eval is more important than it looks. It is the difference between a tool that improves the user's thinking and one that flatters it.

---

## 8. The scrubber

The single highest-consequence component. INV-1 and INV-2 both live here.

### 8.1 Contract

```python
def execute_external(intent: ToolIntent) -> ToolResult:
    """The ONLY function in this codebase that reaches a cloud model.

    MUST NOT accept: transcripts, memory rows, graph edges,
    personality traits, stance entries, reasoning chains,
    raw user utterances, or person names.

    MUST accept only: ToolIntent, whose schema is a closed whitelist.
    """
```

```python
@dataclass(frozen=True)
class ToolIntent:
    tool: str                      # from the registry, validated
    action: str                    # from the tool's declared actions
    params: dict[str, Primitive]   # primitives only, no nested objects
    entity_refs: dict[str, str]    # opaque local refs: "<person_44>"
    risk_class: RiskClass
```

### 8.2 Pipeline

```
local model produces intent
  → schema validation (reject-by-default on unknown fields)   SCR-2
  → entity substitution: names → opaque refs                  SCR-3
  → PII detector on serialised payload (hard fail)            SCR-7
  → audit log write (full payload, local, before send)        SCR-4
  → egress proxy (network allowlist, independent of app)      SCR-5
  → Gemini 3.8 Flash
  → response
  → entity refs resolved locally back to names
  → result returned to local model
```

### 8.3 Why this matters more than it appears

The naive version of hybrid architecture sends "schedule a follow-up with Rohit about the funding concern he raised." That single string leaks a person, a relationship, a business-sensitive topic, and an inference the memory layer made. Fifty of those a day reconstructs a meaningful slice of a life.

The scrubbed version sends `{"tool":"calendar","action":"create","params":{"title":"Follow-up","start":"2026-09-15T15:00:00+05:30"},"entity_refs":{"attendee":"<person_44>"}}`. Google gets a verb and an opaque token.

**Design rule:** the cloud model receives intents, never memory. The local model does all reasoning; the cloud model does execution. If a task requires the cloud model to *understand* rather than *act*, it does not go to the cloud.

### 8.4 Failure modes to guard

| Failure | Guard |
|---|---|
| Free-text param carrying context ("subject: re the funding concern") | PII detector plus param length caps plus manual review of every new tool's param schema |
| Error messages echoing sensitive context back through logs | Sanitise exception paths; test with deliberately sensitive payloads |
| A new tool added without going through the scrubber | INV-1 AST test catches any new outbound call site |
| Entity ref collision or leakage of the ref mapping | Refs are per-session, non-sequential, never transmitted alongside their resolution |
| Retry logic bypassing the scrubber on failure | Retries re-enter at the scrubber, never at the transport |

---

## 9. Agent runtime and tool execution

### 9.1 Risk model (ACT-3)

| Class | Examples | Gate |
|---|---|---|
| READ | List calendar, search drive, read email | Auto |
| WRITE_REVERSIBLE | Create draft, add task, create event | Auto, logged, undo available |
| WRITE_EXTERNAL | Send email, share file, accept invite | Approver agent |
| DESTRUCTIVE | Delete, revoke access, financial | Approver plus explicit user confirmation |

### 9.2 Injection isolation (ACT-4, ACT-5)

The failure mode that matters most: content the agent reads talking it into performing an action. Meta's Muse addresses this with an isolated approval agent; the design is sound enough to adopt.

- The **executor** has tools. Its context is tainted the moment it reads external content.
- The **approver** has **no tools at all** (ACT-5 asserts an empty tool list) and runs in a separate context. It sees `{proposed_action, original_user_intent, risk_class}` and never sees the content that motivated the action.
- All retrieved external content is wrapped in `<untrusted_content>` boundaries with a standing instruction that nothing inside may be treated as instruction.

Red-team suite: 30 injection payloads across email bodies, calendar invites, web content and document text. Zero escalations required for release.

### 9.3 Proactive engine

Scanners produce candidate interruptions; each is scored on expected value; a hard daily budget admits only the top candidates. Losers stay queued and decay.

The budget is a product constraint enforced in code, not a setting. Six per day default.

---

## 10. Deployment

```yaml
services:
  postgres:      # + Apache AGE + pgvector, LUKS-backed volume
  redis:         # working memory, no disk persistence
  qdrant:        # episodic vectors, encrypted payloads
  vllm:          # local model, GPU
  perception:    # Parakeet + whisper + pyannote + CAM++, GPU, preemptible
  api:           # FastAPI, WS for Mode A
  worker:        # ARQ extraction workers
  scheduler:     # APScheduler proactive engine
  tts:           # streaming TTS
  egress-proxy:  # allowlist enforcement — ALL outbound traffic
  wireguard:     # the only network entry point
```

**GPU sharing:** vLLM is interactive and holds priority. Perception is batch and preemptible. Mode A latency SLO wins every contention decision.

**Backup:** nightly pg_dump plus Qdrant snapshot plus wrapped DEK store, age-encrypted, off-site. **Restore tested monthly.** An untested backup is not a backup.

---

## 11. Build vs adopt

| Component | Decision | Rationale |
|---|---|---|
| ASR, VAD, diarization models | Adopt | Best-in-class open weights exist; training our own destroys value |
| Agent runtime (LangGraph) | Adopt | Mature, explicit graph control |
| Flutter BLE/audio client patterns | Adopt from Omi (MIT) | Solved problem |
| Memory layer | **Build** | Bi-temporal, contradiction detection, provenance, crypto erasure. The IP. |
| Personality subsystem | **Build** | No prior art at this specification |
| Scrubber | **Build** | Small, critical, must be fully auditable |
| Mode state machine | **Build** | Correctness surface, no acceptable dependency |
| Extraction workers | **Build** | Commitment and decision extraction have no off-the-shelf equivalent |
| Crypto primitives | **Adopt** (never roll your own) | Standard libraries only; own the key hierarchy, not the algorithms |

---

## 12. Interface contracts between agent pods

Agents work in parallel. These contracts are frozen at phase start and changed only through explicit amendment. Silent interface drift is the most common failure mode in parallel agent development.

| Contract | Producer | Consumer | Frozen at |
|---|---|---|---|
| `SessionBundle` | Client agent | Perception agent | P1 |
| `DiarizedTranscript` | Perception agent | Memory agent | P2 |
| `MemoryQuery` / `MemoryResult` | Memory agent | Agent-runtime agent | P2 |
| `UserProfileSlice` | Personality agent | Agent-runtime agent | P3 |
| `ToolIntent` / `ToolResult` | Agent-runtime agent | Scrubber agent | P3 |
| `ProactiveCandidate` | All scanners | Scheduler | P4 |

Each contract MUST have: a versioned schema file, a golden-fixture test, and a compatibility test that fails if a field is removed or its type changes.

---

*End of technical architecture v2.0.*
