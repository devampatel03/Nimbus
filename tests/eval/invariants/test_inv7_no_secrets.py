# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-7: no secret/token value appears in logs, prompts, audit rows or error
messages. Canary values are planted and asserted absent from every sink.

Scope (P1.5): log filter, llm scrub/guard (prompts, audit, errors), settings
validation errors, repr of secrets. The canary sweep of DB tables (memory) is
P4.4 per TARGET_DESIGN section 9. Mutation proof: disabling
`install_log_scrubbing` (or `redact_secrets`) turns the log tests red.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from src.core.logging import install_log_scrubbing
from src.llm.settings import LLMSettings
from src.llm.types import ChatMessage, ChatRequest, Lane, LLMRefused, Purpose
from src.net.settings import NetSettings
from tests.fakes.llm import RecordingTransport, build_env

CANARY = "CANARY-inv7-5d1e9b3a"
TG_TOKEN = "bot987654321:" + "Z" * 35  # pragma: allowlist secret
SK = "sk-" + "inv7abcdefghijklmnopqrstuv" + "9999"  # pragma: allowlist secret
JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eXoxMjM0NTY.c2lnbmF0dXJlMTIzNDU2"  # pragma: allowlist secret
)
SECRETS = (CANARY, TG_TOKEN, SK, JWT)


def _no_secret(text: str) -> None:
    for s in SECRETS:
        assert s not in text, "INV-7: secret value leaked"


@pytest.fixture
def root_log() -> Iterator[io.StringIO]:
    restore = install_log_scrubbing(secrets=[CANARY])
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)  # deliberately NO per-handler filter
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        restore()


def test_logs_never_carry_secrets(root_log: io.StringIO) -> None:
    lg = logging.getLogger("nimbus.test.inv7")
    for s in SECRETS:
        lg.info("plain %s", s)
        lg.warning(f"fstring {s}")
        lg.error("dict %(k)s", {"k": s})
        lg.info("url https://api.telegram.org/%s/sendMessage", s, extra={"hdr": f"Bearer {s}"})
        try:
            raise ValueError(f"failed with {s}")
        except ValueError:
            lg.exception("caught")
    lg.info("Authorization: Bearer %s", "abcdefghijklmnopqrstuvwxyz0123456789")
    out = root_log.getvalue()
    _no_secret(out)
    assert "ValueError" in out  # exception type still visible


def test_library_loggers_are_covered(root_log: io.StringIO) -> None:
    logging.getLogger("httpx").error("GET https://api.telegram.org/%s/getMe", TG_TOKEN)
    logging.getLogger("uvicorn.error").error("token=%s", SK)
    _no_secret(root_log.getvalue())


async def test_prompt_with_secret_is_refused_and_leaks_nowhere() -> None:
    transport = RecordingTransport()
    env = build_env(transport=transport, canaries=(CANARY,))
    for secret in SECRETS:
        req = ChatRequest(
            messages=(ChatMessage(role="user", content=f"remember {secret} please"),),
            purpose=Purpose.CHAT,
            lane=Lane.TRUSTED,
        )
        with pytest.raises(LLMRefused) as ei:
            await env.llm.chat(req)
        assert ei.value.reason == "scrub_blocked"
        _no_secret(str(ei.value) + repr(ei.value))
    assert transport.calls == []
    _no_secret(repr(env.audit.records))


def test_settings_errors_and_reprs_hide_values() -> None:
    with pytest.raises(ValidationError) as ei:
        LLMSettings(_env_file=None, timeout_seconds=f"{SK}")  # type: ignore[call-arg,arg-type]
    _no_secret(str(ei.value))
    with pytest.raises(ValidationError) as ej:
        NetSettings(_env_file=None, llm_base_url=f"https://user:{SK}@llm.example.com/v1")  # type: ignore[call-arg]
    _no_secret(str(ej.value))
    s = LLMSettings(_env_file=None, api_key=SK)  # type: ignore[call-arg]
    _no_secret(repr(s) + str(s) + repr(s.model_dump()))


def test_extras_and_every_record_attribute_never_carry_secrets(root_log: io.StringIO) -> None:
    """Sinks render EVERY attribute: a formatter with %(hdr)s and a capture
    handler asserting on repr(record.__dict__) (round-1 HIGH-1)."""
    records: list[logging.LogRecord] = []

    class Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    buf = io.StringIO()
    sink = logging.StreamHandler(buf)
    sink.setFormatter(logging.Formatter("%(message)s | hdr=%(hdr)s"))
    lg = logging.getLogger("nimbus.test.inv7.extras")
    lg.propagate = False
    lg.handlers[:] = [Cap(), sink]
    lg.setLevel(logging.DEBUG)
    try:
        for s in SECRETS:
            lg.info("m", extra={"hdr": f"Bearer {s}"})
            lg.info("m", extra={"hdr": s})
            lg.info("m", extra={"hdr": {"k": s}})
            lg.info("m", extra={"hdr": [s, (s,)]})
        _no_secret(buf.getvalue())
        assert records
        _no_secret("".join(repr(r.__dict__) for r in records))
    finally:
        lg.handlers.clear()


def test_shared_settings_errors_and_reprs_hide_values() -> None:
    from config.settings import Settings

    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None, postgres_port=SK)  # type: ignore[call-arg,arg-type]
    _no_secret(str(ei.value))
    s = Settings(_env_file=None, postgres_password=SK)  # type: ignore[call-arg,arg-type]
    _no_secret(repr(s) + str(s) + repr(s.model_dump()))
    assert SK in s.postgres_dsn  # the DSN property is the one deliberate unwrap
