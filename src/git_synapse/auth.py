"""Who may read this dashboard, and how that is proven.

Everything the dashboard shows is derived from public repositories, but the
deployment is not public: it says which repositories an organisation tracks,
where its coupling is weakest, and which files one person alone understands.
So the UI is behind a sign-in.

No new dependency. Passwords are hashed with :func:`hashlib.scrypt`, which is
memory-hard and in the standard library; sessions are random tokens compared in
constant time. A password library would be the conventional choice, and would
also be a supply-chain edge on a tool whose whole argument is that dependencies
propagate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from git_synapse.db.engine import connection, execute, query, query_one

log = logging.getLogger(__name__)

#: scrypt cost. 2**15 * 8 * 128 bytes is 32MB per hash, which is a fraction of
#: a second here and expensive in bulk for anyone with the table.
_N, _R, _P, _DKLEN = 2**15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024

#: How long a session lasts without being used. Long enough not to interrupt a
#: working day, short enough that a forgotten browser is not a standing key.
SESSION_DAYS = 14

ROLES = ("admin", "member")

#: Deliberately permissive: the point is to catch a typo, not to adjudicate
#: RFC 5322. Anything stricter rejects addresses that genuinely exist.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Twelve, not eight. This is a shared dashboard on an internal network, where
#: the realistic attack is someone reusing a password, not a brute-force run.
MIN_PASSWORD = 12


#: Wrong passwords allowed before an address is made to wait, and how long the
#: window is. scrypt already costs about 70ms an attempt, which throttles one
#: attacker on one thread; it does nothing about a thousand in parallel.
#:
#: Counted per address rather than per client, which is the deliberate trade:
#: an attacker cannot spread attempts across addresses to keep working on one,
#: but can lock a colleague out for fifteen minutes by guessing at their
#: address. On an internal dashboard that is an annoyance; on a public one it
#: would want a per-client budget as well.
MAX_FAILURES = 10
LOCKOUT_MINUTES = 15


class AuthError(Exception):
    """Something a caller did wrong: bad credentials, duplicate email, weak password."""


class TooManyAttempts(AuthError):
    """Refused for now, not refused outright. Distinguished so the API can
    answer 429 rather than 401: the credentials were never examined."""


class SetupAlreadyClaimed(AuthError):
    """The first administrator was created while this request was waiting."""


# ----------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    """Hash a password, with its parameters recorded alongside it.

    Storing n, r and p means they can be raised later without invalidating
    every existing password: an old hash still verifies under its own cost.
    """
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                            dklen=_DKLEN, maxmem=_MAXMEM)
    return "$".join(("scrypt", str(_N), str(_R), str(_P),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(digest).decode()))


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False rather than raising."""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)), maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


# --------------------------------------------------------------------- users

def _row(user: dict | None) -> dict | None:
    """A user as the API returns it. Never the hash, on any path."""
    if user is None:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


def count_users() -> int:
    return int(query_one("SELECT count(*) AS n FROM app_user")["n"])


def list_users() -> list[dict]:
    return query(
        """
        SELECT u.id, u.email, u.name, u.role, u.is_active, u.created_at,
               u.last_login_at, c.email AS created_by_email,
               (SELECT count(*) FROM user_session s
                 WHERE s.user_id = u.id AND s.expires_at > now()) AS active_sessions
        FROM app_user u
        LEFT JOIN app_user c ON c.id = u.created_by
        ORDER BY u.created_at
        """
    )


def get_user(user_id: int) -> dict | None:
    return _row(query_one("SELECT * FROM app_user WHERE id = %s", (user_id,)))


def by_email(email: str) -> dict | None:
    """Including the hash: only the sign-in path uses this."""
    return query_one("SELECT * FROM app_user WHERE lower(email) = lower(%s)",
                     (email.strip(),))


def create_user(
    email: str, name: str, password: str,
    role: str = "member", created_by: int | None = None,
) -> dict:
    """Add someone. Raises AuthError on anything the caller can fix."""
    email = email.strip()
    name = name.strip()
    if not _EMAIL.match(email):
        raise AuthError(f"{email!r} does not look like an email address")
    if not name:
        raise AuthError("a name is required")
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    if by_email(email) is not None:
        raise AuthError(f"{email} already has an account")

    row = query_one(
        """
        INSERT INTO app_user (email, name, role, password_hash, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, email, name, role, is_active, created_at, last_login_at
        """,
        (email, name, role, hash_password(password), created_by),
    )
    log.info("user created: %s (%s)", email, role)
    return row


