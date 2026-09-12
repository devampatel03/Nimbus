# Project Nimbus — Product Requirements Document

**Version:** 2.0
**Date:** 10 September 2026
**Supersedes:** v1.0 (9 Sep 2026)
**Owner:** CTO
**Build team:** Autonomous agent pods + human owner
**Companion docs:** `TECHNICAL_ARCHITECTURE.md`, `EXECUTION_PLAN.md`, `AGENT_OPERATING_MANUAL.md`

---

## Changelog from v1.0

| Change | Rationale |
|---|---|
| Wearable hardware deferred out of Phases 0-6 | Phone-first removes the hardware critical path entirely (~4 months) |
| Capture split into two explicit modes | Push-to-talk conversation and long-form recording have different latency budgets and pipelines |
| Session boundary detection removed from scope | User-issued stop phrase eliminates the hardest unsolved problem in v1 |
| Hybrid model routing introduced | Gemini 3.8 Flash for execution, local models for everything touching memory |
| Scrubber added as mandatory egress boundary | Tool-call context leaks more than tool-call verbs |
| Dual personality system elevated to core requirement | Was implicit in v1; now a first-class subsystem with its own schema |
| Open-source compliance and release track added | Licence obligations are load-bearing (Parakeet is CC-BY, not permissive) |
| Team model changed from human pods to agent pods | Acceptance criteria rewritten to be machine-verifiable |

---

## 1. Product definition

Nimbus is a private, self-hosted personal assistant for a single user. It captures conversation on demand through a phone app, builds a durable and inspectable model of its user, and acts on their behalf through external tools — with a hard architectural boundary ensuring memory never leaves the user's infrastructure.

It is not a chatbot with memory bolted on. The memory *is* the product; chat is one interface to it.

### 1.1 Two modes, one system

**Mode A — Converse.** Press to talk. Sub-second voice interaction with an assistant that knows the user's history and has a stable personality of its own. Latency-bound.

**Mode B — Record.** "Record this conversation" and the assistant goes silent, capturing until told to stop. Post-processes into diarized transcript, extracted commitments, decisions, facts and relationship updates. Quality-bound, latency-free.

### 1.2 What makes it different

Every competitor is one of: a cloud agent with connectors (Muse, ChatGPT, Gemini), a transcription wearable (Plaud, Bee, Omi), or a note-taker (Granola, CraftNote). None combine durable personal memory, a genuine two-way personality, action execution, and full data sovereignty.

The four capabilities that require this exact substrate and cannot be replicated by a chat-only product: **commitment tracking, relationship intelligence, pre-meeting briefing, and temporal contradiction detection.**

---

## 2. Users and scope

**Single user.** The owner. Personal use only through Phase 6.

**Explicitly out of scope:** multi-user, sharing, team features, commercialisation, medical or diagnostic claims, covert operation, general-purpose chatbot capability.

The single-user constraint is not a limitation to be removed later — it *is* the security model for Phases 0-6. Multi-user is a different product with a different threat model and a full DPDP compliance workstream attached.

---

## 3. Product principles

1. **Memory never leaves.** Cloud models receive intents, never memory. Enforced in code at exactly one function.
2. **Latency is a feature of Mode A, irrelevant in Mode B.** Never let Mode B quality requirements degrade Mode A responsiveness.
3. **The user can read everything the system believes about them.** Inspectable, editable, vetoable. Silent accumulation is a defect.
4. **Personality with a job.** The assistant has stable character and is mandated to create friction when the user's stated goals conflict with their current request. Sycophancy is a bug.
5. **Degrade, never fail.** Cloud down means local answers. Server down means phone buffers. Model unavailable means smaller model.
6. **Forgettable by design.** Cryptographic erasure of any person or timeframe, propagating to derived artefacts.
7. **Open source by default.** Components with no privacy coupling get released. Licence obligations are tracked as engineering requirements, not legal afterthoughts.

---

## 4. Functional requirements

Requirement IDs are referenced by task IDs in `EXECUTION_PLAN.md`. Every requirement has a verification method; requirements without one are defects in this document.

