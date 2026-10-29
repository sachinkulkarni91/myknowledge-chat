"""Service-account sign-in for the MyKnowledge agent.

`login.py` signs in once (interactive browser login). After that the server only
ever calls `get_token()`, which refreshes silently from the MSAL token cache.

The token cache is a long-lived credential: anyone who can read the file can call
the agent as the service account. It is written with 0600 permissions (owner
only) on Linux and macOS; on Windows, keep it in a folder only your user can read.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import msal

from .config import Settings

log = logging.getLogger(__name__)


class LoginRequiredError(RuntimeError):
    """There is no usable cached sign-in. Run `python login.py`."""


_lock = threading.Lock()
_state: dict[str, tuple[msal.SerializableTokenCache, msal.PublicClientApplication]] = {}


def authority(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.tenant_id}"


def load_cache(path: str) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    file = Path(path)
    if file.exists():
        cache.deserialize(file.read_text(encoding="utf-8"))
    return cache


def save_cache(cache: msal.SerializableTokenCache, path: str) -> None:
    """Write the cache if it changed. Owner-only permissions, replaced atomically."""
    if not cache.has_state_changed:
        return
    file = Path(path)
    if not file.parent.exists():
        file.parent.mkdir(parents=True, mode=0o700)
    tmp = file.with_name(file.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(cache.serialize())
    os.replace(tmp, file)
    cache.has_state_changed = False


def build_app(settings: Settings, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id=settings.client_id,
        authority=authority(settings),
        token_cache=cache,
    )


def _app_and_cache(settings: Settings):
    key = settings.token_cache_path
    if key not in _state:
        cache = load_cache(key)
        _state[key] = (cache, build_app(settings, cache))
    return _state[key]


def get_token(settings: Settings) -> str:
    """Return a valid access token, refreshing silently. Blocking: call from a thread.

    Raises LoginRequiredError if nobody has signed in yet or the refresh token
    has expired or been revoked.
    """
    last_error = ""
    with _lock:
        # Two passes: if login.py was re-run after this server started, the
        # second pass re-reads the cache from disk.
        for _ in range(2):
            cache, app = _app_and_cache(settings)
            accounts = app.get_accounts(username=settings.service_account_username or None)
            result = None
            if accounts:
                result = app.acquire_token_silent([settings.token_scope], account=accounts[0])
            if result and result.get("access_token"):
                save_cache(cache, settings.token_cache_path)
                return result["access_token"]
            if result:
                last_error = str(result.get("error", ""))
            _state.pop(settings.token_cache_path, None)
    detail = f" (reason: {last_error})" if last_error else ""
    raise LoginRequiredError(
        "No valid sign-in for the service account" + detail + ". Run `python login.py` on this server."
    )


async def get_token_async(settings: Settings) -> str:
    return await asyncio.to_thread(get_token, settings)
