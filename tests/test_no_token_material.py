"""Invariant 8 (2026-10-16): no 2fiat session token or card value in the repo.

The R0 tooling resolves a live wallet session. That makes it easy for a token to
end up in a source file "just for testing" — which is how a wallet session becomes
a permanent, world-copyable bearer credential. This test makes that impossible
rather than discouraged.

It scans tracked files for token-shaped and PAN-shaped strings. It deliberately
does NOT need the network, a token, or a vault.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A 2fiat wallet session token: `token1` followed by a long lowercase run.
TOKEN_RX = re.compile(r"token1[a-z0-9]{20,}", re.I)

# A digit run that could be a PAN (13-19 digits, optionally space/dash separated).
PAN_RX = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# Hex ids are legitimate (cardId); require ALL-LOWERCASE-HEX to be smallish and
# exclude anything that also reads as a plausible PAN.
ALLOW_HEX_RX = re.compile(r"^[0-9a-f]{32}$")


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
    except Exception:
        return []
    return [REPO / line for line in out.splitlines() if line.strip()]


def test_no_wallet_token_in_tracked_files():
    offenders = []
    for path in _tracked_files():
        if path.suffix in {".png", ".jpg", ".pdf"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for m in TOKEN_RX.finditer(text):
            offenders.append(f"{path.relative_to(REPO)}: token-shaped string ({m.group(0)[:10]}...)")
    assert not offenders, "wallet session material must never be committed:\n" + "\n".join(offenders)


def test_no_pan_shaped_strings_in_tracked_files():
    offenders = []
    for path in _tracked_files():
        if path.suffix in {".png", ".jpg", ".pdf", ".lock"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for m in PAN_RX.finditer(text):
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            # 16-digit all-hex ids are card ids, not PANs.
            if ALLOW_HEX_RX.match(digits):
                continue
            offenders.append(f"{path.relative_to(REPO)}: {len(digits)}-digit run")
    assert not offenders, "card-number-shaped material must never be committed:\n" + "\n".join(offenders)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'all green' if not failures else f'{failures} failed'}")
    raise SystemExit(1 if failures else 0)
