# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""P4.4 canary values, plant lines and settings: high-entropy, per site,
hidden from repr and validation errors, and never blocked by the LLM scrubber
(the model is meant to see them)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from src.canary.settings import CanarySettings
from src.canary.tokens import BODY_CHARS, CanarySet, Site, derive_canaries, generate_canaries
from src.llm.scrub import Scrubber
from tests._canary_helpers import SEED, canaries


def test_one_distinct_high_entropy_value_per_site() -> None:
    cs = canaries()
    values = cs.values()
    assert len(values) == len(Site) == len(set(values))
    for v in values:
        assert len(v) == BODY_CHARS and v.isalnum() and v == v.lower()
        assert len(set(v)) >= 12  # not degenerate


def test_repr_and_str_never_show_values() -> None:
    cs = canaries()
    for shown in (repr(cs), str(cs), repr(list(cs))):
        assert not any(v in shown for v in cs.values())


@pytest.mark.parametrize("_", range(25))
def test_plant_lines_pass_the_llm_scrubber_and_contain_the_value_once(_: int) -> None:
    cs = generate_canaries()
    scrubber = Scrubber(secrets=["provider-key-0123456789"])
    for site in Site:
        line = cs.plant(site)
        scrubber.check(line)  # would raise ScrubBlocked
        assert line.count(cs.value(site)) == 1


def test_bad_sets_and_short_seeds_are_refused() -> None:
    with pytest.raises(ValueError):
        derive_canaries("short")
    with pytest.raises(ValueError):
        CanarySet({Site.SYSTEM: "a" * BODY_CHARS})
    with pytest.raises(ValueError):
        CanarySet(dict.fromkeys(Site, "not-alnum-" + "a" * 22))


def test_settings_defaults_and_hidden_seed() -> None:
    s = CanarySettings(_env_file=None)  # type: ignore[call-arg]
    assert s.seed is None and s.alert_telegram is True and s.secret_values() == ()
    s2 = CanarySettings(_env_file=None, seed=SecretStr(SEED))  # type: ignore[call-arg]
    assert s2.secret_values() == (SEED,) and SEED not in repr(s2)


def test_short_seed_error_does_not_echo_the_value() -> None:
    bad = "too-short-seed-value"
    with pytest.raises(ValidationError) as ei:
        CanarySettings(_env_file=None, seed=SecretStr(bad))  # type: ignore[call-arg]
    assert bad not in str(ei.value)


def test_seed_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANARY_SEED", SEED)
    monkeypatch.setenv("CANARY_ALERT_TELEGRAM", "false")
    s = CanarySettings(_env_file=None)  # type: ignore[call-arg]
    assert s.seed is not None and s.alert_telegram is False
    assert derive_canaries(s.seed.get_secret_value()).values() == canaries().values()


@pytest.mark.parametrize("bad", [0, 5, 29.9, 86_401])
def test_alert_cooldown_has_a_floor_and_a_ceiling(bad: float) -> None:
    with pytest.raises(ValidationError):
        CanarySettings(_env_file=None, alert_cooldown_s=bad)  # type: ignore[call-arg]
    assert CanarySettings(_env_file=None, alert_cooldown_s=30).alert_cooldown_s == 30  # type: ignore[call-arg]


def test_telegram_notice_and_guard_reply_are_one_text() -> None:
    from src.canary.guard import BLOCKED_REPLY
    from src.gateway.telegram import CANARY_NOTICE

    assert CANARY_NOTICE == BLOCKED_REPLY and CANARY_NOTICE is BLOCKED_REPLY
