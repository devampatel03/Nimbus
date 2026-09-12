# Project Nimbus — Agent Operating Manual and Grading Checklists

**Version:** 2.0
**Date:** 10 September 2026
**Audience:** Build agents
**Authority:** This document is binding. Where it conflicts with an agent's judgement, this document wins.

---

## 0. Read this first

You are one pod in an eight-pod team building a system where a single privacy failure destroys the entire product thesis. Your work will be verified by a different agent (P-EVAL) who did not write it and has no stake in it passing.

Three rules override everything else:

1. **You do not mark your own work complete.** P-EVAL does. Submitting work that fails its checklist wastes a verification cycle and is tracked.
2. **You do not modify acceptance criteria, test files owned by P-EVAL, or invariant tests.** If a criterion seems wrong, raise it; do not edit it.
3. **When the specification and your implementation disagree, the specification is right.** Amend the spec through review, or change your code. Never let them silently diverge.

---

## 1. Before you start any task

Complete this before writing code. If any answer is "no" or "unclear", stop and ask.

- [ ] I have read the task's acceptance criterion and can state how it will be measured
- [ ] I know which requirement IDs (from PRD section 4) this task satisfies
- [ ] I know which interface contracts I produce and which I consume
- [ ] I have read the current schema of every contract I touch
- [ ] I know which invariants (INV-1 to INV-5) my work could violate
- [ ] I have checked whether an existing module already does this
- [ ] I know what "done" excludes — the scope ceiling, not just the floor
- [ ] I have identified the external dependencies I need and confirmed their real APIs (not from memory)

---

## 2. Universal completion checklist

**Every task, every pod.** All items must pass before submitting to P-EVAL.

### 2.1 Correctness

- [ ] The acceptance criterion is met and I have the measurement output to prove it
- [ ] The measurement was taken with real dependencies, not stubs or mocks
- [ ] I ran the test at least 3 times; results are stable
- [ ] Edge cases handled: empty input, malformed input, maximum size, concurrent access
- [ ] Error paths return useful, non-sensitive messages
- [ ] Timeouts set on every external call, with a documented value and rationale
- [ ] Retries have backoff and a maximum attempt count
- [ ] Partial failure leaves the system in a consistent state

### 2.2 Invariants

- [ ] INV-1: I have not added any outbound call to a cloud model outside the scrubber
- [ ] INV-2: No memory content, transcript, or reasoning chain can reach an egress payload through my code
- [ ] INV-3: I have not written an UPDATE that mutates a graph fact's object or valid_from
- [ ] INV-4: My code does not add a blocking network call to the Mode A response path
- [ ] INV-5: Every memory row my code creates has non-null provenance
- [ ] I ran the full invariant suite locally and it passed

### 2.3 Interfaces

- [ ] I did not change a frozen contract; or if I did, it was through an approved amendment
- [ ] Golden fixtures updated if and only if the change was approved
- [ ] Compatibility test passes: no field removed, no type narrowed
- [ ] My consumers' tests still pass

### 2.4 Security

- [ ] No secrets, keys, tokens or credentials in code, config, comments or test fixtures
- [ ] No TLS verification disabled, anywhere, for any reason
- [ ] No permissions broadened "temporarily"
- [ ] Any new external dependency has been checked for licence and known CVEs
- [ ] Logging contains no personal data, transcript content, or memory content
- [ ] Exception messages sanitised — they do not echo input back

### 2.5 Tests

- [ ] Tests exist and are meaningful (they fail when the code is deliberately broken)
- [ ] I verified this by breaking the code and confirming the test fails
- [ ] No test was weakened, skipped, or marked xfail to achieve a pass
- [ ] Fault injection performed for every external dependency
- [ ] Test data is synthetic; no real capture data in fixtures

### 2.6 Documentation

- [ ] Module README updated
- [ ] Architecture doc updated in the same commit if an interface changed
- [ ] Rationale recorded for any non-obvious decision
- [ ] Licence header applied

### 2.7 Scope

- [ ] I built what was asked and no more
- [ ] Anything extra I noticed was filed as a new task rather than built
- [ ] I did not refactor unrelated code

---

## 3. Pod-specific checklists

### 3.1 P-CLIENT

