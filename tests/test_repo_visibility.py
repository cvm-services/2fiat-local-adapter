"""Invariant 7 (2026-10-15): this repo is PRIVATE and must stay private.

Why a test and not a README line: the repo moved into the `cvm-services` org,
whose OTHER repos are public by default. This component reads a PAN/CVV, so
world-readable history is the one outcome that is unrecoverable. A convention
nobody checks is how a repo ends up public during a "make things findable"
sweep.

Gate semantics (see docs/SCOPE.md §4.7):
  * `private == true`                      -> PASS
  * `private == false`                     -> FAIL (the dangerous case)
  * API unreachable / no token / rate limit -> SKIP with a clear marker, exit 0

The SKIP is deliberate: this must not become a flaky build breaker on a machine
without GitHub credentials. It is a guard, not a supply-chain attestation. Run it
in CI where a token IS present, and it fails closed on the only case that matters.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("ADAPTER_REPO", "cvm-services/2fiat-local-adapter")


def _gh_api(path: str) -> dict | None:
    """Fetch via `gh api` (carries the user's auth). None if unavailable."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None
    try:
        out = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def main() -> int:
    data = _gh_api(f"/repos/{REPO}")
    if data is None:
        print(f"SKIP: could not read {REPO} (no token / gh absent / rate limited)")
        return 0

    is_private = data.get("private")
    if is_private is True:
        print(f"PASS: {REPO} is private")
        return 0
    print(
        f"FAIL: {REPO} is NOT private (private={is_private!r}).\n"
        "This repo reads card material. Make it private immediately:\n"
        f"  gh api -X PATCH /repos/{REPO} -F private=true\n"
        "and re-run this test.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
