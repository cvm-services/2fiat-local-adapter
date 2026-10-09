# 2fiat local card adapter

PRIVATE. Sibling of the `cvm-services` platform, deliberately **not** in that org.

This is the only component in the 2fiat setup that ever touches card material. It runs
locally, on the operator's own machine, for one payment at a time. It announces nothing,
listens on nothing, and shares nothing.

Scope, threat model and the hard invariants: [docs/SCOPE.md](docs/SCOPE.md).

Status: PARTIALLY BUILT (2026-10-10). Built + tested: session custody (0600, never
argv/env), `capabilities()`, the balance read, the OTP-gated details call, the local
one-attempt ledger, the shared-box refusal and the receipt. WRITTEN BUT NOT EXERCISED:
the browser step (needs the operator's machine and a real checkout). STILL OPEN: the
2fiat details surface (SCOPE.md section 7), and the optional signed summary to the rail.
See docs/RUNBOOK.md section 0 for the honest state table and section 8 for what to do
when something refuses.

The public sibling (`cvm-services/cvm-service-kit`, `services/cvm-2fiat/`) exposes
`rail_info`, `payment.quote`, an owner-only balance read, and (`card.pay_checkout`) the
ONE owner-only, sats-gated path by which this adapter's `pay_checkout` can be reached;
it still never receives a card number.

Do not add a network listener to this repo. Do not add a Nostr identity to this repo.