def update_user(user_id: int, **fields: Any) -> dict | None:
    """Change a name, role, password or active flag. Unset fields are left alone."""
    sets, params = [], {"id": user_id}
    if "name" in fields and fields["name"] is not None:
        if not str(fields["name"]).strip():
            raise AuthError("a name is required")
        sets.append("name = %(name)s")
        params["name"] = str(fields["name"]).strip()
    if fields.get("role") is not None:
        if fields["role"] not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        sets.append("role = %(role)s")
        params["role"] = fields["role"]
    if fields.get("is_active") is not None:
        sets.append("is_active = %(is_active)s")
        params["is_active"] = bool(fields["is_active"])
    if fields.get("password"):
        sets.append("password_hash = %(password_hash)s")
        params["password_hash"] = hash_password(fields["password"])
    if not sets:
        return get_user(user_id)

    row = query_one(
        f"""
        UPDATE app_user SET {', '.join(sets)} WHERE id = %(id)s
        RETURNING id, email, name, role, is_active, created_at, last_login_at
        """,
        params,
    )
    # A password change or a deactivation must end the sessions it was meant to
    # stop; leaving them alive makes both changes advisory.
    if row is not None and (fields.get("password") or fields.get("is_active") is False):
        revoke_all(user_id)
    return row


def delete_user(user_id: int) -> bool:
    return bool(execute("DELETE FROM app_user WHERE id = %s", (user_id,)))


def admin_count(exclude: int | None = None) -> int:
    """Active administrators, optionally ignoring one. Used to refuse the
    change that would leave the deployment with nobody able to add a person."""
    return int(query_one(
        "SELECT count(*) AS n FROM app_user"
        " WHERE role = 'admin' AND is_active AND (%s::bigint IS NULL OR id <> %s)",
        (exclude, exclude),
    )["n"])


# ------------------------------------------------------------------ sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def recent_failures(email: str) -> int:
    return int(query_one(
        "SELECT count(*) AS n FROM login_attempt"
        " WHERE lower(email) = lower(%s) AND at > now() - make_interval(mins => %s)",
        (email.strip(), LOCKOUT_MINUTES),
    )["n"])


def prune_login_attempts() -> int:
    """Drop attempts past the window. Called with the other housekeeping."""
    return int(execute(
        "DELETE FROM login_attempt WHERE at < now() - make_interval(mins => %s)",
        (LOCKOUT_MINUTES,),
    ) or 0)


def sign_in(email: str, password: str, user_agent: str | None = None) -> tuple[str, dict]:
    """Verify credentials and open a session. Returns (token, user).

    The same message for an unknown address and a wrong password, and the hash
    is computed either way: a faster "no such user" tells an attacker which
    addresses are real.
    """
    if recent_failures(email) >= MAX_FAILURES:
        # Counted per address, not per connection: an attacker picks the
        # address, and cannot pick a different one to keep attacking this one.
        raise TooManyAttempts(
            f"too many failed attempts; try again in {LOCKOUT_MINUTES} minutes")

    user = by_email(email)
    stored = user["password_hash"] if user else _DUMMY_HASH
    ok = verify_password(password, stored)
    if user is None or not ok:
        execute("INSERT INTO login_attempt (email, client) VALUES (%s, %s)",
                (email.strip()[:254], (user_agent or "")[:200]))
        raise AuthError("wrong email or password")
    if not user["is_active"]:
        raise AuthError("this account has been deactivated")

    token = secrets.token_urlsafe(32)
    execute(
        "INSERT INTO user_session (token_hash, user_id, expires_at, user_agent)"
        " VALUES (%s, %s, %s, %s)",
        (_token_hash(token), user["id"],
         datetime.now(UTC) + timedelta(days=SESSION_DAYS),
         (user_agent or "")[:200]),
    )
    execute("UPDATE app_user SET last_login_at = now() WHERE id = %s", (user["id"],))
    # A success clears the record: the person proved it was them, and a stale
    # count would lock them out on their next typo.
    execute("DELETE FROM login_attempt WHERE lower(email) = lower(%s)", (email.strip(),))
    return token, _row(user)


