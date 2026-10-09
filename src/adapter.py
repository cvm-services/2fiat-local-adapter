"""Local 2fiat adapter — the only component in the 2fiat setup that ever touches
card material.

NOT a service (docs/SCOPE.md section 2): no inbound listener, no Nostr identity,
no announcements. It runs on the operator's own machine, for one payment at a time.

The ORDER of the steps below is the safety property, so it is enforced here and
tested:

    1. shared-context check      (SCOPE.md section 4.4 - refuse a shared box)
    2. argument validation       (https checkout URL, decimal cap)
    3. local one-attempt ledger  (a second local attempt refuses, durably)
    4. balance read              (safe: no card values)
    5. card details              (GATED: one-shot OTP, human step, bounded window)
    6. browser fill              (dedicated profile, DOM only, no clipboard)
    7. STOP at 3DS               (human completes it; "human_required" is honest)
    8. receipt                   (order id, amount, result - no card values)

What is NOT built here is stated plainly rather than faked: the browser step needs
the operator's machine, and the exact 2fiat details surface (SCOPE.md section 7)
is still only partially probed, so unrecognised responses are refused instead of
parsed optimistically.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

from src.browser import CheckoutDriver, DriveResult, PlaywrightDriver
from src.carddata import (
    CardDataError,
    CardDetails,
    DetailsClient,
    OneShotOtp,
    fetch_card_details,
)
from src.session import Session, SessionUnavailable, assert_no_secrets_in_process, load_session

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("TWOFIAT_ADAPTER_CONFIG", "~/.config/2fiat-adapter/config.json")
).expanduser()


# ---------------------------------------------------------------------------
# refusals: every one names a step and a reason; none is silent
# ---------------------------------------------------------------------------


class Refused(Exception):
    """The adapter refuses rather than pretending. Carries a machine reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class SharedContextRefused(Refused):
    """Invariant 4: a context this program cannot control is not a place for a PAN."""


class AlreadyAttempted(Refused):
    """One checkout gets one local attempt, even before the rail's own claim."""


# ---------------------------------------------------------------------------
# values reported back (never card material)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Balance:
    currency: str
    amount: str
    as_of: str

    def to_mapping(self) -> dict[str, str]:
        return {"currency": self.currency, "amount": self.amount, "as_of": self.as_of}


@dataclass(frozen=True)
class Receipt:
    order_id: str
    amount: str
    status: str
    at: str

    def to_mapping(self) -> dict[str, str]:
        return {"order_id": self.order_id, "amount": self.amount, "status": self.status, "at": self.at}


@dataclass(frozen=True)
class CheckoutResult:
    status: str  # "human_required" | "completed" | "failed"
    order_id: str
    human_step: str = ""
    receipt: Receipt | None = None


# ---------------------------------------------------------------------------
# the local one-attempt ledger (defence in depth, not the rail's gate)
# ---------------------------------------------------------------------------


