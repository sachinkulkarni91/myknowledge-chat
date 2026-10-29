"""Activity -> browser event mapping, using payloads shaped like Copilot Studio's."""
from __future__ import annotations

from microsoft_agents.activity import Activity

from app.mapping import (
    activity_to_events,
    clean_markdown,
    extract_citations,
    is_final_plan,
    stream_events,
)


def activity(**fields) -> Activity:
    return Activity.model_validate({"conversation": {"id": "conv-1"}, **fields})


def citation_entity(*claims) -> dict:
    return {
        "type": "https://schema.org/Message",
        "@type": "Message",
        "@context": "https://schema.org",
        "citation": list(claims),
    }


def claim(position, name, text, url="https://zs.sharepoint.com/sites/kb/doc.docx") -> dict:
    return {
        "@type": "Claim",
        "position": position,
        "appearance": {"@type": "DigitalDocument", "name": name, "text": text, "url": url},
    }


async def agen(items):
    for item in items:
        yield item


# ---- events ---------------------------------------------------------------


def test_progress_event_becomes_thinking():
    act = activity(type="event", name="DynamicPlanStepTriggered", value={"isFinalPlan": False})
    assert activity_to_events(act) == [{"type": "thinking"}]


def test_final_plan_event_becomes_done():
    act = activity(type="event", name="DynamicPlanFinished", value={"isFinalPlan": True})
    assert activity_to_events(act) == [{"type": "done"}]


def test_final_plan_accepts_snake_case_and_string_values():
    assert is_final_plan({"is_final_plan": True})
    assert is_final_plan({"isFinalPlan": "true"})
    assert not is_final_plan({"isFinalPlan": "false"})
    assert not is_final_plan(None)
    assert not is_final_plan("isFinalPlan")


def test_typing_activity_is_thinking():
    assert activity_to_events(activity(type="typing")) == [{"type": "thinking"}]


def test_end_of_conversation_becomes_ended():
    assert activity_to_events(activity(type="endOfConversation")) == [{"type": "ended"}]


def test_unknown_activity_types_are_ignored():
    assert activity_to_events(activity(type="conversationUpdate")) == []


# ---- answers and citations ---------------------------------------------------


def test_answer_carries_markdown_and_citations_without_urls():
    act = activity(
        type="message",
        text="Leave is 20 days [1].",
        entities=[citation_entity(claim(1, "Leave policy.docx", "Employees get 20 days."))],
    )
    [event] = activity_to_events(act)

    assert event["type"] == "answer"
    assert event["markdown"] == "Leave is 20 days [1]."
    assert event["citations"] == [
        {"position": 1, "title": "Leave policy.docx", "excerpt": "Employees get 20 days."}
    ]
    assert "sharepoint" not in str(event).lower()


def test_citation_definitions_are_removed_from_the_text():
    text = 'Answer text [1].\n\n[1]: cite:1 "Leave policy.docx"\n[2]: cite:2 "Other.docx"'
    assert clean_markdown(text) == "Answer text [1]."


def test_url_citation_definitions_are_removed_not_left_as_text():
    text = (
        "Answer text [1].\n\n"
        '[1]: https://zs.sharepoint.com/sites/kb/Webinar.pptx "Immunology Webinar_Q2 2026.pptx"\n'
        '[2]: <https://zs.sharepoint.com/sites/kb/GI.pptx> "PoF - GI Insights V0.5.pptx"'
    )
    assert clean_markdown(text, ["sharepoint.com"]) == "Answer text [1]."


def test_sharepoint_links_lose_the_link_but_keep_the_label():
    text = "See [the policy](https://zs.sharepoint.com/sites/kb/a.docx) and [docs](https://example.com/x)."
    cleaned = clean_markdown(text, ["sharepoint.com"])
    assert cleaned == "See the policy and [docs](https://example.com/x)."


def test_bare_sharepoint_urls_are_removed():
    cleaned = clean_markdown("Open https://zs.sharepoint.com/x/y now", ["sharepoint.com"])
    assert "sharepoint" not in cleaned


def test_link_hosts_match_on_domain_boundary():
    text = "[a](https://notsharepoint.com/x)"
    assert clean_markdown(text, ["sharepoint.com"]) == text


def test_citations_are_sorted_and_duplicates_collapsed():
    act = activity(
        type="message",
        text="x",
        entities=[
            citation_entity(
                claim(2, "B.docx", "second"),
                claim(1, "A.docx", "first"),
                claim(3, "A.docx", "first"),  # same title and excerpt as position 1
            )
        ],
    )
    assert [(c["position"], c["title"]) for c in extract_citations(act)] == [(1, "A.docx"), (2, "B.docx")]


def test_excerpt_falls_back_to_abstract_and_is_trimmed():
    long_text = "word " * 600
    ent = citation_entity(
        {"@type": "Claim", "position": 1, "appearance": {"@type": "DigitalDocument", "name": "A", "abstract": long_text}}
    )
    [c] = extract_citations(activity(type="message", text="x", entities=[ent]))
    assert c["excerpt"].endswith("…")
    assert len(c["excerpt"]) <= 1501


def test_message_without_citations_has_empty_list():
    [event] = activity_to_events(activity(type="message", text="Hello"))
    assert event["citations"] == []


def test_suggested_actions_become_suggestions():
    act = activity(
        type="message",
        text="Would you like me to continue?",
        suggestedActions={
            "actions": [
                {"type": "imBack", "title": "Yes", "value": "Yes"},
                {"type": "imBack", "title": "No", "value": "No"},
            ]
        },
    )
    [event] = activity_to_events(act)
    assert event["suggestions"] == ["Yes", "No"]


def test_empty_message_is_ignored():
    assert activity_to_events(activity(type="message", text="")) == []


# ---- whole turns ---------------------------------------------------------------


async def collect(items):
    return [event async for event in stream_events(agen(items))]


async def test_turn_gets_done_even_without_a_final_plan_event():
    events = await collect([activity(type="message", text="Continue? yes/no")])
    assert [e["type"] for e in events] == ["answer", "done"]


async def test_turn_has_exactly_one_done_and_no_thinking_after_it():
    final = activity(type="event", value={"isFinalPlan": True})
    progress = activity(type="event", value={"isFinalPlan": False})
    events = await collect([progress, activity(type="message", text="hi"), final, progress, final])
    assert [e["type"] for e in events] == ["thinking", "answer", "done"]


async def test_no_done_is_added_after_the_conversation_ends():
    events = await collect([activity(type="endOfConversation")])
    assert [e["type"] for e in events] == ["ended"]