#: A real hash of a value nobody knows, so an unknown address costs the same
#: scrypt work as a known one.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def session_user(token: str | None) -> dict | None:
    """The signed-in user for a cookie value, or None."""
    if not token:
        return None
    row = query_one(
        """
        SELECT u.* FROM user_session s
        JOIN app_user u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active
        """,
        (_token_hash(token),),
    )
    if row is None:
        return None
    # Sliding expiry: a session in daily use should not end mid-week, and one
    # abandoned a fortnight ago should.
    execute(
        "UPDATE user_session SET last_seen_at = now(), expires_at = %s"
        " WHERE token_hash = %s",
        (datetime.now(UTC) + timedelta(days=SESSION_DAYS), _token_hash(token)),
    )
    return _row(row)


def sign_out(token: str | None) -> None:
    if token:
        execute("DELETE FROM user_session WHERE token_hash = %s", (_token_hash(token),))


def revoke_all(user_id: int) -> int:
    return int(execute("DELETE FROM user_session WHERE user_id = %s", (user_id,)) or 0)


# -------------------------------------------------------------- api tokens

#: Recognisable in a log or a paste, and greppable in a leaked file.
TOKEN_PREFIX = "gss_"  # noqa: S105 - a prefix, not a secret


def create_token(user_id: int, name: str, days: int | None = None) -> tuple[str, dict]:
    """Mint a personal token. Returns (secret, row); the secret is not stored.

    Anyone may hold one: a token carries the identity and role of the person who
    made it, so it can do exactly what they can do and nothing more, and it dies
    with their account.
    """
    name = (name or "").strip()
    if not name:
        raise AuthError("give the token a name, so it can be told from the others")
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(days=days)) if days else None
    row = query_one(
        """
        INSERT INTO api_token (token_hash, prefix, user_id, name, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, prefix, name, created_at, expires_at, last_used_at
        """,
        (_token_hash(secret), secret[:len(TOKEN_PREFIX) + 6], user_id, name, expires),
    )
    return secret, row


