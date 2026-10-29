"""FastAPI app: a thin, authenticated relay between the browser and the MyKnowledge agent.

  POST /api/chat/start    -> {session_id, greeting}
  POST /api/chat/message  -> Server-Sent Events (see app/mapping.py for the event shapes)
  GET  /healthz
  GET  /docs              -> Swagger UI, only when ENABLE_DOCS=true
  GET  /                  -> the chat page

The browser never sees the service-account token. It only holds a random
session id that this server maps to the agent's conversation id.

Privacy: questions and answers are never written to the logs.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders

from .agent_client import AgentClient, CopilotStudioAgent, MockAgent
from .auth import LoginRequiredError
from .config import ConfigError, Settings
from .mapping import activity_to_events, stream_events
from .sessions import InMemorySessionStore, SessionStore

log = logging.getLogger("myknowledge")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SIGNED_OUT = "The assistant is signed out. Ask the administrator to run login.py again."
UNREACHABLE = "The assistant could not be reached. Try again in a moment."
REJECTED = "The assistant rejected the sign-in. Ask the administrator to check the service account and run login.py again."
FAILED_TURN = "The assistant could not finish that answer. Try asking again."

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class SecurityHeaders:
    """Plain ASGI middleware, so streaming responses are not buffered."""

    def __init__(self, app, docs_path: str | None = None):
        self.app = app
        self.docs_path = docs_path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Swagger UI loads its scripts from a CDN and runs an inline script.
        is_docs = self.docs_path is not None and scope["path"] == self.docs_path

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if is_docs and name == "Content-Security-Policy":
                        continue
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class PasswordGate:
    """HTTP Basic auth with one shared password (any username). /healthz stays open for health checks."""

    OPEN_PATHS = {"/healthz"}

    def __init__(self, app, password: str):
        self.app = app
        self.password = password.encode("utf-8")

    def _authorized(self, header: str) -> bool:
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return False
        _, _, password = decoded.partition(b":")
        return secrets.compare_digest(password, self.password)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        if self._authorized(Headers(scope=scope).get("authorization", "")):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Password required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MyKnowledge", charset="UTF-8"'},
        )
        await response(scope, receive, send)


class MessageIn(BaseModel):
    session_id: str
    text: str


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _looks_like_auth_failure(exc: Exception) -> bool:
    # The SDK raises ClientError("Error sending request: 401") for HTTP failures.
    message = str(exc)
    return "401" in message or "403" in message


def create_app(
    settings: Settings | None = None,
    agent: AgentClient | None = None,
    store: SessionStore | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()
    if resolved.require_app_password and not resolved.app_password:
        raise ConfigError("REQUIRE_APP_PASSWORD is on but APP_PASSWORD is empty. Set APP_PASSWORD.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if agent is not None:
            resolved_agent = agent
        elif resolved.mock_agent:
            resolved_agent = MockAgent(resolved.mock_delay_seconds)
            log.warning("MOCK_AGENT is on: answers are fake. Set MOCK_AGENT=false for the real agent.")
        else:
            resolved.require_real_agent_config()
            resolved_agent = CopilotStudioAgent(resolved)
        app.state.settings = resolved
        app.state.agent = resolved_agent
        app.state.store = store or InMemorySessionStore()
        app.state.busy = set()  # sessions with a turn in progress
        yield

    docs_url = "/docs" if resolved.enable_docs else None
    app = FastAPI(
        title="MyKnowledge chat",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_url else None,
    )
    if resolved.app_password:
        app.add_middleware(PasswordGate, password=resolved.app_password)
    app.add_middleware(SecurityHeaders, docs_path=docs_url)

    @app.get("/healthz")
    async def healthz(request: Request):
        return {"status": "ok", "mock": request.app.state.settings.mock_agent}

    @app.post("/api/chat/start")
    async def chat_start(request: Request):
        state = request.app.state
        try:
            result = await state.agent.start()
        except LoginRequiredError as exc:
            log.error("start failed: %s", exc)
            raise HTTPException(status_code=503, detail=SIGNED_OUT)
        except Exception as exc:
            log.error("start failed: %s", type(exc).__name__, exc_info=True)
            detail = REJECTED if _looks_like_auth_failure(exc) else UNREACHABLE
            raise HTTPException(status_code=502, detail=detail)

        hosts = state.settings.strip_link_hosts
        greeting = "\n\n".join(
            event["markdown"]
            for activity in result.activities
            for event in activity_to_events(activity, hosts)
            if event["type"] == "answer" and event["markdown"]
        )
        session_id = state.store.create(result.conversation_id)
        log.info("session %s started", session_id[:6])
        return {"session_id": session_id, "greeting": greeting or None}

    @app.post("/api/chat/message")
    async def chat_message(body: MessageIn, request: Request):
        state = request.app.state
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="Type a question first.")
        if len(text) > state.settings.max_question_chars:
            raise HTTPException(
                status_code=422,
                detail=f"Keep questions under {state.settings.max_question_chars} characters.",
            )
        conversation_id = state.store.get(body.session_id)
        if conversation_id is None:
            raise HTTPException(status_code=404, detail="This conversation has expired.")
        if body.session_id in state.busy:
            raise HTTPException(status_code=409, detail="Wait for the current answer to finish.")

        state.busy.add(body.session_id)
        short_id = body.session_id[:6]
        hosts = state.settings.strip_link_hosts

        async def events():
            try:
                log.info("session %s: question received (%d chars)", short_id, len(text))
                async for event in stream_events(state.agent.ask(text, conversation_id), hosts):
                    yield sse(event)
            except LoginRequiredError as exc:
                log.error("session %s: %s", short_id, exc)
                yield sse({"type": "error", "message": SIGNED_OUT})
            except Exception as exc:
                log.error("session %s: turn failed: %s", short_id, type(exc).__name__, exc_info=True)
                message = REJECTED if _looks_like_auth_failure(exc) else FAILED_TURN
                yield sse({"type": "error", "message": message})
            finally:
                state.busy.discard(body.session_id)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