### 4.1 Capture — Mode A (Converse)

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| CVA-1 | Push-to-talk activation from app, lock-screen widget, and hardware button | Manual test on 3 entry points | P0 |
| CVA-2 | End-to-end voice latency 800ms p50, 1200ms p95, mic-release to first audio out | Automated latency harness, 100 turns | P0 |
| CVA-3 | Barge-in: user speech interrupts assistant playback within 200ms | Automated, synthetic interruption | P0 |
| CVA-4 | Turn continues across ASR uncertainty without false endpointing | 5% max premature cutoff on eval set | P0 |
| CVA-5 | Conversation state persists across app backgrounding | Manual, 10 backgrounding events | P1 |
| CVA-6 | Tool-requiring turns acknowledge within latency budget, execute async | Automated: verify ack under 800ms when tool invoked | P0 |

### 4.2 Capture — Mode B (Record)

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| CVB-1 | Voice-triggered entry ("record this") and explicit UI entry | Automated intent classification, 98% min | P0 |
| CVB-2 | Voice-triggered exit ("stop listening") recognised in noisy multi-speaker audio | 99% detection on adversarial eval set | P0 |
| CVB-3 | Unmistakable recording state: persistent notification, entry/exit chime, spoken confirmation on query | Manual checklist, 6 state transitions | P0 |
| CVB-4 | Audio buffered locally, encrypted, uploaded on connectivity | Automated airplane-mode test, verify no loss | P0 |
| CVB-5 | Continuous recording 3h without crash, memory leak or thermal throttle | Automated 3h soak test | P0 |
| CVB-6 | Hard cap with warning at configurable duration (default 2h) | Automated | P1 |
| CVB-7 | Mode B never auto-activates; entry is always explicit | Code review plus test asserting no auto-trigger path | P0 |

### 4.3 Perception

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| PER-1 | Streaming ASR, Mode A, 300ms max lag | Automated latency harness | P0 |
| PER-2 | Batch ASR, Mode B, WER 10% max in office acoustics | WER harness on labelled eval set | P0 |
| PER-3 | English-Hindi code-switching mid-utterance | WER 18% max on code-switched eval set | P0 |
| PER-4 | Speaker diarization, no fixed speaker cap, DER 15% max at 4+ speakers | DER harness | P0 |
| PER-5 | Persistent voice-print speaker identification, 95% min for enrolled speakers | Automated against enrollment set | P0 |
| PER-6 | Over-segmentation consolidation (one person split into multiple clusters) | Cluster-count anomaly test | P0 |
| PER-7 | Ambient context classification: meeting / call / social / solo | 80% min accuracy on labelled set | P2 |

### 4.4 Memory

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| MEM-1 | Four-tier memory: working, episodic, semantic, temporal graph | Integration test across all tiers | P0 |
| MEM-2 | Bi-temporal graph — valid time and ingestion time separate; facts supersede, never overwrite | Schema assertion plus supersession test suite | P0 |
| MEM-3 | Contradiction detection on subject+predicate with divergent objects | 85% min recall on synthetic contradiction set | P0 |
| MEM-4 | Full provenance: session, speaker, timestamp, confidence on every memory | Schema NOT NULL constraints plus audit query | P0 |
| MEM-5 | Weekly human-in-the-loop review: approve / edit / veto derived beliefs | Manual flow test | P0 |
| MEM-6 | Hybrid retrieval: similarity, graph proximity, recency, importance, speaker relevance | Ablation study showing each term contributes | P0 |
| MEM-7 | Local LoCoMo 85% min, LongMemEval 80% min | CI regression gate | P0 |
| MEM-8 | Consolidation: near-duplicate episodics merged into semantic facts | Storage growth curve sub-linear over 90d | P1 |

### 4.5 Personality — the user model

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| PSU-1 | Structured, versioned user profile with evidence links on every trait | Schema check: every trait row has at least 1 source session | P0 |
| PSU-2 | Traits carry confidence and observation count; low-confidence traits not asserted in prompts | Automated prompt-assembly test | P0 |
| PSU-3 | Profile fully inspectable and editable by the user | UI flow test | P0 |
| PSU-4 | Vetoed traits suppressed and fed back into extraction prompt tuning | Test: vetoed trait absent from next 10 prompt assemblies | P0 |
| PSU-5 | Trait extraction runs on a schedule, never inline in a conversational turn | Latency assertion in Mode A path | P0 |
| PSU-6 | Profile diff reviewable — user sees what changed week over week | UI flow test | P1 |
| PSU-7 | Communication-style traits measurably affect response style | A/B: responses with and without profile differ on style metrics | P1 |

