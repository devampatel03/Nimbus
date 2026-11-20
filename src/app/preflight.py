# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""`python -m src.app preflight [--online]`: a PASS/WARN/FAIL checklist for a
first local run. Exit 0 when nothing FAILed, 1 otherwise.

* Offline by default: reads settings and the local Postgres only. No call to
  Telegram or a model provider unless `--online`.
* `--online` makes ONE Telegram `getMe` (through `src.net`) and ONE tiny model
  call (through `src.llm`, so the kill switch, spend cap and audit row apply),
  plus one embedding call when `APP_EMBEDDER=api`. Nothing here imports a
  transport or an LLM SDK (INV-1).
* INV-7 / R12: output carries variable NAMES, shapes and counts, never a value.
  Settings errors are reduced to field names; other errors to their type name.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from src.app.settings import AppSettings
from src.personality.agent_character import inspect_character_card

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_CMD = "uv run alembic upgrade head"
PAID_DEFAULT_RPM = 120  # LLMSettings default; a free key must set it lower


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    for c in checks:
        lines.append(f"[{c.status.value}] {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"       -> {c.fix}")
    n_fail = sum(c.status is Status.FAIL for c in checks)
    n_warn = sum(c.status is Status.WARN for c in checks)
    lines.append(f"summary: {len(checks)} checks, {n_fail} FAIL, {n_warn} WARN")
    return "\n".join(lines)


# -- helpers -------------------------------------------------------------


def _module_available(name: str) -> bool:
    try:
        importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _fields(exc: ValidationError) -> dict[str, str]:
    """field -> 'missing' | 'invalid'. Names only; never the input."""
    out: dict[str, str] = {}
    for e in exc.errors():
        loc = str(e["loc"][0]) if e["loc"] else "?"
        out[loc] = "missing" if e["type"] == "missing" else "invalid"
    return out


def _env_name(prefix: str, field: str) -> str:
    return f"{prefix}{field}".upper()


def _dotenv_has(name: str) -> bool:
    path = Path(".env")
    if not path.is_file():
        return False
    try:
        from dotenv import dotenv_values

        return name in dotenv_values(path)
    except Exception:  # noqa: BLE001 - diagnostic only
        return False


def _describe(prefix: str, exc: ValidationError) -> str:
    return ", ".join(f"{_env_name(prefix, f)} ({kind})" for f, kind in sorted(_fields(exc).items()))


# -- offline checks ------------------------------------------------------


def _app_checks() -> tuple[AppSettings | None, list[Check]]:
    try:
        app = AppSettings()
    except ValidationError as exc:
        return None, [Check("app settings", Status.FAIL, "invalid " + _describe("APP_", exc))]
    out = [
        Check(
            "app env",
            Status.PASS if app.env == "development" else Status.WARN,
            f"APP_ENV={app.env}"
            + (
                ""
                if app.env == "development"
                else " (a local run with a placeholder card needs development)"
            ),
        )
    ]
    if app.fake_backends and app.env != "development":
        out.append(
            Check("fake backends", Status.FAIL, "APP_FAKE_BACKENDS needs APP_ENV=development")
        )
    return app, out


def _telegram_checks() -> list[Check]:
    from src.gateway.settings import GatewaySettings

    try:
        GatewaySettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        f = _fields(exc)
        out: list[Check] = []
        for fld, label, hint in (
            ("bot_token", "telegram token", "create a bot with @BotFather and copy its token"),
            ("allowed_user_id", "telegram user id", "your numeric id, e.g. from @userinfobot"),
        ):
            name = _env_name("TELEGRAM_", fld)
            if fld in f:
                out.append(Check(label, Status.FAIL, f"{name} is {f[fld]}", fix=hint))
            else:
                out.append(Check(label, Status.PASS, f"{name} ok"))
        rest = sorted(set(f) - {"bot_token", "allowed_user_id"})
        if rest:
            out.append(
                Check(
                    "telegram settings",
                    Status.FAIL,
                    "invalid " + ", ".join(_env_name("TELEGRAM_", r) for r in rest),
                )
            )
        return out
    return [
        Check(
            "telegram token", Status.PASS, "TELEGRAM_BOT_TOKEN present, format ok (not verified)"
        ),
        Check("telegram user id", Status.PASS, "TELEGRAM_ALLOWED_USER_ID is a positive integer"),
    ]


def _llm_checks() -> list[Check]:
    from src.llm.settings import DEFAULT_API_KEY, LLMSettings
    from src.net import NetSettings
    from src.net.policy import llm_and_telegram_hosts
    from src.net.settings import host_from_https_url

    try:
        llm = LLMSettings()
    except ValidationError as exc:
        return [Check("llm settings", Status.FAIL, "invalid " + _describe("LLM_", exc))]
    out: list[Check] = []
    if llm.api_key.get_secret_value() == DEFAULT_API_KEY:
        out.append(
            Check(
                "llm api key",
                Status.FAIL,
                "LLM_API_KEY is not set",
                fix="put your provider key in .env",
            )
        )
    else:
        out.append(Check("llm api key", Status.PASS, "LLM_API_KEY present"))

    host: str | None = None
    try:
        host = host_from_https_url(llm.base_url)
        out.append(Check("llm base url", Status.PASS, f"https, host {host}"))
    except Exception as exc:  # noqa: BLE001 - EgressConfigError carries a fixed message
        out.append(
            Check(
                "llm base url",
                Status.FAIL,
                f"LLM_BASE_URL unset or unusable ({exc})",
                fix="set LLM_BASE_URL to the provider's https OpenAI-compatible endpoint "
                "(host name, no port, no credentials)",
            )
        )
    try:
        allowed, tg_host = llm_and_telegram_hosts(NetSettings())
        if host is not None and allowed != host:
            out.append(
                Check(
                    "egress allowlist",
                    Status.FAIL,
                    "NET_LLM_BASE_URL points at a different host than LLM_BASE_URL",
                    fix="unset NET_LLM_BASE_URL",
                )
            )
        else:
            out.append(
                Check(
                    "egress allowlist",
                    Status.PASS,
                    f"llm -> {allowed}, telegram -> {tg_host}",
                )
            )
    except Exception as exc:  # noqa: BLE001 - fixed-message egress errors only
        out.append(
            Check(
                "egress allowlist",
                Status.FAIL,
                f"LLM egress would be denied ({type(exc).__name__})",
            )
        )

    if llm.model == "Qwen/Qwen2.5-7B-Instruct":
        out.append(
            Check(
                "llm model",
                Status.WARN,
                "LLM_MODEL is the built-in default; set your provider's model id",
            )
        )
    else:
        out.append(Check("llm model", Status.PASS, f"LLM_MODEL={llm.model}"))

    notes: list[str] = []
    status = Status.PASS
    free = llm.free_tier_priced_models()
    if free:
        status = Status.WARN
        notes.append(
            "free-tier price: the INR cap bounds nothing, only request limits do "
            f"(rpm={llm.rpm_limit}, daily={llm.daily_request_limit})"
        )
        if llm.rpm_limit >= PAID_DEFAULT_RPM:
            notes.append("LLM_RPM_LIMIT is still the paid default: set it below the key's quota")
    elif llm.model not in llm.price_table:
        status = Status.WARN
        p = llm.price_table["*"]
        notes.append(
            f"placeholder price {p.in_per_mtok_inr}/{p.out_per_mtok_inr} INR per Mtok applies; "
            f"cap {llm.daily_spend_cap_inr} INR trips early. Set LLM_PRICE_IN_PER_MTOK_INR "
            "and LLM_PRICE_OUT_PER_MTOK_INR"
        )
    if llm.daily_spend_cap_inr > Decimal(100):  # pragma: no cover - settings enforce <= 100
        status = Status.FAIL
    notes.append(f"cap {llm.daily_spend_cap_inr} INR/day")
    out.append(Check("llm price and limits", status, "; ".join(notes)))
    return out


def _data_key_check() -> Check:
    from src.db.crypto import KEY_ENV_VAR, CryptoError, DataCipher

    if not os.environ.get(KEY_ENV_VAR):
        if _dotenv_has(KEY_ENV_VAR):
            return Check(
                "data key",
                Status.WARN,
                f"{KEY_ENV_VAR} is in .env but this one is read from the process environment only",
                fix=f'PowerShell: $env:{KEY_ENV_VAR} = "<value from .env>" before running',
            )
        return Check(
            "data key",
            Status.WARN,
            f"{KEY_ENV_VAR} unset: LLM_STORE_PAYLOAD and Google tokens stay off (fine for a first run)",
        )
    try:
        DataCipher.from_env()
    except CryptoError as exc:
        return Check("data key", Status.FAIL, str(exc))
    return Check("data key", Status.PASS, f"{KEY_ENV_VAR} present, 32 bytes")


def _approvals_check() -> Check:
    from src.approvals.settings import ApprovalSettings

    try:
        ap = ApprovalSettings()
    except ValidationError as exc:
        return Check("approvals", Status.FAIL, "invalid " + _describe("APPROVAL_", exc))
    if not ap.enabled:
        return Check(
            "approvals",
            Status.WARN,
            "APPROVAL_HMAC_KEY unset => approvals OFF => writes disabled (every write is denied)",
            fix="optional for a read/chat first run",
        )
    if ap.totp_secret is None:
        return Check(
            "approvals",
            Status.WARN,
            "on; APPROVAL_TOTP_SECRET unset: destructive and external actions can never be approved",
        )
    return Check("approvals", Status.PASS, "on (HMAC key and TOTP secret present)")


def _card_check(app: AppSettings | None) -> Check:
    from src.app.card import StartupRefused, load_card

    status = inspect_character_card()
    if status.production_ready:
        return Check("character card", Status.PASS, "human-approved and pinned (R9)")
    kind = "placeholder" if status.is_placeholder else "not approved"
    env = app.env if app is not None else "production"
    logging.disable(logging.CRITICAL)  # load_card logs a loud WARNING in dev mode
    try:
        load_card(env)
    except StartupRefused:
        return Check(
            "character card",
            Status.FAIL,
            f"card is a {kind} and the dev switch is set but APP_ENV={env}: startup refuses",
            fix="set APP_ENV=development for a local run",
        )
    except Exception:  # noqa: BLE001 - CardNotApprovedError and friends
        return Check(
            "character card",
            Status.FAIL,
            f"card is a {kind}: startup refuses it (R9)",
            fix="local test run: APP_ENV=development and the card dev switch (see "
            "docs/runbooks/LOCAL_FIRST_RUN.md, step 4). Real use: the human writes the "
            "card and pins it (ADR-0015); this tool never edits it",
        )
    finally:
        logging.disable(logging.NOTSET)
    return Check(
        "character card",
        Status.WARN,
        f"card is a {kind}; the approval gate is BYPASSED by the dev switch: "
        "local testing only, never real use. The assistant answers with the placeholder text as its persona",
    )


def _hf_cache_has_model(model_name: str) -> bool:
    hub = os.environ.get("HF_HUB_CACHE")
    if not hub:
        home = os.environ.get("HF_HOME")
        base = Path(home) / "hub" if home else Path.home() / ".cache" / "huggingface" / "hub"
        hub = str(base)
    return (Path(hub) / ("models--" + model_name.replace("/", "--"))).is_dir()


def _embedder_check(app: AppSettings | None) -> Check:
    from src.memory.embedder import MODEL_NAME

    if app is None:
        return Check("embedder", Status.WARN, "skipped (APP_* settings invalid)")
    if app.embedder == "api":
        return Check(
            "embedder",
            Status.WARN,
            f"APP_EMBEDDER=api, model {app.embed_model} on the LLM host, 1024 dims, counted in the "
            "spend cap. Your provider must serve /embeddings for that model: "
            "`preflight --online` tests it",
        )
    if not _module_available("sentence-transformers"):
        return Check(
            "embedder",
            Status.WARN,
            "APP_EMBEDDER=local but sentence-transformers is not installed: memory writes and "
            "recall will fail (chat still works)",
            fix="cheap option: set APP_EMBEDDER=api (and APP_EMBED_MODEL); or uv sync --extra dev "
            "--extra local-embed (pulls torch, large)",
        )
    if not _hf_cache_has_model(MODEL_NAME):
        return Check(
            "embedder",
            Status.WARN,
            f"local embedder installed but {MODEL_NAME} is not in the Hugging Face cache; the app "
            "runs offline (HF_HUB_OFFLINE=1) and will not download it",
            fix=f"uv run python -c \"from huggingface_hub import snapshot_download as d; d('{MODEL_NAME}')\"",
        )
    return Check("embedder", Status.PASS, f"local {MODEL_NAME} installed and cached")


def _optional_checks() -> list[Check]:
    from src.integrations.google.auth import GoogleOAuthSettings
    from src.integrations.notion.settings import NotionSettings

    out: list[Check] = []
    try:
        g = GoogleOAuthSettings()
        have = g.client_id is not None and g.client_secret is not None
        out.append(
            Check(
                "google",
                Status.PASS,
                "client id and secret present (also needs scripts/google_oauth.py)"
                if have
                else "off (optional)",
            )
        )
    except ValidationError as exc:
        out.append(Check("google", Status.FAIL, "invalid " + _describe("GOOGLE_", exc)))
    try:
        n = NotionSettings()
        out.append(
            Check(
                "notion", Status.PASS, "token present" if n.token is not None else "off (optional)"
            )
        )
    except ValidationError as exc:
        out.append(Check("notion", Status.FAIL, "invalid " + _describe("NOTION_", exc)))
    return out


def offline_checks() -> list[Check]:
    app, checks = _app_checks()
    if app is not None and app.fake_backends:
        checks.append(
            Check(
                "backends",
                Status.WARN,
                "APP_FAKE_BACKENDS: telegram, model and embeddings are fakes; their checks are skipped",
            )
        )
    else:
        checks.extend(_telegram_checks())
        checks.extend(_llm_checks())
    checks.append(_data_key_check())
    checks.append(_approvals_check())
    checks.append(_card_check(app))
    if app is None or not app.fake_backends:
        checks.append(_embedder_check(app))
    checks.extend(_optional_checks())
    return checks


# -- database ------------------------------------------------------------


def _alembic_head() -> str | None:
    from alembic.config import Config  # type: ignore[import-not-found,unused-ignore]
    from alembic.script import ScriptDirectory  # type: ignore[import-not-found,unused-ignore]

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    head: str | None = ScriptDirectory.from_config(cfg).get_current_head()
    return head


async def _probe_db(dsn: str) -> tuple[str | None, bool]:
    """(current alembic revision or None, pgvector installed)."""
    from sqlalchemy import text

    from src.app.db import build_engine

    engine = build_engine(dsn, statement_timeout_s=10)
    try:
        async with asyncio.timeout(15):
            async with engine.connect() as conn:
                has_table: bool = (
                    await conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
                ).scalar_one()
                rev = None
                if has_table:
                    rev = (
                        await conn.execute(text("SELECT max(version_num) FROM alembic_version"))
                    ).scalar()
                vec: int = (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                    )
                ).scalar_one()
                return (str(rev) if rev is not None else None), bool(vec)
    finally:
        await engine.dispose()


