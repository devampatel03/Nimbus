# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""`python -m src.app preflight`: a checklist with no secret values (INV-7, R12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.app import cli, preflight
from src.app.preflight import Check, Status

TOKEN = "123456789:AAH-secret_token_value_0123456789abcdef"  # pragma: allowlist secret
KEY = "sk-live-SUPERSECRETKEYVALUE1234567890"  # pragma: allowlist secret
HMAC = "H" * 40 + "-hmac-secret-value"  # pragma: allowlist secret
DATA_KEY = "A" * 43 + "="  # base64 of 32 bytes  # pragma: allowlist secret
PG_PW = "pgpassword-SECRET"  # pragma: allowlist secret

SECRETS = (TOKEN, KEY, HMAC, PG_PW, DATA_KEY)

_NAMES = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_PRICE_IN_PER_MTOK_INR",
    "LLM_PRICE_OUT_PER_MTOK_INR",
    "LLM_RPM_LIMIT",
    "LLM_DAILY_REQUEST_LIMIT",
    "NIMBUS_DATA_KEY",
    "APPROVAL_HMAC_KEY",
    "APPROVAL_TOTP_SECRET",
    "APP_ENV",
    "APP_EMBEDDER",
    "APP_EMBED_MODEL",
    "APP_FAKE_BACKENDS",
    "NIMBUS_CARD_DEV_MODE",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "NOTION_TOKEN",
    "NET_LLM_BASE_URL",
    "LLM_DAILY_SPEND_CAP_INR",
    "POSTGRES_PASSWORD",
    "LLM_MODEL_CHEAP",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


def good_env(monkeypatch: pytest.MonkeyPatch, **over: str) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_ALLOWED_USER_ID": "42",
        "LLM_BASE_URL": "https://api.example-llm.com/v1",
        "LLM_API_KEY": KEY,
        "LLM_MODEL": "some-free-model",
        "LLM_PRICE_IN_PER_MTOK_INR": "0.0001",
        "LLM_PRICE_OUT_PER_MTOK_INR": "0.0001",
        "LLM_RPM_LIMIT": "10",
        "LLM_DAILY_REQUEST_LIMIT": "200",
        "NIMBUS_DATA_KEY": DATA_KEY,
        "APPROVAL_HMAC_KEY": HMAC,
        "POSTGRES_PASSWORD": PG_PW,
        "APP_ENV": "development",
        "NIMBUS_CARD_DEV_MODE": "1",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def by_name(checks: list[Check], name: str) -> Check:
    hits = [c for c in checks if c.name == name]
    assert len(hits) == 1, [c.name for c in checks]
    return hits[0]


def test_good_env_has_no_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    checks = preflight.offline_checks()
    assert [c.name for c in checks if c.status is Status.FAIL] == []
    assert by_name(checks, "telegram token").status is Status.PASS
    assert by_name(checks, "llm base url").status is Status.PASS
    assert by_name(checks, "egress allowlist").status is Status.PASS


def test_missing_required_names_fail_with_names_only() -> None:
    checks = preflight.offline_checks()
    assert by_name(checks, "telegram token").status is Status.FAIL
    assert "TELEGRAM_BOT_TOKEN" in by_name(checks, "telegram token").detail
    assert by_name(checks, "telegram user id").status is Status.FAIL
    assert by_name(checks, "llm api key").status is Status.FAIL


def test_malformed_token_and_user_id_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, TELEGRAM_BOT_TOKEN="not-a-token", TELEGRAM_ALLOWED_USER_ID="@devam")
    checks = preflight.offline_checks()
    assert by_name(checks, "telegram token").status is Status.FAIL
    assert by_name(checks, "telegram user id").status is Status.FAIL
    assert "not-a-token" not in preflight.render(checks)


def test_http_or_localhost_llm_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, LLM_BASE_URL="http://localhost:8000/v1")
    assert by_name(preflight.offline_checks(), "llm base url").status is Status.FAIL


