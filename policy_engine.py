"""
policy_engine.py

The firewall itself. An agent proposes a purchase; evaluate() checks it
against a machine-readable policy *before* any payment tool is called.
Nothing here talks to PayPal or any vendor -- this module only decides
approve/block and says exactly why, so the decision is auditable on its
own, independent of whether the payment actually executed.

Policy is intentionally a plain dict (not a DB row or a fine-tuned model)
-- the whole pitch is that spending limits for an agent should be a
legible, editable document a human set, not a black box the agent infers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Policy:
    budget_max: float
    approved_categories: set[str]
    vendor_allowlist: set[str]
    human_confirmation_threshold: float  # amounts >= this need human sign-off
    max_stops: Optional[int] = None


DEFAULT_POLICY = Policy(
    budget_max=450.00,
    approved_categories={"travel"},
    vendor_allowlist={"united", "delta", "alaska", "southwest"},
    human_confirmation_threshold=200.00,
    max_stops=1,
)


@dataclass
class PurchaseRequest:
    amount: float
    currency: str
    category: str
    vendor: str
    description: str
    stops: Optional[int] = None
    human_confirmed: bool = False


@dataclass
class PolicyDecision:
    id: str
    approved: bool
    reasons: list[str]
    request: PurchaseRequest
    policy_snapshot: Policy
    timestamp: float = field(default_factory=time.time)


def evaluate(request: PurchaseRequest, policy: Policy = DEFAULT_POLICY) -> PolicyDecision:
    """Check a purchase request against policy. Never raises -- a blocked
    request is a normal, expected result, not an error."""

    reasons: list[str] = []

    if request.amount > policy.budget_max:
        reasons.append(
            f"amount ${request.amount:.2f} exceeds budget cap ${policy.budget_max:.2f}"
        )

    if request.category not in policy.approved_categories:
        reasons.append(
            f"category '{request.category}' is not in approved categories "
            f"{sorted(policy.approved_categories)}"
        )

    if request.vendor.lower() not in policy.vendor_allowlist:
        reasons.append(
            f"vendor '{request.vendor}' is not in the vendor allowlist "
            f"{sorted(policy.vendor_allowlist)}"
        )

    if policy.max_stops is not None and request.stops is not None and request.stops > policy.max_stops:
        reasons.append(
            f"{request.stops} stop(s) exceeds the max of {policy.max_stops}"
        )

    if request.amount >= policy.human_confirmation_threshold and not request.human_confirmed:
        reasons.append(
            f"amount ${request.amount:.2f} is at/above the human-confirmation "
            f"threshold ${policy.human_confirmation_threshold:.2f} and has not been confirmed"
        )

    return PolicyDecision(
        id=str(uuid.uuid4()),
        approved=len(reasons) == 0,
        reasons=reasons,
        request=request,
        policy_snapshot=policy,
    )
