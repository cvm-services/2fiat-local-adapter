"""The money and custody properties of the pay_checkout path, as tests.

Written against fakes so they run anywhere: the real browser step and the real
2fiat surface need the operator's machine (see docs/RUNBOOK.md). What is tested
here is exactly what can be proven off-box - the ORDER of the steps, and the
refusals - which is where the safety properties live.

Fixture card values are all zeros on purpose: tests/test_invariants.py refuses any
card-shaped literal in this tree, and a fixture must never look like a real PAN.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src import adapter, carddata, session as session_mod  # noqa: E402

# An all-zero, non-real PAN: safe to commit (invariants test), useless to a thief.
FIXTURE_PAN = "0000000000000000"
FIXTURE_CVV = "000"
FIXTURE_EXPIRY = "12/29"
CHECKOUT = "https://merchant.example/checkout/abc123"


class FakeBalanceClient:
    def __init__(self, amount: str = "100.00", currency: str = "EUR", payload=None) -> None:
        self.amount, self.currency = amount, currency
        self.payload = payload
        self.calls = 0

    def fetch(self, _session):
        self.calls += 1
        if self.payload is not None:
            return self.payload
        return {"currency": self.currency, "amount": self.amount, "as_of": "2026-10-10T00:00:00+00:00"}


class FakeDetailsClient:
    def __init__(self, status: str = "otp_sent", fields: dict | None = None) -> None:
        self.status = status
        self.fields = fields or {"number": FIXTURE_PAN, "cvv": FIXTURE_CVV, "expiry": FIXTURE_EXPIRY}
        self.requested = 0
        self.submitted: list[str] = []

    def request_details(self, _session):
        self.requested += 1
        return {"status": self.status}

    def submit_otp(self, _session, otp: str):
        self.submitted.append(otp)
        return dict(self.fields)


class FakeDetailsClientNoOtp(FakeDetailsClient):
    def request_details(self, _session):
        self.requested += 1
        return {"status": "ok", **self.fields}


class FakeDriver:
    def __init__(self, status: str = "completed", order_id: str = "venue-order-1", human_step: str = "") -> None:
        self.status, self.order_id, self.human_step = status, order_id, human_step
        self.calls: list[dict] = []
        self.closed = 0

    def drive(self, *, url, card_form, max_amount, order_reference):
        # Recording the form dict here is what proves the values reached the DOM
        # step; nothing outside this process ever sees it.
        self.calls.append(
            {
                "url": url,
                "card_form": dict(card_form),
                "max_amount": max_amount,
                "order_reference": order_reference,
            }
        )
        return browser_result(self)

    def close(self):
        self.closed += 1


def browser_result(driver: FakeDriver):
    from src.browser import DriveResult

    return DriveResult(status=driver.status, order_id=driver.order_id, human_step=driver.human_step)


class FakeOtpProvider:
    def __init__(self, value: str = "123456") -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.value


def make_deps(tmp_path, **overrides):
    """Build an Adapter with every collaborator faked. Returns (deps, parts)."""
    parts = {
        "session": session_mod.Session(token="test-token"),
        "balance_client": overrides.get("balance_client") or FakeBalanceClient(),
        "details_client": overrides.get("details_client") or FakeDetailsClient(),
        "driver": overrides.get("driver") or FakeDriver(),
        "ledger": adapter.AttemptLedger(tmp_path / "ledger.json"),
        "otp_provider": overrides.get("otp_provider") or FakeOtpProvider(),
        "otp_window_seconds": overrides.get("otp_window_seconds", 300.0),
        "allow_shared_box": overrides.get("allow_shared_box", False),
        "env": overrides.get("env", {}),
        "argv": [],
    }
    if "clock" in overrides:
        parts["clock"] = overrides["clock"]
    deps = adapter.Adapter(**parts)
    return deps, parts


# ---------------------------------------------------------------------------
# 1. the happy path: the order of the steps, and what comes out
# ---------------------------------------------------------------------------


def test_completed_checkout_runs_the_steps_in_order_and_returns_a_receipt(tmp_path):
    deps, parts = make_deps(tmp_path)
    result = deps.pay_checkout(CHECKOUT, "24.90")

    assert result.status == "completed"
    assert result.order_id == "venue-order-1"
    assert result.human_step == ""
    assert result.receipt is not None
    assert result.receipt.amount == "24.90"

    # balance read first, then one details call, then the browser step
    assert parts["balance_client"].calls == 1
    assert parts["details_client"].requested == 1
    assert len(parts["driver"].calls) == 1
    call = parts["driver"].calls[0]
    assert call["url"] == CHECKOUT
    assert call["max_amount"] == "24.90"
    # the values reached the form step, and only there
    assert call["card_form"]["number"] == FIXTURE_PAN
    assert call["card_form"]["cvv"] == FIXTURE_CVV


def test_a_human_required_result_is_reported_as_such_not_as_success(tmp_path):
    driver = FakeDriver(status="human_required", order_id="venue-order-1", human_step="3DS is open")
    deps, _ = make_deps(tmp_path, driver=driver)
    result = deps.pay_checkout(CHECKOUT, "24.90")

    assert result.status == "human_required"
    assert "3DS" in result.human_step
    assert result.receipt is not None
    assert result.receipt.status == "human_required"


def test_a_failed_browser_step_is_reported_as_failed(tmp_path):
    deps, _ = make_deps(tmp_path, driver=FakeDriver(status="failed"))
    assert deps.pay_checkout(CHECKOUT, "24.90").status == "failed"


# ---------------------------------------------------------------------------
# 2. never a second attempt, even locally
# ---------------------------------------------------------------------------


def test_a_second_attempt_on_the_same_checkout_refuses_and_never_reopens_the_browser(tmp_path):
    deps, parts = make_deps(tmp_path)
    deps.pay_checkout(CHECKOUT, "24.90")

    # A fresh adapter over the SAME ledger - i.e. a re-run after a restart.
    second, parts2 = make_deps(tmp_path)
    with pytest.raises(adapter.AlreadyAttempted):
        second.pay_checkout(CHECKOUT, "24.90")
    assert parts2["driver"].calls == []
    assert parts2["balance_client"].calls == 0


def test_the_claim_is_written_before_any_work_so_a_crash_is_not_a_free_retry(tmp_path):
    class Boom(Exception):
        pass

    class ExplodingBalance(FakeBalanceClient):
        def fetch(self, session):  # noqa: ANN001
            raise Boom("the balance read died mid-flight")

    deps, _ = make_deps(tmp_path, balance_client=ExplodingBalance())
    with pytest.raises(Boom):
        deps.pay_checkout(CHECKOUT, "24.90")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert len(ledger) == 1  # the claim survived the crash: no silent second try
    with pytest.raises(adapter.AlreadyAttempted):
        make_deps(tmp_path)[0].pay_checkout(CHECKOUT, "24.90")


# ---------------------------------------------------------------------------
# 3. the OTP gate
# ---------------------------------------------------------------------------


def test_the_one_shot_code_is_asked_for_once_and_never_kept(tmp_path):
    provider = FakeOtpProvider("123456")
    details = FakeDetailsClient()
    deps, _ = make_deps(tmp_path, otp_provider=provider, details_client=details)

    deps.pay_checkout(CHECKOUT, "24.90")
    assert provider.calls == 1
    assert details.submitted == ["123456"]


def test_an_expired_window_refuses_instead_of_reusing_the_code(tmp_path):
    provider = FakeOtpProvider("123456")
    clock = {"t": 1000.0}
    deps, _ = make_deps(tmp_path, otp_provider=provider, clock=lambda: clock["t"], otp_window_seconds=30.0)

    # The human step happens well after the code was sent.
    def late() -> str:
        clock["t"] += 120.0
        return "123456"

    deps.otp_provider = late
    with pytest.raises(carddata.OtpWindowExpired):
        deps.pay_checkout(CHECKOUT, "24.90")


def test_a_response_without_the_expected_card_fields_is_refused_not_half_filled(tmp_path):
    details = FakeDetailsClient(fields={"number": FIXTURE_PAN})  # no cvv/expiry
    deps, parts = make_deps(tmp_path, details_client=details)
    with pytest.raises(carddata.SurfaceUnknown):
        deps.pay_checkout(CHECKOUT, "24.90")
    assert parts["driver"].calls == []


def test_the_twofiat_surface_may_answer_without_a_human_step_when_the_probe_says_so(tmp_path):
    provider = FakeOtpProvider()
    deps, parts = make_deps(tmp_path, details_client=FakeDetailsClientNoOtp(), otp_provider=provider)
    deps.pay_checkout(CHECKOUT, "24.90")
    assert provider.calls == 0  # no human step is claimed if the API did not need one
    assert len(parts["driver"].calls) == 1


# ---------------------------------------------------------------------------
# 4. refusals before anything is typed
# ---------------------------------------------------------------------------


def test_a_shared_agent_box_is_refused_unless_acknowledged(tmp_path):
    env = {"HERMES_KANBAN_TASK": "t_63be63f1"}
    deps, parts = make_deps(tmp_path, env=env)
    with pytest.raises(adapter.SharedContextRefused):
        deps.pay_checkout(CHECKOUT, "24.90")
    assert parts["driver"].calls == []

    acknowledged, parts2 = make_deps(tmp_path, env=env, allow_shared_box=True)
    acknowledged.pay_checkout(CHECKOUT, "24.90")
    assert len(parts2["driver"].calls) == 1


def test_a_plaintext_checkout_url_is_refused(tmp_path):
    deps, parts = make_deps(tmp_path)
    with pytest.raises(adapter.Refused) as exc:
        deps.pay_checkout("http://merchant.example/checkout", "24.90")
    assert exc.value.reason == "not_https"
    assert parts["driver"].calls == []


def test_a_balance_below_the_cap_refuses_before_the_browser_opens(tmp_path):
    deps, parts = make_deps(tmp_path, balance_client=FakeBalanceClient(amount="10.00"))
    with pytest.raises(adapter.Refused) as exc:
        deps.pay_checkout(CHECKOUT, "24.90")
    assert exc.value.reason == "balance_below_cap"
    assert parts["driver"].calls == []
    assert parts["details_client"].requested == 0  # no card values were even requested


# ---------------------------------------------------------------------------
# 5. nothing card-shaped leaves the process
# ---------------------------------------------------------------------------


def test_no_card_value_appears_in_stdout_stderr_or_any_repr(tmp_path, capsys):
    provider = FakeOtpProvider()
    details = FakeDetailsClient()
    deps, _ = make_deps(tmp_path, otp_provider=provider, details_client=details)
    deps.pay_checkout(CHECKOUT, "24.90")

    captured = capsys.readouterr()
    for secret in (FIXTURE_PAN, FIXTURE_CVV, FIXTURE_EXPIRY, "123456"):
        assert secret not in captured.out
        assert secret not in captured.err

    # reprs must not leak either: they are what lands in tracebacks and logs
    card = carddata.CardDetails(number=FIXTURE_PAN, cvv=FIXTURE_CVV, expiry=FIXTURE_EXPIRY)
    assert FIXTURE_PAN not in repr(card) and FIXTURE_PAN not in str(card)
    assert FIXTURE_CVV not in repr(card)
    assert "redacted" in repr(card)
    assert FIXTURE_PAN not in repr(deps.session)
    assert "123456" not in repr(deps.session)


# ---------------------------------------------------------------------------
# 6. session custody
# ---------------------------------------------------------------------------


def test_a_world_readable_session_file_is_refused(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"token": "abc"}))
    path.chmod(0o644)
    with pytest.raises(session_mod.SessionFileInsecure):
        session_mod.load_session(path)


def test_a_0600_session_file_is_read_and_its_token_is_redacted(tmp_path):
    path = tmp_path / "session.json"
    session_mod.save_session(path, "super-secret-token")
    assert (path.stat().st_mode & 0o777) == 0o600
    sess = session_mod.load_session(path)
    assert sess.header()["Cookie"].endswith("super-secret-token")
    assert "super-secret-token" not in repr(sess)


def test_session_material_from_the_environment_or_argv_is_refused(tmp_path):
    path = tmp_path / "session.json"
    session_mod.save_session(path, "tok")
    with pytest.raises(session_mod.SecretsInArgvOrEnv):
        session_mod.load_session(path, env={"TWOFIAT_SESSION": "leak"})
    with pytest.raises(session_mod.SecretsInArgvOrEnv):
        session_mod.load_session(path, argv=["--token", "leak"])


def test_a_missing_session_file_is_refused_not_invented(tmp_path):
    with pytest.raises(session_mod.SessionUnavailable):
        session_mod.load_session(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# 7. configuration
# ---------------------------------------------------------------------------


def test_a_config_file_readable_by_others_is_refused(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"session_path": "/x", "balance_url": "https://x", "details_request_url": "https://x", "details_submit_url": "https://x", "profile_dir": "/tmp/p", "ledger_path": "/tmp/l"}))
    cfg.chmod(0o644)
    with pytest.raises(adapter.Refused) as exc:
        adapter.load_config(cfg)
    assert exc.value.reason == "config_insecure"


def test_the_cli_refuses_loudly_when_there_is_no_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(adapter, "DEFAULT_CONFIG_PATH", tmp_path / "nope.json")
    assert adapter.main(["pay-checkout", "--url", CHECKOUT, "--max-amount", "24.90"]) == 1
    assert "REFUSED" in capsys.readouterr().err