def test_unset_llm_url_is_a_fail_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    monkeypatch.delenv("LLM_BASE_URL")
    assert by_name(preflight.offline_checks(), "llm base url").status is Status.FAIL


def test_free_tier_pricing_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    c = by_name(preflight.offline_checks(), "llm price and limits")
    assert c.status is Status.WARN
    assert "free" in c.detail.lower()


def test_placeholder_pricing_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    monkeypatch.delenv("LLM_PRICE_IN_PER_MTOK_INR")
    monkeypatch.delenv("LLM_PRICE_OUT_PER_MTOK_INR")
    c = by_name(preflight.offline_checks(), "llm price and limits")
    assert c.status is Status.WARN
    assert "placeholder" in c.detail


def test_approvals_off_is_reported_as_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    monkeypatch.delenv("APPROVAL_HMAC_KEY")
    c = by_name(preflight.offline_checks(), "approvals")
    assert c.status is Status.WARN
    assert "writes disabled" in c.detail


def test_short_hmac_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, APPROVAL_HMAC_KEY="short")
    assert by_name(preflight.offline_checks(), "approvals").status is Status.FAIL


def test_data_key_missing_warns_and_bad_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    monkeypatch.delenv("NIMBUS_DATA_KEY")
    assert by_name(preflight.offline_checks(), "data key").status is Status.WARN
    monkeypatch.setenv("NIMBUS_DATA_KEY", "not base64 !!")
    assert by_name(preflight.offline_checks(), "data key").status is Status.FAIL


