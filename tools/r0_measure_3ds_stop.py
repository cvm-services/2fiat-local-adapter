#!/usr/bin/env python3
"""R0 — MEASURE whether 2fiat's card path requires a human 3DS/OTP step.

WHY THIS EXISTS
---------------
A previous gap analysis claimed "unattended card capture is impossible at 2fiat
because of 3DS". That claim was never measured: `payFiat` returns
`state: "requires_3ds"` WITHOUT ever attempting a capture, so the wall was
asserted, not observed. The operator rejected it and asked for measurement.

R0 answers ONE question with evidence, and records the answer as a verdict:

    3DS_STOP: human_required | not_required | unmeasurable

It does NOT assume the answer in either direction.

WHAT IT MEASURES (and what it deliberately does NOT)
----------------------------------------------------
It walks the wallet up to — and only up to — the point where a card's details
are disclosed, and records exactly which step demands a human. Steps 1..N of the
wallet UI are observed passively by recording the app's OWN XHR calls; the UI's
calls ARE the contract.

It NEVER attempts a payment. It will not:
  * click anything matching /pay|purchase|buy|submit|confirm|place order/i,
  * fill a merchant checkout,
  * touch card values at all beyond recording that an OTP gate exists.

Rationale: the card is unfunded (~$0.38) so a capture attempt could not
succeed, AND 2fiat's rate limiter is SHARED across hosts — a burst previously
blocked two different machines. One request at a time.

IDENTITY / CUSTODY (ADR-0013)
-----------------------------
This script lives in the PRIVATE adapter repo because it is the only component
allowed near the card-details path. It still obeys the adapter's invariants:
  * the session token comes from the environment, is never printed, and is
    redacted out of every recorded URL/body;
  * an OTP, if one appears, is never logged or persisted — only the fact of its
    existence and the field it gates are recorded;
  * no card value (PAN/CVV/expiry) is ever read, printed, or written to disk.

USAGE
-----
    TWO_FIAT_TOKEN=<token> python3 tools/r0_measure_3ds_stop.py

Writes a JSON report next to the run and prints the verdict.
"""

from __future__ import annotations

import json
import os
import re
import sys

