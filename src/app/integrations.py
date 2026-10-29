# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Third-party tool groups (Google Calendar, Google Drive, Notion): bound at
startup ONLY when their credentials are configured (H25, H26, P3.4).

`wire_integrations` returns the tool scopes to enable. A scope is added only
when its group was bound, so an absent credential means the group stays
unbound (its tools are not offered and fail closed "unavailable") and the
policy keeps denying it (`deny.scope_disabled`). With no Google or Notion
settings the result is exactly `LOCAL_SCOPES` and the app boots as before.

* Notion: `NOTION_TOKEN` set and non-empty -> `notion`.
* Google: `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` set, `NIMBUS_DATA_KEY`
  set, and the encrypted token store holds an in-policy grant (scope-lint with
  the same compose flag as the token client). Then `calendar` if the grant has
  `calendar.events`, `drive` if it has `drive.readonly`. The stored refresh
  token is added to the log scrubber (INV-7). Reauth notices go to the owner
  chat as fixed text.
* No network egress (fake backends): nothing is bound.

Every failure here is logged as a fixed code or type name only and leaves the
group off; it never stops the app from booting. Gmail is not wired (H16).
Untrusted text from these tools reaches the model only through the quarantine
lane, under the per-turn budget (P3.0 MED-D; wired in `src.app.runtime`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock
from src.core.logging import install_log_scrubbing
from src.db.crypto import CryptoError, DataCipher
from src.gateway.protocols import Gateway
from src.integrations.google.auth import (
    SCOPE_CALENDAR_EVENTS,
    SCOPE_DRIVE_READONLY,
    GoogleAuth,
    GoogleAuthError,
    GoogleOAuthSettings,
    GoogleTokenClient,
    PostgresTokenStore,
    ReauthNotice,
    ReauthNotifier,
    TokenStore,
    lint_scopes,
)
from src.integrations.google.calendar_api import CalendarClient
from src.integrations.google.drive_api import DriveReader
from src.integrations.notion.api import NotionClient
from src.integrations.notion.settings import NotionSettings
from src.net import EgressClient
from src.policy import LOCAL_SCOPES
from src.tools.integrations.drive_read import DriveReadTool
from src.tools.integrations.google_calendar import GoogleCalendarTool
from src.tools.integrations.notion import NotionTool
from src.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WiredIntegrations:
    scopes: frozenset[str]
    google_auth: GoogleAuth | None = field(default=None, repr=False)


def google_client_configured(settings: GoogleOAuthSettings | None) -> bool:
    if settings is None or settings.client_id is None or settings.client_secret is None:
        return False
    return bool(settings.client_id.get_secret_value() and settings.client_secret.get_secret_value())


def notion_configured(settings: NotionSettings | None) -> bool:
    return (
        settings is not None
        and settings.token is not None
        and bool(settings.token.get_secret_value())
    )


def _owner_notifier(gateway: Gateway) -> ReauthNotifier:
    async def notify(notice: ReauthNotice) -> None:
        # Fixed text from `REAUTH_MESSAGES`; the owner chat only (R2).
        await gateway.send_text(notice.message)

    return notify


def _group(registry: ToolRegistry, name: str, kind: type) -> object | None:
    try:
        group = registry.get(name)
    except KeyError:
        return None
    return group if isinstance(group, kind) else None


async def wire_integrations(
    registry: ToolRegistry,
    *,
    egress: EgressClient | None,
    sf: async_sessionmaker[AsyncSession] | None,
    gateway: Gateway,
    clock: Clock,
    google: GoogleOAuthSettings | None,
    notion: NotionSettings | None,
    google_store: TokenStore | None = None,
) -> WiredIntegrations:
    scopes = set(LOCAL_SCOPES)
    if egress is None:
        if notion_configured(notion) or google_client_configured(google):
            log.info("integrations off: no network egress (fake backends)")
        return WiredIntegrations(frozenset(scopes))

    if notion_configured(notion):
        assert notion is not None and notion.token is not None
        token = notion.token
        group = _group(registry, "notion", NotionTool)
        if isinstance(group, NotionTool):
            if _bind("notion", lambda: group.bind(NotionClient(egress, token))):
                scopes.add("notion")
                log.info("integration on: notion")
        else:
            log.warning("integration off: notion tool group missing")

    auth: GoogleAuth | None = None
    if google_client_configured(google):
        assert google is not None
        auth, granted = await _google_auth(google, egress, sf, gateway, google_store)
        if auth is not None:
            cal = _group(registry, "calendar", GoogleCalendarTool)
            drive = _group(registry, "drive", DriveReadTool)
            ga = auth
            if (
                SCOPE_CALENDAR_EVENTS in granted
                and isinstance(cal, GoogleCalendarTool)
                and _bind("calendar", lambda: cal.bind(CalendarClient(egress, ga), clock))
            ):
                scopes.add("calendar")
            if (
                SCOPE_DRIVE_READONLY in granted
                and isinstance(drive, DriveReadTool)
                and _bind("drive", lambda: drive.bind(DriveReader(egress, ga)))
            ):
                scopes.add("drive")
            log.info(
                "integration google: calendar=%s drive=%s",
                "on" if "calendar" in scopes else "off",
                "on" if "drive" in scopes else "off",
            )
    return WiredIntegrations(frozenset(scopes), auth)


def _bind(name: str, bind: Callable[[], None]) -> bool:
    """Construct a client and bind its group. Any failure leaves the group
    unbound (its scope stays off) and is logged by type name only; boot goes on."""
    try:
        bind()
    except Exception as exc:  # noqa: BLE001 - never block boot on an integration
        log.warning("integration off: %s err=%s", name, type(exc).__name__)
        return False
    return True


async def _google_auth(
    settings: GoogleOAuthSettings,
    egress: EgressClient,
    sf: async_sessionmaker[AsyncSession] | None,
    gateway: Gateway,
    store: TokenStore | None,
) -> tuple[GoogleAuth | None, frozenset[str]]:
    allow_compose = settings.allow_gmail_compose
    if store is None:
        if sf is None:
            log.warning("integration off: google (no database)")
            return None, frozenset()
        try:
            cipher = DataCipher.from_env()
        except CryptoError:
            log.warning("integration off: google (NIMBUS_DATA_KEY not set)")
            return None, frozenset()
        except Exception as exc:  # noqa: BLE001 - never block boot on an integration
            log.warning("integration off: google err=%s", type(exc).__name__)
            return None, frozenset()
    try:
        if store is None:
            # One flag for store and client: scope-lint agrees everywhere (P3.4).
            store = PostgresTokenStore(sf, cipher, allow_compose=allow_compose)  # type: ignore[arg-type]
        creds = await store.load()
        if creds is None:
            log.info("integration off: google (not authorised; run scripts/google_oauth.py)")
            return None, frozenset()
        granted = lint_scopes(creds.scopes, allow_compose=allow_compose)
        auth = GoogleAuth(
            GoogleTokenClient(settings, egress), store, notifier=_owner_notifier(gateway)
        )
    except GoogleAuthError as exc:
        log.warning("integration off: google code=%s", exc.code)
        return None, frozenset()
    except Exception as exc:  # noqa: BLE001 - never block boot on an integration
        log.warning("integration off: google err=%s", type(exc).__name__)
        return None, frozenset()
    install_log_scrubbing([creds.refresh_token.get_secret_value()])
    return auth, granted


__all__ = [
    "WiredIntegrations",
    "google_client_configured",
    "notion_configured",
    "wire_integrations",
]
