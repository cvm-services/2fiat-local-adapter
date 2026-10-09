# Runbook — one payment with the local 2fiat adapter

Operator-facing. Everything here runs on the operator's own machine. Read
[docs/SCOPE.md](SCOPE.md) section 3 first if you are not on a machine you alone control.

## 0. What is built today (be honest with yourself about this)

| Piece | State |
|---|---|
| session custody (0600, never argv/env) | built + tested |
| `capabilities()` / `balance()` | built + tested against fakes |
| one-shot OTP gate on the details call | built + tested |
| local one-attempt ledger (never a second attempt) | built + tested |
| shared-box refusal | built + tested |
| receipt with no card values | built + tested |
| **the real browser step** | **written, NOT exercised anywhere** — needs your machine, a real checkout and a real browser |
| **the real 2fiat details surface** | **unverified** — SCOPE.md section 7 (email-only OTP vs API-surfaced) is still open, so the field names are config and an unrecognised response is refused |
| signed one-way summary to the CVM rail | not built (build order item 6, second half) |

## 1. Install (operator machine, once)

```sh
git clone <this repo> ~/2fiat-adapter && cd ~/2fiat-adapter
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # playwright + pytest
python -m playwright install chromium    # the browser the adapter drives
python -m pytest -q                      # 24 tests must pass before you pay anything
```

## 2. Config (0600, no secrets inside)

`~/.config/2fiat-adapter/config.json`, mode **0600**:

```json
{
  "session_path": "~/.config/2fiat-adapter/session.json",
  "balance_url": "https://<2fiat host>/api/v1/wallet",
  "details_request_url": "https://<2fiat host>/api/v1/card/details/request",
  "details_submit_url": "https://<2fiat host>/api/v1/card/details/confirm",
  "profile_dir": "~/.local/share/2fiat-adapter/chromium-profile",
  "ledger_path": "~/.local/share/2fiat-adapter/attempts.json",
  "allow_shared_box": false
}
```

The three URLs come from the section 7 probe. Until that probe says otherwise they
are **your hypothesis, not a verified fact** — if the shape is wrong the adapter
refuses with `upstream_error` / `upstream_not_json` / `SurfaceUnknown` and touches
nothing.

## 3. Session (0600, never argv, never env)

Get the session cookie from the 2fiat site as you normally would, then write it
**at a prompt** (never on the command line):

```sh
python3 - <<'PY'
from src.session import save_session
save_session("~/.config/2fiat-adapter/session.json", input("2fiat session token (not echoed by this script): "))
PY
chmod 600 ~/.config/2fiat-adapter/session.json
python -m src.adapter balance        # proves the session works, touches no card values
```

If the file mode ever slips to 0644 the adapter refuses (`SessionFileInsecure`) and
will not read it. Setting `TWOFIAT_SESSION`, `TWOFIAT_TOKEN`, `TWOFIAT_COOKIE`,
`TWOFIAT_PASSWORD`, `TWOFIAT_OTP`, or passing `--token`/`--session`/`--otp` refuses
too: those channels are world-readable through `/proc`.

## 4. One payment

```sh
python -m src.adapter pay-checkout --url https://merchant.example/checkout/abc --max-amount 24.90
```

What happens, in this order:

1. shared-context check (refuses if the box is shared with an agent session);
2. `https://` and amount sanity;
3. the **claim** is written to the ledger — before anything else, so a crash is not a
   free retry;
4. balance read; refuses if it is below the cap;
5. the details call; if the surface wants an OTP you get a prompt (`getpass`, not
   argv) — paste the code from the 2fiat email within 5 minutes;
6. a browser window opens on a **dedicated profile** and the form is filled through
   the DOM (no clipboard, ever);
7. at the 3DS/SCA prompt the adapter **stops and waits** (bounded, 3 minutes). Finish
   it yourself in that window — including the second code the card's 3DS sends you;
8. you get a receipt: `order_id`, `amount`, `status`. No card values, ever.

`status: "human_required"` means exactly what it says: the money state is not known to
be settled, and **a human still has to look**. It is never reported as success.

## 5. When the OTP never arrives

- The 5-minute window expires and the run fails. **The ledger keeps the claim**: a
  re-run on the same checkout URL is refused (`AlreadyAttempted`). That is deliberate:
  the merchant may already hold a cart/order for that link.
- Check the 2fiat mailbox and spam folder. If the mail arrives late, let the window
  expire and start a **fresh** details request in a new run.
- If you need to retry the *same* URL: first confirm at the merchant that no order is
  pending, then remove that entry from the 0600 ledger file by hand (it is keyed by the
  sha256 of the URL). Deleting a claim when an order *is* pending is how you pay twice.

## 6. Rotating the session

```sh
python3 - <<'PY'
from src.session import save_session
save_session("~/.config/2fiat-adapter/session.json", input("new session token: "))
PY
```
Rotating the session does not touch the ledger: an attempted checkout stays attempted.

## 7. Shared machines

The adapter refuses when it sees `HERMES_*`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
`CURSOR_TRACE_ID` or `CODEX_SANDBOX` in the environment — that is SCOPE.md invariant 4.
`--allow-shared-box` acknowledges the risk (card values in a process whose memory,
argv and clipboard other sessions can read). Prefer a machine that is yours alone:
SCOPE.md Q1 is still unanswered.

## 8. When something goes wrong

| Symptom | Meaning | Do |
|---|---|---|
| `REFUSED no_config` / `config_insecure` | no 0600 config | section 2 |
| `REFUSED session` | missing/0644/unreadable session file | section 3 |
| `REFUSED shared_context` | agent markers in env | section 7 |
| `REFUSED balance_below_cap` | card cannot cover the checkout | top up the card |
| `REFUSED already_attempted` | this URL was attempted here | section 5 |
| `REFUSED upstream_error` | 2fiat answered non-2xx | session likely expired → section 6 |
| `SurfaceUnknown` | the details response is not the shape this adapter knows | re-probe (§7 of SCOPE), do not guess |
| browser step failed | no card field found / navigation died | the message names the class; no card values were logged |
