"""Short-TTL in-process cache of verified credentials.

PBKDF2 is deliberately expensive, and HTTP Basic re-sends credentials on every
request — the SSE ``EventSource`` reconnects and polls continuously — so without
this the platform re-derives 240k rounds many times a second per active viewer.
Only the *fact* of a successful verification is cached, fingerprinted by the
stored password hash so a password change invalidates it. Lockout is still
checked on every request, so a cached hit never bypasses a lock.

The cache is process-local (never shared, never persisted): it holds no secret
beyond an HMAC of the password under a per-process random salt.
"""

from __future__ import annotations

import hmac
import secrets
import time

_SALT = secrets.token_bytes(16)
# username -> (expiry_monotonic, password_token, password_hash_fingerprint)
_cache: dict[str, tuple[float, str, str]] = {}


def _token(password: str) -> str:
    return hmac.new(_SALT, password.encode(), "sha256").hexdigest()


def check(username: str, password: str, password_hash: str, *, now: float | None = None) -> bool:
    """True if this exact credential was verified recently and unchanged since."""
    now = time.monotonic() if now is None else now
    entry = _cache.get(username)
    if entry is None:
        return False
    expiry, token, fingerprint = entry
    if expiry <= now:
        return False
    return fingerprint == password_hash and hmac.compare_digest(token, _token(password))


def store(
    username: str, password: str, password_hash: str, *, ttl_s: int, now: float | None = None
) -> None:
    now = time.monotonic() if now is None else now
    _cache[username] = (now + ttl_s, _token(password), password_hash)


def invalidate(username: str) -> None:
    _cache.pop(username, None)


def clear_all() -> None:
    """Drop every cached verification (used by tests)."""
    _cache.clear()
