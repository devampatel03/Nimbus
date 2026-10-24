# Project Nimbus — Execution Plan

**Version:** 2.0
**Date:** 10 September 2026
**Build team:** Autonomous agent pods, human owner as reviewer and gate authority
**Companion docs:** `PRD.md`, `TECHNICAL_ARCHITECTURE.md`, `AGENT_OPERATING_MANUAL.md`

---

## 1. Agent pod structure

Eight pods. Each owns a layer end to end and is accountable for its own test suite. Durations are indicative — agent throughput varies widely and phase gates, not calendars, control progression.

| Pod | Owns | Key artefacts |
|---|---|---|
| **P-CLIENT** | Flutter app, mode state machine, on-device VAD and stop-phrase, buffering | `mobile/` |
| **P-PERCEPT** | ASR, diarization, speaker ID, enrollment, language routing | `perception/` |
| **P-MEMORY** | Four-tier memory, bi-temporal graph, retrieval, contradiction detection | `memory/` |
| **P-PERSONA** | User trait model, agent character card, stance log, friction mandate | `personality/` |
| **P-AGENT** | LangGraph runtime, tool registry, risk model, injection isolation, proactive | `orchestrator/`, `tools/` |
| **P-SEC** | Scrubber, key hierarchy, crypto erasure, egress proxy, audit | `security/`, `infra/` |
| **P-EVAL** | **All benchmarks. Verifies other pods' work. Writes no feature code.** | `eval/` |
| **P-OSS** | Licence compliance, SBOM, NOTICE, release packaging, upstreaming | `compliance/` |

### 1.1 The P-EVAL rule

**P-EVAL never grades work it wrote, and no pod grades its own work.**

This is the single most important structural decision in an agent-built system. Agents are strongly biased toward declaring completion. An agent that writes both the implementation and its acceptance test will produce a test that passes. P-EVAL owns every metric in this document, writes the harnesses, and holds sole authority to mark a task complete.

P-EVAL's own harnesses are reviewed by the human owner.

---

## 2. Phase overview

| Phase | Objective | Gate |
|---|---|---|
| **P0** | De-risk assumptions before building on them | Latency spike passes, eval harnesses reproduce |
| **P1** | Mode A working — daily-usable voice assistant | 2 weeks unprompted daily use |
| **P2** | Mode B working — record, transcribe, recall | Speaker ID and memory targets met |
| **P3** | Personality and privacy boundary | Anti-sycophancy and scrubber gates pass |
| **P4** | Agency and the first differentiators | Commitments and contradictions working |
| **P5** | Depth features | Surfaces a non-obvious personal insight |
| **P6** | Hardening and open-source release | Red-team clean, components released |

Each phase must produce something independently useful. If a phase's output is not used voluntarily, the thesis is wrong and the programme stops rather than compounding on a bad foundation.

---

## PHASE 0 — Foundation and de-risking

**Objective:** kill bad assumptions before anything depends on them.

### Tasks

| ID | Task | Pod | Acceptance criterion (machine-checkable) |
|---|---|---|---|
| T0.1 | Repo, CI, pre-commit, secret scanning | P-SEC | CI green; deliberate secret commit is blocked |
| T0.2 | Server provisioned: LUKS, WireGuard, egress proxy, no public ingress | P-SEC | `nmap` from outside shows 0 open ports; proxy blocks non-allowlisted host |
| T0.3 | **Mode A latency spike** — end-to-end stub with real models | P-AGENT | p50 800ms, p95 1200ms over 100 synthetic turns |
| T0.4 | LoCoMo + LongMemEval harness reproducing | P-EVAL | Harness runs unattended, produces score, variance across 3 runs under 2% |
| T0.5 | ASR baseline measured on real recordings | P-PERCEPT | WER reported for EN and EN/HI on a 60-min labelled set |
| T0.6 | Diarization baseline measured | P-PERCEPT | DER reported at 2, 4 and 6 speakers |
| T0.7 | Build EN/HI code-switched eval set | P-EVAL | 30 min labelled audio, word-level language tags |
| T0.8 | Interface contract schemas frozen and fixture-tested | All | Every contract in TECH ARCH section 12 has schema plus golden fixture |
| T0.9 | Licence inventory and SBOM automation | P-OSS | SBOM generated in CI; Parakeet CC-BY flagged in NOTICE |
| T0.10 | Invariant tests INV-1 to INV-5 written and failing-by-default | P-EVAL | All 5 tests exist; each fails against a deliberate violation |

