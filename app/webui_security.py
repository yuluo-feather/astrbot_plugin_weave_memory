"""V2.1 WebUI write protection: short-lived CSRF tokens and one-time nonces.

AstrBot 4.29.4 does not provide CSRF tokens or a one-shot confirmation
primitive to plugin pages (the bridge only forwards Bearer/cookie auth), so the
plugin keeps this state itself. Tokens are bound to the Dashboard username and
live in process memory; a restart invalidates them and the page re-fetches.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta

from ..core.errors import AuthorizationDenied
from ..core.ports import Clock

_TOKEN_TTL = timedelta(minutes=5)
_NONCE_TTL = timedelta(minutes=2)


class WebUiSecurity:
    def __init__(self, clock: Clock, *,
                 token_ttl: timedelta = _TOKEN_TTL,
                 nonce_ttl: timedelta = _NONCE_TTL) -> None:
        self._clock = clock
        self._token_ttl = token_ttl
        self._nonce_ttl = nonce_ttl
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self._nonces: dict[str, tuple[str, str, str, datetime]] = {}

    def _now(self) -> datetime:
        return self._clock.now()

    def issue_token(self, username: str) -> dict[str, object]:
        now = self._now()
        token = secrets.token_urlsafe(32)
        expires_at = now + self._token_ttl
        with self._lock:
            self._tokens = {
                key: value for key, value in self._tokens.items()
                if value[1] > now
            }
            self._tokens[token] = (username, expires_at)
        return {
            "csrf_token": token,
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": int(self._token_ttl.total_seconds()),
        }

    def require_token(self, username: str, token: object) -> None:
        if not isinstance(token, str) or not token:
            raise AuthorizationDenied("csrf token is required")
        now = self._now()
        with self._lock:
            entry = self._tokens.get(token)
        if entry is None:
            raise AuthorizationDenied("csrf token is missing or expired")
        owner, expires_at = entry
        if expires_at <= now:
            with self._lock:
                self._tokens.pop(token, None)
            raise AuthorizationDenied("csrf token is missing or expired")
        if owner != username:
            raise AuthorizationDenied("csrf token does not belong to this user")

    def issue_nonce(self, username: str, *, action: str, target: str) -> dict[str, object]:
        now = self._now()
        nonce = secrets.token_urlsafe(24)
        expires_at = now + self._nonce_ttl
        with self._lock:
            self._nonces = {
                key: value for key, value in self._nonces.items()
                if value[3] > now
            }
            self._nonces[nonce] = (username, action, target, expires_at)
        return {
            "nonce": nonce,
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": int(self._nonce_ttl.total_seconds()),
        }

    def consume_nonce(self, username: str, *, action: str, target: str,
                      nonce: object) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise AuthorizationDenied("confirmation nonce is required")
        now = self._now()
        with self._lock:
            entry = self._nonces.get(nonce)
        if entry is None:
            raise AuthorizationDenied("confirmation nonce is invalid or already used")
        owner, expected_action, expected_target, expires_at = entry
        if expires_at <= now:
            with self._lock:
                self._nonces.pop(nonce, None)
            raise AuthorizationDenied("confirmation nonce has expired")
        if owner != username or expected_action != action or expected_target != target:
            # A mismatched attempt must not burn the nonce for its real target.
            raise AuthorizationDenied("confirmation nonce does not match this operation")
        with self._lock:
            if self._nonces.get(nonce) is None:
                raise AuthorizationDenied(
                    "confirmation nonce is invalid or already used",
                )
            self._nonces.pop(nonce, None)
