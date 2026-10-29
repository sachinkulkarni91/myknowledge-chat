"""Turn raw agent activities into the small events the browser understands.

Pure functions, no network, so they are easy to test.

Events sent to the browser (one JSON object per SSE message):
  {"type": "thinking"}
  {"type": "answer", "markdown": str,
   "citations": [{"position": int | None, "title": str, "excerpt": str}],
   "suggestions": [str]}
  {"type": "done"}      end of the agent's turn
  {"type": "ended"}     the agent closed the whole conversation
  {"type": "error", "message": str}   (added by the API layer, not here)

Citations carry a document name and an excerpt only. The SharePoint URL is
dropped on purpose, because visitors cannot open it.
"""
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlparse

from microsoft_agents.activity import Activity

MAX_EXCERPT_CHARS = 1500

# Copilot Studio appends reference definitions such as  [1]: cite:1 "Leave policy.docx"
# or  [1]: https://tenant.sharepoint.com/... "Leave policy.docx". The citations come
# from the activity's entities instead, so every numbered definition is dropped.
_CITE_DEFINITION = re.compile(r"^[ \t]*\[\d+\]:[ \t]*\S.*$", re.MULTILINE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BARE_URL = re.compile(r"https?://[^\s)<>\]]+")


def _host_blocked(url: str, blocked_hosts: Iterable[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in blocked_hosts)


def clean_markdown(text: str, strip_hosts: Iterable[str] = ()) -> str:
    """Remove citation definitions and links visitors cannot open."""
    hosts = tuple(strip_hosts)
    text = _CITE_DEFINITION.sub("", text or "")

    def keep_label(match: re.Match) -> str:
        return match.group(1) if _host_blocked(match.group(2), hosts) else match.group(0)

    def drop_bare(match: re.Match) -> str:
        return "" if _host_blocked(match.group(0), hosts) else match.group(0)

    if hosts:
        text = _MARKDOWN_LINK.sub(keep_label, text)
        text = _BARE_URL.sub(drop_bare, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def extract_citations(activity: Activity) -> list[dict]:
    """Pull {position, title, excerpt} from the activity's citation entities."""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entity in activity.entities or []:
        try:
            data = entity.model_dump(mode="json", by_alias=True)
        except Exception:  # an entity type we do not recognise
            continue
        for claim in _as_list(data.get("citation")):
            if not isinstance(claim, dict):
                continue
            appearance = claim.get("appearance") or {}
            title = _clean_str(appearance.get("name"))
            excerpt = _clean_str(appearance.get("text")) or _clean_str(appearance.get("abstract"))
            if not title and not excerpt:
                continue
            if len(excerpt) > MAX_EXCERPT_CHARS:
                excerpt = excerpt[:MAX_EXCERPT_CHARS].rstrip() + "…"
            key = (title, excerpt)
            if key in seen:
                continue
            seen.add(key)
            position = claim.get("position")
            found.append(
                {
                    "position": position if isinstance(position, int) else None,
                    "title": title or "Untitled document",
                    "excerpt": excerpt,
                }
            )
    found.sort(key=lambda c: (c["position"] is None, c["position"] or 0))
    return found


def is_final_plan(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    flag = value.get("isFinalPlan", value.get("is_final_plan"))
    return flag is True or (isinstance(flag, str) and flag.lower() == "true")


def _suggestions(activity: Activity) -> list[str]:
    actions = activity.suggested_actions.actions if activity.suggested_actions else []
    labels = [(a.title or (a.value if isinstance(a.value, str) else "")) for a in actions or []]
    return [label.strip() for label in labels if label and label.strip()]


def _kind(activity: Activity) -> str:
    kind = getattr(activity.type, "value", activity.type)
    return str(kind).lower()


def activity_to_events(activity: Activity, strip_hosts: Iterable[str] = ()) -> list[dict]:
    kind = _kind(activity)

    if kind == "typing":
        return [{"type": "thinking"}]

    if kind == "event":
        if is_final_plan(activity.value):
            return [{"type": "done"}]
        return [{"type": "thinking"}]

    if kind == "message":
        markdown = clean_markdown(activity.text or "", strip_hosts)
        suggestions = _suggestions(activity)
        if not markdown and not suggestions:
            return []
        return [
            {
                "type": "answer",
                "markdown": markdown,
                "citations": extract_citations(activity),
                "suggestions": suggestions,
            }
        ]

    if kind == "endofconversation":
        return [{"type": "ended"}]

    return []


async def stream_events(
    activities: AsyncIterator[Activity], strip_hosts: Iterable[str] = ()
) -> AsyncIterator[dict]:
    """Map a whole turn. Guarantees exactly one `done` unless the conversation ended.

    The agent marks the end of a turn with an event whose value has
    isFinalPlan: true. Some turns (for example a yes/no follow-up question) end
    without one, so the end of the stream also counts as done.
    """
    done = False
    ended = False
    async for activity in activities:
        for event in activity_to_events(activity, strip_hosts):
            if event["type"] == "done":
                if done:
                    continue
                done = True
            elif event["type"] == "thinking" and done:
                continue
            elif event["type"] == "ended":
                ended = True
            yield event
    if not done and not ended:
        yield {"type": "done"}