### Exit gate

- T0.3 passes, or a documented architecture change is made before P1 begins.
- All eval harnesses run unattended and reproduce.
- Invariant tests exist and demonstrably catch violations.

### What can go wrong here

**The latency spike is done with a stubbed model and declared passing.** The spike MUST use the actual model at the actual quantisation with actual retrieval. A stub proves nothing and this is the most likely place for an agent to produce a false green.

**Eval harnesses that don't reproduce.** If three runs give three different scores, the harness is measuring noise and every downstream gate is meaningless. Variance under 2% is a hard requirement.

**Published benchmark numbers copied instead of reproduced.** Vendors dispute each other's LoCoMo methodology and at least one independent audit has questioned the benchmark. Only our own run counts.

**Interface contracts left informal.** Parallel agent pods with informal contracts produce silent drift that surfaces three phases later as an integration disaster.

---

## PHASE 1 — Mode A (Converse)

**Objective:** a voice assistant good enough to use daily, with basic memory. No recording yet.

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T1.1 | Flutter app shell, audio session, PTT from 3 entry points | P-CLIENT | Manual test passes on app, widget, hardware button |
| T1.2 | Mode state machine, IDLE and CONVERSING only | P-CLIENT | State transition test suite, 100% branch coverage |
| T1.3 | Streaming ASR pipeline, WS transport | P-PERCEPT | Partial hypothesis within 300ms, measured |
| T1.4 | Streaming TTS integration | P-AGENT | First audio within 120ms of first token |
| T1.5 | vLLM serving with chosen model | P-AGENT | TTFT 250ms max at p95 under single-user load |
| T1.6 | Working + episodic memory tiers | P-MEMORY | Integration test writes and retrieves across both |
| T1.7 | Warm context slice in Redis, refreshed post-turn | P-MEMORY | Retrieval within turn 100ms max, measured |
| T1.8 | Barge-in handling | P-CLIENT | Playback stops within 200ms of user speech onset |
| T1.9 | Endpointing tuned against false cutoff | P-PERCEPT | 5% max premature cutoff on eval set |
| T1.10 | Basic conversational agent loop | P-AGENT | Multi-turn coherence over 20-turn eval conversations |
| T1.11 | E2E encryption phone to server | P-SEC | Disk inspection shows no plaintext; MITM test fails to decrypt |
| T1.12 | Latency regression test in CI | P-EVAL | CI fails if p50 exceeds 800ms |

### Metrics

| Metric | Target |
|---|---|
| Mode A latency p50 / p95 | 800 / 1200 ms |
| ASR partial latency | 300ms max |
| Barge-in response | 200ms max |
| False endpoint rate | 5% max |
| Multi-turn coherence (human rated) | 4/5 min |
| Crash-free sessions | 99% min |

### Exit gate

Owner has used Mode A voluntarily, daily, for 14 consecutive days. **This is a behavioural gate, not a technical one, and it cannot be argued past.**

### What can go wrong here

**Latency creeps up silently as features land.** Every commit adds a little. Without T1.12 in CI from day one, you discover at P4 that you are at 2.3 seconds and the fix is architectural.

**Retrieval moves inside the turn.** Someone will "improve" quality by adding a live vector search to the response path. This breaks the budget and INV-4. The warm-slice pattern exists precisely to prevent this.

**Endpointing tuned for latency at the cost of cutoffs.** Aggressive endpointing looks great on the latency dashboard and makes the product infuriating. CVA-4 is the counterweight; both metrics must be reported together, always.

**TTS chosen for quality without measuring first-audio latency.** Non-streaming TTS is disqualified regardless of how good it sounds.

**The agent declares Mode A done because the happy path works.** Interruptions, network blips, backgrounding, and simultaneous speech are where voice assistants actually fail.

---

## PHASE 2 — Mode B (Record and recall)

