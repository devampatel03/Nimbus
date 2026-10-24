# 0015 — Character card stays at `personality/character_card.md`; R9 guard = marker + placeholder check + agent-edit hook

- Status: Accepted. Resolves a path conflict between HUMAN_TODO H1 (`config/character_card.md`) and the code (`personality/character_card.md`, loaded by `src/personality/agent_character.py`).

## Context
R9 says CI must fail if the card is generated or edited by an agent without a human-approved marker. No CI check can tell a human edit from an agent edit by content alone. An agent can also type the marker.

## Decision
- **Path:** keep `personality/character_card.md`, which is the path the code already loads. The orchestrator corrects H1's path.
- **CI check** (`tests/unit/test_r9_character_card.py`): line 1 must be `human-authored: true`. The file must not contain `PLACEHOLDER` or `TODO(owner)`. Its sha256 must match `personality/character_card.sha256`.
- **Agent prevention:** add both files to `.claude/hooks/protect-files.sh`, so agent Write/Edit is blocked at the harness level. Also recommend a GitHub CODEOWNERS entry plus branch protection requiring the owner's review for `personality/**` (human, H-item).
- **Placeholder period:** until H1 is done, the check runs in "placeholder allowed, but marked" mode on `pivot` and is mandatory for the final PR to `main`.

## Consequences
- The guard is deterministic, but it is honest about its limit: it stops harness-mediated agent edits and accidental edits, not a determined actor with repo write access.

## Alternatives
- **Signed commits / commit trailer check.** Rejected: an agent can forge trailers, and GPG signing setup is a human burden for little gain over CODEOWNERS.
- **Move to `config/`.** Rejected: churn with no benefit.
