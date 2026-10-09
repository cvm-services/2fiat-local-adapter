"""Session custody for the local 2fiat adapter.

Hard invariants (docs/SCOPE.md section 4.1 and 4.6): session material lives in ONE
vault-backed file with mode 0600. It never arrives via argv, never via the
environment, never from the repo, never in a unit file, never in shell history.

The token is wrapped in a type whose repr is redacted, so an unhandled exception,
a `print(session)` or a debugger dump shows `Session(<redacted>)` instead of the
cookie value.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SessionUnavailable(Exception):
    """The session cannot be established. The caller must refuse, never guess."""


class SessionFileInsecure(SessionUnavailable):
    """The session file exists but is not 0600: another user could read it."""


class SecretsInArgvOrEnv(SessionUnavailable):
    """Session material was offered via argv or the environment: refuse loudly."""


#: Environment keys that would put session material in /proc/<pid>/environ.
FORBIDDEN_ENV_KEYS = (
    "TWOFIAT_SESSION",
    "TWOFIAT_TOKEN",
    "TWOFIAT_COOKIE",
    "TWOFIAT_PASSWORD",
    "TWOFIAT_OTP",
)

#: argv flags that would put session material in /proc/<pid>/cmdline.
FORBIDDEN_ARGV_FLAGS = ("--session", "--token", "--cookie", "--password", "--otp")


@dataclass(frozen=True)
class Session:
    """A 2fiat session. Raw values are reachable only through `header()`."""

    token: str
    cookie_name: str = "session"

    def __repr__(self) -> str:  # never leak in a traceback or a debug print
        return "Session(<redacted>)"

    __str__ = __repr__

    def header(self) -> dict[str, str]:
        """The only accessor that yields the raw token, for one request."""
        return {"Cookie": f"{self.cookie_name}={self.token}"}


def assert_no_secrets_in_process(
    *, env: Mapping[str, str] | None = None, argv: Sequence[str] | None = None
) -> None:
    """Refuse if this process was handed session material on a world-readable channel.

    `/proc/<pid>/environ` and `/proc/<pid>/cmdline` are readable by every user on
    a shared box, and both end up in crash reports and shell history.
    """
    env = os.environ if env is None else env
    argv = list(argv if argv is not None else [])
    for key in FORBIDDEN_ENV_KEYS:
        if env.get(key):
            raise SecretsInArgvOrEnv(
                f"{key} is set in the environment: the adapter will not read session "
                "material from /proc/<pid>/environ. Use the 0600 session file."
            )
    joined = " ".join(argv).lower()
    for flag in FORBIDDEN_ARGV_FLAGS:
        if flag in joined:
            raise SecretsInArgvOrEnv(
                f"{flag} was passed on the command line: argv is world-readable. "
                "Enter secrets at the prompt or use the 0600 session file."
            )


def _require_0600(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SessionFileInsecure(
            f"{path} has mode {mode:04o}; session files must be 0600 "
            "(request another session and re-write the file with 0600)."
        )


def load_session(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> Session:
    """Read the vault-backed session file. Refuses on any insecure channel."""
    assert_no_secrets_in_process(env=env, argv=argv)
    p = Path(path).expanduser()
    if not p.is_file():
        raise SessionUnavailable(
            f"no session file at {p}: run the session bootstrap in docs/RUNBOOK.md "
            "before any payment. Nothing is attempted without a session."
        )
    _require_0600(p)
    try:
        raw = json.loads(p.read_text())
    except (ValueError, OSError) as exc:  # never echo the body: it holds the token
        raise SessionUnavailable(f"{p} is not a readable session file ({type(exc).__name__})") from None
    token = str(raw.get("token") or "")
    if not token:
        raise SessionUnavailable(f"{p} carries no token")
    return Session(token=token, cookie_name=str(raw.get("cookie_name") or "session"))


def save_session(path: str | Path, token: str, *, cookie_name: str = "session") -> Path:
    """Write the session file 0600, atomically, and never through a shell."""
    if not token:
        raise SessionUnavailable("refusing to write an empty session")
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"token": token, "cookie_name": cookie_name}, fh)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p