**Objective:** record a real conversation, get an accurate speaker-attributed transcript, ask questions about it later.

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T2.1 | Mode B state machine: ARMING, RECORDING, exits | P-CLIENT | All transitions tested; no auto-entry path exists (CVB-7 assertion) |
| T2.2 | On-device stop-phrase detector | P-CLIENT | 99% min detection on adversarial set incl. crosstalk and music |
| T2.3 | Recording state indicators: notification, chimes, spoken confirmation | P-CLIENT | Manual checklist across 6 transitions |
| T2.4 | Encrypted local ring buffer, chunked upload | P-CLIENT | Airplane-mode test: 0 loss over 90 min |
| T2.5 | 3h soak test | P-EVAL | No crash, no leak, no thermal throttle |
| T2.6 | Batch ASR with language routing | P-PERCEPT | WER 10% max EN, 18% max EN/HI |
| T2.7 | Diarization pipeline plus consolidation pass | P-PERCEPT | DER 15% max at 4+ speakers; 0 duplicate-speaker artefacts |
| T2.8 | Speaker enrollment flow and voice-print store | P-PERCEPT | 95% min ID accuracy on enrolled set |
| T2.9 | Bi-temporal graph schema and supersession | P-MEMORY | INV-3 test passes; supersession suite green |
| T2.10 | Semantic tier and entity/relation extraction | P-MEMORY | Extraction F1 0.80 min on labelled set |
| T2.11 | Hybrid retrieval with ablation study | P-MEMORY | Each scoring term shown to contribute; LoCoMo 85% min |
| T2.12 | Provenance completeness enforcement | P-MEMORY | INV-5 test passes; nightly integrity job deletes orphans |
| T2.13 | Release eval harness as OSS | P-OSS | Public repo, NOTICE correct, reproducible from clean clone |

### Metrics

| Metric | Target |
|---|---|
| WER English | 10% max |
| WER code-switched EN/HI | 18% max |
| DER, 4+ speakers | 15% max |
| Speaker ID, enrolled | 95% min |
| Stop-phrase detection | 99% min |
| Capture loss | 0.5% max |
| LoCoMo (local) | 85% min |
| LongMemEval (local) | 80% min |
| Entity extraction F1 | 0.80 min |

### Exit gate

Ask "what did we decide about X in Tuesday's conversation?" and receive a correct, speaker-attributed, sourced answer. Twenty such queries, 85% correct minimum.

### What can go wrong here

**Speaker ID is the make-or-break of the entire product.** Every differentiator in P4 and P5 depends on it. If DER and ID accuracy miss target, do not proceed to P4 — fix perception or re-scope the differentiators. This is the highest-risk item in the programme.

**Enrollment is treated as optional.** Zero-shot speaker ID is why incumbents label users "Unknown" in their own recordings. Enrollment for the top 20 speakers is mandatory work, not a nice-to-have.

**Over-segmentation ships unnoticed.** One person appearing as three speakers corrupts every downstream attribution. PER-6 consolidation must be verified explicitly, not assumed.

**Code-switching evaluated on public benchmarks.** There is no good public EN/HI code-switched conversational benchmark. T0.7 exists because this must be measured on real data.

**Graph mutation creeps in.** An agent will find supersession verbose and "simplify" it to an UPDATE. INV-3 catches this only if the test is actually in CI and actually asserts on the object columns.

**Stop-phrase tested only in quiet conditions.** It is a safety control. Test it against music, crosstalk, the phrase spoken by another person, and the phrase inside a longer sentence.

---

## PHASE 3 — Personality and the privacy boundary

