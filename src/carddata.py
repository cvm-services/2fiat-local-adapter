"""Card values and the one-shot OTP gate.

SCOPE.md section 4.1/4.2: card values move 2fiat API -> process memory -> the
browser's own form field, via the DOM, and nowhere else. Never argv, never the
environment, never disk, never a log line, never the clipboard, never an LLM.

Section 4.3: the OTP is one-shot, used inside a bounded window, and is never
logged or persisted. This module therefore does NOT store the OTP value at all:
the endpoint validates it, and `OneShotOtp` only tracks that the human step
happened once, in time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol


class CardDataError(Exception):
    """Card values could not be obtained. Refuse; never fabricate a placeholder."""


class OtpRequired(CardDataError):
    """The details endpoint wants an OTP that the human has not supplied yet."""


class OtpWindowExpired(CardDataError):
    """The human step did not happen inside the bounded window."""


class OtpAlreadyUsed(CardDataError):
    """A second OTP use was attempted for one details request."""


class OtpShapeRejected(CardDataError):
    """The entered code is not an OTP shape (never echo the value back)."""


class SurfaceUnknown(CardDataError):
    """The endpoint answered in a shape this adapter does not recognise.

    The 2fiat details surface has been probed only partially (SCOPE.md section 7),
    so an unrecognised response is refused rather than half-parsed. The refusal
    names the step, never the payload.
    """


@dataclass(frozen=True)
class CardDetails:
    """Card values in memory, for one checkout. Raw access is deliberate.

    `as_form()` is the only accessor that yields raw values, so every place that
    touches them is greppable. repr/str are redacted: a traceback or a debug print
    must not contain a PAN.
    """

    number: str
    cvv: str
    expiry: str
    holder: str = ""

    def __repr__(self) -> str:
        return "CardDetails(<redacted>)"

    __str__ = __repr__

    def as_form(self) -> dict[str, str]:
        return {
            "number": self.number,
            "cvv": self.cvv,
            "expiry": self.expiry,
            "holder": self.holder,
        }


def _otp_shaped(value: str) -> bool:
    v = value.strip()
    return v.isdigit() and 4 <= len(v) <= 8


@dataclass
class OneShotOtp:
    """The human step, once, inside a bounded window. Holds no OTP value."""

    window_seconds: float = 300.0
    now: Callable[[], float] = time.monotonic
    _sent_at: float | None = None
    _used_at: float | None = None

    @property
    def sent(self) -> bool:
        return self._sent_at is not None

    @property
    def used(self) -> bool:
        return self._used_at is not None

    def mark_sent(self) -> None:
        """The details request was accepted and an OTP is now with the operator."""
        self._sent_at = self.now()

    def consume(self, entered: str) -> None:
        """Validate the human entry, once, in time. The value is not kept."""
        if self._used_at is not None:
            raise OtpAlreadyUsed("the one-shot code was already used for this request")
        if self._sent_at is None:
            raise OtpRequired("no details request is open: nothing to enter an OTP for")
        if self.now() - self._sent_at > self.window_seconds:
            raise OtpWindowExpired(
                f"the one-shot code expired after {self.window_seconds:.0f}s; "
                "request a new details call rather than reusing it"
            )
        if not _otp_shaped(entered):
            raise OtpShapeRejected("the entered value is not a one-time code")
        self._used_at = self.now()


class DetailsClient(Protocol):
    """The 2fiat details endpoint, as much of it as this adapter needs."""

    def request_details(self, session) -> Mapping[str, object]:  # noqa: ANN001 - Session
        """Ask for card details; the endpoint decides whether an OTP is needed."""
        ...

    def submit_otp(self, session, otp: str) -> Mapping[str, object]:  # noqa: ANN001
        """Present the one-shot code; returns the card fields on success."""
        ...


def _field(payload: Mapping[str, object], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def fetch_card_details(
    client: DetailsClient,
    session,  # noqa: ANN001 - Session, untyped to keep this module import-light
    *,
    otp: OneShotOtp,
    otp_provider: Callable[[], str],
) -> CardDetails:
    """One details call, behind the OTP gate. Returns values in memory only."""
    first = client.request_details(session)
    status = str(first.get("status") or first.get("otp") or "")
    if status in {"otp_sent", "otp_required", "pending_otp"}:
        otp.mark_sent()
        entered = otp_provider()
        otp.consume(entered)
        payload = client.submit_otp(session, entered.strip())
    elif status in {"ok", "success", "delivered"}:
        # The probe (SCOPE.md section 7) has not yet established whether the API can
        # surface details without a human step. When it can, no human step is
        # claimed - the adapter just consumes what it was given.
        payload = first
    else:
        raise SurfaceUnknown(
            "the details endpoint answered with an unrecognised status: refusing "
            "rather than half-parsing a card response"
        )

    number = _field(payload, "number", "card_number", "pan")
    cvv = _field(payload, "cvv", "cvc", "security_code")
    expiry = _field(payload, "expiry", "exp", "expiration", "expiry_date")
    if not (number and cvv and expiry):
        raise SurfaceUnknown(
            "the details response did not carry the expected card fields: refusing "
            "rather than filling a checkout with a partial card"
        )
    return CardDetails(number=number, cvv=cvv, expiry=expiry, holder=_field(payload, "holder", "name"))