def test_data_key_only_in_dotenv_is_called_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NIMBUS_DATA_KEY is a raw os.environ read: a .env entry alone is not seen."""
    good_env(monkeypatch)
    monkeypatch.delenv("NIMBUS_DATA_KEY")
    (tmp_path / ".env").write_text(f"NIMBUS_DATA_KEY={DATA_KEY}\n", encoding="utf-8")
    c = by_name(preflight.offline_checks(), "data key")
    assert c.status is Status.WARN
    assert ".env" in c.detail and "process environment" in c.detail
    assert DATA_KEY not in c.detail


def test_card_placeholder_without_dev_mode_fails_and_with_dev_mode_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_env(monkeypatch)
    monkeypatch.delenv("NIMBUS_CARD_DEV_MODE")
    c = by_name(preflight.offline_checks(), "character card")
    assert c.status is Status.FAIL
    assert "placeholder" in c.detail
    monkeypatch.setenv("NIMBUS_CARD_DEV_MODE", "1")
    c = by_name(preflight.offline_checks(), "character card")
    assert c.status is Status.WARN
    assert "BYPASSED" in c.detail


def test_card_dev_mode_in_production_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, APP_ENV="production")
    assert by_name(preflight.offline_checks(), "character card").status is Status.FAIL


def test_card_approved_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    ready = type("S", (), {"production_ready": True, "problems": (), "is_placeholder": False})()
    monkeypatch.setattr(preflight, "inspect_character_card", lambda: ready)
    assert by_name(preflight.offline_checks(), "character card").status is Status.PASS


def test_local_embedder_without_package_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    monkeypatch.setattr(preflight, "_module_available", lambda name: False)
    c = by_name(preflight.offline_checks(), "embedder")
    assert c.status is Status.WARN
    assert "APP_EMBEDDER=api" in c.fix


def test_api_embedder_reports_model(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, APP_EMBEDDER="api", APP_EMBED_MODEL="my-embed-model")
    c = by_name(preflight.offline_checks(), "embedder")
    assert c.status is Status.WARN  # the provider must serve it: only --online proves it
    assert "my-embed-model" in c.detail


def test_optional_integrations_never_fail_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    checks = preflight.offline_checks()
    assert by_name(checks, "google").status is Status.PASS
    assert by_name(checks, "notion").status is Status.PASS


def test_render_never_contains_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch, APP_EMBEDDER="api")
    text = preflight.render(preflight.offline_checks())
    for s in SECRETS:
        assert s not in text
    assert "[PASS]" in text and "[WARN]" in text


def test_exit_code_is_1_on_any_fail_else_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good_env(monkeypatch)

    def ok_db() -> list[Check]:
        return [Check("database", Status.PASS, "ok")]

    def bad_db() -> list[Check]:
        return [
            Check("database migrations", Status.FAIL, "behind", fix="uv run alembic upgrade head")
        ]

    assert preflight.run_preflight(online=False, db_checks=ok_db) == 0
    assert "[PASS] database" in capsys.readouterr().out
    assert preflight.run_preflight(online=False, db_checks=bad_db) == 1
    assert "uv run alembic upgrade head" in capsys.readouterr().out


def test_online_checks_run_only_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)
    called: list[str] = []

    def online() -> list[Check]:
        called.append("x")
        return [Check("telegram getMe", Status.PASS, "ok")]

    def db() -> list[Check]:
        return []

    preflight.run_preflight(online=False, db_checks=db, online_checks=online)
    assert called == []
    preflight.run_preflight(online=True, db_checks=db, online_checks=online)
    assert called == ["x"]


def test_online_is_skipped_when_offline_checks_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[str] = []

    def online() -> list[Check]:
        called.append("x")
        return []

    assert preflight.run_preflight(online=True, db_checks=list, online_checks=online) == 1
    assert called == []
    assert "skipped" in capsys.readouterr().out


def test_database_error_text_is_type_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)

    async def boom(dsn: str) -> Any:
        raise OSError(f"connect failed password={PG_PW}")

    monkeypatch.setattr(preflight, "_probe_db", boom)
    checks = preflight.database_checks()
    assert by_name(checks, "database").status is Status.FAIL
    assert PG_PW not in preflight.render(checks)
    assert "OSError" in by_name(checks, "database").detail
    assert "docker compose up -d" in by_name(checks, "database").fix


def test_database_checks_report_migration_and_pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)

    async def probe(dsn: str) -> tuple[str | None, bool]:
        return "oldrev", False

    monkeypatch.setattr(preflight, "_probe_db", probe)
    monkeypatch.setattr(preflight, "_alembic_head", lambda: "newrev")
    checks = preflight.database_checks()
    mig = by_name(checks, "database migrations")
    assert mig.status is Status.FAIL and "uv run alembic upgrade head" in mig.fix
    assert by_name(checks, "pgvector").status is Status.FAIL


def test_database_checks_pass_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    good_env(monkeypatch)

    async def probe(dsn: str) -> tuple[str | None, bool]:
        return "head1", True

    monkeypatch.setattr(preflight, "_probe_db", probe)
    monkeypatch.setattr(preflight, "_alembic_head", lambda: "head1")
    assert all(c.status is Status.PASS for c in preflight.database_checks())


def test_cli_dispatches_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def fake(*, online: bool = False, **_: Any) -> int:
        seen.append(online)
        return 0

    monkeypatch.setattr(preflight, "run_preflight", fake)
    assert cli.main(["preflight"]) == 0
    assert cli.main(["preflight", "--online"]) == 0
    assert seen == [False, True]


def test_embed_model_setting_reaches_the_api_embedder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.app import runtime as rt
    from src.app.settings import AppSettings
    from tests.unit.app.test_startup_gates import make_cfg

    seen: dict[str, Any] = {}

    def fake_build(llm: Any, settings: Any = None, **kw: Any) -> str:
        seen.update(kw)
        return "embedder"

    monkeypatch.setattr(rt, "build_api_embedder", fake_build)
    cfg = make_cfg(tmp_path, embedder="api", embed_model="my-embed-model")
    assert rt.build_embedder(cfg, object()) == "embedder"  # type: ignore[arg-type]
    assert seen == {"model": "my-embed-model"}
    assert AppSettings(_env_file=None).embed_model == "text-embedding-3-small"