### 4.6 Personality — the assistant model

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| PSA-1 | Character card: hand-authored, version-controlled, never model-generated | Repo check: file exists, is not in any generation path | P0 |
| PSA-2 | Stance log: assistant's own prior positions recorded and retrievable | Schema plus retrieval test | P0 |
| PSA-3 | Stance consistency — no silent reversal of a prior position; reversals acknowledged | Adversarial eval: 20 reversal-bait prompts, 90% min acknowledged | P0 |
| PSA-4 | Friction mandate — conflicts between stated goals and current request are named | Eval set of 20 conflict scenarios, 80% min surfaced | P0 |
| PSA-5 | Anti-sycophancy — agreement rate with user assertions bounded | Eval: 60% max agreement on a set with 50% incorrect assertions | P0 |
| PSA-6 | Voice consistency across sessions and modes | Human rating, 4/5 min on 20 sampled exchanges | P1 |
| PSA-7 | Personality survives model swap | Same eval suite passes on 2 different base models | P1 |

### 4.7 Agency and tool execution

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| ACT-1 | Tool registry with auto-discovery and schema generation | Integration test: new tool discovered without registration edit | P0 |
| ACT-2 | Calendar, tasks, email, Drive, notes, search, reminders | Per-tool integration tests | P0 |
| ACT-3 | Four-tier risk model: READ / WRITE_REVERSIBLE / WRITE_EXTERNAL / DESTRUCTIVE | Test each tier gating behaviour | P0 |
| ACT-4 | Prompt-injection isolation: content read cannot authorise action | Red-team suite, 30 injection payloads, 0 escalations | P0 |
| ACT-5 | Approver runs in separate context with no tool access | Architecture test asserting tool list is empty | P0 |
| ACT-6 | Failed tool calls degrade gracefully with user-legible explanation | Fault injection on each tool | P0 |
| ACT-7 | Every external action logged with full pre/post state | Audit completeness query | P0 |

### 4.8 Privacy boundary — the scrubber

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| SCR-1 | Exactly one function in the codebase performs cloud egress | Static analysis: no other outbound HTTP to model endpoints | P0 |
| SCR-2 | Scrubber emits structured intent only — no transcripts, no memory, no reasoning chains | Schema whitelist; reject-by-default on unknown fields | P0 |
| SCR-3 | Person references replaced with opaque local refs; resolved after response | Round-trip test asserting no PII in egress payload | P0 |
| SCR-4 | Every egress payload logged locally in full, user-inspectable | Audit log completeness test | P0 |
| SCR-5 | Egress allowlist enforced at network layer independent of application | Test: app-layer bypass attempt blocked at proxy | P0 |
| SCR-6 | Kill switch: disable all cloud egress; system degrades to local-only | Full functionality matrix tested in local-only mode | P0 |
| SCR-7 | Named-entity leak detection on outbound payloads with hard fail | Adversarial test: 50 payloads containing PII, 0 pass | P0 |

### 4.9 Security

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| SEC-1 | E2E encryption phone to server; server holds no plaintext at rest | Disk inspection test | P0 |
| SEC-2 | Per-record envelope encryption, user-held master key | Key hierarchy test | P0 |
| SEC-3 | Cryptographic erasure per person or timeframe including derived summaries | Erasure completeness test across all stores | P0 |
| SEC-4 | Zero telemetry; no crash reporting, no analytics | 24h network capture shows no unexpected egress | P0 |
| SEC-5 | Encrypted off-site backup with monthly tested restore | Automated restore drill | P0 |
| SEC-6 | No secrets in repo; pre-commit secret scanning | CI gate | P0 |

### 4.10 Open source compliance