**Objective:** the assistant becomes a specific entity rather than a generic model, and the cloud boundary is built before any cloud call is made.

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T3.1 | User trait schema, evidence linkage, confidence | P-PERSONA | PSU-1 check: 0 traits without evidence |
| T3.2 | Trait extraction workers, scheduled not inline | P-PERSONA | INV-4 unaffected; latency assertion passes |
| T3.3 | Weekly review flow: approve, edit, veto | P-PERSONA | Full UI flow test; vetoed trait absent from next 10 assemblies |
| T3.4 | Character card authored and CI-protected | P-PERSONA | File exists; CI fails if any code path writes it |
| T3.5 | Stance log schema and retrieval | P-PERSONA | Stances retrievable by topic; reversal detection works |
| T3.6 | Friction mandate implementation | P-PERSONA | 80% min of 20 conflict scenarios surfaced |
| T3.7 | Anti-sycophancy eval and tuning | P-EVAL | 60% max overall agreement; 20% max on incorrect subset |
| T3.8 | Stance consistency eval | P-EVAL | 90% min of 20 reversal-bait prompts acknowledged |
| T3.9 | **Scrubber implementation** | P-SEC | INV-1 and INV-2 pass; SCR-1 to SCR-7 all green |
| T3.10 | Egress proxy allowlist, network layer | P-SEC | App-layer bypass attempt blocked at proxy |
| T3.11 | PII leak detector on egress payloads | P-SEC | 50 adversarial payloads, 0 pass |
| T3.12 | Cloud kill switch and local-only mode | P-SEC | Full functionality matrix documented in local-only mode |
| T3.13 | Egress audit log, user-inspectable | P-SEC | Every payload logged before send; log is complete |
| T3.14 | Release scrubber as OSS | P-OSS | Public repo with docs and threat model |

### Metrics

| Metric | Target |
|---|---|
| Traits without evidence | 0 |
| Anti-sycophancy agreement rate | 60% max |
| Agreement with incorrect assertions | 20% max |
| Stance reversal acknowledgement | 90% min |
| Friction scenarios surfaced | 80% min |
| PII leaks in egress | 0 |
| Voice consistency (human rated) | 4/5 min |

### Exit gate

Two gates, both required:

1. The scrubber passes all adversarial tests with zero leaks.
2. Owner rates the assistant's personality as consistent and useful (4/5 min) across 20 sampled exchanges, **and** it has demonstrably pushed back on at least three of the owner's requests.

### What can go wrong here

**Personality becomes decoration.** The friction mandate and anti-sycophancy evals are what make personality substantive rather than a voice skin. If those evals are weakened to pass, the whole subsystem is theatre. The reference case: the owner's earlier prototype "gave too much personality to the bot" — personality without a job.

**The character card gets generated.** An agent will find hand-authoring inefficient and offer to generate it. This defeats the purpose entirely: a generated character card is the model describing itself, which produces exactly the generic voice this subsystem exists to avoid. PSA-1's CI check is not optional.

**Trait extraction hallucinates.** Traits asserted without evidence are the personality equivalent of a hallucinated citation, and they are worse because they silently shape every future response. The nightly integrity job that deletes evidence-free traits is a P0 control.

**Veto feedback loop not closed.** If vetoed traits keep being re-derived, the user stops reviewing, and unreviewed accumulation is exactly the failure mode this system exists to avoid.

**Scrubber built after the first cloud call.** Build the boundary before anything crosses it. Retrofitting a privacy boundary means auditing every existing call site, and one missed site is a permanent leak.

**Free-text parameters as a leak channel.** The scrubber's schema whitelist is only as good as the per-tool param schemas. Every new tool's params need review specifically for context-carrying free text.

---

## PHASE 4 — Agency and first differentiators

**Objective:** the assistant does things, and the commitment ledger goes live.

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T4.1 | Tool registry with auto-discovery | P-AGENT | New tool discovered without editing registration |
| T4.2 | Google tool integrations: calendar, gmail, drive, tasks | P-AGENT | Per-tool integration tests against real accounts |
| T4.3 | Four-tier risk model and gating | P-AGENT | Each tier's behaviour tested |
| T4.4 | Approver agent, zero tools, separate context | P-AGENT | ACT-5 assertion: tool list empty |
| T4.5 | Injection red-team suite | P-EVAL | 30 payloads, 0 escalations |
| T4.6 | Gemini 3.8 Flash integration via scrubber only | P-AGENT | INV-1 holds; all calls route through scrubber |
| T4.7 | Async tool execution with acknowledgement | P-AGENT | CVA-6: ack 800ms max even when tool takes 15s |
| T4.8 | **Commitment extraction worker** | P-MEMORY | F1 0.90 min against hand-labelled set |
| T4.9 | Commitment ledger, tracking and closure detection | P-MEMORY | Closure detected from later sessions |
| T4.10 | **Contradiction detection live** | P-MEMORY | 85% min recall on synthetic set |
| T4.11 | **Relationship intelligence layer** | P-MEMORY | Per-person cadence, topics, facts, open loops |
| T4.12 | Proactive engine with hard interruption budget | P-AGENT | Budget enforced in code; 6 max per day |
| T4.13 | Action audit log, full pre/post state | P-SEC | Audit completeness query returns 100% |
| T4.14 | Cloud cost monitoring and alerting | P-SEC | Alert at 80% of monthly budget |

