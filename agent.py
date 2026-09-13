"""
agent.py -- the "agent" half of the demo.

Turns the user's free-text request into structured constraints and picks a
flight from the (mocked) search results, via Nebius Token Factory.

Deliberately isolated from policy_engine.py: this module's output only ever
suggests which flight gets *proposed*. It has no ability to approve a
payment -- that stays pure, deterministic Python in policy_engine.py,
which re-checks the chosen flight from scratch regardless of what the
agent said. An LLM picking a flight is a UX nicety; an LLM approving a
spend would defeat the entire point of Latch.
"""

from __future__ import annotations

import json
import os
from typing import Optional

NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY")
NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
NEBIUS_MODEL = os.environ.get("NEBIUS_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")

_client = None


def _get_client():
    global _client
    if _client is None:
        if not NEBIUS_API_KEY:
            raise RuntimeError("NEBIUS_API_KEY not set")
        from openai import OpenAI

        _client = OpenAI(base_url=NEBIUS_BASE_URL, api_key=NEBIUS_API_KEY)
    return _client


def _extract_json(content: Optional[str]) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


RECOMMEND_SYSTEM_PROMPT = """You are a travel-booking agent. Given the \
user's request and a list of flight options, pick the single option that \
best satisfies the stated budget and stop constraints, and explain why in \
one sentence. If no option satisfies every constraint, pick the closest \
one and say so plainly in the rationale -- never claim an option fits \
when it doesn't.

Respond with ONLY a JSON object, no prose, no markdown fences:
{"flight_id": "<id from the options list>", "rationale": "<one sentence>"}
"""


def recommend_flight(request_text: str, flights: list[dict]) -> dict:
    """Ask the model to pick a flight. Returns {flight_id, rationale};
    falls back to the first option (with an explicit note) if the model
    call fails or returns something unparseable -- the UI still needs a
    default to display, but must not misrepresent it as a real pick."""

    fallback = {
        "flight_id": flights[0]["id"],
        "rationale": "Agent recommendation unavailable — defaulted to the first search result.",
    }
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=NEBIUS_MODEL,
            messages=[
                {"role": "system", "content": RECOMMEND_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({"request": request_text, "options": flights}),
                },
            ],
            max_tokens=200,
            temperature=0,
        )
        data = _extract_json(resp.choices[0].message.content)
        valid_ids = {f["id"] for f in flights}
        if data.get("flight_id") in valid_ids and data.get("rationale"):
            return data
        return fallback
    except Exception as exc:
        fallback["rationale"] = f"Agent call failed ({exc}) — defaulted to the first search result."
        return fallback
