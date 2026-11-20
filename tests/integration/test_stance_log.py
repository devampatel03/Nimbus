# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Integration tests for `src/personality/agent_character.py`'s `StanceLog`
against real Postgres (per AGENT_OPERATING_MANUAL.md: measurements must use
real dependencies, not mocks).
"""

import uuid
from datetime import UTC, datetime

from src.personality.agent_character import StanceLog


async def test_stance_retrievable_by_topic(db_session):
    log = StanceLog(db_session)
    now = datetime.now(UTC)
    await log.record_stance(
        topic="remote work",
        position="hybrid is better than fully remote for this team",
        reasoning="team cohesion suffers past three days remote per past retros",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="held",
    )

    stances = await log.get_stances_by_topic("remote work")

    assert len(stances) == 1
    assert stances[0].position == "hybrid is better than fully remote for this team"


async def test_stances_for_unrelated_topic_not_returned(db_session):
    log = StanceLog(db_session)
    now = datetime.now(UTC)
    await log.record_stance(
        topic="remote work",
        position="hybrid is better",
        reasoning="team cohesion",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="held",
    )

    stances = await log.get_stances_by_topic("tabs vs spaces")

    assert stances == []


async def test_reversal_is_detected(db_session):
    log = StanceLog(db_session)
    now = datetime.now(UTC)
    first = await log.record_stance(
        topic="tabs vs spaces",
        position="tabs",
        reasoning="accessibility",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="tentative",
    )

    second = await log.record_stance(
        topic="tabs vs spaces",
        position="spaces",
        reasoning="ecosystem convention won out",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="held",
        revision_reason="ecosystem convention won out",
    )

    assert second.revised_from == first.id


async def test_reaffirming_the_same_position_is_not_a_reversal(db_session):
    log = StanceLog(db_session)
    now = datetime.now(UTC)
    await log.record_stance(
        topic="tabs vs spaces",
        position="spaces",
        reasoning="ecosystem convention",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="held",
    )

    reaffirmed = await log.record_stance(
        topic="tabs vs spaces",
        position="spaces",
        reasoning="reaffirming",
        taken_at=now,
        session_id=uuid.uuid4(),
        strength="held",
    )

    assert reaffirmed.revised_from is None