- [ ] Mode state machine: every transition tested, 100% branch coverage
- [ ] **No code path enters RECORDING without explicit user action** (CVB-7) — verified by test, not inspection
- [ ] Recording state is visible in the notification tray at all times while recording
- [ ] Entry and exit chimes play and are distinguishable from each other
- [ ] "Are you recording?" returns a correct spoken answer in every state
- [ ] Stop-phrase detection runs on-device and works with the network disabled
- [ ] Stop-phrase tested against: music, crosstalk, another person's voice, phrase inside a longer sentence
- [ ] App crash or force-quit during recording ends the session and preserves the buffer
- [ ] Buffer is encrypted at rest on the device
- [ ] Airplane-mode test: no capture loss over 90 minutes
- [ ] 3h soak: no crash, no memory growth, no thermal throttle
- [ ] Barge-in stops playback within 200ms
- [ ] Backgrounding and foregrounding preserve conversation state

### 3.2 P-PERCEPT

- [ ] WER measured on the labelled eval set, not a demo clip
- [ ] Code-switched WER measured separately and reported separately
- [ ] DER reported at 2, 4 and 6 speakers, not averaged
- [ ] Over-segmentation consolidation verified: no duplicate speakers in output
- [ ] Speaker ID accuracy measured against the enrolled set only
- [ ] Unenrolled speakers are labelled unknown, never guessed as a known speaker
- [ ] Language routing tested on utterances that switch mid-sentence
- [ ] Streaming path partial-hypothesis latency measured, not estimated
- [ ] Plaintext audio never written to disk; tmpfs only, shredded after use
- [ ] Voice prints stored encrypted

### 3.3 P-MEMORY

- [ ] INV-3 holds: no in-place mutation of graph facts
- [ ] Supersession sets valid_to and superseded_by; new row inserted
- [ ] Every row has source_session, confidence, and ingested_at
- [ ] Contradiction detection is a SQL predicate; the LLM only judges interestingness
- [ ] Retrieval ablation run: every scoring term shown to contribute
- [ ] LoCoMo and LongMemEval run locally, 3 runs, variance under 2%
- [ ] Benchmark numbers are our own, not copied from any vendor's published figures
- [ ] Retrieval latency measured inside the Mode A budget (100ms max)
- [ ] Nightly integrity job deletes evidence-free rows
- [ ] Consolidation preserves the provenance chain

### 3.4 P-PERSONA

- [ ] **Character card is hand-authored** and no code path writes it (CI-verified)
- [ ] Every trait has at least one evidence row with a real session reference
- [ ] Traits below confidence 0.6 are excluded from prompt assembly
- [ ] Vetoed traits are absent from the next 10 prompt assemblies (tested)
- [ ] Veto feeds back into extraction prompt tuning
- [ ] Trait extraction runs scheduled, never inline in a Mode A turn
- [ ] Stance log records position, reasoning, strength and session
- [ ] Reversal-bait eval: 90% min acknowledged
- [ ] Anti-sycophancy eval: 60% max overall, 20% max on incorrect assertions
- [ ] Friction eval: 80% min of conflict scenarios surfaced
- [ ] Personality evals pass on two different base models

### 3.5 P-AGENT

- [ ] Tool registry discovers new tools without editing registration code
- [ ] Every tool declares its risk class
- [ ] Approver agent has an empty tool list (asserted in test, not assumed)
- [ ] Approver never sees the content that motivated the action
- [ ] External content wrapped in untrusted-content boundaries
- [ ] Injection red-team: 30 payloads, 0 escalations, including subtle ones in invite descriptions and email signatures
- [ ] Every cloud call routes through the scrubber (INV-1 verified)
- [ ] No Mode A turn awaits a cloud response (INV-4 verified with 30s stub)
- [ ] Async tool acknowledgement within 800ms
- [ ] Proactive budget enforced in code, not configuration
- [ ] Tool failures produce user-legible explanations
- [ ] `total_thought_tokens` monitored and included in cost tracking

### 3.6 P-SEC

- [ ] Exactly one egress function exists (AST-verified)
- [ ] Egress payload schema is a closed whitelist, reject-by-default
- [ ] PII detector: 50 adversarial payloads, 0 pass
- [ ] Entity refs are per-session, non-sequential, never sent with their resolution
- [ ] Retries re-enter at the scrubber, never at the transport
- [ ] Network-layer allowlist blocks app-layer bypass attempts
- [ ] Every egress payload logged locally before send
- [ ] Kill switch tested: full local-only functionality matrix documented
- [ ] Cryptographic erasure verified across all stores including derived summaries
- [ ] Erasure verified by searching for known content post-erasure
- [ ] Backup restore drill completed on a clean machine
- [ ] 24h network capture: zero unexpected egress
- [ ] No plaintext at rest on the server (disk inspection)

