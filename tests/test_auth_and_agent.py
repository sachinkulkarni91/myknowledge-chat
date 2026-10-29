"""Token cache handling, session store, and the real-agent wrapper (with a fake SDK client)."""
from __future__ import annotations

import os
import stat
import sys

import msal
import pytest
from microsoft_agents.activity import Activity

from app import agent_client, auth
from app.agent_client import AgentError, CopilotStudioAgent
from app.config import ConfigError, Settings
from app.sessions import InMemorySessionStore

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX only")


def settings_for(tmp_path, **overrides) -> Settings:
    values = dict(
        tenant_id="tenant",
        client_id="client",
        environment_id="env",
        agent_schema_name="schema",
        service_account_username="svc@example.com",
        token_cache_path=str(tmp_path / "secrets" / "cache.bin"),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def clear_auth_state():
    auth._state.clear()
    yield
    auth._state.clear()


# ---- token cache -------------------------------------------------------------


@posix_only
def test_cache_is_saved_owner_only(tmp_path):
    settings = settings_for(tmp_path)
    cache = msal.SerializableTokenCache()
    cache.has_state_changed = True

    auth.save_cache(cache, settings.token_cache_path)

    assert stat.S_IMODE(os.stat(settings.token_cache_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(settings.token_cache_path)).st_mode) == 0o700
    assert not cache.has_state_changed


def test_unchanged_cache_is_not_rewritten(tmp_path):
    settings = settings_for(tmp_path)
    cache = msal.SerializableTokenCache()
    cache.has_state_changed = False
    auth.save_cache(cache, settings.token_cache_path)
    assert not os.path.exists(settings.token_cache_path)


class FakeMsalApp:
    def __init__(self, accounts, result):
        self.accounts, self.result = accounts, result
        self.requested_username = "unset"

    def get_accounts(self, username=None):
        self.requested_username = username
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        return self.result


def test_get_token_returns_silent_token(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    fake = FakeMsalApp([{"username": "svc@example.com"}], {"access_token": "token-123"})
    monkeypatch.setattr(auth, "build_app", lambda s, cache: fake)

    assert auth.get_token(settings) == "token-123"
    assert fake.requested_username == "svc@example.com"


def test_get_token_without_sign_in_asks_for_login_and_retries_from_disk(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    built = []

    def build(s, cache):
        built.append(1)
        return FakeMsalApp([], None)

    monkeypatch.setattr(auth, "build_app", build)

    with pytest.raises(auth.LoginRequiredError, match="login.py"):
        auth.get_token(settings)
    assert len(built) == 2  # second pass re-read the cache, in case login.py ran meanwhile


def test_get_token_reports_refresh_failure_reason(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    fake = FakeMsalApp([{"username": "svc@example.com"}], {"error": "invalid_grant"})
    monkeypatch.setattr(auth, "build_app", lambda s, cache: fake)

    with pytest.raises(auth.LoginRequiredError, match="invalid_grant"):
        auth.get_token(settings)


def test_real_agent_config_lists_missing_settings():
    with pytest.raises(ConfigError, match="TENANT_ID"):
        Settings().require_real_agent_config()


# ---- sessions ------------------------------------------------------------------


def test_sessions_expire_after_ttl():
    now = [0.0]
    store = InMemorySessionStore(ttl_seconds=10, clock=lambda: now[0])
    sid = store.create("conv")
    now[0] = 9
    assert store.get(sid) == "conv"  # touching it extends its life
    now[0] = 18
    assert store.get(sid) == "conv"
    now[0] = 40
    assert store.get(sid) is None


def test_session_store_is_bounded():
    store = InMemorySessionStore(max_sessions=3)
    ids = [store.create(f"c{i}") for i in range(10)]
    assert sum(store.get(i) is not None for i in ids) <= 3
    assert store.get(ids[-1]) == "c9"


# ---- the real agent wrapper, against a fake SDK client ----------------------------


class FakeSdkClient:
    instances: list["FakeSdkClient"] = []

    def __init__(self, connection, token):
        self.connection, self.token, self.asked = connection, token, []
        FakeSdkClient.instances.append(self)

    async def start_conversation(self, emit_start_event):
        assert emit_start_event is True
        yield Activity.model_validate({"type": "message", "text": "hi", "conversation": {"id": "conv-9"}})

    async def ask_question(self, question, conversation_id):
        self.asked.append((question, conversation_id))
        yield Activity.model_validate({"type": "message", "text": "ok", "conversation": {"id": conversation_id}})


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSdkClient.instances = []
    monkeypatch.setattr(agent_client, "CopilotClient", FakeSdkClient)
    return FakeSdkClient


async def test_start_returns_the_conversation_id(fake_sdk, tmp_path):
    tokens = iter(["t1"])
    agent = CopilotStudioAgent(settings_for(tmp_path), token_provider=lambda: _async(next(tokens)))

    result = await agent.start()

    assert result.conversation_id == "conv-9"
    assert [a.text for a in result.activities] == ["hi"]
    connection = fake_sdk.instances[0].connection
    assert (connection.environment_id, connection.agent_identifier) == ("env", "schema")


async def test_ask_passes_the_conversation_id_and_reuses_the_client_for_the_same_token(fake_sdk, tmp_path):
    agent = CopilotStudioAgent(settings_for(tmp_path), token_provider=lambda: _async("t1"))

    [_ async for _ in agent.ask("q1", "conv-9")]
    [_ async for _ in agent.ask("q2", "conv-9")]

    assert len(fake_sdk.instances) == 1
    assert fake_sdk.instances[0].asked == [("q1", "conv-9"), ("q2", "conv-9")]


async def test_a_refreshed_token_gets_a_new_client(fake_sdk, tmp_path):
    tokens = iter(["t1", "t2"])
    agent = CopilotStudioAgent(settings_for(tmp_path), token_provider=lambda: _async(next(tokens)))

    [_ async for _ in agent.ask("q1", "c")]
    [_ async for _ in agent.ask("q2", "c")]

    assert [c.token for c in fake_sdk.instances] == ["t1", "t2"]


async def test_start_without_conversation_id_is_an_error(monkeypatch, tmp_path):
    class NoId(FakeSdkClient):
        async def start_conversation(self, emit_start_event):
            yield Activity.model_validate({"type": "event", "name": "x"})

    monkeypatch.setattr(agent_client, "CopilotClient", NoId)
    agent = CopilotStudioAgent(settings_for(tmp_path), token_provider=lambda: _async("t"))
    with pytest.raises(AgentError):
        await agent.start()


async def _async(value):
    return value