| ID | Requirement | Verification | Pri |
|---|---|---|---|
| OSS-1 | Licence inventory for every dependency and model weight | Automated SBOM generation in CI | P0 |
| OSS-2 | NOTICE file with all attribution-required components (Parakeet-TDT is CC-BY) | Manual review plus CI presence check | P0 |
| OSS-3 | Copyleft isolation — no GPL/AGPL in the distributable path without review | CI licence-policy gate | P0 |
| OSS-4 | Own code licensed and headers applied consistently | CI header check | P1 |
| OSS-5 | Releasable components separated from private ones at package boundaries | Architecture review per phase | P1 |
| OSS-6 | Upstream contributions where fixes benefit source projects | Tracked per phase | P2 |

---

## 5. Legal position

Unchanged from v1.0 and reaffirmed:

- India follows one-party consent (*R.M. Malkani v. State of Maharashtra*, 1973 1 SCC 471). Recording conversations you participate in is lawful.
- Recording conversations you are **not** party to is unlawful under Telecommunications Act 2023 s.20. Mode B requires explicit user activation and the user is always present — CVB-7 is the architectural enforcement of this.
- Absolute prohibited zones (restrooms, changing rooms) under IT Act s.66E and BNS 2023 s.77.
- DPDP Act 2023 and Rules 2025: voice is personal data. **The personal-use exemption is load-bearing.** It ends the moment Nimbus processes data for anyone but its owner.
- Any two-party-consent jurisdiction (much of the EU, several US states) requires a different Mode B design.

*General information, not legal advice. Retain counsel before any commercial or multi-user path.*

---

## 6. Differentiating features by phase

| Feature | Phase | Depends on |
|---|---|---|
| Commitment ledger | P4 | Diarization, speaker ID, extraction |
| Relationship intelligence | P4 | Speaker ID, fact extraction, graph |
| Pre-meeting briefing | P5 | Relationship layer, calendar tool |
| Temporal contradiction detection | P4 | Bi-temporal graph |
| Decision journal and retrospective | P5 | Extraction, scheduler |
| Prospective memory (person/location triggers) | P5 | Speaker ID, geofence |
| Idea resurrection | P5 | Episodic memory, semantic triggers |
| Cryptographic selective amnesia | P6 | Per-record DEKs |

---

## 7. KPIs

### 7.1 Technical KPIs — gate phase completion

| KPI | Target | Measured |
|---|---|---|
| Mode A voice latency p50 | 800ms max | Per build, automated |
| Mode A voice latency p95 | 1200ms max | Per build, automated |
| ASR WER, English, office acoustics | 10% max | Weekly, eval set |
| ASR WER, code-switched EN/HI | 18% max | Weekly, eval set |
| Diarization DER, 4+ speakers | 15% max | Weekly, eval set |
| Speaker ID accuracy, enrolled | 95% min | Weekly |
| Memory retrieval, local LoCoMo | 85% min | Per commit to memory layer |
| Memory retrieval, LongMemEval | 80% min | Per commit to memory layer |
| Commitment extraction F1 | 0.90 min | Weekly, labelled set |
| Contradiction detection recall | 85% min | Weekly, synthetic set |
| Injection escalations | 0 | Per release, red-team suite |
| PII leak in egress payloads | 0 | Per commit, adversarial suite |
| Mode B capture loss | 0.5% max | Weekly soak test |
| Unplanned cloud egress events | 0 | Continuous network monitoring |

### 7.2 Product KPIs — the ones that decide if this was worth building

| KPI | Target | Measured |
|---|---|---|
| Days used per week, after month 2 | 6 min | Usage log |
| Mode A turns per day | 8 min | Usage log |
| Mode B sessions per week | 5 min | Usage log |
| Commitment closure rate vs pre-Nimbus baseline | +40% | Monthly self-report plus ledger |
| Proactive interruptions rated useful | 70% min | In-app rating |
| Proactive interruptions per day | 6 max (ceiling) | Enforced by budget |
| Weekly memory-review veto rate | 10% max | Curation flow |
| Assistant personality rated consistent | 4/5 min | Monthly self-rating |
| "Told me something I didn't know about myself" | 1 per month min | Qualitative log |
| Cloud spend | 40 USD per month max | Billing |

The interruption ceiling is a hard constraint, not a target to optimise toward. Every incumbent's community feedback shows notification-happy assistants get permanently muted in week two.

---

## 8. Risks