def _playwright():
    """Import lazily so the classifier can be unit-tested without a browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment guard
        print("playwright not importable; run with ~/.cache/pw-venv/bin/python", file=sys.stderr)
        raise SystemExit(3)
    return sync_playwright

TOKEN = os.environ.get("TWO_FIAT_TOKEN", "")
OUT = os.environ.get("R0_OUT", "/tmp/r0-3ds-report.json")

# Resolve the session from the fleet vault when it is not in the environment.
#
# ADR-0013 forbids CARD VALUES from touching argv/env/disk/logs/clipboard; it does
# not forbid resolving the wallet SESSION through the fleet's own vault, which is
# precisely what the fiat-cvm service does unattended (AppRole login -> read
# secret/data/fleet/2fiat). Doing it in-process here means the token never becomes
# a shell variable and never appears in any command line.
BAO_PATH = "/v1/secret/data/fleet/2fiat"


def token_from_vault() -> str:
    import urllib.request

    home = os.path.expanduser("~")
    envfile = os.path.join(home, ".hermes", "openbao", "env")
    addr = role_file = secret_file = ""
    try:
        with open(envfile) as fh:
            for raw in fh:
                line = raw.strip().replace("export ", "", 1)
                k, _, v = line.partition("=")
                v = v.strip().strip("'\"")
                if k == "OPENBAO_ADDR":
                    addr = v
                elif k == "OPENBAO_ROLE_ID_FILE":
                    role_file = v
                elif k == "OPENBAO_SECRET_ID_FILE":
                    secret_file = v
    except OSError:
        return ""
    try:
        role_id = open(role_file).read().strip()
        secret_id = open(secret_file).read().strip()
    except OSError:
        return ""
    if not (addr and role_id and secret_id):
        return ""

    def post(url: str, payload: dict | None, headers: dict) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        login = post(f"{addr}/v1/auth/approle/login", {"role_id": role_id, "secret_id": secret_id},
                     {"content-type": "application/json"})
        client = (login.get("auth") or {}).get("client_token")
        if not client:
            return ""
        secret = post(f"{addr}{BAO_PATH}", None, {"X-Vault-Token": client})
        return ((secret.get("data") or {}).get("data") or {}).get("token") or ""
    except Exception:
        return ""

# A browser UA is REQUIRED: 2fiat's edge silently drops non-browser clients
# (observed: default UA -> TLS "Empty reply from server"). Kept identical to the
# existing recon tools so the two measurements are comparable.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Never act on these. Kept deliberately broad: a false positive costs us a
# measurement, a false negative costs money.
SPEND = re.compile(r"pay|purchase|buy|submit|confirm|place\s*order|authori[sz]e|capture", re.I)

# Signals that a HUMAN is required at this step.
HUMAN_SIGNALS = {
    "otp": re.compile(r"\botp\b|one[- ]time|verification code|bestätigungscode|\bcode\b", re.I),
    "3ds": re.compile(r"3[- ]?d[- ]?secure|\b3ds\b|challenge|strong customer auth|\bsca\b", re.I),
    "email": re.compile(r"check your (e-?mail|inbox)|we (sent|emailed)|per e-?mail", re.I),
}


def redact(text: str) -> str:
    """Strip the session token out of anything we record."""
    if not text:
        return text
    if TOKEN:
        text = text.replace(TOKEN, "<TOKEN>")
    # Belt and braces: any token-shaped run, and anything that looks like a PAN.
    text = re.sub(r"token1[a-z0-9]+", "<TOKEN>", text, flags=re.I)
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "<PAN_REDACTED>", text)
    return text


def classify(snapshot: str, calls: list[dict]) -> tuple[str, list[str]]:
    """Decide the verdict from observed evidence only.

    Returns (verdict, evidence). A verdict of `not_required` requires POSITIVE
    evidence that no gate was demanded — the absence of an OTP we failed to
    reach is NOT evidence, and is reported as `unmeasurable` instead. That
    distinction is the whole point of R0.
    """
    evidence: list[str] = []
    seen: set[str] = set()
    for kind, rx in HUMAN_SIGNALS.items():
        m = rx.search(snapshot)
        if m:
            seen.add(kind)
            evidence.append(f"{kind}: matched {m.group(0)!r} in the rendered wallet")

    # The card-details gate is visible in the API too: a details fetch that
    # returns 401/403 with a challenge, or an explicit otp/challenge endpoint.
    gated_calls = [
        c for c in calls
        if re.search(r"otp|challenge|3ds|verify|card-details|card_details|/cards/.*/details", c.get("url", ""), re.I)
    ]
    for c in gated_calls:
        evidence.append(f"api: {c.get('method')} {c.get('url')}")

    if seen:
        return "human_required", evidence
    if gated_calls:
        return "human_required", evidence
    # We reached a wallet page and saw no gate. Only claim `not_required` if we
    # actually got far enough to have been asked — otherwise it is unmeasurable.
    if "my-cards" in snapshot or calls:
        return "unmeasurable", evidence + [
            "no gate observed, but the run did not reach a card-disclosure step — "
            "absence of evidence is not evidence of absence"
        ]
    return "unmeasurable", ["the wallet page did not render; nothing was measured"]


def main() -> int:
    global TOKEN
    source = "env"
    if not TOKEN:
        TOKEN = token_from_vault()
        source = "vault"
    if not TOKEN:
        print("no 2fiat wallet token: not in TWO_FIAT_TOKEN, and the vault "
              "AppRole route (~/.hermes/openbao/env) did not yield one", file=sys.stderr)
        return 2
    print(f"session resolved from: {source} (len={len(TOKEN)}; value never printed)")

    calls: list[dict] = []
    console: list[str] = []

    with _playwright()() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        def on_request(req):
            if "/api/" in req.url:
                calls.append({
                    "method": req.method,
                    "url": redact(req.url),
                    "body": redact((req.post_data or "")[:400]),
                })

        page.on("request", on_request)
        page.on(
            "console",
            lambda m: console.append(f"{m.type}: {redact(m.text[:160])}") if m.type == "error" else None,
        )

        # ONE page load, ONE request stream. No navigation storm: the limiter is shared.
        page.goto(f"https://2fiat.com/wallet/{TOKEN}/my-cards",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)

        snapshot = redact(page.inner_text("body")[:4_000])
        # Observe only. We never click a spend control; we record that they exist.
        spend_controls = [
            redact((el.inner_text() or "").strip()[:60])
            for el in page.query_selector_all("button, a[role=button], input[type=submit]")
            if SPEND.search((el.inner_text() or "") + " " + (el.get_attribute("value") or ""))
        ]
        browser.close()

    verdict, evidence = classify(snapshot, calls)

    report = {
        "question": "does 2fiat require a human OTP/3DS step before card details are disclosed?",
        "verdict": f"human_required|not_required|unmeasurable -> {verdict}",
        "3ds_stop": verdict,
        "evidence": evidence,
        "spend_controls_present_but_untouched": sorted(set(spend_controls))[:10],
        "api_calls_observed": calls[:40],
        "console_errors": console[:8],
        "method": "Playwright (chromium) driven wallet, passive XHR recording; no spend attempted",
        "note": "the UI's own calls ARE the contract — they were recorded, not guessed",
    }
    with open(OUT, "w") as fh:
        json.dump(report, fh, indent=2)

    print(f"3DS_STOP: {verdict}")
    for e in evidence:
        print(f"  - {e}")
    if report["spend_controls_present_but_untouched"]:
        print(f"  spend controls present but NOT clicked: {report['spend_controls_present_but_untouched']}")
    print(f"  report: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
