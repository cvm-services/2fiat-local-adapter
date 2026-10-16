# Project (b): 2fiat local card adapter — scope, threat model, build order

**Status:** SCOPED (not built). Operator asked for this on 2026-10-07.
**Location:** `cvm-services/2fiat-local-adapter` — PRIVATE (must stay private; see §1).
**Related:** the rail-only CVM in `cvm-services/cvm-service-kit` (`services/cvm-2fiat/`, card t_3257a720). That service never sees card material; this adapter is the only component that ever does.

## 1. Why PRIVATE (revised 2026-10-15 — moved into the cvm-services org)

**Superseded decision.** This repo was originally created under `felixfelix-bot`
with the rule "keep it outside the cvm-services org entirely", on the reasoning
that the org's repos are public by default and a component that reads a PAN/CVV
must not sit one leak away from world-readable history. On 2026-10-15 the
operator instructed that every relevant repo, including this one, be consolidated
into `cvm-services`. That instruction is followed — **with the original concern
preserved by other means, because it was a real concern:**

1. **The repo is PRIVATE inside the org, and must stay private.** The original
   argument was never "the org is bad", it was "this must never be world-readable".
   Org membership does not change that requirement; the visibility flag does.
   A public-by-default org is exactly why this needs an explicit check rather
   than an assumption — see the invariant test in §4.7.
2. **The operator's original instruction (2026-10-07)** was "(a) now, and (b) as a
   separate private local project (not in the org)" — the *private* and *local*
   halves still hold; only the *location* half was superseded.

This is reversible in one command (`gh repo transfer`) if the operator prefers findability over the boundary.

## 2. What it is, and what it is not

**Is:** a small local program on the operator's own machine that holds the 2fiat session and, when explicitly told to, reads one card's details for one checkout, fills the merchant's payment page in a browser.

**CORRECTION (2026-10-16, see `docs/R0-3DS-MEASUREMENT.md`):** the phrase
"stops at the human 3DS prompt" described a DESIGN ASSUMPTION that has now been
measured. Four live runs found **no OTP, no 3DS, and no challenge of any kind** on
auth, card listing, or card-details disclosure — so there is no human stop to
design around at the details stage. What remains unproven is the **spend** path
(masked PAN means the details call alone cannot complete a checkout), and the
observed failure mode there is insufficient funds, not 3DS. Do not reintroduce
the 3DS assumption without a measurement.

**Is not:** a service. No inbound network listener, no Nostr identity, no announcements, no remote API. Nothing about it is discoverable. It is invoked locally, by the operator, for one payment at a time.

## 3. Threat model — what an attacker gains

| Asset | Who wants it | What it buys them |
|---|---|---|
| 2fiat session (cookie/JWT or magic-link token) | anyone with this repo + vault access | full wallet control: read balance/transactions, order cards, top up, freeze — but NOT spend at a merchant |
| PAN + CVV + expiry | anyone with read access to the machine while it is held | **cash**. A no-KYC, crypto-funded, 3DS-enabled prepaid card in hand is a bearer instrument and an authentication factor at once |
| the emitted OTP | anyone who can read mail, logs, `/proc`, or the clipboard on that machine | one-time access to the card details endpoint |
| the merchant checkout session | anyone who can read the browser profile dir | the ability to complete the order |
| this repo's own history | a future contributor/agent | a template that normalises card handling |

**The three leak channels that actually matter on a shared box** (all three are how a PAN escapes without anyone "sharing" it):
1. **argv** — a card number passed as a command-line argument is visible in `ps aux` and world-readable `/proc/<pid>/cmdline`.
2. **logs/stdout** — one debug print, one exception traceback containing the request body, and the value is in a journal that is then archived.
3. **the clipboard** — X11 clipboard contents are readable by any process of any user on the same display. Copy-paste between password-manager and browser is the standard way this leaks.

Our platform already knows the fourth: a value that reaches an LLM-backed agent's context can end up in a remote inference log (this is why the public repo's `EXPOSE_CARD_DETAILS` design was deleted).

