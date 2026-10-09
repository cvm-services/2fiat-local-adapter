"""The invariants from docs/SCOPE.md section 4, as tests.

These are deliberately written BEFORE the implementation: they are the reason the
implementation is allowed to exist. They must never be weakened to make a build pass.
"""

from __future__ import annotations

import subprocess
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src import adapter  # noqa: E402


def test_capabilities_never_claims_it_can_authorize():
    caps = adapter.capabilities()
    assert caps["can_authorize"] is False
    assert caps["needs_human_3ds"] is True


def test_entry_points_refuse_rather_than_pretend(tmp_path, monkeypatch, capsys):
    """With no session/config on this machine, every entry point REFUSES.

    This replaced a test that asserted `NotImplementedYet`: the implementation now
    exists, so the property that must survive is the honest one - an unconfigured
    adapter refuses loudly instead of inventing a balance or a checkout result.
    """
    monkeypatch.setattr(adapter, "DEFAULT_CONFIG_PATH", tmp_path / "nope.json")
    with pytest.raises(adapter.Refused):
        adapter.balance()
    with pytest.raises(adapter.Refused):
        adapter.pay_checkout("https://example.invalid/checkout", "1.00")
    assert adapter.main(["capabilities"]) == 0  # metadata needs no secrets
    assert adapter.main(["balance"]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_no_entry_point_takes_card_material_as_an_argument():
    """argv is world-readable via /proc. Card material must never be a parameter name."""
    forbidden = ("pan", "cvv", "cvc", "card_number", "expiry")
    for fn in (adapter.balance, adapter.pay_checkout, adapter.capabilities):
        params = " ".join(getattr(fn, "__annotations__", {}).keys()).lower()
        assert not any(f in params for f in forbidden), f"{fn.__name__} exposes card material as a parameter"


def test_argv_and_environment_carry_no_card_material():
    """Runnable as a smoke check: this process's own argv/env must be clean."""
    argv = " ".join(sys.argv).lower()
    env = " ".join(f"{k}={v}" for k, v in __import__("os").environ.items()).lower()
    for token in ("pan=", "cvv=", "cvc="):
        assert token not in argv
        assert token not in env


def test_repo_tree_has_no_card_shaped_literals():
    """A 13-19 digit run that looks like a PAN must not be committed, ever."""
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    pan_like = re.compile(r"\b(?:\d[ -]?){13,19}\b")
    for p in root.rglob("*"):
        if not p.is_file() or ".git/" in str(p):
            continue
        if p.suffix in {".png", ".jpg", ".pdf"}:
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        hits = [m.group(0) for m in pan_like.finditer(text)]
        # Allow obvious non-PANs (version strings, long digit runs in test fixtures we control)
        assert not hits or all(set(h.replace(" ", "").replace("-", "")) == {"0"} for h in hits), f"{p}: card-shaped literal {hits}"
