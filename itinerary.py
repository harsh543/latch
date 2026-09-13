"""
itinerary.py -- Deep Research / Linkup track.

Recommends free/low-cost things to do at the traveler's destination using
real-time web search, not a hardcoded list. Two-step search, per the
track's own entry requirement ("identify missing information, make
another search to find it"):

  1. Structured search for candidate activities, each with a free/paid
     guess and a source.
  2. A targeted follow-up search for the first activity Linkup couldn't
     confirm as free. If that still can't confirm it, it's reported to
     the caller as unconfirmed rather than guessed at.

Entirely separate from policy_engine.py and the payment flow -- this only
ever informs what gets *shown* to the user, never a spending decision.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

LINKUP_API_KEY = os.environ.get("LINKUP_API_KEY")
LINKUP_BASE_URL = "https://api.linkup.so/v1"

ITINERARY_SCHEMA = {
    "type": "object",
    "properties": {
        "destination": {"type": "string"},
        "activities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "is_free": {"type": ["boolean", "null"]},
                    "source_url": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        },
    },
    "required": ["destination", "activities"],
}


def _search(query: str, output_type: str, schema: Optional[dict] = None, depth: str = "standard") -> dict:
    if not LINKUP_API_KEY:
        raise RuntimeError("LINKUP_API_KEY not set")
    body = {"q": query, "depth": depth, "outputType": output_type}
    if schema:
        body["structuredOutputSchema"] = schema
    resp = requests.post(
        f"{LINKUP_BASE_URL}/search",
        headers={"Authorization": f"Bearer {LINKUP_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def build_itinerary(destination: str, max_activities: int = 4) -> dict:
    primary = _search(
        f"free or low-cost things to do in {destination} for a short visit, "
        f"whether each is free, and a source link",
        output_type="structured",
        schema=ITINERARY_SCHEMA,
    )
    activities = (primary.get("activities") or [])[:max_activities]

    unconfirmed: list[str] = []
    for activity in activities:
        if activity.get("is_free") is not None:
            continue
        try:
            followup = _search(
                f'Is "{activity["name"]}" in {destination} free to visit? Answer with a citation.',
                output_type="sourcedAnswer",
            )
            answer = (followup.get("answer") or "").lower()
            sources = followup.get("sources") or []
            if "not free" in answer or "isn't free" in answer or "paid" in answer:
                activity["is_free"] = False
            elif "free" in answer:
                activity["is_free"] = True
                if not activity.get("source_url") and sources:
                    activity["source_url"] = sources[0].get("url", "")
            else:
                unconfirmed.append(activity["name"])
        except Exception:
            unconfirmed.append(activity["name"])
        break  # only follow up on the first unclear item -- keeps this bounded

    return {"destination": destination, "activities": activities, "unconfirmed": unconfirmed}
