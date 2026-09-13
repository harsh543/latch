"""
paypal_client.py

PayPal Orders v2 (sandbox) client shared by:
  - payments.py          (FastAPI endpoints for the voice platform's Custom API Tool)
  - paypal_mcp_server.py (standalone MCP server, for demoing MCP itself)

Flow (two-step, confirm-then-pay -- see module docstrings in payments.py /
paypal_mcp_server.py for why):
  propose_payment() creates an order and returns the buyer's approval link.
  The buyer must open that link and approve the payment in PayPal's UI --
  there is no way to approve an Orders v2 payment from voice input alone.
  Once approved, capture_payment() finalizes it.

Credentials come from PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET (sandbox app).
PAYPAL_BASE_URL defaults to the sandbox REST API -- do not point this at
api-m.paypal.com without a live app, live credentials, and a deliberate
decision to move real money.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional

import requests

# Raw HTTP trace of every call this module makes to PayPal -- separate from
# (and one layer below) the MCP tool trace in mcp_bridge.py, so the demo UI
# can show "here is the actual PayPal API request/response", not just the
# tool's summarized return value. Request/response headers are never
# recorded (so Authorization: Bearer ... never enters the trace), and
# _redact() strips bearer-usable fields (e.g. the OAuth response's
# access_token) out of logged bodies -- this trace is rendered on an
# unauthenticated local endpoint for the demo UI, so nothing a viewer
# could replay against PayPal belongs in it, sandbox or not.
_http_trace: list[dict] = []
_http_trace_lock = threading.Lock()
_REDACT_KEYS = {"access_token", "security_code", "number"}


def _redact(value):
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _request(method: str, url: str, **kwargs) -> requests.Response:
    started = time.time()
    resp = requests.request(method, url, **kwargs)
    entry = {
        "ts": started,
        "method": method,
        "url": url,
        "requestBody": _redact(kwargs.get("json")),
        "statusCode": resp.status_code,
        "responseBody": _redact(_safe_json(resp)),
        "elapsedMs": round((time.time() - started) * 1000),
    }
    with _http_trace_lock:
        _http_trace.append(entry)
    return resp


def _safe_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def get_http_trace(limit: int = 100) -> list[dict]:
    with _http_trace_lock:
        return _http_trace[-limit:]

PAYPAL_BASE_URL = os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
PAYPAL_CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]
PAYPAL_CLIENT_SECRET = os.environ["PAYPAL_CLIENT_SECRET"]

# Demo safety ceiling -- enforced server-side regardless of what any caller
# (voice platform, MCP client, curl) asks for. Raise only for a deliberate,
# non-demo reason.
MAX_PAYMENT_AMOUNT = float(os.environ.get("MAX_PAYMENT_AMOUNT", "100.00"))

TOKEN_URL = f"{PAYPAL_BASE_URL}/v1/oauth2/token"
ORDERS_URL = f"{PAYPAL_BASE_URL}/v2/checkout/orders"

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP"}


class PayPalError(RuntimeError):
    """Raised when PayPal auth or Orders API calls fail."""


class PaymentAmountError(PayPalError):
    """Raised when a requested amount violates the demo safety ceiling."""


# -------------------------------------------------------------------- auth --

class PayPalAuth:
    """OAuth token manager: fetch once, cache, refresh 5 min before expiry.

    Mirrors SabreAuth in sabre_voice_flight_search.py -- don't request a new
    token per call, PayPal sandbox tokens live for hours.
    """

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            resp = _request(
                "POST",
                TOKEN_URL,
                auth=(self._client_id, self._client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            if not resp.ok:
                raise PayPalError(
                    f"Auth failed ({resp.status_code}): {resp.text[:300]}"
                )
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 3600))
            return self._token


# ------------------------------------------------------------------ orders --

def create_order(
    auth: PayPalAuth,
    amount: float,
    currency: str,
    description: str,
    return_url: str,
    cancel_url: str,
) -> dict:
    """Create a PayPal order and return {orderId, approveUrl, status}.

    The order is not paid yet -- the buyer must open approveUrl and approve
    it before capture_order() will succeed.
    """
    if amount <= 0:
        raise PaymentAmountError("amount must be positive")
    if amount > MAX_PAYMENT_AMOUNT:
        raise PaymentAmountError(
            f"amount {amount:.2f} exceeds the demo ceiling of "
            f"{MAX_PAYMENT_AMOUNT:.2f} {currency}"
        )
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise PayPalError(f"unsupported currency: {currency}")

    resp = _request(
        "POST",
        ORDERS_URL,
        headers={
            "Authorization": f"Bearer {auth.token()}",
            "Content-Type": "application/json",
        },
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            },
        },
        timeout=30,
    )
    if not resp.ok:
        raise PayPalError(f"create_order failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    approve_url = next(
        (link["href"] for link in body.get("links", []) if link["rel"] == "approve"),
        None,
    )
    return {"orderId": body["id"], "status": body["status"], "approveUrl": approve_url}


def get_order(auth: PayPalAuth, order_id: str) -> dict:
    resp = _request(
        "GET",
        f"{ORDERS_URL}/{order_id}",
        headers={"Authorization": f"Bearer {auth.token()}"},
        timeout=30,
    )
    if not resp.ok:
        raise PayPalError(f"get_order failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    return {"orderId": body["id"], "status": body["status"]}


def capture_order(auth: PayPalAuth, order_id: str) -> dict:
    """Capture a previously-approved order. Fails if status != APPROVED."""
    resp = _request(
        "POST",
        f"{ORDERS_URL}/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {auth.token()}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if not resp.ok:
        raise PayPalError(f"capture_order failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    capture = body["purchase_units"][0]["payments"]["captures"][0]
    return {
        "orderId": body["id"],
        "status": body["status"],
        "captureId": capture["id"],
        "amount": capture["amount"]["value"],
        "currency": capture["amount"]["currency_code"],
    }


# PayPal's published sandbox test card -- Luhn-valid, sandbox-only, captures
# synchronously with no 3-D Secure challenge. Never use in a live app; this
# only works because PAYPAL_BASE_URL is the sandbox API.
TEST_CARD = {
    "number": "4111111111111111",
    "expiry": "2030-12",
    "security_code": "123",
    "name": "Sandbox Buyer",
}


def pay_with_test_card(auth: PayPalAuth, amount: float, currency: str, description: str) -> dict:
    """Create AND capture a payment in one call using a card payment_source.

    Unlike create_order()/capture_order() (the redirect-based Checkout flow,
    which needs a human to approve in PayPal's UI), a card payment_source
    captures synchronously server-to-server -- no browser, no login. This is
    what makes it possible for an agent to complete a payment purely by
    calling tools.
    """
    if amount <= 0:
        raise PaymentAmountError("amount must be positive")
    if amount > MAX_PAYMENT_AMOUNT:
        raise PaymentAmountError(
            f"amount {amount:.2f} exceeds the demo ceiling of "
            f"{MAX_PAYMENT_AMOUNT:.2f} {currency}"
        )
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise PayPalError(f"unsupported currency: {currency}")

    resp = _request(
        "POST",
        ORDERS_URL,
        headers={
            "Authorization": f"Bearer {auth.token()}",
            "Content-Type": "application/json",
            # Required whenever payment_source is set on order creation --
            # lets a retried request be recognized as a duplicate instead of
            # double-charging.
            "PayPal-Request-Id": str(uuid.uuid4()),
        },
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
            "payment_source": {"card": TEST_CARD},
        },
        timeout=30,
    )
    if not resp.ok:
        raise PayPalError(f"pay_with_test_card failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    if body["status"] != "COMPLETED":
        raise PayPalError(f"card payment did not complete synchronously: status={body['status']}")
    capture = body["purchase_units"][0]["payments"]["captures"][0]
    return {
        "orderId": body["id"],
        "status": body["status"],
        "captureId": capture["id"],
        "amount": capture["amount"]["value"],
        "currency": capture["amount"]["currency_code"],
    }