@dataclass
class AttemptLedger:
    """Durable, 0600, one claim per checkout target.

    The rail's durable intent store is the authority on "one sats payment -> one
    fiat attempt" (cvm-service-kit, card t_63be63f1). This is the LOCAL net: it
    stops a second invocation on this machine - a re-run, a cron, an impatient
    operator - from opening a second checkout before anything is typed.
    """

    path: Path

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError):
            raise Refused(
                "ledger_unreadable",
                f"{self.path} exists but cannot be read: refusing to run without the "
                "attempt record (a lost ledger is a lost safety net)",
            ) from None
        return data if isinstance(data, dict) else {}

    def _write(self, data: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(dict(data), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def claim(self, key: str) -> None:
        """Claim the attempt BEFORE any work. Raises if this target was attempted."""
        data = self._read()
        prior = data.get(key)
        if prior:
            raise AlreadyAttempted(
                "already_attempted",
                f"this checkout was already attempted here at {prior.get('claimed_at', '?')} "
                f"(status {prior.get('status') or 'in flight'}): a second attempt is refused. "
                "Resolve it with the operator/rail rather than re-running.",
            )
        data[key] = {"claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": ""}
        self._write(data)

    def record(self, key: str, *, status: str, order_id: str) -> None:
        data = self._read()
        entry = data.get(key) or {}
        entry.update({"status": status, "order_id": order_id})
        data[key] = entry
        self._write(data)


# ---------------------------------------------------------------------------
# the 2fiat client side (stdlib only; paths come from the 0600 config)
# ---------------------------------------------------------------------------


class BalanceClient(Protocol):
    def fetch(self, session: Session) -> Mapping[str, object]:
        ...


def _http_json(
    url: str,
    session: Session,
    *,
    payload: Mapping[str, object] | None,
    timeout: float,
) -> Mapping[str, object]:
    """One request. The response body is returned, never logged, never echoed."""
    body = None if payload is None else json.dumps(dict(payload)).encode()
    req = urlrequest.Request(url, data=body, method="GET" if body is None else "POST")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for header, value in session.header().items():
        req.add_header(header, value)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured https
            raw = resp.read()
    except urlerror.HTTPError as exc:
        raise Refused(
            "upstream_error",
            f"{url} answered {exc.code}: refusing to continue (the session may be "
            "expired or the surface changed). No payload was read.",
        ) from None
    except (urlerror.URLError, OSError) as exc:
        raise Refused("upstream_unreachable", f"{url} is unreachable ({type(exc).__name__})") from None
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        raise Refused(
            "upstream_not_json",
            f"{url} did not answer JSON: refusing rather than scraping an unknown page",
        ) from None
    if not isinstance(parsed, dict):
        raise Refused("upstream_not_json", f"{url} answered a non-object JSON body")
    return parsed


@dataclass
class HttpBalanceClient:
    url: str
    timeout: float = 15.0

    def fetch(self, session: Session) -> Mapping[str, object]:
        return _http_json(self.url, session, payload=None, timeout=self.timeout)


@dataclass
class HttpDetailsClient:
    """The details surface. Paths are config, because section 7's probe is partial."""

    request_url: str
    submit_url: str
    timeout: float = 15.0

    def request_details(self, session: Session) -> Mapping[str, object]:
        return _http_json(self.request_url, session, payload={}, timeout=self.timeout)

    def submit_otp(self, session: Session, otp: str) -> Mapping[str, object]:
        # The code travels in the request body over the operator-configured https
        # endpoint: not argv, not the environment, not the clipboard.
        return _http_json(self.submit_url, session, payload={"code": otp}, timeout=self.timeout)


# ---------------------------------------------------------------------------
# the adapter itself
# ---------------------------------------------------------------------------


def stdin_otp_provider() -> str:
    """The human step: typed at the prompt, un-echoed, never on the command line."""
    return getpass.getpass("2fiat one-time code from your email (not echoed): ")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_https_checkout(url: str) -> str:
    target = (url or "").strip()
    if not target.startswith("https://"):
        raise Refused(
            "not_https",
            "the merchant checkout URL must be https:// (a plaintext checkout would "
            "put card values on the wire)",
        )
    return target


def _require_decimal(max_amount: str) -> str:
    value = (max_amount or "").strip()
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise Refused("bad_amount", "the cap must be a decimal amount, e.g. 24.90") from None
    if amount <= 0:
        raise Refused("bad_amount", "the cap must be greater than zero")
    return value


#: Environment markers that mean "this box is shared with an agent session".
SHARED_CONTEXT_MARKERS = (
    "HERMES_KANBAN_TASK",
    "HERMES_PROFILE",
    "HERMES_SESSION",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CURSOR_TRACE_ID",
    "CODEX_SANDBOX",
)


@dataclass
class Adapter:
    """The orchestration. Every collaborator is injected, so it is fully testable."""

    session: Session
    balance_client: BalanceClient
    details_client: DetailsClient
    driver: CheckoutDriver
    ledger: AttemptLedger
    otp_provider: Callable[[], str] = stdin_otp_provider
    otp_window_seconds: float = 300.0
    human_timeout_seconds: float = 180.0
    allow_shared_box: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    argv: Sequence[str] = field(default_factory=lambda: list(sys.argv))
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], str] = utcnow_iso

    # -- step 0: is this a context that may handle card values at all? ---------
    def assert_runnable_context(self) -> None:
        if self.allow_shared_box:
            return
        present = [marker for marker in SHARED_CONTEXT_MARKERS if self.env.get(marker)]
        if present:
            raise SharedContextRefused(
                "shared_context",
                "this machine reports an agent/shared session "
                f"({', '.join(present)}): it is not a place to hold card values. Run "
                "this on the operator's own machine, or acknowledge explicitly with "
                "--allow-shared-box (and accept the risk in docs/SCOPE.md section 3).",
            )

    # -- safe read: no card values involved -----------------------------------
    def capabilities(self) -> dict[str, object]:
        return capabilities()

    def balance(self) -> Balance:
        payload = self.balance_client.fetch(self.session)
        amount = str(payload.get("amount") or "")
        if not amount:
            raise Refused(
                "no_balance",
                "the balance endpoint did not report an amount: refusing to guess "
                "whether the card can cover the checkout",
            )
        return Balance(
            currency=str(payload.get("currency") or ""),
            amount=amount,
            as_of=str(payload.get("as_of") or ""),
        )

    # -- the money path -------------------------------------------------------
    def pay_checkout(self, url: str, max_amount: str) -> CheckoutResult:
        self.assert_runnable_context()
        target = _require_https_checkout(url)
        cap = _require_decimal(max_amount)

        key = hashlib.sha256(target.encode()).hexdigest()
        self.ledger.claim(key)  # BEFORE any work: the durable local one-attempt claim

        before = self.balance()
        if Decimal(before.amount) < Decimal(cap):
            self.ledger.record(key, status="failed", order_id="")
            raise Refused(
                "balance_below_cap",
                f"the card holds {before.amount} {before.currency}, the checkout may take "
                f"up to {cap}: refusing to start a checkout that cannot be paid",
            )

        # The human step: an OTP that arrives by email, entered once, in a bounded
        # window. The value is never stored, logged or passed as an argument.
        otp = OneShotOtp(window_seconds=self.otp_window_seconds, now=self.clock)
        details = fetch_card_details(
            self.details_client,
            self.session,
            otp=otp,
            otp_provider=self.otp_provider,
        )

        try:
            result = self.driver.drive(
                url=target,
                card_form=details.as_form(),
                max_amount=cap,
                order_reference=key[:16],
            )
        finally:
            # Dropping the reference is NOT a memory wipe (CPython strings are
            # immutable and may have been copied). The real guarantees are that the
            # values were never persisted, never logged, and never left this
            # process except into the merchant's own form field.
            del details

        receipt = Receipt(order_id=result.order_id, amount=cap, status=result.status, at=self.now())
        self.ledger.record(key, status=result.status, order_id=result.order_id)
        return CheckoutResult(
            status=result.status,
            order_id=result.order_id,
            human_step=result.human_step,
            receipt=receipt,
        )