### 3.7 P-EVAL

- [ ] I did not write the code I am evaluating
- [ ] Harnesses run unattended and reproduce (variance under 2% over 3 runs)
- [ ] I verified tests are meaningful by deliberately breaking the code
- [ ] I checked for weakened, skipped or xfail tests in the diff
- [ ] I verified measurements were taken with real dependencies
- [ ] I checked the acceptance criterion was not modified
- [ ] Metrics published as numbers without narrative framing
- [ ] Regressions reported even when the pod says they are expected

### 3.8 P-OSS

- [ ] SBOM generated and current
- [ ] **Parakeet-TDT CC-BY attribution present** in NOTICE and product surface
- [ ] Every attribution-required dependency listed
- [ ] No GPL/AGPL in the distributable path without recorded review
- [ ] Licence headers applied consistently
- [ ] Released repos contain no real capture data, no real eval sets, no key material
- [ ] Released repos reproduce from a clean clone
- [ ] Upstream PRs opened where fixes benefit the source project

---

## 4. Phase gate checklist

P-EVAL runs this. A phase is not complete until every box is checked.

- [ ] Every task in the phase has a P-EVAL completion record
- [ ] Every phase metric meets its target, measured this week
- [ ] All invariant tests pass
- [ ] All interface contracts have current golden fixtures
- [ ] No test was weakened during the phase (diff reviewed)
- [ ] No acceptance criterion was modified without human approval
- [ ] Documentation is current with code
- [ ] The phase's behavioural gate is met (see EXECUTION_PLAN)
- [ ] Open defects triaged; no criticals outstanding
- [ ] SBOM current; no new licence violations
- [ ] The human owner has signed off

---

## 5. Escalate immediately, do not work around

Stop and escalate to the human owner rather than proceeding, if:

- An acceptance criterion appears unachievable
- Meeting a criterion would require violating an invariant
- Two frozen contracts are mutually incompatible
- A security control blocks a needed capability
- You find a privacy leak anywhere, including in another pod's code
- Real capture data appears in a test fixture or a release candidate
- A dependency's licence conflicts with the release plan
- You cannot determine whether a change is in scope

Working around any of these is worse than stopping.

---

## 6. Anti-patterns with named consequences

| Anti-pattern | Why it happens | Consequence | Control |
|---|---|---|---|
| Declaring completion without measurement | Bias toward finishing | False green propagates through gates | P-EVAL sole authority |
| Weakening a failing test | It unblocks | Product ships broken with green CI | Test files owned by P-EVAL |
| Stubbing a dependency in a benchmark | Faster iteration | Measurement is meaningless | Real dependencies required |
| Changing a shared schema unilaterally | Local convenience | Integration failure three phases later | Frozen contracts, compatibility tests |
| Generating the character card | Hand-authoring seems inefficient | Generic voice; the subsystem's entire purpose defeated | CI check |
| Adding retrieval to the Mode A turn | Improves answer quality | Latency budget destroyed | INV-4 test with 30s stub |
| UPDATE on a graph fact | Supersession is verbose | Temporal reasoning and contradiction detection break silently | INV-3 test |
| A second egress call site | Convenience | Permanent privacy leak | INV-1 AST scan |
| Giving the approver tools for debugging | Debugging is hard without them | Injection isolation eliminated | ACT-5 assertion in CI |
| Copying published benchmark numbers | Reproduction is slow | Every downstream gate meaningless | Own-run requirement |
| Real data in test fixtures | Realistic tests | Privacy breach on OSS release | Synthetic-only rule, release scan |
| Raising the proactive budget | Something feels urgent | Assistant muted permanently | Budget hardcoded |

---

## 7. Commit and PR requirements

Every commit message states: task ID, requirement IDs satisfied, and any invariant it touches.

Every PR includes:

- [ ] Task ID and acceptance criterion restated
- [ ] Measurement output proving the criterion is met
- [ ] Confirmation that measurement used real dependencies
- [ ] List of contracts touched
- [ ] Invariant suite result
- [ ] Confirmation that no P-EVAL-owned file was modified
- [ ] Anything deliberately left out of scope

PRs without measurement output are returned unreviewed.

---

*End of agent operating manual v2.0.*
