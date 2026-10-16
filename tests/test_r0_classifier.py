"""R0 self-test: prove the classifier BEFORE pointing it at a live wallet.

A measurement tool that cannot distinguish "no gate exists" from "I never
reached the gate" would reproduce exactly the unmeasured assumption R0 exists to
replace. So the classifier is pinned here against synthetic snapshots.

No network, no token, no card material: this only exercises `classify()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from r0_measure_3ds_stop import classify  # noqa: E402


def test_otp_in_wallet_is_human_required():
    verdict, ev = classify("Enter the OTP we sent to your e-mail to view card details", [])
    assert verdict == "human_required", verdict
    assert ev, "a verdict must cite evidence"


def test_3ds_challenge_is_human_required():
    verdict, _ = classify("3-D Secure: your bank needs to confirm this payment", [])
    assert verdict == "human_required", verdict


def test_gated_api_call_alone_is_human_required():
    # The API's own call names a challenge endpoint even if the UI text is thin.
    calls = [{"method": "POST", "url": "https://2fiat.com/api/v1/cards/abc/details/otp"}]
    verdict, ev = classify("My cards", calls)
    assert verdict == "human_required", verdict
    assert any("api:" in e for e in ev)


def test_no_gate_seen_is_UNMEASURABLE_not_not_required():
    # THE CENTRAL HONESTY PROPERTY. We reached the wallet, saw no gate, and did
    # not get to a card disclosure — that is NOT proof no gate exists.
    verdict, _ = classify("My cards  Balance: 0.38 USD", [{"method": "GET", "url": "/api/v1/cards"}])
    assert verdict == "unmeasurable", verdict


def test_blank_page_is_unmeasurable():
    verdict, _ = classify("", [])
    assert verdict == "unmeasurable", verdict


def test_classify_never_reports_not_required():
    # `not_required` must not be reachable from passive observation at all: only
    # a real (forbidden here) attempt could prove it. If this ever starts
    # failing, someone has loosened the tool into claiming more than it measured.
    for snapshot in ("OTP required", "3-D Secure", "My cards", "", "pay now"):
        verdict, _ = classify(snapshot, [])
        assert verdict in ("human_required", "unmeasurable"), (snapshot, verdict)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'all green' if not failures else f'{failures} failed'}")
    raise SystemExit(1 if failures else 0)