def database_checks() -> list[Check]:
    from config.settings import Settings as DbSettings

    try:
        dsn = DbSettings().postgres_dsn
    except ValidationError as exc:
        return [Check("database", Status.FAIL, "invalid " + _describe("POSTGRES_", exc))]
    try:
        rev, vector = asyncio.run(_probe_db(dsn))
    except Exception as exc:  # noqa: BLE001 - type name only: the DSN carries the password
        return [
            Check(
                "database",
                Status.FAIL,
                f"cannot connect ({type(exc).__name__})",
                fix="docker compose up -d (Docker Desktop must be running); check POSTGRES_* "
                "match what the container was first created with",
            )
        ]
    out = [Check("database", Status.PASS, "reachable")]
    try:
        head = _alembic_head()
    except Exception as exc:  # noqa: BLE001
        return [
            *out,
            Check(
                "database migrations",
                Status.FAIL,
                f"cannot read alembic head ({type(exc).__name__})",
            ),
        ]
    if rev == head:
        out.append(Check("database migrations", Status.PASS, f"at head {head}"))
    else:
        out.append(
            Check(
                "database migrations",
                Status.FAIL,
                f"at {rev or 'no migrations'}, head is {head}",
                fix=MIGRATE_CMD,
            )
        )
    out.append(
        Check("pgvector", Status.PASS, "extension installed")
        if vector
        else Check(
            "pgvector",
            Status.FAIL,
            "extension 'vector' is not installed",
            fix=f"{MIGRATE_CMD} (creates it); needs the repo's Postgres image (docker compose), not a stock one",
        )
    )
    return out