# ---------------------------------------------------------------------------
# the interface the org-side rail relies on (docs/SCOPE.md section 6)
# ---------------------------------------------------------------------------


def capabilities() -> dict[str, object]:
    """What this adapter honestly is. Must never claim it can authorize a payment."""
    return {
        "card_present": True,
        "can_authorize": False,  # 2fiat is an issuer portal: no authorize endpoint exists
        "needs_human_3ds": True,
        "adapter": "local",
    }


def balance(*, deps: Adapter | None = None) -> Balance:
    return (deps or default_adapter()).balance()


def pay_checkout(url: str, max_amount: str, *, deps: Adapter | None = None) -> CheckoutResult:
    """Fill the merchant checkout and stop at the human 3DS prompt."""
    return (deps or default_adapter()).pay_checkout(url, max_amount)


# ---------------------------------------------------------------------------
# configuration: 0600 file, no secrets in it, no secrets from argv/env
# ---------------------------------------------------------------------------

CONFIG_KEYS = (
    "session_path",
    "balance_url",
    "details_request_url",
    "details_submit_url",
    "profile_dir",
    "ledger_path",
)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, object]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise Refused(
            "no_config",
            f"no adapter config at {p}: this adapter takes no session material from the "
            "environment or the command line, so it refuses to act until the operator "
            "writes one (docs/RUNBOOK.md)",
        )
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise Refused("config_insecure", f"{p} has mode {mode:04o}; it must be 0600")
    try:
        cfg = json.loads(p.read_text())
    except (ValueError, OSError):
        raise Refused("config_unreadable", f"{p} is not readable JSON") from None
    missing = [k for k in CONFIG_KEYS if not cfg.get(k)]
    if missing:
        raise Refused("config_incomplete", f"{p} is missing {', '.join(missing)}")
    return cfg


