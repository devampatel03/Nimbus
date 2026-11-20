# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""INV-7: the scrubbing log filter redacts secrets in msg, args, extras and
exception text, using `src.llm.scrub` patterns (no duplicated regexes)."""

from __future__ import annotations

import io
import logging
import traceback
from collections.abc import Iterator

import pytest

from src.core.logging import ScrubbingFilter, install_log_scrubbing

TG = "bot123456789:" + "A" * 35  # pragma: allowlist secret
SK = "sk-" + "abcdefghijklmnopqrstuvwxyz" + "123456"  # pragma: allowlist secret
CANARY = "CANARY-7f3a9c1e-zz"


@pytest.fixture
def stream_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s | %(extra_k)s"))
    lg = logging.getLogger("t.core.logging.fixture")
    lg.propagate = False
    lg.handlers.clear()
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    yield lg, buf


def _attach(lg: logging.Logger) -> None:
    for h in lg.handlers:
        h.addFilter(ScrubbingFilter(secrets=[CANARY]))


def test_unfiltered_logger_leaks(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    """Self-check: without the filter the canary reaches the output."""
    lg, buf = stream_logger
    lg.info("key %s", SK, extra={"extra_k": "x"})
    assert SK in buf.getvalue()


def test_redacts_message_and_args(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    lg, buf = stream_logger
    _attach(lg)
    lg.info("GET https://api.telegram.org/%s/getMe", TG, extra={"extra_k": "x"})
    lg.info(f"inline {SK}", extra={"extra_k": "x"})
    lg.info("lit %(a)s", {"a": CANARY}, extra={"extra_k": "x"})
    out = buf.getvalue()
    assert TG not in out and SK not in out and CANARY not in out
    assert "[REDACTED:" in out


def test_redacts_extra_attributes(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    lg, buf = stream_logger
    _attach(lg)
    lg.info("hello", extra={"extra_k": SK})
    assert SK not in buf.getvalue()


def test_redacts_exception_text(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    lg, buf = stream_logger
    _attach(lg)
    try:
        raise RuntimeError(f"upstream said {SK} and {CANARY}")
    except RuntimeError:
        lg.exception("boom", extra={"extra_k": "x"})
    out = buf.getvalue()
    assert "RuntimeError" in out and SK not in out and CANARY not in out


def test_obfuscated_secret_fails_closed(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    lg, buf = stream_logger
    _attach(lg)
    lg.info("tok %s", SK.replace("-", "%2D"), extra={"extra_k": "x"})
    assert "abcdefghijklmnop" not in buf.getvalue()


def test_bad_format_args_do_not_raise_or_leak(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    lg, buf = stream_logger
    _attach(lg)
    lg.info("one %s two %s", SK, extra={"extra_k": "x"})  # arg/format mismatch
    assert SK not in buf.getvalue()


def test_benign_text_untouched(stream_logger: tuple[logging.Logger, io.StringIO]) -> None:
    lg, buf = stream_logger
    _attach(lg)
    lg.info("meeting with Asha at 5pm %d", 3, extra={"extra_k": "x"})
    assert "meeting with Asha at 5pm 3" in buf.getvalue()


def test_install_covers_every_logger_and_is_idempotent() -> None:
    restore = install_log_scrubbing(secrets=[CANARY])
    restore2 = install_log_scrubbing(secrets=[CANARY])
    try:
        buf = io.StringIO()
        h = logging.StreamHandler(buf)  # no filter attached to it
        lg = logging.getLogger("t.core.installed.child")
        lg.addHandler(h)
        lg.propagate = False
        lg.setLevel(logging.INFO)
        lg.info("a %s b", CANARY)
        lg.info("c %s", SK)
        assert CANARY not in buf.getvalue() and SK not in buf.getvalue()
        assert buf.getvalue().count("\n") == 2
    finally:
        restore2()
        restore()
    after = logging.getLogger("t.core.installed.child")
    buf2 = io.StringIO()
    after.handlers[:] = [logging.StreamHandler(buf2)]
    after.info("d %s", SK)
    assert SK in buf2.getvalue()  # factory restored: no filtering after uninstall
    after.handlers.clear()


# ---- review round 1 ---------------------------------------------------------


def _capture() -> tuple[logging.Logger, list[logging.LogRecord], io.StringIO]:
    """A logger with a capture handler (sees the final record) and a formatter
    that renders custom attributes, as a real sink would."""
    records: list[logging.LogRecord] = []

    class Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    buf = io.StringIO()
    sink = logging.StreamHandler(buf)
    sink.setFormatter(logging.Formatter("%(message)s | hdr=%(hdr)s"))
    lg = logging.getLogger("t.core.logging.capture")
    lg.propagate = False
    lg.handlers[:] = [Cap(), sink]
    lg.setLevel(logging.DEBUG)
    return lg, records, buf


def test_install_scrubs_extras_after_merge() -> None:
    restore = install_log_scrubbing(secrets=[CANARY])
    try:
        lg, records, buf = _capture()
        lg.info("m", extra={"hdr": f"Bearer {SK}"})
        lg.info("m", extra={"hdr": {"k": SK, "n": [CANARY]}})
        lg.info("m", extra={"hdr": (TG,)})
        lg.info("m", extra={"hdr": 7})
        out = buf.getvalue()
        for s in (SK, CANARY, TG):
            assert s not in out
            assert all(s not in repr(r.__dict__) for r in records)
        assert "hdr=7" in out  # benign non-str extras keep their value
    finally:
        restore()


def test_extras_scrubbed_for_loggers_created_before_install() -> None:
    lg, _records, buf = _capture()  # logger exists first
    restore = install_log_scrubbing()
    try:
        lg.info("m", extra={"hdr": SK})
        assert SK not in buf.getvalue()
    finally:
        restore()


def test_handler_filter_still_scrubs_after_install() -> None:
    """No creation-time short-circuit: a record that was tampered with after
    creation is still scrubbed by a handler-attached filter."""
    restore = install_log_scrubbing()
    try:
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "clean", None, None)
        rec.__dict__["hdr"] = SK
        ScrubbingFilter().filter(rec)
        assert SK not in rec.__dict__["hdr"]
    finally:
        restore()


def test_second_install_merges_literals() -> None:
    r1 = install_log_scrubbing(secrets=["FIRST-literal-secret"])
    r2 = install_log_scrubbing(secrets=["SECOND-literal-secret"])
    try:
        lg, _, buf = _capture()
        lg.info("a FIRST-literal-secret b SECOND-literal-secret", extra={"hdr": "-"})
        assert "literal-secret" not in buf.getvalue()
    finally:
        r2()
        r1()


def test_exc_info_is_sanitised_for_re_rendering_formatters() -> None:
    restore = install_log_scrubbing()
    try:
        lg, records, buf = _capture()
        try:
            raise RuntimeError(f"bad {SK}")
        except RuntimeError:
            lg.exception("boom", extra={"hdr": "-"})
        rec = records[0]
        assert (
            SK not in "".join(traceback.format_exception(*rec.exc_info)) if rec.exc_info else True
        )
        assert SK not in (rec.exc_text or "")
        assert SK not in buf.getvalue()
        assert "RuntimeError" in (rec.exc_text or "")
    finally:
        restore()


def test_stack_info_is_redacted() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "m", None, None)
    rec.stack_info = f"Stack (most recent call last):\n  x = '{SK}'"
    ScrubbingFilter().filter(rec)
    assert rec.stack_info is not None and SK not in rec.stack_info


def test_fail_closed_replaces_message_and_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.logging as core_logging

    def boom(text: str, literals: tuple[str, ...]) -> str:
        raise RuntimeError("scrub broke")

    monkeypatch.setattr(core_logging, "_redact", boom)
    rec = logging.LogRecord("x", logging.INFO, "f", 1, f"has {SK}", None, None)
    rec.__dict__["hdr"] = SK
    rec.__dict__["obj"] = {"k": SK}
    rec.stack_info = SK
    ScrubbingFilter().filter(rec)
    assert "log_scrub_error" in str(rec.msg)
    assert SK not in repr(rec.__dict__)
