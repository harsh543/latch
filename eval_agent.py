"""
eval_agent.py -- Applied AI / Nebius track evidence for Latch.

Runs agent.recommend_flight() (the main-flow Nebius Token Factory call)
against 5 representative requests with known-correct answers, and reports
accuracy + latency. One case is deliberately impossible (no flight fits)
to show how the agent behaves when it can't satisfy the request.
"""

from __future__ import annotations

import time

from agent import recommend_flight

FLIGHTS = [
    {"id": "UA-422", "vendor": "united", "airline": "United Airlines", "amount": 422.0, "currency": "USD", "stops": 1, "description": "SFO round-trip, 1 stop (United)"},
    {"id": "DL-398", "vendor": "delta", "airline": "Delta Air Lines", "amount": 398.0, "currency": "USD", "stops": 2, "description": "SFO round-trip, 2 stops (Delta)"},
    {"id": "B6-515", "vendor": "jetblue", "airline": "JetBlue", "amount": 515.0, "currency": "USD", "stops": 0, "description": "SFO round-trip, nonstop (JetBlue)"},
]

CASES = [
    ("Find a flight to SF under $450 with one stop or fewer.", "UA-422"),
    ("I need the cheapest flight regardless of stops.", "DL-398"),
    ("Book a nonstop flight, budget isn't a concern.", "B6-515"),
    ("Find a flight under $400 with at most 1 stop.", None),  # impossible: no flight satisfies both
    ("Any flight under $450 is fine.", "UA-422"),  # DL-398 also qualifies on budget alone; UA-422 is the stronger pick (fewer stops)
]


def main() -> None:
    correct = 0
    latencies = []
    print(f"{'request':<55} {'expected':<10} {'got':<10} {'ms':>7}")
    for request_text, expected in CASES:
        start = time.time()
        result = recommend_flight(request_text, FLIGHTS)
        latency_ms = (time.time() - start) * 1000
        latencies.append(latency_ms)
        got = result["flight_id"]
        is_impossible_case = expected is None
        ok = (got == expected) if not is_impossible_case else True  # graded manually below
        correct += 1 if (ok and not is_impossible_case) else 0
        print(f"{request_text:<55} {str(expected):<10} {got:<10} {latency_ms:7.0f}")
        if is_impossible_case:
            print(f"  -> impossible case, rationale: {result['rationale']}")

    scored = [c for c in CASES if c[1] is not None]
    print(f"\nAccuracy on {len(scored)} answerable cases: {correct}/{len(scored)} ({correct/len(scored):.0%})")
    print(f"Mean latency: {sum(latencies)/len(latencies):.0f}ms")


if __name__ == "__main__":
    main()
