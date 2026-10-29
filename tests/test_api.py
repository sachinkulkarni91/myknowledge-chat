"""HTTP API behaviour, driven through the mock agent (no network)."""
from __future__ import annotations

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.agent_client import MockAgent
from app.auth import LoginRequiredError
from app.config import ConfigError, Settings
from app.main import create_app


def parse_sse(body: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


def make_client(agent=None, **settings):
    settings = Settings(mock_agent=True, mock_delay_seconds=0, **settings)
    return TestClient(create_app(settings=settings, agent=agent or MockAgent(0)))


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def start(client) -> str:
    return client.post("/api/chat/start").json()["session_id"]


def ask(client, session_id, text):
    return client.post("/api/chat/message", json={"session_id": session_id, "text": text})


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok", "mock": True}


def test_index_is_served_with_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "MyKnowledge" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_start_returns_session_and_greeting(client):
    body = client.post("/api/chat/start").json()
    assert body["session_id"]
    assert "MyKnowledge" in body["greeting"]


def test_answer_streams_thinking_then_cited_answer_then_done(client):
    response = ask(client, start(client), "How are requests handled?")

    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thinking"
    assert kinds[-1] == "done"
    assert kinds.count("answer") == 1

    answer = next(e for e in events if e["type"] == "answer")
    assert [c["title"] for c in answer["citations"]] == [
        "Request handling guide.docx",
        "Exceptions and approvals.docx",
    ]
    assert all(set(c) == {"position", "title", "excerpt"} for c in answer["citations"])
    assert "sharepoint" not in response.text.lower()
    assert "cite:" not in answer["markdown"]


def test_yes_no_follow_up_keeps_the_same_conversation(client):
    session = start(client)

    first = parse_sse(ask(client, session, "please follow up").text)
    answer = next(e for e in first if e["type"] == "answer")
    assert answer["suggestions"] == ["Yes", "No"]
    assert first[-1]["type"] == "done"

    second = parse_sse(ask(client, session, "Yes").text)
    assert next(e for e in second if e["type"] == "answer")["markdown"].startswith("Continuing")


def test_questions_reuse_one_conversation_id():
    asked = []

    class Recording(MockAgent):
        async def ask(self, text, conversation_id):
            asked.append(conversation_id)
            async for activity in super().ask(text, conversation_id):
                yield activity

    with make_client(agent=Recording(0)) as client:
        first, second = start(client), start(client)
        ask(client, first, "one")
        ask(client, first, "two")
        ask(client, second, "three")

    assert asked[0] == asked[1] != asked[2]


def test_unknown_session_is_404(client):
    response = ask(client, "does-not-exist", "hello")
    assert response.status_code == 404
    assert "expired" in response.json()["detail"]


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_question_is_rejected(client, text):
    assert ask(client, start(client), text).status_code == 422


def test_overlong_question_is_rejected():
    with make_client(max_question_chars=10) as client:
        assert ask(client, start(client), "x" * 11).status_code == 422


def test_agent_failure_becomes_an_error_event_without_details(client):
    session = start(client)
    events = parse_sse(ask(client, session, "error").text)

    assert events[-1]["type"] == "error"
    assert "mock failure" not in events[-1]["message"]

    # the session is usable again afterwards
    assert parse_sse(ask(client, session, "hello").text)[-1]["type"] == "done"


def test_signed_out_service_account_tells_the_admin_what_to_do():
    class SignedOut(MockAgent):
        async def ask(self, text, conversation_id):
            raise LoginRequiredError("no cached account")
            yield  # pragma: no cover

    with make_client(agent=SignedOut(0)) as client:
        events = parse_sse(ask(client, start(client), "hello").text)
    assert "login.py" in events[-1]["message"]


def test_start_failure_is_reported():
    class Down(MockAgent):
        async def start(self):
            raise LoginRequiredError("signed out")

    with make_client(agent=Down(0)) as client:
        response = client.post("/api/chat/start")
    assert response.status_code == 503
    assert "login.py" in response.json()["detail"]


def test_questions_are_not_logged(client, caplog):
    session = start(client)
    with caplog.at_level(logging.DEBUG):
        ask(client, session, "a-very-private-question")
    assert "a-very-private-question" not in caplog.text


def test_swagger_is_off_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_swagger_can_be_enabled_and_keeps_csp_elsewhere():
    with make_client(enable_docs=True) as c:
        docs = c.get("/docs")
        assert docs.status_code == 200
        assert "Content-Security-Policy" not in docs.headers
        assert "/api/chat/message" in c.get("/openapi.json").json()["paths"]
        assert "Content-Security-Policy" in c.get("/").headers


def _basic(password: str, user: str = "anyone") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_password_gate_blocks_pages_and_api_without_the_password():
    with make_client(app_password="s3cret pass") as c:
        for response in (c.get("/"), c.post("/api/chat/start"), c.get("/static/chat.js")):
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"].startswith("Basic")
        assert c.get("/", headers=_basic("wrong")).status_code == 401
        assert c.get("/", headers={"Authorization": "Basic not-base64!"}).status_code == 401


def test_password_gate_lets_the_right_password_through_and_keeps_healthz_open():
    with make_client(app_password="s3cret pass") as c:
        assert c.get("/healthz").status_code == 200
        headers = _basic("s3cret pass")
        assert c.get("/", headers=headers).status_code == 200
        session_id = c.post("/api/chat/start", headers=headers).json()["session_id"]
        reply = c.post("/api/chat/message", json={"session_id": session_id, "text": "hi"}, headers=headers)
        assert [e["type"] for e in parse_sse(reply.text)][-1] == "done"


def test_required_password_must_be_set():
    with pytest.raises(ConfigError):
        create_app(settings=Settings(mock_agent=True, require_app_password=True), agent=MockAgent(0))
