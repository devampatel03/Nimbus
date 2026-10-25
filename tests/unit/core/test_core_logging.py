# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""INV-7: the scrubbing log filter redacts secrets in msg, args, extras and
exception text, using `src.llm.scrub` patterns (no duplicated regexes)."""

from __future__ import annotations

import io
import logging
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
