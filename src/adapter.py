"""Local 2fiat adapter — interface stub.

NOT IMPLEMENTED. This file exists so the org-side rail can code against a named
interface, and so the invariant tests have something to bind to.

See docs/SCOPE.md section 4 for the rules this module must satisfy. Nothing here
may accept card material via argv or environment, and nothing here may return it.
"""

from __future__ import annotations

from dataclasses import dataclass


class NotImplementedYet(NotImplementedError):
    """Raised by every entry point until the corresponding step is built."""


@dataclass(frozen=True)
class Balance:
    currency: str
    amount: str
    as_of: str


@dataclass(frozen=True)
class CheckoutResult:
    status: str  # "human_required" | "completed" | "failed"
    order_id: str


def capabilities() -> dict:
    """What this adapter honestly is. Must never claim it can authorize a payment."""
    return {
        "card_present": True,
        "can_authorize": False,  # 2fiat is an issuer portal: no authorize endpoint exists
        "needs_human_3ds": True,
        "adapter": "local",
    }


def balance() -> Balance:
    raise NotImplementedYet("session store + balance read not built yet")


def pay_checkout(url: str, max_amount: str) -> CheckoutResult:
    """Fill the merchant checkout and stop at the human 3DS prompt."""
    raise NotImplementedYet("browser step not built yet")
