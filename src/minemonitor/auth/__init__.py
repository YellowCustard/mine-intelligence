"""Authentication and role-based access control.

HTTP Basic against a users table, with a small role hierarchy and per-site
scoping. Passwords are hashed with PBKDF2 from the standard library — no extra
dependency ships to the mine.
"""

from minemonitor.auth.hashing import hash_password, verify_password

__all__ = ["hash_password", "verify_password"]
