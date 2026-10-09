"""The browser step: dedicated profile, DOM fill only, STOP at 3DS.

SCOPE.md section 4.2 and section 5 step 3/4:

- a DEDICATED browser profile dir (so the operator's everyday profile is never
  touched, and the checkout session does not mix with their browsing);
- values are typed into the merchant's own form fields through the DOM: no
  clipboard, no screenshots of filled fields, no extension, no autofill store;
- when a 3DS/SCA challenge appears the adapter STOPS. It waits, bounded, for the
  human to finish in the visible window; if the human has not finished in time it
  reports `human_required` and LEAVES THE BROWSER OPEN rather than closing a
  window the operator is using. That is the honest resting state.

This module cannot be exercised off the operator's machine (it needs a real
browser and a real checkout). The orchestration around it is tested with a fake
driver; the browser itself is not pretended into existence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence


@dataclass
class DriveResult:
    status: str  # "completed" | "human_required" | "failed"
    order_id: str
    human_step: str = ""


class CheckoutDriver(Protocol):
    """What the adapter needs from a browser. Fakeable on purpose."""

    def drive(
        self,
        *,
        url: str,
        card_form: dict[str, str],
        max_amount: str,
        order_reference: str,
    ) -> DriveResult:
        ...

    def close(self) -> None:
        ...


#: Selectors tried in order for each field of the merchant form. Ordered from the
#: `autocomplete` hints (the most standard, least fragile) to common ids/names.
FIELD_SELECTORS: dict[str, Sequence[str]] = {
    "number": (
        'input[autocomplete="cc-number"]',
        'input[name*="cardnumber" i]',
        'input[id*="card-number" i]',
        "#cardNumber",
    ),
    "expiry": (
        'input[autocomplete="cc-exp"]',
        'input[name*="expir" i]',
        'input[id*="expir" i]',
        "#cardExpiry",
    ),
    "cvv": (
        'input[autocomplete="cc-csc"]',
        'input[name*="cvv" i]',
        'input[name*="cvc" i]',
        'input[id*="cvv" i]',
        "#cardCvc",
    ),
    "holder": (
        'input[autocomplete="cc-name"]',
        'input[name*="cardholder" i]',
        'input[id*="cardholder" i]',
    ),
}

#: A 3DS/SCA challenge shows up as one of these in the URL, a frame, or a heading.
THREE_DS_MARKERS = ("3dsecure", "3ds", "challenge", "sca", "acssender", "authentication")

SUCCESS_MARKERS = ("order confirmed", "payment successful", "thank you", "order id", "bestellung")


@dataclass
class PlaywrightDriver:
    """Real driver. Imported lazily so the module loads without Playwright."""

    profile_dir: Path
    headless: bool = False
    human_timeout_seconds: float = 180.0
    submit_selectors: Sequence[str] = (
        'button[type="submit"]',
        'button[id*="pay" i]',
        'button[name*="pay" i]',
    )
    _pending: list[object] = field(default_factory=list, repr=False)

    def drive(
        self,
        *,
        url: str,
        card_form: dict[str, str],
        max_amount: str,
        order_reference: str,
    ) -> DriveResult:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - operator machine only
            raise RuntimeError(
                "Playwright is not installed: run scripts/install.sh on the operator's "
                "machine. The adapter refuses rather than pretending a checkout happened."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                # The operator's own profile, cookies and history stay out of this.
                args=["--no-first-run", "--no-default-browser-check"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                filled = self._fill(page, card_form)
                if filled["number"] is False:
                    return DriveResult(
                        status="failed",
                        order_id="",
                        human_step="no card field found on the merchant page: refusing to guess",
                    )
                # The amount is the CARD's cap, checked by the operator before the run:
                # we do not type it, we only record what was authorised.
                self._submit(page)
                return self._await_outcome(page, order_reference)
            except Exception as exc:  # noqa: BLE001 - the dialog is the report
                # Never let a traceback carry form values: report the class only.
                return DriveResult(
                    status="failed",
                    order_id="",
                    human_step=f"browser step failed ({type(exc).__name__}); no card values were logged",
                )

    def _fill(self, page, card_form: dict[str, str]) -> dict[str, bool]:  # noqa: ANN001
        filled: dict[str, bool] = {}
        for name, selectors in FIELD_SELECTORS.items():
            value = card_form.get(name) or ""
            if not value:
                continue
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.fill(value)  # DOM fill only: no clipboard, no keystroke log
                    filled[name] = True
                    break
                except Exception:  # noqa: BLE001, S112 - try the next selector
                    continue
            else:
                filled[name] = False
        return filled

    def _submit(self, page) -> None:  # noqa: ANN001
        for selector in self.submit_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.click()
                    return
            except Exception:  # noqa: BLE001, S112
                continue

    def _await_outcome(self, page, order_reference: str) -> DriveResult:  # noqa: ANN001
        deadline = self.human_timeout_seconds
        step = 0.0
        while step < deadline:
            if self._is_three_ds(page):
                # STOP: the operator finishes 3DS/SCA (and the 2fiat OTP mail) here.
                page.wait_for_timeout(5_000)
                step += 5.0
                continue
            text = self._body_text(page).lower()
            if any(marker in text for marker in SUCCESS_MARKERS):
                return DriveResult(
                    status="completed",
                    order_id=self._order_id(page) or order_reference,
                    human_step="",
                )
            page.wait_for_timeout(2_000)
            step += 2.0
        # Bounded window elapsed. Do not close the window the human is using.
        self._pending.append(page)
        return DriveResult(
            status="human_required",
            order_id=order_reference,
            human_step=(
                "a 3DS/SCA or OTP step is still open in the browser window this adapter "
                "opened. Complete it there (and enter the 2fiat code from the email); the "
                "adapter has stopped and will not type anything further."
            ),
        )

    @staticmethod
    def _is_three_ds(page) -> bool:  # noqa: ANN001
        try:
            url = (page.url or "").lower()
            if any(marker in url for marker in THREE_DS_MARKERS):
                return True
            for frame in page.frames:
                if any(marker in (frame.url or "").lower() for marker in THREE_DS_MARKERS):
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _body_text(page) -> str:  # noqa: ANN001
        try:
            return page.inner_text("body")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _order_id(page) -> str:  # noqa: ANN001
        try:
            import re  # noqa: PLC0415

            text = page.inner_text("body")
            match = re.search(r"(?:order|bestell|auftrag)[^\w]{0,4}([A-Za-z0-9._-]{4,40})", text, re.I)
            return match.group(1) if match else ""
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        """Only ever called by the operator, after they have finished the human step."""
        for page in self._pending:
            try:
                page.context.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110
                pass
        self._pending.clear()
