"""aeon.provision_admin — one-time admin/operator account bootstrap.

Admin identity on the mothership is ENV-DRIVEN: a user is admin iff their username is listed in
AEON_ADMIN_USERS (see `accounts.is_admin`). No admin bit is stored in the DB. That is deliberate —
grant is entirely operator-controlled config.

The catch: public signup (`accounts.signup`) REFUSES to create any admin username — an
anti-front-running guard so an attacker can't pre-register a handle you're about to make admin — and
it hides that refusal behind the SAME generic "couldn't create the account right now" message it uses
for taken/capped names (anti-enumeration: you must not be able to tell WHY it failed). Net effect: the
legitimate operator cannot self-register their own admin handle through the web form; it just returns a
vague "try again later".

This CLI is the sanctioned way to close that gap. Shell access to the container IS proof of operator,
so it may create the reserved name directly. It prompts for the password via getpass (never on argv, so
it can't leak via `ps` or shell history, and is never logged), hashes it with argon2id EXACTLY as signup
does (`accounts._hash`), and inserts the row. Idempotent: it refuses if the name already exists.

Usage (inside an app container, or anywhere AEON_DB_URL points at the mothership DB):

    python -m aeon.provision_admin AEON-7

Then log in normally at the mothership URL — admin features unlock because `is_admin` matches the handle
against AEON_ADMIN_USERS. The name does NOT strictly have to be in AEON_ADMIN_USERS to be provisioned,
but you'll get a warning if it isn't (you'd be creating an ordinary account, not an admin one).
"""
from __future__ import annotations

import getpass
import secrets
import sys

from . import accounts, db


def provision(username: str) -> int:
    username = (username or "").strip()
    if not accounts.USERNAME_RE.match(username):
        print("username must be 3-24 chars (letters, digits, . _ -)", file=sys.stderr)
        return 2
    if db.get_user_by_username(username):
        print(f"'{username}' already exists — nothing to do (just log in).")
        return 0
    admins = {u.lower() for u in accounts.admin_usernames()}
    if username.lower() not in admins:
        print(f"NOTE: '{username}' is not in AEON_ADMIN_USERS — this creates a NORMAL account, not an "
              f"admin one. Current admins: {sorted(admins) or 'none'}.")
    pw = getpass.getpass(f"Set a password for {username}: ")
    if pw != getpass.getpass("Confirm password: "):
        print("passwords did not match", file=sys.stderr)
        return 1
    if len(pw) < accounts.MIN_PASSWORD:
        print(f"password must be at least {accounts.MIN_PASSWORD} characters", file=sys.stderr)
        return 1
    # cap bypassed on purpose: this is an authenticated operator action, not a public signup.
    outcome = db.create_user_if_capped(
        secrets.token_hex(8), username=username, pw_hash=accounts._hash(pw),
        pw_salt="", signup_ip="admin-bootstrap", cap=10 ** 9)
    if outcome != "ok":
        print(f"could not create account (db returned: {outcome})", file=sys.stderr)
        return 1
    print(f"provisioned '{username}'. Log in at the mothership URL with this password.")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m aeon.provision_admin <username>", file=sys.stderr)
        return 2
    return provision(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