def default_adapter(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Adapter:
    """Build the real adapter from the 0600 config + the 0600 session file."""
    assert_no_secrets_in_process()
    cfg = load_config(config_path)
    session = load_session(str(cfg["session_path"]))
    return Adapter(
        session=session,
        balance_client=HttpBalanceClient(str(cfg["balance_url"])),
        details_client=HttpDetailsClient(
            str(cfg["details_request_url"]), str(cfg["details_submit_url"])
        ),
        driver=PlaywrightDriver(profile_dir=Path(str(cfg["profile_dir"])).expanduser()),
        ledger=AttemptLedger(Path(str(cfg["ledger_path"])).expanduser()),
        allow_shared_box=bool(cfg.get("allow_shared_box")),
    )


# ---------------------------------------------------------------------------
# CLI: one payment at a time, by the operator
# ---------------------------------------------------------------------------

USAGE = """2fiat local adapter

  python -m src.adapter capabilities
  python -m src.adapter balance
  python -m src.adapter pay-checkout --url https://... --max-amount 24.90 [--allow-shared-box]

Card values are never passed on the command line. The one-time code is typed at
the prompt. The session comes from the 0600 file named in the config.
"""


def main(argv: Sequence[str] | None = None, *, deps: Adapter | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command, rest = args[0], args[1:]

    def flag(name: str) -> bool:
        return name in rest

    def value(name: str) -> str | None:
        if name in rest:
            idx = rest.index(name)
            if idx + 1 < len(rest):
                return rest[idx + 1]
        return None

    try:
        if command == "capabilities":
            print(json.dumps(capabilities(), indent=2))
            return 0
        adapter = deps
        if adapter is None:
            cfg_path = value("--config") or DEFAULT_CONFIG_PATH
            adapter = default_adapter(cfg_path)
            adapter.allow_shared_box = flag("--allow-shared-box")
        if command == "balance":
            print(json.dumps(adapter.balance().to_mapping(), indent=2))
            return 0
        if command == "pay-checkout":
            url, cap = value("--url"), value("--max-amount")
            if not url or not cap:
                print(USAGE)
                return 2
            result = adapter.pay_checkout(url, cap)
            out: dict[str, object] = {"status": result.status, "order_id": result.order_id}
            if result.human_step:
                out["human_step"] = result.human_step
            if result.receipt:
                out["receipt"] = result.receipt.to_mapping()
            print(json.dumps(out, indent=2))
            return 0 if result.status in {"completed", "human_required"} else 1
        print(USAGE)
        return 2
    except Refused as exc:
        print(f"REFUSED {exc.reason}: {exc.detail}", file=sys.stderr)
        return 1
    except SessionUnavailable as exc:
        print(f"REFUSED session: {exc}", file=sys.stderr)
        return 1
    except CardDataError as exc:
        print(f"REFUSED carddata: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
