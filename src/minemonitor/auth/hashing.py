"""Password hashing with PBKDF2-HMAC-SHA256 (standard library only).

Stored form: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``. Verification is
constant-time. No third-party crypto dependency ships to site.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)
