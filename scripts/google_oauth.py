#!/usr/bin/env python3
# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""One-time Google OAuth for Nimbus (P3.4). Runs on the OPERATOR'S machine,
never inside the bot (R3: the app has no inbound port).

    uv run python scripts/google_oauth.py authorize [--scopes calendar,gmail-read,...]
    uv run python scripts/google_oauth.py status
    uv run python scripts/google_oauth.py revoke
    NIMBUS_LIVE_GOOGLE=1 uv run python scripts/google_oauth.py smoke

Needs in the environment: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, NIMBUS_DATA_KEY
and the POSTGRES_* settings of the database that holds the bot's `system_flag`
table (an SSH tunnel to the VM's Postgres works). See docs/runbooks/google_oauth.md.

The consent URL is printed (it holds no secret: client id, PKCE challenge,
state) and opened in your browser. The loopback listener binds 127.0.0.1 on an
ephemeral port and exits after one request. Tokens, codes and the PKCE
verifier are never printed or logged. `smoke` is the only command meant for
live verification and is gated by NIMBUS_LIVE_GOOGLE=1; it performs one token
refresh (no Google API data is read) and prints the granted scopes only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.logging import install_log_scrubbing
from src.db.crypto import CryptoError, DataCipher
from src.integrations.google.auth import (
    SCOPE_CALENDAR_EVENTS,
    SCOPE_DRIVE_READONLY,
    SCOPE_GMAIL_COMPOSE,
    SCOPE_GMAIL_READONLY,
    GoogleAuth,
    GoogleAuthError,
    GoogleOAuthSettings,
    GoogleTokenClient,
    PostgresTokenStore,
)
from src.integrations.google.consent import authorize_interactive
from src.net import EgressClient

SCOPE_NAMES = {
    "calendar": SCOPE_CALENDAR_EVENTS,
    "gmail-read": SCOPE_GMAIL_READONLY,
    "gmail-compose": SCOPE_GMAIL_COMPOSE,
    "drive-read": SCOPE_DRIVE_READONLY,
}
# gmail-compose is NOT in the default: whether to request it is the human
# decision H16 (ADR-0007). It is refused unless GOOGLE_ALLOW_GMAIL_COMPOSE=true.
DEFAULT_SCOPE_NAMES = "calendar,gmail-read,drive-read"
COMPOSE_NAME = "gmail-compose"
LIVE_ENV = "NIMBUS_LIVE_GOOGLE"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("authorize", help="run the one-time consent flow")
    auth.add_argument(
        "--scopes",
        default=DEFAULT_SCOPE_NAMES,
        help="comma list from: " + ", ".join(SCOPE_NAMES) + f" (default: {DEFAULT_SCOPE_NAMES}; "
        f"'{COMPOSE_NAME}' needs GOOGLE_ALLOW_GMAIL_COMPOSE=true, pending decision H16)",
    )
    auth.add_argument(
        "--port", type=int, default=0, help="fixed loopback port (default: ephemeral)"
    )
    auth.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for consent")
    auth.add_argument("--no-browser", action="store_true", help="only print the URL")
    sub.add_parser("status", help="show whether a token is stored and its scopes")
    sub.add_parser("revoke", help="revoke at Google and delete the stored token")
    sub.add_parser("smoke", help=f"live token refresh check (needs {LIVE_ENV}=1)")
    return parser.parse_args(argv)


def _resolve_scopes(raw: str, *, allow_compose: bool) -> list[str]:
    scopes: list[str] = []
    for name in (n.strip() for n in raw.split(",") if n.strip()):
        if name not in SCOPE_NAMES:
            raise SystemExit(f"unknown scope name: {name!r} (allowed: {', '.join(SCOPE_NAMES)})")
        if name == COMPOSE_NAME and not allow_compose:
            raise SystemExit(
                f"scope {COMPOSE_NAME!r} is disabled: set GOOGLE_ALLOW_GMAIL_COMPOSE=true only "
                "after the human decision H16 (ADR-0007)"
            )
        scopes.append(SCOPE_NAMES[name])
    if not scopes:
        raise SystemExit("no scopes selected")
    return scopes


def _present(url: str, *, open_browser: bool) -> None:
    print("Open this URL in your browser and approve access:\n")
    print(url)
    print(
        "\nGoogle will show an 'unverified app' warning: choose Advanced, then "
        "'Go to <app> (unsafe)'. See docs/runbooks/google_oauth.md (R15).\n"
    )
    if open_browser:
        webbrowser.open(url)


async def _main(args: argparse.Namespace) -> int:
    from src.db.postgres import async_session_factory, engine  # lazy: builds the engine

    settings = GoogleOAuthSettings()
    secrets = [
        v.get_secret_value() for v in (settings.client_id, settings.client_secret) if v is not None
    ]
    install_log_scrubbing(secrets=secrets)
    try:
        cipher = DataCipher.from_env()
    except CryptoError:
        print("FAILED: NIMBUS_DATA_KEY is missing or invalid")
        return 2
    store = PostgresTokenStore(
        async_session_factory, cipher, allow_compose=settings.allow_gmail_compose
    )
    try:
        async with EgressClient() as egress:
            auth = GoogleAuth(GoogleTokenClient(settings, egress), store)
            if args.command == "authorize":
                granted = await authorize_interactive(
                    settings=settings,
                    egress=egress,
                    store=store,
                    scopes=_resolve_scopes(args.scopes, allow_compose=settings.allow_gmail_compose),
                    present_url=lambda url: _present(url, open_browser=not args.no_browser),
                    timeout_s=args.timeout,
                    port=args.port,
                )
                print("OK: refresh token stored (encrypted). Granted scopes:")
                for scope in sorted(granted):
                    print(f"  {scope}")
            elif args.command == "status":
                st = await auth.status()
                print(f"token stored: {st.has_token}")
                print(f"scopes ok:    {st.scope_ok}" + (f" (error={st.error})" if st.error else ""))
                for scope in sorted(st.scopes):
                    print(f"  {scope}")
            elif args.command == "revoke":
                await auth.revoke_and_forget()
                print("OK: revoked at Google and deleted locally")
            else:  # smoke
                if os.environ.get(LIVE_ENV) != "1":
                    print(f"SKIPPED: set {LIVE_ENV}=1 to run the live smoke test")
                    return 3
                await auth.access_token()
                st = await auth.status()
                print("OK: access token obtained. Scopes:", ", ".join(sorted(st.scopes)))
    except GoogleAuthError as exc:
        print(f"FAILED: code={exc.code}" + (" (re-authorise needed)" if exc.needs_reauth else ""))
        if exc.code == "revoke_failed":
            print(
                "Local token deleted. Revoke manually at https://myaccount.google.com/permissions"
            )
        return 1
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
