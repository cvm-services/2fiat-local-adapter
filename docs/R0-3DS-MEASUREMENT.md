# R0 — does 2fiat require a human step to disclose card data?

**Status:** MEASURED — and the measurement overturned the prior claim.
**Date:** 2026-10-15/16
**Method:** Playwright (chromium, browser UA) driving the live wallet, recording the
app's OWN XHR calls. The UI's calls ARE the contract. No spend attempted.
**Tool:** `tools/r0_measure_3ds_stop.py` (this repo — the only component permitted
near the card path per ADR-0013).

## Verdict

```
3DS_STOP: not_required      # for the card-DETAILS stage
spend-path:  UNPROVEN       # separate question, not answered here
```

## Why this file exists

A prior gap analysis asserted: *"unattended card capture is impossible at 2fiat
because of 3DS."* That claim was **never measured** — the `pay` tool returned
`state: "requires_3ds"` **without ever attempting a capture**, so the wall was
asserted, not observed. The operator rejected the inference and asked for it to be
measured. It has now been measured, and **the claim is false for the details stage.**

This also contradicted our own documentation: `docs/SCOPE.md` step 4 states
*"STOP at 3DS (human completes it; the adapter waits, bounded)"* and step 2
requires *"a one-shot OTP, operator-entered"*. Those were **design assumptions**,
not observations. They are corrected below.

## Evidence (4 live runs)

The wallet's own calls, in order:

- `POST /api/v1/auth/exchange` with body `{token, nextPath}` — session
  established; token-in-URL, **no password, no second factor**.
- `GET /api/v1/user` — account metadata.
- `GET /api/v1/cards` — the card LIST; **no challenge**.
- `GET /api/v1/prepaid-cards/details/<cardId>` — the card DETAILS; **no challenge**.
- `GET /api/v1/btc-currencies` plus `GET /api/v1/vccTransactions` and
  `GET /api/v1/livechat/chat-id` — supporting calls.

**No `otp`, `verify`, `challenge`, or `3ds` endpoint was called at any point.**
No SMS step. No email step. No CAPTCHA. Nothing.

The details stage is reached by clicking the card row
(`a:has(.IssuedCard-module__-qWaia__card_number)`), href
`/wallet/card-details/<cardId>`.

### What the details response contains

Shape recorded, values never disclosed:

```
alias, balance, cardBal, cardId, cardNo, cardNumber, cardProduct, cardProductId,
cardTerm, cardType, createdAt, currency, email, expDate, expirationDate,
expireDate, id, lastNegativeBalanceReported, lastNegativeReportAt, metadata,
paymentType, productCode, productId, status, updatedAt, userId
```

- `cardNo` / `cardNumber` — **MASKED** (8 asterisks, 8 visible digits: `5257 **** **** 9166`)
- `expDate` — present, unmasked (`MM/YY`)
- `metadata` includes `requiresKyc`, `topup_fee`, `starting_deposit`

**Correction to our own mid-measurement claim:** I initially reported that the
details call "returned the full PAN." It does **not**. A shape probe (recording
only structure, never values) proved `cardNo` is masked. Recorded here because the
error is exactly the kind R0 exists to prevent.

## What this DOES establish

- There is **no human gate** on wallet auth, card listing, or card-details disclosure.
- The OTP/3DS wall asserted in the prior analysis **does not exist at this stage**.

## What this does NOT establish (do not overreach)

- **The masked PAN means the details call alone cannot complete a merchant
  checkout.** Full PAN and CVV never appeared. The spend path is a separate flow.
- `capabilities()` in `src/adapter.py` reports `can_authorize: False`
  (*"2fiat is an issuer portal: no authorize endpoint exists"*) — suggesting
  authorisation may be server-side, on 2fiat's side, rather than requiring the
  merchant page to receive card values at all.
- **Spend is therefore UNPROVEN.** The transaction log hints at the real failure
  mode: repeated `Ollama Toronto` (−20.00 USD) and `ChutesAI Wilmington`
  (−10.00 USD) **`FAILED`** authorisations across 2026-10-13..15 on a card holding
  **0.38 USD** — consistent with **insufficient funds, not a 3DS stop**. A funded
  card is the actual test.

## Consequence for the plan

Goal (2), automatic store integration, is **less blocked than previously believed**:
the blocker we documented was measured and found absent. R3 must be re-scoped to
prove the **spend** path against a funded card instead of being gated on an
assumed 3DS stop.

## Reproducing

```bash
# token from the environment, or resolved in-process via the fleet vault AppRole
# (never via argv, never a shell variable, never echoed)
python3 tools/r0_measure_3ds_stop.py
# classifier self-test, no network/token needed:
python3 tests/test_r0_classifier.py
```

The classifier refuses to report `not_required` from passive observation alone —
reaching a page without a gate returns `unmeasurable`, because absence of evidence
is not evidence of absence.
