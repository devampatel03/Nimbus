# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Scrubbing log filter (INV-7): no secret or token reaches a log record.

`ScrubbingFilter` is a stdlib `logging.Filter` that rewrites the record in
place: the formatted message (so `%s` args cannot smuggle a secret past it),
`args` (cleared once folded in), string `extra=` attributes, exception text
and stack info. Patterns come from `src.llm.scrub` (single definition of
"secret"); canaries / literal keys are passed in.

`install_log_scrubbing()` is the single setup function. It wraps the logging
record factory, so every record from every logger is scrubbed at creation,
whichever handlers are attached now or later (a per-handler filter would miss
handlers added afterwards, e.g. by uvicorn or pytest). Idempotent; returns a
restore callable for tests.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Iterable
from typing import Any

_MARK = "_nimbus_scrubbed"
_STD_ATTRS = frozenset(
    logging.LogRecord("x", 0, "x", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName", _MARK}
)


def _redact(text: str, literals: tuple[str, ...]) -> str:
    # Lazy import: src.llm's package init pulls the SDK stack, and src.llm may
    # itself use src.core later; keep this module import-cycle free.
    from src.llm.scrub import redact_secrets

    return redact_secrets(text, literals)


class ScrubbingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._literals = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.__dict__.get(_MARK):
            return True
        try:
            self._scrub(record)
        except Exception:  # noqa: BLE001 - fail closed on any scrub error
            # Fail closed: never emit an unscrubbed record we could not process.
            record.msg = "[REDACTED:log_scrub_error]"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        record.__dict__[_MARK] = True
        return True

    def _scrub(self, record: logging.LogRecord) -> None:
        lits = self._literals
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never raise
            # Bad format/args: fall back to msg + repr of args, both scrubbed.
            message = f"{record.msg} {record.args!r}"
        record.msg = _redact(message, lits)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.exc_text:
            record.exc_text = _redact(record.exc_text, lits)
        # Keep exc_info for handlers that inspect the type, but the formatter
        # reuses exc_text, so the raw traceback is never rendered.
        if record.stack_info:
            record.stack_info = _redact(record.stack_info, lits)
        for key, value in list(record.__dict__.items()):
            if key not in _STD_ATTRS and isinstance(value, str):
                record.__dict__[key] = _redact(value, lits)


def install_log_scrubbing(secrets: Iterable[str] = ()) -> Callable[[], None]:
    """Scrub every log record process-wide. Returns a callable that undoes it."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_nimbus_scrubbing", False):
        return lambda: None
    flt = ScrubbingFilter(secrets)

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        flt.filter(record)
        return record

    factory._nimbus_scrubbing = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)

    def restore() -> None:
        if logging.getLogRecordFactory() is factory:
            logging.setLogRecordFactory(previous)

    return restore
