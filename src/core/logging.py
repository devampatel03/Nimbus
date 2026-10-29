# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Scrubbing log filter (INV-7): no secret or token reaches a log record.

`ScrubbingFilter` is a stdlib `logging.Filter` that rewrites the record in
place: the formatted message (so `%s` args cannot smuggle a secret past it),
`args` (cleared once folded in), string `extra=` attributes, exception text
and stack info. Patterns come from `src.llm.scrub` (single definition of
"secret"); canaries / literal keys are passed in.

`install_log_scrubbing()` is the single setup function. It wraps
`logging.Logger.makeRecord` at class level: the stdlib merges `extra=` into the
record only *after* the record factory ran, so a factory hook would never see
extras. Scrubbing after the merge covers every logger (created before or after
install) and every handler. Idempotent: a second call MERGES its literals into
the live filter. Returns a restore callable for tests.

Scrubbing is idempotent and has no "already done" short-circuit, so a
handler-attached `ScrubbingFilter` still scrubs. `exc_info` is replaced by a
redacted `exc_text` so a re-rendering formatter cannot leak the traceback.

Matchers (P4.4): `install_log_scrubbing(matchers=...)` adds predicates (the
canary detector) run on every scrubbed string after literal/pattern redaction;
True replaces the whole string with `[REDACTED:canary]`, so an obfuscated canary
(base64, spaced, homoglyphs ...) cannot reach a log either.

Not redacted here: card/Aadhaar/OTP-shaped numbers (those are redacted in
outbound prompts by `Scrubber.scrub`, but logs only drop secrets/tokens/
canaries; do not log such values). Free-form personal text is also not
detected: the filter is a backstop, not a licence to log user content.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Iterable
from typing import Any

_STD_ATTRS = frozenset(
    logging.LogRecord("x", 0, "x", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def _redact(text: str, literals: tuple[str, ...]) -> str:
    # Lazy import: src.llm's package init pulls the SDK stack, and src.llm may
    # itself use src.core later; keep this module import-cycle free.
    from src.llm.scrub import redact_secrets

    return redact_secrets(text, literals)


Matcher = Callable[[str], bool]
MATCHED = "[REDACTED:canary]"


class ScrubbingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = (), matchers: Iterable[Matcher] = ()) -> None:
        super().__init__()
        self._literals: tuple[str, ...] = tuple(secrets)
        self._matchers: tuple[Matcher, ...] = tuple(matchers)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        self._literals = tuple(dict.fromkeys((*self._literals, *secrets)))

    def add_matchers(self, matchers: Iterable[Matcher]) -> None:
        self._matchers = tuple(dict.fromkeys((*self._matchers, *matchers)))

    def _red(self, text: str) -> str:
        out = _redact(text, self._literals)
        if any(m(out) for m in self._matchers):
            return MATCHED
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception:  # noqa: BLE001 - fail closed on any scrub error
            record.msg = "[REDACTED:log_scrub_error]"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            for key in [k for k in record.__dict__ if k not in _STD_ATTRS]:
                record.__dict__[key] = "[REDACTED:log_scrub_error]"
        return True

    def _scrub_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._red(value)
        # Non-str extras (dict/list/exception/bytes ...): scrub their repr; keep
        # the original object (and its type) only when nothing was found.
        text = repr(value)
        redacted = self._red(text)
        return value if redacted == text else redacted

    def _scrub(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never raise
            # Bad format/args: fall back to msg + repr of args, both scrubbed.
            message = f"{record.msg} {record.args!r}"
        record.msg = self._red(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.exc_text:
            record.exc_text = self._red(record.exc_text)
        # The raw traceback must not survive on the record: a formatter that
        # re-renders from exc_info would bypass exc_text.
        record.exc_info = None
        if record.stack_info:
            record.stack_info = self._red(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key not in _STD_ATTRS:
                record.__dict__[key] = self._scrub_value(value)


def install_log_scrubbing(
    secrets: Iterable[str] = (), matchers: Iterable[Matcher] = ()
) -> Callable[[], None]:
    """Scrub every log record process-wide. Returns a callable that undoes it."""
    current = logging.Logger.makeRecord
    live: ScrubbingFilter | None = getattr(current, "_nimbus_filter", None)
    if live is not None:
        live.add_secrets(secrets)
        live.add_matchers(matchers)
        return lambda: None
    flt = ScrubbingFilter(secrets, matchers)

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(self, *args, **kwargs)  # extras are merged in here
        flt.filter(record)
        return record

    make_record._nimbus_filter = flt  # type: ignore[attr-defined]
    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]

    def restore() -> None:
        if logging.Logger.makeRecord is make_record:
            logging.Logger.makeRecord = current  # type: ignore[method-assign]

    return restore


def add_log_matchers(matchers: Iterable[Matcher]) -> Callable[[], None]:
    """Add matchers to the live scrubber (installing it if needed: fail closed)."""
    return install_log_scrubbing((), matchers)
