"""
main.py -- Latch backend.

Flow: search (mocked) -> pick a flight -> POST /api/book.
/api/book runs policy_engine.evaluate() FIRST. Only if approved does it call
PayPal's paypal_client.pay_with_test_card() (real sandbox order + capture,
no browser step). A blocked request never reaches PayPal at all -- the
policy is a firewall in front of the payment tool, not a check the agent
could route around.

Every attempt (approved or blocked) becomes a receipt, so the audit trail
covers both outcomes, not just successful payments.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from policy_engine import DEFAULT_POLICY, Policy, PurchaseRequest, evaluate

app = FastAPI(title="Latch")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# In-memory state (per brief: local state is fine, no DB needed tonight)
# ---------------------------------------------------------------------------

EVENTS: list[dict] = []
RECEIPTS: list[dict] = []


def log_event(kind: str, message: str, data: Optional[dict] = None) -> None:
    EVENTS.append(
        {
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "kind": kind,
            "message": message,
            "data": data or {},
        }
    )


# ---------------------------------------------------------------------------
# Mocked flight search -- deterministic, no external vendor call.
# Deliberately spans: one bookable-as-is option, one that trips the stops
# rule, one that trips both budget and vendor-allowlist at once.
# ---------------------------------------------------------------------------

FLIGHTS = [
    {
        "id": "UA-422",
        "vendor": "united",
        "airline": "United Airlines",
        "amount": 422.00,
        "currency": "USD",
        "stops": 1,
        "description": "SFO round-trip, 1 stop (United)",
    },
    {
        "id": "DL-398",
        "vendor": "delta",
        "airline": "Delta Air Lines",
        "amount": 398.00,
        "currency": "USD",
        "stops": 2,
        "description": "SFO round-trip, 2 stops (Delta)",
    },
    {
        "id": "B6-515",
        "vendor": "jetblue",
        "airline": "JetBlue",
        "amount": 515.00,
        "currency": "USD",
        "stops": 0,
        "description": "SFO round-trip, nonstop (JetBlue)",
    },
]


@app.get("/api/policy")
def get_policy():
    p = DEFAULT_POLICY
    return {
        "budget_max": p.budget_max,
        "approved_categories": sorted(p.approved_categories),
        "vendor_allowlist": sorted(p.vendor_allowlist),
        "human_confirmation_threshold": p.human_confirmation_threshold,
        "max_stops": p.max_stops,
    }


@app.post("/api/search")
def search_flights(request: dict):
    destination = request.get("destination", "San Francisco")
    log_event("search", f"Agent searched flights to {destination}", {"results": len(FLIGHTS)})
    return {"destination": destination, "results": FLIGHTS}


class AgentRequest(BaseModel):
    text: str


@app.post("/api/agent")
def agent_recommend(req: AgentRequest):
    """Nebius-backed NLU: parses the request and recommends one flight.
    Advisory only -- /api/book independently re-checks whatever gets
    booked against policy_engine.py regardless of what's recommended here."""
    log_event("agent_request", f'Agent received: "{req.text}"', {})

    from agent import recommend_flight

    recommendation = recommend_flight(req.text, FLIGHTS)
    log_event(
        "agent_recommend",
        f"Agent recommends {recommendation['flight_id']} — {recommendation['rationale']}",
        recommendation,
    )
    return {"results": FLIGHTS, "recommendation": recommendation}


class BookRequest(BaseModel):
    flight_id: str
    human_confirmed: bool = False


@app.post("/api/book")
def book(req: BookRequest):
    flight = next((f for f in FLIGHTS if f["id"] == req.flight_id), None)
    if flight is None:
        raise HTTPException(status_code=404, detail="unknown flight_id")

    log_event(
        "select",
        f"Agent selected {flight['airline']} — ${flight['amount']:.2f}, {flight['stops']} stop(s)",
        flight,
    )

    purchase = PurchaseRequest(
        amount=flight["amount"],
        currency=flight["currency"],
        category="travel",
        vendor=flight["vendor"],
        description=flight["description"],
        stops=flight["stops"],
        human_confirmed=req.human_confirmed,
    )
    decision = evaluate(purchase)

    if not decision.approved:
        log_event(
            "blocked",
            f"Latch blocked the purchase: {'; '.join(decision.reasons)}",
            {"reasons": decision.reasons},
        )
        receipt = _make_receipt(decision, flight, payment=None)
        RECEIPTS.append(receipt)
        return {"decision": "blocked", "reasons": decision.reasons, "receipt": receipt}

    log_event("approved", "Latch approved the purchase — calling payment tool", {})

    try:
        if not os.environ.get("PAYPAL_CLIENT_ID") or not os.environ.get("PAYPAL_CLIENT_SECRET"):
            raise RuntimeError(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set — payment tool is unavailable"
            )
        from paypal_client import PayPalAuth, PayPalError, PaymentAmountError, pay_with_test_card

        auth = PayPalAuth(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"])
        result = pay_with_test_card(
            auth, amount=flight["amount"], currency=flight["currency"], description=flight["description"]
        )
    except Exception as exc:
        log_event("payment_failed", f"Payment tool rejected the approved purchase: {exc}", {})
        receipt = _make_receipt(decision, flight, payment=None, payment_error=str(exc))
        RECEIPTS.append(receipt)
        raise HTTPException(status_code=502, detail=str(exc))

    log_event(
        "captured",
        f"Payment captured — PayPal order {result['orderId']}, capture {result['captureId']}",
        result,
    )
    receipt = _make_receipt(decision, flight, payment=result)
    RECEIPTS.append(receipt)
    return {"decision": "approved", "receipt": receipt}


class ItineraryRequest(BaseModel):
    destination: str


@app.post("/api/itinerary")
def get_itinerary(req: ItineraryRequest):
    """Deep Research / Linkup track: real-time web search for free/low-cost
    things to do at the destination. Informational only -- never touches
    the policy or payment decision."""
    log_event("itinerary_search", f"Researching things to do in {req.destination}", {})
    try:
        from itinerary import build_itinerary

        result = build_itinerary(req.destination)
    except Exception as exc:
        log_event("itinerary_failed", f"Itinerary search failed: {exc}", {})
        raise HTTPException(status_code=502, detail=str(exc))

    log_event(
        "itinerary_result",
        f"Found {len(result['activities'])} activities for {req.destination}"
        + (f" ({len(result['unconfirmed'])} unconfirmed)" if result["unconfirmed"] else ""),
        result,
    )
    return result


def _make_receipt(decision, flight: dict, payment: Optional[dict], payment_error: Optional[str] = None) -> dict:
    body = {
        "id": decision.id,
        "ts": decision.timestamp,
        "approved": decision.approved,
        "reasons": decision.reasons,
        "flight": flight,
        "payment": payment,
        "payment_error": payment_error,
    }
    # A content hash, not a cryptographic signature or blockchain entry --
    # it lets a viewer confirm the receipt shown wasn't edited after the
    # fact, nothing more. Framed honestly in the UI as "content hash".
    canonical = json.dumps(body, sort_keys=True).encode()
    body["content_hash"] = hashlib.sha256(canonical).hexdigest()
    return body


@app.get("/api/receipts")
def get_receipts():
    return list(reversed(RECEIPTS))


@app.get("/api/events")
def get_events():
    return list(reversed(EVENTS))


@app.get("/api/paypal_trace")
def get_paypal_trace():
    from paypal_client import get_http_trace

    return get_http_trace()