### Metrics

| Metric | Target |
|---|---|
| Commitment extraction F1 | 0.90 min |
| Commitment closure detection | 80% min |
| Contradiction recall | 85% min |
| Injection escalations | 0 |
| Tool success rate | 95% min |
| Async ack latency | 800ms max |
| Proactive usefulness rating | 70% min |
| Interruptions per day | 6 max |
| Cloud spend | 40 USD per month max |

### Exit gate

Commitment ledger captures 90% of commitments the owner makes over a two-week period, measured against manual ground truth. At least one contradiction surfaced that the owner agrees was worth surfacing.

### What can go wrong here

**Gemini's 13.3s TTFT blocks a conversational turn.** T4.7 exists for exactly this. Any code path where a Mode A turn awaits a Gemini response is a defect regardless of how it performs in testing on a fast day.

**Thinking tokens blow the budget.** Gemini bills thinking tokens at the output rate and they are invisible in the response body. Monitor `total_thought_tokens`, not just visible output. Budget the second year at doubled rates from 1 January 2027.

**Injection testing done only against obvious payloads.** Test subtle ones: an instruction in a calendar invite description, in an email signature, in a shared document's metadata.

**Commitment extraction over-fires.** Every "I'll think about it" becoming a tracked commitment produces a ledger the owner abandons. Precision matters as much as recall — F1, not recall alone.

**The proactive budget is treated as a default rather than a ceiling.** It is enforced in code because a configurable limit will be raised the first time something feels urgent, and then the assistant gets muted permanently.

**Approver agent given tools "temporarily" for debugging.** This eliminates the entire isolation guarantee. ACT-5's assertion must run in CI, not just at review.

---

## PHASE 5 — Depth

**Objective:** capabilities that make the system feel like it knows something the owner does not.

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T5.1 | Pre-meeting briefing generation | P-AGENT | Brief generated 30 min before every calendar event with known attendees |
| T5.2 | Decision journal extraction and storage | P-MEMORY | Decisions captured with reasoning and review date |
| T5.3 | Automatic decision retrospective | P-AGENT | Review triggered at horizon; outcome captured |
| T5.4 | Prospective memory: time, location, person triggers | P-AGENT | Person-triggered reminder fires on voice ID match |
| T5.5 | Idea resurrection | P-MEMORY | Semantically adjacent idea surfaced from 30+ days prior |
| T5.6 | Energy and cognitive-load mapping | P-PERSONA | Pattern report generated; no clinical language |
| T5.7 | Memory consolidation jobs | P-MEMORY | Storage growth sub-linear over 90 days |
| T5.8 | Graph performance at 2-year synthetic scale | P-EVAL | Retrieval 100ms max at 2-year data volume |
| T5.9 | Release commitment extraction as OSS | P-OSS | Repo with prompt, schema, eval set (synthetic data only) |

### Metrics

| Metric | Target |
|---|---|
| Briefing usefulness rating | 75% min |
| Prospective memory trigger accuracy | 90% min |
| Idea resurrection precision | 60% min |
| Retrieval latency at 2-year scale | 100ms max |
| Storage growth | Sub-linear |

### Exit gate

The system surfaces at least one genuinely non-obvious insight about the owner's own patterns that they did not already know. Qualitative, owner-judged, and deliberately so.

### What can go wrong here

**Energy mapping drifts into diagnosis.** Report patterns, never conclusions about health. "Your meeting density on Thursdays correlates with shorter responses" is fine. Anything resembling a clinical claim is out of scope and out of competence.

**Idea resurrection becomes noise.** Precision target is 60% and low precision here is worse than not shipping it — resurfacing irrelevant old ideas trains the owner to ignore the channel.

**Graph performance degrades unnoticed.** T5.8 must run against synthetic two-year data, not the few months of real data that exists by then.

**Consolidation destroys provenance.** Merging episodics into semantic facts must preserve the source chain, or INV-5 breaks and every downstream claim becomes unverifiable.

