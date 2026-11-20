# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Integration tests for `src/personality/user_profile.py` against real
Postgres (per AGENT_OPERATING_MANUAL.md: measurements must use real
dependencies, not mocks).
"""

import uuid
from datetime import UTC, datetime

import pytest

from src.personality.user_profile import (
    EvidenceInput,
    ProfileManager,
    TraitWithoutEvidenceError,
    format_traits_for_prompt,
)


async def test_trait_without_evidence_is_rejected(db_session):
    manager = ProfileManager(db_session)
    with pytest.raises(TraitWithoutEvidenceError):
        await manager.save_trait(
            category="communication",
            statement="prefers being told the conclusion first",
            confidence=0.9,
            first_observed=datetime.now(UTC),
            evidence=[],
        )


async def test_trait_with_evidence_is_persisted_and_loadable(db_session):
    manager = ProfileManager(db_session)
    now = datetime.now(UTC)
    trait = await manager.save_trait(
        category="communication",
        statement="prefers being told the conclusion first",
        confidence=0.9,
        first_observed=now,
        evidence=[
            EvidenceInput(
                session_id=uuid.uuid4(), excerpt="just tell me the answer", observed_at=now
            )
        ],
    )

    loaded = await manager.load_trait(trait.id)

    assert loaded is not None
    assert loaded.statement == trait.statement
    assert loaded.observation_count == 1


async def test_vetoed_trait_excluded_from_assembly(db_session):
    manager = ProfileManager(db_session)
    now = datetime.now(UTC)
    approved = await manager.save_trait(
        category="value",
        statement="values directness",
        confidence=0.8,
        first_observed=now,
        evidence=[
            EvidenceInput(session_id=uuid.uuid4(), excerpt="just be direct", observed_at=now)
        ],
    )
    vetoed = await manager.save_trait(
        category="pattern",
        statement="always available after 9pm",
        confidence=0.9,
        first_observed=now,
        evidence=[
            EvidenceInput(
                session_id=uuid.uuid4(), excerpt="I'm free anytime after 9", observed_at=now
            )
        ],
    )
    vetoed.user_status = "vetoed"
    await db_session.flush()

    active = await manager.get_traits_above_confidence()
    active_ids = {trait.id for trait in active}
    assert approved.id in active_ids
    assert vetoed.id not in active_ids

    prompt = format_traits_for_prompt(active)
    assert "directness" in prompt
    assert "available after 9pm" not in prompt


async def test_low_confidence_trait_excluded_from_assembly(db_session):
    manager = ProfileManager(db_session)
    now = datetime.now(UTC)
    await manager.save_trait(
        category="preference",
        statement="might prefer dark mode",
        confidence=0.3,
        first_observed=now,
        evidence=[
            EvidenceInput(
                session_id=uuid.uuid4(), excerpt="dark mode is nice I guess", observed_at=now
            )
        ],
    )

    active = await manager.get_traits_above_confidence()

    assert active == []
