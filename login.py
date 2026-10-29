#!/usr/bin/env python
"""One-time sign-in for the MyKnowledge service account.

Run this once on the machine that runs the server:

    python login.py            # opens a browser; sign in as the service account
    python login.py --link     # print the sign-in link instead of opening a browser
    python login.py --check    # confirm the saved sign-in still works (no browser)

You type the password into the Microsoft sign-in window. This script never sees,
prints or stores it. What it saves is the MSAL token cache (a refresh token),
which the server then uses to get new access tokens silently.

The token cache is a long-lived credential. Keep it on this machine, readable
only by you, and never commit it to git.
"""
from __future__ import annotations

import argparse
import sys

import msal
from msal.oauth2cli.authcode import AuthCodeReceiver

from app import auth
from app.config import ConfigError, Settings


def check(settings: Settings) -> int:
    try:
        auth.get_token(settings)
    except auth.LoginRequiredError as exc:
        print(f"Not signed in: {exc}")
        return 1
    print(f"OK: cached sign-in for {settings.service_account_username} works.")
    return 0


LINK_TIMEOUT_SECONDS = 600


def _sign_in_with_link(app: msal.PublicClientApplication, settings: Settings) -> dict:
    """Same interactive auth-code flow, but print the sign-in link instead of opening a browser."""
    with AuthCodeReceiver(port=0) as receiver:
        flow = app.initiate_auth_code_flow(
            scopes=[settings.token_scope],
            redirect_uri=f"http://localhost:{receiver.get_port()}",
            login_hint=settings.service_account_username,
            prompt=msal.Prompt.SELECT_ACCOUNT,
            response_mode="form_post",
        )
        print(f"Open this link in a browser on THIS computer and sign in as: {settings.service_account_username}")
        print("(An InPrivate/Incognito window avoids picking up another signed-in account.)\n")
        print(flow["auth_uri"])
        print(f"\nWaiting up to {LINK_TIMEOUT_SECONDS // 60} minutes. The link works once, for this run only.")
        auth_response = receiver.get_auth_response(timeout=LINK_TIMEOUT_SECONDS, state=flow["state"])
    if not auth_response:
        return {"error": "timed_out", "error_description": "No sign-in arrived before the link expired."}
    return app.acquire_token_by_auth_code_flow(flow, auth_response)


def sign_in(settings: Settings, use_link: bool = False) -> int:
    cache = auth.load_cache(settings.token_cache_path)
    app = auth.build_app(settings, cache)

    if use_link:
        result = _sign_in_with_link(app, settings)
    else:
        print(f"A browser window will open. Sign in as: {settings.service_account_username}")
        print("If the browser is already signed in to another account, choose 'Use another account'.")
        result = app.acquire_token_interactive(
            scopes=[settings.token_scope],
            login_hint=settings.service_account_username,
            prompt=msal.Prompt.SELECT_ACCOUNT,
        )

    if "access_token" not in result:
        print(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
        return 1

    signed_in = (result.get("id_token_claims") or {}).get("preferred_username", "")
    if signed_in.lower() != settings.service_account_username.lower():
        # Do not cache a personal account by mistake: the server would then call
        # the agent as that person.
        print(
            f"You signed in as {signed_in or 'an unknown account'}, "
            f"but this app needs {settings.service_account_username}. Nothing was saved."
        )
        return 1

    auth.save_cache(cache, settings.token_cache_path)
    print(f"Signed in as {signed_in}.")
    print(f"Token cache saved to {settings.token_cache_path} (owner-only permissions).")
    print("Treat that file like a password. Now start the server.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify the saved sign-in without opening a browser")
    parser.add_argument("--link", action="store_true", help="print the sign-in link instead of opening a browser")
    args = parser.parse_args()

    settings = Settings.from_env()
    try:
        settings.require_real_agent_config()
    except ConfigError as exc:
        print(exc)
        return 1
    try:
        return check(settings) if args.check else sign_in(settings, use_link=args.link)
    except (OSError, ValueError) as exc:  # requests' network errors are OSErrors; MSAL raises ValueError for a bad authority
        print(
            f"Could not reach Microsoft sign-in ({type(exc).__name__}). Check this machine's internet "
            "connection and proxy settings, confirm TENANT_ID is correct, then run the command again."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
