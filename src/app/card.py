# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""The ONLY place outside `src/personality` that may touch the character-card
dev bypass (R9, ADR-0015). Kept tiny because the AST guard in
`tests/unit/test_r9_character_card.py` scans it against an exact budget: one
use of the env helper, one `dev_mode=True` call, no other environment access.

Rules:
* Production loads go through `load_approved_character_card()` and fail closed
  (`CardNotApprovedError`) unless the card is human-authored, not a placeholder
  and matches the approval pin.
* Dev mode is the operator environment switch read by `card_dev_mode_from_env`,
  and only when `APP_ENV=development`. With `APP_ENV=production` the process refuses to start rather than quietly
  ignoring the variable, so a leftover dev flag is noticed.
* Dev mode is announced loudly at WARNING on every start.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.personality.agent_character import (
    CHARACTER_CARD_PATH,
    CHARACTER_CARD_PIN_PATH,
    card_dev_mode_from_env,
    load_approved_character_card,
)

log = logging.getLogger(__name__)


class StartupRefused(RuntimeError):
    """The process must not start (fixed message, no values)."""


def load_card(
    app_env: str,
    *,
    path: Path = CHARACTER_CARD_PATH,
    pin_path: Path = CHARACTER_CARD_PIN_PATH,
) -> str:
    if card_dev_mode_from_env():
        if app_env != "development":
            log.critical("card dev switch is set but APP_ENV is not development; refusing")
            raise StartupRefused("character card dev mode is not allowed outside development")
        log.warning(
            "!!! CHARACTER CARD GATE BYPASSED (dev mode): this process must never serve real use"
        )
        return load_approved_character_card(path, pin_path, dev_mode=True)
    return load_approved_character_card(path, pin_path)
