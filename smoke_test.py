#!/usr/bin/env python
"""Ask the real agent one question from the command line (run login.py first).

    python smoke_test.py "What is the leave policy?"
    python smoke_test.py "What is the leave policy?" --raw

Prints the events the browser would receive. With --raw it also prints each
activity exactly as the agent sent it, which is the quickest way to check
how citations and progress events look in your tenant.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agent_client import CopilotStudioAgent
from app.config import ConfigError, Settings
from app.mapping import activity_to_events


async def run(question: str, raw: bool) -> int:
    settings = Settings.from_env()
    try:
        settings.require_real_agent_config()
    except ConfigError as exc:
        print(exc)
        return 1

    agent = CopilotStudioAgent(settings)
    started = await agent.start()
    print(f"Conversation started ({len(started.activities)} start activities).")
    for activity in started.activities:
        for event in activity_to_events(activity, settings.strip_link_hosts):
            print("  start:", json.dumps(event, ensure_ascii=False))

    print(f"\nAsking: {question}\n")
    async for activity in agent.ask(question, started.conversation_id):
        if raw:
            dump = activity.model_dump(mode="json", by_alias=True, exclude_none=True)
            print("RAW:", json.dumps(dump, indent=2, ensure_ascii=False)[:6000])
        for event in activity_to_events(activity, settings.strip_link_hosts):
            print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--raw", action="store_true", help="also print raw activities")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.question, args.raw)))