def list_tokens(user_id: int) -> list[dict]:
    return query(
        "SELECT id, prefix, name, created_at, expires_at, last_used_at"
        " FROM api_token WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    )


def delete_token(token_id: int, user_id: int) -> bool:
    """Scoped to the owner: a token id is not a capability to revoke it."""
    return bool(execute("DELETE FROM api_token WHERE id = %s AND user_id = %s",
                        (token_id, user_id)))


def token_user(secret: str | None) -> dict | None:
    """The user behind a bearer token, or None."""
    if not secret or not secret.startswith(TOKEN_PREFIX):
        return None
    row = query_one(
        """
        SELECT u.* FROM api_token t
        JOIN app_user u ON u.id = t.user_id
        WHERE t.token_hash = %s AND u.is_active
          AND (t.expires_at IS NULL OR t.expires_at > now())
        """,
        (_token_hash(secret),),
    )
    if row is None:
        return None
    execute("UPDATE api_token SET last_used_at = now() WHERE token_hash = %s",
            (_token_hash(secret),))
    return _row(row)


# --------------------------------------------------------------- first admin

#: Where the minted token lives. Not routed through `analysis.settings`, whose
#: WRITABLE list is the set of things an administrator may change from the UI;
#: this is neither settable nor readable there.
_SETUP_KEY = "setup:token"

#: The sentinel the failed-token attempts are counted against. `_EMAIL` demands
#: an `@` and a dot, so no real address can ever collide with this one and no
#: person can be locked out by someone hammering setup.
_SETUP_PRINCIPAL = "setup"


def setup_token() -> str:
    """The token that must be presented to create the first administrator.

    `ADMIN_SETUP_TOKEN` wins when set, so an automated deployment can put a
    known value in place and claim the account without reading a log. Otherwise
    one is minted here and stored, because the alternatives are worse: deriving
    it from anything already in the deployment makes it guessable from that
    thing, and generating it per process gives every worker a different answer.

    The INSERT is the arbitration. Four workers racing on a cold database all
    attempt it, exactly one row survives, and the SELECT that follows returns
    that row to all four.
    """
    from git_synapse.config import get_config

    configured = get_config().server.admin_setup_token
    if configured:
        return configured

    execute(
        """
        INSERT INTO meta (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO NOTHING
        """,
        (_SETUP_KEY, json.dumps(secrets.token_urlsafe(32))),
    )
    row = query_one("SELECT value FROM meta WHERE key = %s", (_SETUP_KEY,))
    return str(row["value"])


def setup_token_is_minted() -> bool:
    """Whether the token was generated here, rather than supplied. The console
    needs to say where to find it, and the two answers differ."""
    from git_synapse.config import get_config

    return not get_config().server.admin_setup_token


def check_setup_token(supplied: str) -> None:
    """Raise unless `supplied` is the setup token.

    Rate-limited like a password. A 32-byte token is not going to fall to
    guessing, but the endpoint is reachable during the one window in the
    deployment's life when nothing is signed in, and an unbounded loop against
    it is free noise in the log at best.
    """
    if recent_failures(_SETUP_PRINCIPAL) >= MAX_FAILURES:
        raise TooManyAttempts(
            f"too many failed attempts; try again in {LOCKOUT_MINUTES} minutes")
    if not hmac.compare_digest(supplied.strip(), setup_token()):
        execute("INSERT INTO login_attempt (email, client) VALUES (%s, %s)",
                (_SETUP_PRINCIPAL, "setup"))
        raise AuthError("that is not the setup token for this deployment")


def clear_setup_token() -> None:
    """Drop it once it has been used. It authorises exactly one thing, and that
    thing has now happened; leaving the value in the table is a standing secret
    that nothing will ever check again."""
    execute("DELETE FROM meta WHERE key = %s", (_SETUP_KEY,))


def claim_first_admin(
    email: str, name: str, password: str, setup_secret: str,
    user_agent: str | None = None,
) -> tuple[str, dict]:
    """Atomically claim the empty deployment for its first administrator."""
    from git_synapse.config import get_config

    email = email.strip()
    name = name.strip()
    configured = get_config().server.admin_setup_token
    token = secrets.token_urlsafe(32)
    with connection() as conn:
        # A transaction-scoped lock closes the check-then-insert race between
        # two first-run requests.  The lock is deliberately held through the
        # account, session, and setup-secret writes.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x47534649525354,))
        if conn.execute("SELECT count(*) FROM app_user").fetchone()[0] > 0:
            raise SetupAlreadyClaimed("this deployment already has users")
        if not _EMAIL.match(email):
            raise AuthError(f"{email!r} does not look like an email address")
        if not name:
            raise AuthError("a name is required")
        stored = conn.execute(
            "SELECT value #>> '{}' FROM meta WHERE key = %s", (_SETUP_KEY,)
        ).fetchone()
        expected = configured or (stored[0] if stored else "")
        if not hmac.compare_digest(setup_secret.strip(), expected):
            raise AuthError("that is not the setup token for this deployment")

        row = conn.execute(
            """
            INSERT INTO app_user (email, name, role, password_hash)
            VALUES (%s, %s, 'admin', %s)
            RETURNING id, email, name, role, is_active, created_at, last_login_at
            """,
            (email, name, hash_password(password)),
        ).fetchone()
        user = dict(zip(
            ("id", "email", "name", "role", "is_active", "created_at", "last_login_at"),
            row,
            strict=True,
        ))
        conn.execute(
            "INSERT INTO user_session (token_hash, user_id, expires_at, user_agent)"
            " VALUES (%s, %s, %s, %s)",
            (_token_hash(token), user["id"],
             datetime.now(UTC) + timedelta(days=SESSION_DAYS),
             (user_agent or "")[:200]),
        )
        conn.execute("UPDATE app_user SET last_login_at = now() WHERE id = %s",
                     (user["id"],))
        conn.execute("DELETE FROM meta WHERE key = %s", (_SETUP_KEY,))
        return token, user


# ------------------------------------------------------------- access policy

#: What a deployment requires of a caller. `open` is the behaviour before any
#: of this existed, kept because a laptop demo and a shared internal dashboard
#: are different things and only the person running it knows which this is.
ACCESS_MODES = ("required", "open")


def access_mode(surface: str) -> str:
    """Whether `dashboard` or `mcp` currently requires a caller to identify.

    Read live from the database on every request rather than cached: an
    administrator turning sign-in on expects it to take effect now, not at the
    next restart.
    """
    from git_synapse.analysis import settings

    if count_users() == 0:
        # Nothing to sign in as yet. Requiring it here would lock the first
        # administrator out of the screen that creates them.
        return "open"
    value = settings.effective(f"{surface}_auth", "required")
    return value if value in ACCESS_MODES else "required"


def prune_sessions() -> int:
    """Drop expired sessions. Called from the ingest run, like the call log."""
    return int(execute("DELETE FROM user_session WHERE expires_at < now()") or 0)
