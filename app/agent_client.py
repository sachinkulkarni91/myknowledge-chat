"""Talks to the MyKnowledge agent. A real client and a mock one share an interface.

Both yield `Activity` objects exactly as the Microsoft 365 Agents SDK does, so
everything downstream (mapping, API, UI) behaves the same in mock mode.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol

from microsoft_agents.activity import Activity
from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

from . import auth
from .config import Settings


class AgentError(RuntimeError):
    """The agent could not be reached or returned something unusable."""


@dataclass
class StartResult:
    conversation_id: str
    activities: list[Activity]  # usually just the greeting


class AgentClient(Protocol):
    async def start(self) -> StartResult: ...

    def ask(self, text: str, conversation_id: str) -> AsyncIterator[Activity]: ...


class CopilotStudioAgent:
    """Calls the published Copilot Studio agent as the signed-in service account."""

    def __init__(
        self,
        settings: Settings,
        token_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider or (lambda: auth.get_token_async(settings))
        self._client: CopilotClient | None = None
        self._client_token = ""

    async def _get_client(self) -> CopilotClient:
        # CopilotClient is bound to one token. The cached token stays the same
        # until it is refreshed (about hourly), so the client is reused until then.
        token = await self._token_provider()
        if self._client is None or token != self._client_token:
            connection = ConnectionSettings(
                environment_id=self._settings.environment_id,
                agent_identifier=self._settings.agent_schema_name,
            )
            self._client = CopilotClient(connection, token)
            self._client_token = token
        return self._client

    async def start(self) -> StartResult:
        client = await self._get_client()
        activities: list[Activity] = []
        conversation_id = ""
        async for activity in client.start_conversation(True):
            activities.append(activity)
            if activity.conversation and activity.conversation.id:
                conversation_id = activity.conversation.id
        if not conversation_id:
            raise AgentError("The agent did not return a conversation id.")
        return StartResult(conversation_id, activities)

    async def ask(self, text: str, conversation_id: str) -> AsyncIterator[Activity]:
        client = await self._get_client()
        async for activity in client.ask_question(text, conversation_id):
            yield activity


# --------------------------------------------------------------------------- mock

_MOCK_ANSWER = (
    "Here is what the knowledge base says about **{topic}**:\n\n"
    "- Requests are reviewed within five working days [1].\n"
    "- Exceptions need written approval from a manager [2].\n\n"
    "Full policy: [Policy document](https://example.sharepoint.com/sites/kb/policy.docx)\n\n"
    '[1]: cite:1 "Request handling guide.docx"\n'
    '[2]: cite:2 "Exceptions and approvals.docx"'
)

_MOCK_CITATIONS = [
    {
        "@type": "Claim",
        "position": 1,
        "appearance": {
            "@type": "DigitalDocument",
            "name": "Request handling guide.docx",
            "text": "All requests are logged on receipt and reviewed within five working days. "
            "Urgent requests are triaged the same day.",
            "url": "https://example.sharepoint.com/sites/kb/request-handling.docx",
        },
    },
    {
        "@type": "Claim",
        "position": 2,
        "appearance": {
            "@type": "DigitalDocument",
            "name": "Exceptions and approvals.docx",
            "text": "Any exception to the standard process must be approved in writing by the "
            "requester's line manager before work begins.",
            "url": "https://example.sharepoint.com/sites/kb/exceptions.docx",
        },
    },
]


def _activity(conversation_id: str, **fields) -> Activity:
    return Activity.model_validate({"conversation": {"id": conversation_id}, **fields})


def _progress(conversation_id: str, final: bool = False) -> Activity:
    return _activity(
        conversation_id,
        type="event",
        name="DynamicPlanFinished" if final else "DynamicPlanStepTriggered",
        value={"planIdentifier": "mock-plan", "isFinalPlan": final},
    )


class MockAgent:
    """Fake agent for building and testing the UI before sign-in is set up.

    Try these messages:
      anything         -> a cited answer
      "follow up"      -> a yes/no question (then answer "yes" or "no")
      "error"          -> a failed turn
    """

    def __init__(self, delay_seconds: float = 0.5) -> None:
        self._delay = delay_seconds
        self._awaiting_yes_no: set[str] = set()

    async def _pause(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)

    async def start(self) -> StartResult:
        conversation_id = "mock-" + uuid.uuid4().hex[:12]
        greeting = _activity(
            conversation_id,
            type="message",
            text="Hi, I'm the MyKnowledge assistant (running in **mock mode**). Ask me anything.",
        )
        return StartResult(conversation_id, [greeting])

    async def ask(self, text: str, conversation_id: str) -> AsyncIterator[Activity]:
        query = text.strip().lower()
        yield _progress(conversation_id)
        await self._pause()

        if query == "error":
            raise AgentError("mock failure")

        if conversation_id in self._awaiting_yes_no and query in {"yes", "no"}:
            self._awaiting_yes_no.discard(conversation_id)
            reply = "Continuing with the extra detail." if query == "yes" else "Okay, I'll stop here."
            yield _activity(conversation_id, type="message", text=reply)
            yield _progress(conversation_id, final=True)
            return

        if "follow" in query:
            self._awaiting_yes_no.add(conversation_id)
            yield _activity(
                conversation_id,
                type="message",
                text="I found related material in another agent. Would you like me to continue?",
                suggestedActions={
                    "actions": [
                        {"type": "imBack", "title": "Yes", "value": "Yes"},
                        {"type": "imBack", "title": "No", "value": "No"},
                    ]
                },
            )
            return  # no final-plan event: the stream just ends

        yield _progress(conversation_id)
        await self._pause()
        yield _activity(
            conversation_id,
            type="message",
            text=_MOCK_ANSWER.format(topic=text.strip()[:60] or "your question"),
            entities=[
                {
                    "type": "https://schema.org/Message",
                    "@type": "Message",
                    "@context": "https://schema.org",
                    "citation": _MOCK_CITATIONS,
                }
            ],
        )
        yield _progress(conversation_id, final=True)
