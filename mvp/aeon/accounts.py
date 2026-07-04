"""Anonymous evaluator accounts (username + password only).

Signup is intentionally trivial — no email, no recovery — but hardened against
swarming: a few accounts per public IP (IPv6 collapsed to /64), an atomic cap so
concurrent signups can't race past it, and per-IP / per-username rate limiting on
both signup and login. Passwords are pbkdf2-hmac-sha256 salted hashes (stdlib).
Sessions are opaque bearer tokens with an absolute TTL. See DESIGN §12.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time

import argon2

from . import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
MIN_PASSWORD = 6
IP_CAP = 5                 # max accounts per public IP (IPv6 per /64)
_ITERATIONS = 200_000  # legacy pbkdf2 cost (verify-only, for migrate-on-login)


def _pbkdf2(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), _ITERATIONS).hex()


# Argon2id is the at-rest password hash (memory-hard, GPU-resistant); pbkdf2 above stays
# verify-only so existing accounts upgrade to argon2 transparently on next login.
_ph = argon2.PasswordHasher(memory_cost=64 * 1024, time_cost=2, parallelism=1)
_DUMMY_ARGON2 = _ph.hash("x")   # verified against on unknown users so timing doesn't enumerate


def _hash(password):
    """Argon2id PHC string (embeds its own salt + params)."""
    return _ph.hash(password or "")


def _verify_password(password, u):
    """True iff `password` matches user `u`. Always runs a hash (dummy when u is None) so
    response timing doesn't reveal whether a username exists; verifies legacy pbkdf2 too."""
    stored = u["pw_hash"] if u else _DUMMY_ARGON2
    if stored.startswith("$argon2"):
        try:
            _ph.verify(stored, password or "")
            return u is not None
        except Exception:
            return False
    salt = u["pw_salt"] if (u and u.get("pw_salt")) else "00" * 16
    return bool(u) and hmac.compare_digest(_pbkdf2(password or "", salt), stored)


def _canon_ip(raw):
    """Canonicalize an address for the cap key: IPv6 -> its /64 prefix (so a single
    subscriber's delegation / rotating privacy addresses count as one), IPv4 -> /32."""
    try:
        ip = ipaddress.ip_address(raw)
        if ip.version == 6:
            net = ipaddress.ip_network(str(ip) + "/64", strict=False)
            return str(net.network_address) + "/64"
        return str(ip)
    except ValueError:
        return raw or "unknown"


def client_ip(request):
    """The caller's real peer IP for the per-IP cap. We DO NOT trust
    X-Forwarded-For / X-Real-IP unless AEON_TRUST_PROXY=1 — the server is exposed
    directly (Tailscale / LAN, no trusted reverse proxy), so those headers are
    forgeable. Only the socket peer is authoritative."""
    raw = request.client.host if request.client else "unknown"
    if os.environ.get("AEON_TRUST_PROXY") == "1":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            raw = xff.split(",")[0].strip()
    return _canon_ip(raw)


def _rate_ok(key, limit, window):
    """True if `key` is under `limit` events in the last `window` seconds (records this
    one). DB-backed sliding window so the limit holds across replicas (atomic check+record)."""
    return db.rate_hit(key, limit, window, time.time())


def _record_fail(*keys):
    now = time.time()
    for k in keys:
        db.rate_record(k, now)


def _too_many_fails(key, limit, window):
    return db.rate_count(key, window, time.time()) >= limit


def _start_session(uid):
    token = secrets.token_urlsafe(32)
    db.create_session(token, uid)
    return token