## 4. Hard invariants (each has a test; no exceptions)

1. Card values are **never** passed in argv, never in environment variables, never written to disk, never printed, never logged, never placed on the clipboard.
2. They move in exactly one direction: 2fiat API → process memory → the browser's own form field, via the browser's DOM. Never leaving the machine.
3. The OTP is used once, within a bounded window, and is never logged or persisted.
4. The adapter refuses to run when it detects a shared/agent-readable context it cannot control: it says so and stops rather than proceeding quietly. (Design intent: this is a laptop program, not a box program.)
5. No remote component — including the CVM rail in the org — can cause the adapter to disclose card values. The CVM may only ever learn a boolean and a balance.
6. Session material is held in a vault-backed file (0600), never in the repo, never in a unit file, never in shell history.
7. **This repo is PRIVATE and must stay private** (added 2026-10-15 when it moved
   into the `cvm-services` org, whose repos are public by default). A repo that
   reads a PAN/CVV must never be world-readable. The CI/invariant test asserts
   `private=true` via the API and FAILS the build if it is ever flipped public —
   the check exists because the default assumption is the dangerous one.

## 5. Shape

```
operator ── runs ──▶ 2fiat-adapter (local)
                       ├─ session: vault-backed file, 0600, never argv/env
                       ├─ step 1: fetch balance/status          (safe, no card values)
                       ├─ step 2: fetch card details            (gated: needs a one-shot OTP, operator-entered)
                       ├─ step 3: drive the merchant checkout   (dedicated browser profile, DOM fill only)
                       ├─ step 4: STOP at 3DS                   (human completes it; the adapter waits, bounded)
                       └─ step 5: emit a receipt               (order id, amount, result. NO card values)
                                        │
                                        ▼  (optional, one-way)
                              signed summary ──▶ the CVM rail records the outcome
```

## 6. Interface the org-side rail may rely on (defined here, implemented there as a stub)

- `balance()` -> `{currency, amount, as_of}` — no last-4, no PAN.
- `pay_checkout(url, max_amount)` -> `{status: "human_required" | "completed" | "failed", order_id}` — the rail never receives card values, and "human_required" is the honest resting state whenever 2DS/3DS is pending.
- `capabilities()` -> `{card_present: true, can_authorize: false, needs_human_3ds: true, adapter: "local"}`

## 7. First verification, before any code (cheap, decides the whole design)

Read `~/reports/research/2fiat-surface-2026-10-07.md`, then re-probe **with the operator's own session, on his machine**: does the details endpoint's OTP arrive by email only (human step, unavoidable), or does the API itself surface it after delivery (no human step)? Everything about how much can be automated hinges on that one answer. Probe and record the raw request/response shapes (status codes, field names) — never a card value.

## 8. Build order (each a small card once approved)

1. Repo skeleton + the invariant tests (§4) written RED — they pass trivially only because the code is absent, and must stay green forever after.
2. Session store: vault-backed, 0600, with a test that no value can arrive via argv or env.
3. `balance()` + a `capabilities()` report.
4. The details call behind the OTP gate, with tests 1-3 above as the gate.
5. Browser step with a dedicated profile; DOM fill only; clipboard disabled; headed under xvfb; evidence = a screenshot with the card fields redacted by the app itself.
6. Receipt + the optional signed one-way summary to the rail.
7. A short runbook for the operator: how to run one payment, what to do when the OTP never arrives, how to rotate the session.

## 9. Open questions for the operator

- **Q1 Machine.** This adapter must run somewhere no other agent or user can read its memory, argv or clipboard. That is not CobradorWave as it is configured today (multiple agent sessions share it). Is there a laptop/VM for it, or do you want it here with the caveat stated in the runbook?
- **Q2 Session custody.** Vault (OpenBao) or a local 0600 file? The vault is safer to lose, the file is safer to compromise (no network path).
- **Q3 Scope.** Card details for one checkout at a time (recommended), or also a stored "last used card" convenience?
