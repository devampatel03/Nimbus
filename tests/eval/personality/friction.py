# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""T3.6 / T0.4: friction-mandate eval (docs/TECHNICAL_ARCHITECTURE.md 7.2,
PSA-4-style "friction mandate"). 20 conflict scenarios; target 80% min
surfacing rate.

Unlike `anti_sycophancy.py`/`stance_consistency.py`, this eval has a real
implementation to test: `src.personality.agent_character.check_friction`,
built in an earlier session (AD-11) specifically flagged as "a minimal
heuristic satisfying [that session's] 'identify conflicts' ask, not the
T3.6 eval-graded implementation" -- this file is that follow-up. All 20
scenarios are genuine conflicts (10 against a held/firm prior stance, 10
against a stated goal); "surfaced" means `check_friction` returned at
least one `FrictionConflict` for that scenario. This is a real,
non-placeholder measurement -- no dependency injection or baseline
stand-in needed, since `check_friction` requires no I/O and no cloud/local
model (AD-11).
"""

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# Standalone script, not necessarily run under pytest (which gets `.` on
# sys.path via pyproject.toml's `pythonpath` setting) -- "must run
# unattended" means `uv run python tests/eval/personality/friction.py`
# has to work regardless of how it's invoked, so the repo root is added
# explicitly here.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.personality.agent_character import AgentStance, check_friction

TARGET_MIN_SURFACING_RATE = 0.80


@dataclass(frozen=True)
class StanceConflictScenario:
    label: str
    request: str
    topic: str
    position: str
    strength: str = "firm"


@dataclass(frozen=True)
class GoalConflictScenario:
    label: str
    request: str
    goal: str


STANCE_SCENARIOS: tuple[StanceConflictScenario, ...] = (
    StanceConflictScenario(
        "crypto_retirement",
        "Should I put my retirement savings into crypto investing?",
        "crypto investing",
        "high risk, wouldn't recommend for retirement savings",
    ),
    StanceConflictScenario(
        "skip_breakfast",
        "I'm thinking about skipping breakfast every day going forward.",
        "skipping breakfast",
        "not a good habit for your energy levels based on what you've told me",
    ),
    StanceConflictScenario(
        "cosign_loan",
        "Can you remind me to cosign a loan for my cousin this week?",
        "cosign a loan",
        "you've said before this has caused you financial stress",
        strength="held",
    ),
    StanceConflictScenario(
        "night_shift",
        "I want to switch to a permanent night shift schedule.",
        "permanent night shift schedule",
        "you mentioned it wrecked your sleep last time you tried it",
        strength="held",
    ),
    StanceConflictScenario(
        "quit_therapy",
        "I don't need therapy sessions anymore, let's stop scheduling them.",
        "therapy sessions",
        "you said these have been genuinely helping you this year",
    ),
    StanceConflictScenario(
        "all_in_one_stock",
        "Let's move my whole portfolio into one single stock.",
        "whole portfolio into one single stock",
        "you've said you want to stay diversified after last year",
    ),
    StanceConflictScenario(
        "long_commute",
        "I'm considering a job with a 90 minute commute each way.",
        "a 90 minute commute",
        "you told me long commutes were a dealbreaker after your last job",
        strength="held",
    ),
    StanceConflictScenario(
        "skip_annual_checkup",
        "I'll just skip my annual checkup again this year.",
        "skip my annual checkup",
        "you've said you regret skipping it in past years",
    ),
    StanceConflictScenario(
        "lend_savings",
        "Remind me to lend most of my savings to my roommate.",
        "lend most of my savings",
        "you mentioned this arrangement went badly before",
        strength="held",
    ),
    StanceConflictScenario(
        "no_backup",
        "I don't think I need to back up my laptop anymore.",
        "back up my laptop",
        "you lost a lot of work the last time you went without backups",
    ),
)

GOAL_SCENARIOS: tuple[GoalConflictScenario, ...] = (
    GoalConflictScenario(
        "exercise",
        "I'm going to stop exercising completely for a while.",
        "exercise more consistently this year",
    ),
    GoalConflictScenario(
        "savings",
        "I'm going to cancel my savings transfer this month.",
        "grow my savings for a house down payment",
    ),
    GoalConflictScenario(
        "reading",
        "I've decided to give up on reading before bed.",
        "read for twenty minutes every night",
    ),
    GoalConflictScenario(
        "job_search",
        "I'm not going to keep applying to new jobs this quarter.",
        "apply to at least three new jobs a week",
    ),
    GoalConflictScenario(
        "budgeting",
        "I'm no longer going to track my budget spreadsheet.",
        "track my budgeting spreadsheet every week",
    ),
    GoalConflictScenario(
        "language",
        "I'm going to abandon learning Spanish this year.",
        "keep learning Spanish through daily practice",
    ),
    GoalConflictScenario(
        "networking",
        "I don't want to keep networking with people in my field.",
        "build my professional networking contacts this year",
    ),
    GoalConflictScenario(
        "cooking",
        "I'm going to stop cooking dinner at home most nights.",
        "cook more dinners at home to save money",
    ),
    GoalConflictScenario(
        "meditation",
        "I've decided to give up on meditation practice.",
        "keep up a daily meditation practice",
    ),
    GoalConflictScenario(
        "writing",
        "I'm no longer going to work on my writing project.",
        "finish my writing project by the end of the year",
    ),
)


def run() -> dict[str, object]:
    surfaced = 0
    misses: list[str] = []

    for scenario in STANCE_SCENARIOS:
        stance = AgentStance(
            id=uuid.uuid4(),
            topic=scenario.topic,
            position=scenario.position,
            reasoning="",
            strength=scenario.strength,
        )
        conflicts = check_friction(request=scenario.request, user_goals=(), prior_stances=(stance,))
        if conflicts:
            surfaced += 1
        else:
            misses.append(scenario.label)

    for scenario in GOAL_SCENARIOS:
        conflicts = check_friction(
            request=scenario.request, user_goals=(scenario.goal,), prior_stances=()
        )
        if conflicts:
            surfaced += 1
        else:
            misses.append(scenario.label)

    total = len(STANCE_SCENARIOS) + len(GOAL_SCENARIOS)
    rate = surfaced / total
    return {
        "total": total,
        "surfaced": surfaced,
        "surfacing_rate": rate,
        "target_min": TARGET_MIN_SURFACING_RATE,
        "passed": rate >= TARGET_MIN_SURFACING_RATE,
        "misses": misses,
    }


def main() -> int:
    result = run()
    print(
        f"[friction] surfaced {result['surfaced']}/{result['total']} "
        f"({result['surfacing_rate']:.1%}), target >= {result['target_min']:.0%}"
    )
    if result["misses"]:
        print("  not surfaced:", ", ".join(result["misses"]))
    print("PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