def signup(username, password, ip):
    username = (username or "").strip()
    password = password or ""
    if not _rate_ok("signup:" + ip, limit=8, window=60):
        return {"error": "too many signup attempts — slow down and try again shortly"}
    if not USERNAME_RE.match(username):
        return {"error": "username must be 3–24 chars (letters, digits, . _ -)"}
    if len(password) < MIN_PASSWORD:
        return {"error": f"password must be at least {MIN_PASSWORD} characters"}
    # Generic message reused for collisions/caps/reserved names so signup can't be used to
    # enumerate existing (or admin) usernames — same wording login uses.
    generic = {"error": "couldn't create the account right now — please try again later"}
    # Admin-name front-running fix: reject signup of a configured admin handle (case-insensitive,
    # via is_admin) so an attacker can't pre-register a future admin's name. Vague on purpose.
    if is_admin({"username": username}):
        return generic
    # Transactional check-then-create at the DB level, so the per-IP cap + username
    # uniqueness hold across replicas — replaces the single-process _signup_lock TOCTOU guard.
    uid = secrets.token_hex(8)
    outcome = db.create_user_if_capped(uid, username=username, pw_hash=_hash(password),
                                       pw_salt="", signup_ip=ip, cap=IP_CAP)
    if outcome == "taken":
        # Enumeration fix: do not reveal the name exists — same generic message as the cap branch.
        return generic
    if outcome == "capped":
        # Deliberately vague: do not disclose the per-network account cap publicly.
        return generic
    return {"token": _start_session(uid), "user": public_state(uid)}


def login(username, password, ip="unknown"):
    username = (username or "").strip()
    if not _rate_ok("login:" + ip, limit=12, window=60):
        return {"error": "too many attempts — try again shortly"}
    # Targeted-lockout DoS fix: gate the hard lock on a per-(username|IP) bucket AND the
    # global per-username counter, so a stranger flooding fails from other IPs can't lock a
    # legitimate owner out from a clean IP with the right password. The pure per-username
    # counter is kept only as a higher bound (alerting / distributed-attack signal).
    uip = username.lower() + "|" + ip
    if (_too_many_fails("uip:" + uip, limit=10, window=600)
            and _too_many_fails("u:" + username.lower(), limit=50, window=600)) \
            or _too_many_fails("ip:" + ip, limit=40, window=600):
        return {"error": "too many failed attempts — wait a minute and try again"}
    u = db.get_user_by_username(username)   # case-insensitive lookup
    if not _verify_password(password or "", u):   # constant-time-ish even when u is None
        # record per-(username|IP) too so the targeted-lockout gate above accumulates
        _record_fail("u:" + username.lower(), "ip:" + ip, "uip:" + uip)
        return {"error": "invalid username or password"}
    # Migrate-on-login: transparently upgrade a legacy pbkdf2 account to argon2id.
    if not (u.get("pw_hash") or "").startswith("$argon2"):
        db.update_user_password(u["id"], pw_hash=_hash(password or ""), pw_salt="")
    return {"token": _start_session(u["id"]), "user": public_state(u["id"])}


def admin_usernames():
    """Admin allowlist from the environment (AEON_ADMIN_USERS=alice,bob). Grant is
    entirely operator-controlled config — no admin bit is stored in the DB."""
    return {u.strip() for u in os.environ.get("AEON_ADMIN_USERS", "").split(",") if u.strip()}


def is_admin(user):
    if not user:
        return False
    admins = {u.lower() for u in admin_usernames()}
    return (user.get("username") or "").lower() in admins


def public_state(uid):
    """What the client may know about itself. The badge is driven by `ever_verified`
    (sticky), so a user flagged AFTER verifying does not see 'verified' flip back to
    'verifying' — the honeypot/flag stays secret. A user flagged on their first test
    never set ever_verified, so they remain indistinguishable from a pending user."""
    u = db.get_user(uid)
    if not u:
        return None
    s = db.user_stats(uid)
    shown_verified = bool(u.get("ever_verified"))
    return {
        "username": u["username"],
        "votes": s["votes"],
        "counted": s["real_votes"] if shown_verified else 0,
        "verified": shown_verified,
        "admin": (u["username"] or "").lower() in {a.lower() for a in admin_usernames()},
    }


def user_from_request(request):
    """Resolve the bearer token (Authorization: Bearer <token>) to a user row."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if not token:
        token = request.headers.get("x-aeon-token")
    return db.user_for_token(token) if token else None
