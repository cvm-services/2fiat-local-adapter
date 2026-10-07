# 2fiat local card adapter

PRIVATE. Sibling of the `cvm-services` platform, deliberately **not** in that org.

This is the only component in the 2fiat setup that ever touches card material. It runs
locally, on the operator's own machine, for one payment at a time. It announces nothing,
listens on nothing, and shares nothing.

Scope, threat model and the hard invariants: [docs/SCOPE.md](docs/SCOPE.md).

Status: SCOPED, not built. The public sibling (`cvm-services/cvm-service-kit`,
`services/cvm-2fiat/`) exposes `rail_info`, `payment.quote` and an owner-only balance
read; it never receives a card number.

Do not add a network listener to this repo. Do not add a Nostr identity to this repo.
