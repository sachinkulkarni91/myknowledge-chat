"""Maps a visitor's session id to the agent's conversation id.

The store sits behind a small interface so it can be swapped for Redis (or any
shared store) when the app runs on more than one worker or machine. The
in-memory version below is fine for a single process.
"""
from __future__ import annotations

import secrets
import time
from typing import Callable, Protocol


class SessionStore(Protocol):
    def create(self, conversation_id: str) -> str: ...

    def get(self, session_id: str) -> str | None: ...

    def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(
        self,
        ttl_seconds: float = 2 * 60 * 60,
        max_sessions: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._clock = clock
        self._items: dict[str, tuple[str, float]] = {}  # id -> (conversation id, last used)

    def _prune(self) -> None:
        now = self._clock()
        for sid in [s for s, (_, used) in self._items.items() if now - used > self._ttl]:
            del self._items[sid]
        while len(self._items) >= self._max:
            oldest = min(self._items, key=lambda s: self._items[s][1])
            del self._items[oldest]

    def create(self, conversation_id: str) -> str:
        self._prune()
        session_id = secrets.token_urlsafe(24)
        self._items[session_id] = (conversation_id, self._clock())
        return session_id

    def get(self, session_id: str) -> str | None:
        entry = self._items.get(session_id)
        if entry is None:
            return None
        conversation_id, used = entry
        now = self._clock()
        if now - used > self._ttl:
            del self._items[session_id]
            return None
        self._items[session_id] = (conversation_id, now)
        return conversation_id

    def delete(self, session_id: str) -> None:
        self._items.pop(session_id, None)
