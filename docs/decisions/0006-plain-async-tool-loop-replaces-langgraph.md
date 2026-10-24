# 0006 — Plain async tool loop replaces LangGraph; approved actions execute without re-entering the model

- Status: Accepted (P0.4, 2026-10-24). Overrides AUDIT_DISPOSITION's "KEEP langgraph".

## Context
`src/orchestrator/graph.py` is a fixed LangGraph pipeline (route, retrieve, think, act, respond) with one `act` step. It was designed for a local model. Nimbus Lite needs:
- a multi-step OpenAI tool-calling loop with an iteration cap and a token budget (P2.5);
- a pause for approval that may resume minutes later from a Telegram callback, possibly after a restart.

LangGraph would need a persistent Postgres checkpointer package to survive restarts. It also imports `langchain_core`, which is undeclared (AUDIT_DISPOSITION §0.10).

## Decision
`orchestrator/loop.py` is a plain async loop:
1. Assemble context.
2. Call `llm.chat` with ≤ 12 tools.
3. For each tool call, `dispatch` runs policy, then approvals, then the tool.
4. Append results.
5. Repeat until no tool calls, or until the cap (N=5) or the token budget is reached.

A `require_approval` decision persists a `pending_action` holding the **frozen previewed payload** and ends the turn. When the user approves, `approvals.consume` returns that payload and `dispatch` executes it directly. The model is not consulted between approval and execution; at most one later LLM call phrases the result. `langgraph` is removed in P2.5b.

`context_builder.py`, `prompt_assembler.py` and the personality modules are kept.

## Consequences
- Fewer dependencies. The control flow can be read in one file and tested with FakeLLM transcripts.
- "What was approved is exactly what runs" holds by construction (no TOCTOU through the model).
- `tests/unit/test_graph.py` and `test_risk_gating.py` are rewritten.
- No built-in graph visualisation or checkpointing. Turn state that must survive a restart lives in Postgres rows: `inbound_update`, `pending_action`, `job`.

## Alternatives
- **Keep LangGraph and add a Postgres checkpointer.** Rejected: an extra dependency and serialization format for a ~150-line loop.
- **Resume the model conversation after approval.** Rejected: the model could change arguments after the user approved them, which would violate INV-5's spirit.
