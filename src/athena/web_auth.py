"""Shared session tokens for the dashboard and the remote-audio endpoint.

The dashboard and the remote-audio listener are separate services on the same
host. Browser cookies are scoped to a host, not a port, so a session issued by the
dashboard is also presented to the remote-audio port. Both must validate it the
same way, so the token format lives here exactly once.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time


COOKIE = "athena_session"
SESSION_SECONDS = 24 * 60 * 60
# Sessions issued slightly in the future are tolerated for clock skew.
FUTURE_SKEW_SECONDS = 60
# systemd loads this into athena-web.service only. Other interfaces that need the
# same session (the voice service's remote-audio listener) read it as a fallback
# rather than keeping a second copy of the credentials.
DEFAULT_ENV_FILE = "/etc/athena/web.env"

# Switching the password off is deliberately a named, explicit value rather than
# the absence of one, so that an empty or missing ATHENA_WEB_PASSWORD can never be
# mistaken for it. A typo leaves the password in force, which is the safe way for
# this to fail.
DISABLED_VALUES = {"off", "none", "disabled", "no", "0"}


def auth_disabled() -> bool:
    """Whether the dashboard password has been switched off on purpose."""
    return (os.environ.get("ATHENA_WEB_AUTH", "").strip().casefold()
            in DISABLED_VALUES)


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class SessionAuth:
    def __init__(self, password: str, secret: bytes, required: bool = True) -> None:
        # The length floor exists so a guessable password is caught at start-up
        # rather than shipped. It is skipped only when the password is switched
        # off, and the secret is still enforced either way: it signs the session
        # and the CSRF token, so it is not part of what "no password" gives up.
        if required and len(password) < 10:
            raise ValueError("ATHENA_WEB_PASSWORD must contain at least 10 characters.")
        if len(secret) < 32:
            raise ValueError("ATHENA_WEB_SECRET must contain at least 32 characters.")
        self.password = password
        self.secret = secret
        self.disabled = not required

    @classmethod
    def from_environment(cls) -> "SessionAuth":
        """Build from the environment, falling back to the shared credentials file.

        systemd loads ``/etc/athena/web.env`` into the dashboard only. The voice
        service's remote-audio listener needs the same session, and reading the
        same file is better than keeping a second copy of the password that can
        drift out of sync. Override the location with ``ATHENA_WEB_ENV_FILE``.
        """
        disabled = auth_disabled()
        password = os.environ.get("ATHENA_WEB_PASSWORD", "").strip()
        secret = os.environ.get("ATHENA_WEB_SECRET", "").strip()
        if not password or not secret or disabled:
            stored = read_env_file(os.environ.get("ATHENA_WEB_ENV_FILE", DEFAULT_ENV_FILE))
            password = password or stored.get("ATHENA_WEB_PASSWORD", "")
            secret = secret or stored.get("ATHENA_WEB_SECRET", "")
        if disabled and len(secret) < 32:
            # With no password there is nothing to derive a secret from, and the
            # secret is still needed to sign the CSRF token. A per-process random
            # value is sufficient: it is only ever compared against itself, so the
            # only cost of it changing on restart is that open tabs must reload.
            secret = secrets.token_urlsafe(48)
        return cls(password, secret.encode(), required=not disabled)

    def issue(self) -> str:
        payload = f"{int(time.time())}.{secrets.token_urlsafe(18)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str) -> bool:
        try:
            stamp, nonce, signature = token.split(".", 2)
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(signature, self._sign(f"{stamp}.{nonce}")):
            return False
        try:
            age = time.time() - int(stamp)
        except ValueError:
            return False
        return -FUTURE_SKEW_SECONDS <= age <= SESSION_SECONDS

    def csrf(self, token: str) -> str:
        return hmac.new(self.secret, ("csrf:" + token).encode(), hashlib.sha256).hexdigest()

    def accepts_password(self, supplied: str) -> bool:
        # Compare bytes: compare_digest raises TypeError on a non-ASCII str, which
        # would turn a wrong password into a 500 instead of a clean rejection.
        return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                                   self.password.encode("utf-8", "surrogatepass"))

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
