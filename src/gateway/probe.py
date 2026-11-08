# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""One-shot Telegram `getMe` for `preflight --online` (through `src.net`)."""

from __future__ import annotations

from dataclasses import dataclass

from src.gateway.api import TelegramApi
from src.gateway.settings import GatewaySettings
from src.gateway.types import TelegramAuthError, TelegramError
from src.net import EgressClient, EgressError, NetSettings


@dataclass(frozen=True)
class BotProbe:
    ok: bool
    detail: str
    auth_failed: bool = False


async def get_me(settings: GatewaySettings, net: NetSettings | None = None) -> BotProbe:
    """Fixed-text result: never the token or the request URL (INV-7)."""
    try:
        async with EgressClient(net) as egress:
            me = await TelegramApi(egress, settings).call("getMe", {})
        who = me.get("username") if isinstance(me, dict) else None
        return BotProbe(True, f"token accepted, bot @{who}" if who else "token accepted")
    except TelegramAuthError:
        return BotProbe(False, "token rejected by Telegram (401/404)", auth_failed=True)
    except TelegramError as exc:
        return BotProbe(False, f"Telegram error: {exc.reason} code={exc.code}")
    except EgressError as exc:
        return BotProbe(False, f"egress: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - type name only
        return BotProbe(False, f"failed ({type(exc).__name__})")
