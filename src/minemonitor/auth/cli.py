"""Create or update a user from the command line.

    python -m minemonitor.auth.cli <username> <role> [--site SITE]

The password is read from the MM_NEW_USER_PASSWORD env var, or prompted.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from minemonitor.auth.service import ROLES, create_user
from minemonitor.storage.db import get_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minemonitor.auth.cli")
    parser.add_argument("username")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--site", default=None, help="restrict to a site_id (omit for global)")
    args = parser.parse_args(argv)

    password = os.environ.get("MM_NEW_USER_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 2

    session = get_session_factory()()
    try:
        create_user(
            session, username=args.username, password=password, role=args.role, site_id=args.site
        )
        session.commit()
    finally:
        session.close()
    print(f"User {args.username!r} ({args.role}, site={args.site or 'all'}) created/updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