# -- online --------------------------------------------------------------


def _llm_error_hint(status: int | None, code: str) -> str:
    if status in (401, 403):
        return "the key was rejected: check LLM_API_KEY"
    if status == 404:
        return "model or path not found: check LLM_MODEL and LLM_BASE_URL (include /v1 if your provider needs it)"
    if status == 429 or code == "rate_limited":
        return "provider rate limit (free-tier quota): wait, or lower LLM_RPM_LIMIT"
    return ""


async def _online() -> list[Check]:
    from config.settings import Settings as DbSettings
    from src.app.db import build_engine, build_session_factory
    from src.gateway.probe import get_me
    from src.gateway.settings import GatewaySettings
    from src.llm.pg_stores import PostgresAuditStore, PostgresFlagStore
    from src.llm.probe import ping
    from src.llm.settings import LLMSettings

    out: list[Check] = []
    app = AppSettings()
    bot = await get_me(GatewaySettings())  # type: ignore[call-arg]
    out.append(
        Check(
            "telegram getMe",
            Status.PASS if bot.ok else Status.FAIL,
            bot.detail,
            fix="re-copy the token from @BotFather" if bot.auth_failed else "",
        )
    )
    llm_cfg = LLMSettings()
    engine = build_engine(DbSettings().postgres_dsn, statement_timeout_s=app.db_statement_timeout_s)
    try:
        sf = build_session_factory(engine)
        audit = PostgresAuditStore(sf, retention_days=llm_cfg.payload_retention_days)
        chat, emb = await ping(
            llm_cfg,
            flags=PostgresFlagStore(sf),
            audit=audit,
            ledger=audit,
            embed_model=app.embed_model if app.embedder == "api" else None,
        )
    finally:
        await engine.dispose()
    out.append(
        Check(
            "llm call",
            Status.PASS if chat.ok else Status.FAIL,
            chat.detail,
            fix="" if chat.ok else _llm_error_hint(chat.status, chat.code),
        )
    )
    if emb is not None:
        out.append(
            Check(
                "embedding call",
                Status.PASS if emb.ok else Status.FAIL,
                emb.detail if emb.ok else "failed: " + emb.detail,
                fix=""
                if emb.ok
                else "your provider may not serve this embedding model at 1024 dims: set "
                "APP_EMBED_MODEL, or run without memory (chat still works)",
            )
        )
    return out


def online_checks() -> list[Check]:
    return asyncio.run(_online())


# -- entry ---------------------------------------------------------------


def run_preflight(
    *,
    online: bool = False,
    db_checks: Callable[[], list[Check]] = database_checks,
    online_checks: Callable[[], list[Check]] = online_checks,
) -> int:
    checks = offline_checks()
    checks.extend(db_checks())
    failed = any(c.status is Status.FAIL for c in checks)
    if online:
        if failed:
            checks.append(
                Check("online checks", Status.WARN, "skipped: fix the FAIL items above first")
            )
        else:
            checks.extend(online_checks())
    print(render(checks))
    return 1 if any(c.status is Status.FAIL for c in checks) else 0