---

## PHASE 6 — Hardening and release

### Tasks

| ID | Task | Pod | Acceptance criterion |
|---|---|---|---|
| T6.1 | External red-team, full system | P-EVAL | Findings triaged; criticals fixed |
| T6.2 | Cryptographic erasure, per person and timeframe | P-SEC | Erasure verified across all stores incl. derived summaries |
| T6.3 | Backup restore drill | P-SEC | Full restore from off-site to clean machine |
| T6.4 | 24h network capture audit | P-SEC | 0 unexpected egress events |
| T6.5 | Bi-temporal memory layer OSS release | P-OSS | Repo, docs, benchmarks, NOTICE, reproducible |
| T6.6 | Mode state machine OSS release | P-OSS | Repo with state diagram and test suite |
| T6.7 | Upstream contributions submitted | P-OSS | PRs opened to pyannote, Omi, or faster-whisper |
| T6.8 | Full documentation pass | All | Every module has README; architecture doc current |

### Exit gate

Red-team clean on criticals. Restore drill successful. All planned OSS components released with correct attribution.

### What can go wrong here

**Cryptographic erasure misses derived summaries.** This is the step everyone skips. A summary generated from a deleted conversation still contains it. Verify erasure by searching for known content across every store after erasure, including generated summaries.

**OSS release leaks real data.** Eval sets built from real capture must never ship. Only synthetic. Check every fixture file before release.

**Parakeet attribution missing.** CC-BY is not a permissive software licence. The attribution requirement is real and applies to any distribution.

---

## 3. Cross-phase KPI dashboard

P-EVAL publishes weekly. Numbers only, no narrative.

| KPI | Target | Phase introduced |
|---|---|---|
| Mode A latency p50 / p95 | 800 / 1200ms | P1 |
| ASR WER EN / EN-HI | 10% / 18% max | P2 |
| Diarization DER 4+ speakers | 15% max | P2 |
| Speaker ID accuracy | 95% min | P2 |
| LoCoMo / LongMemEval local | 85% / 80% min | P2 |
| Traits without evidence | 0 | P3 |
| Anti-sycophancy agreement | 60% max | P3 |
| PII leaks in egress | 0 | P3 |
| Commitment extraction F1 | 0.90 min | P4 |
| Contradiction recall | 85% min | P4 |
| Injection escalations | 0 | P4 |
| Proactive usefulness | 70% min | P4 |
| Interruptions per day | 6 max | P4 |
| Cloud spend | 40 USD/month max | P4 |
| Days used per week | 6 min | P1 onward |
| Unplanned egress events | 0 | P0 onward |

---

## 4. Universal failure modes

These apply to every phase and every pod. They are the failures that recur in agent-built systems specifically.

**Completion declared without verification.** The single most common failure. Mitigation: P-EVAL owns all completion decisions, and no pod grades its own work.

**Tests weakened to pass.** An agent facing a failing test will sometimes adjust the test. Mitigation: test files owned by P-EVAL; changes to acceptance criteria require human approval and a recorded rationale.

**Silent interface drift.** Parallel pods change shared contracts unilaterally. Mitigation: frozen schemas with golden fixtures and compatibility tests that fail on field removal or type change.

**Architectural context lost across sessions.** Agents lose the "why" and re-litigate settled decisions. Mitigation: invariants INV-1 to INV-5 are tested, not documented; documented rationale for every architectural decision.

**Hallucinated APIs.** Agents invent library functions that do not exist. Mitigation: integration tests against real dependencies, not mocks, for every external library.

**Security shortcuts.** Disabled TLS verification, hardcoded credentials, broadened permissions "for testing." Mitigation: CI secret scanning, and P-SEC review required on any diff touching auth, crypto, or network.

**Gold-plating.** Building beyond the acceptance criterion. Mitigation: acceptance criteria are ceilings as well as floors; extra scope requires a new task ID.

**Happy-path-only implementation.** Error handling, timeouts, retries and partial failures skipped. Mitigation: fault injection required for every external dependency before task completion.

**Documentation drift.** Code changes, spec does not. Mitigation: architecture doc is normative; a diff that changes an interface must update it in the same commit.

---

*End of execution plan v2.0.*