| Risk | Severity | Mitigation | Phase |
|---|---|---|---|
| Mode A latency budget unachievable on local models | High | Week-2 spike before architecture commits; fallback to smaller model or streaming-optimised serving | P0 |
| Diarization fails in real acoustics | High | Aggressive enrollment; hybrid streaming/offline; accept Mode B-only diarization | P2 |
| Assistant personality reads as gimmick, not substance | High | Friction mandate and anti-sycophancy evals as hard gates, not aspirations | P3 |
| Scrubber leaks context through an unanticipated path | Critical | Single egress function, network-layer allowlist, adversarial suite in CI | P3 |
| Agent team declares completion without verification | High | Separate eval agent; no self-grading; machine-checkable criteria only | All |
| Gemini pricing doubles 1 Jan 2027 | Medium | Model second-year budget at 1.50/7.50; keep local fallback viable | P4 |
| Memory graph performance degrades at scale | Medium | Synthetic 2-year load test at P5 | P5 |
| Licence violation in released code | Medium | SBOM in CI; Parakeet CC-BY attribution; copyleft policy gate | All |
| Scope creep — large system for one user | High | Phase gates are hard stops; each phase independently useful | All |

---

## 9. Open decisions

1. **Mode A local model.** Depends on the P0 latency spike. Options: Qwen3.8-27B quantised, Gemma 4 26B A4B, or a smaller dedicated conversational model with the larger model reserved for Mode B processing.
2. **TTS engine.** Kokoro (fast, lighter) vs XTTS-v2 (better quality, voice cloning for a custom assistant voice). Personality consistency argues for a distinctive voice; latency argues for Kokoro. Decide at P1.
3. **Realtime API for Mode A.** Gemini Live-class APIs would solve latency instantly but route raw voice to a cloud provider — architecturally disqualified unless used with zero memory context. *Recommendation: local only for Mode A.*
4. **Assistant voice identity.** Cloned, synthesised, or off-the-shelf. Affects PSA-6.
5. **What to open-source and when.** See section 10.

---

## 10. Open-source strategy

The owner is an active OSS contributor. This is treated as a product requirement, not a preference.

### 10.1 Licence obligations inherited

| Component | Licence | Obligation |
|---|---|---|
| Parakeet-TDT 0.6B v2 | **CC-BY** | **Attribution required in any distribution or product surface** |
| pyannote-segmentation-3.0, CAM++ | MIT | Attribution in NOTICE |
| Silero VAD | MIT | Attribution |
| Qwen3.8-27B | Apache 2.0 | Attribution, state changes |
| Gemma 4 | Apache 2.0 | Attribution, state changes |
| Omi (client reference) | MIT | Attribution |
| LangGraph, Qdrant, Postgres, AGE | MIT / Apache / PostgreSQL | Attribution |

Parakeet's CC-BY is the one that catches people — it is not a permissive software licence and the attribution requirement is real. OSS-2 exists for this.

### 10.2 Candidate releases

Components with no privacy coupling, genuinely novel, and useful standalone:

| Component | Why it's worth releasing | Phase |
|---|---|---|
| **Bi-temporal memory layer** | No open implementation combines valid/ingestion time, supersession, provenance and contradiction detection over Postgres+AGE | P6 |
| **The scrubber** | Reusable pattern for anyone doing hybrid local/cloud agents. Small, auditable, high-value | P4 |
| **Commitment extraction** | Prompt, schema and eval set. Nothing comparable exists open | P5 |
| **Mode A/B state machine** | Two-mode voice capture with unmistakable state is a solved-badly problem | P3 |
| **Local memory eval harness** | Reproducible LoCoMo/LongMemEval runner for self-hosted stacks; the field needs one given vendors dispute each other's numbers | P2 |

**Never released:** personality profile schemas containing real data, the character card, any eval set built from real capture, key management specifics.

### 10.3 Upstream contributions

Where fixes benefit the source project, upstream them. Expected candidates: pyannote over-segmentation consolidation, Omi Flutter BLE client improvements, faster-whisper code-switching handling, Qdrant encrypted-payload patterns.

Tracked as a first-class deliverable per phase, not spare-time work.

---

*End of PRD v2.0.*
