"""
flight_search.py -- live flight search via Linkup, replacing the fixed
3-flight mock list with real, sourced web-search results.

Same technical pattern as itinerary.py (structured Linkup search), applied
to the core booking flow instead of the informational trip-ideas panel.
Honesty note: this is web-search-sourced flight info, not a live fares
API (Amadeus/Duffel/etc.) -- prices and availability are approximate and
should be read as "what's being reported/advertised," not a bookable
quote. That's disclosed in the UI, not hidden.
"""

from __future__ import annotations

import re
from typing import Optional

from itinerary import _search  # reuse the same Linkup client

FLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "flights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "airline": {"type": "string"},
                    "price_usd": {"type": ["number", "null"]},
                    "stops": {"type": ["integer", "null"]},
                    "source_url": {"type": "string"},
                },
                "required": ["airline"],
            },
        },
    },
    "required": ["flights"],
}

# Fallback used when Linkup returns nothing usable -- keeps the demo
# working, but callers can check `source == "fallback"` to know it's not
# live data.
_FALLBACK_FLIGHTS = [
    {"id": "UA-422", "vendor": "united", "airline": "United Airlines", "amount": 422.0, "currency": "USD", "stops": 1, "description": "SFO round-trip, 1 stop (United)", "source": "fallback"},
    {"id": "DL-398", "vendor": "delta", "airline": "Delta Air Lines", "amount": 398.0, "currency": "USD", "stops": 2, "description": "SFO round-trip, 2 stops (Delta)", "source": "fallback"},
    {"id": "B6-515", "vendor": "jetblue", "airline": "JetBlue", "amount": 515.0, "currency": "USD", "stops": 0, "description": "SFO round-trip, nonstop (JetBlue)", "source": "fallback"},
]


def _vendor_from_airline(airline: str) -> str:
    return re.split(r"\s+", airline.strip().lower())[0] if airline else "unknown"


def search_flights(destination: str, max_results: int = 3) -> list[dict]:
    try:
        result = _search(
            f"current flight prices and number of stops flying to {destination} "
            f"next week, from major US airlines (United, Delta, Alaska, Southwest, JetBlue)",
            output_type="structured",
            schema=FLIGHT_SCHEMA,
        )
        flights = result.get("flights") or []
        if not flights:
            return _FALLBACK_FLIGHTS

        out = []
        for f in flights:
            price = f.get("price_usd")
            if price is None:
                continue  # can't run the policy/payment flow without an amount
            airline = f.get("airline") or "Unknown Airline"
            out.append({
                "id": f"LIVE-{len(out)}",
                "vendor": _vendor_from_airline(airline),
                "airline": airline,
                "amount": float(price),
                "currency": "USD",
                "stops": f.get("stops"),
                "description": f"Flight to {destination} ({airline}) -- web-search sourced, not a live quote",
                "source": "linkup",
                "source_url": f.get("source_url", ""),
            })
            if len(out) >= max_results:
                break
        return out or _FALLBACK_FLIGHTS
    except Exception:
        return _FALLBACK_FLIGHTS
