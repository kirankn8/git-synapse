"""Who may read this dashboard, and how that is proven.

Everything the dashboard shows is derived from public repositories, but the
deployment is not public: it says which repositories an organisation tracks,
where its coupling is weakest, and which files one person alone understands.
So the UI is behind a sign-in.

Passwords are hashed with :func:`hashlib.scrypt`, which is memory-hard and in
the standard library; sessions are random tokens compared in constant time.
Database persistence uses the shared SQLAlchemy ORM session layer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

# Kept as a narrow test-cleanup compatibility export. Production auth writes
# use the ORM session layer above.
# Narrow test-cleanup compatibility export; production auth writes use the ORM.
import git_synapse.db.engine as _db_engine
from git_synapse.db.orm import models, session_scope

execute = _db_engine.execute

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
    with session_scope() as session:
        User = models().AppUser
        return int(session.scalar(select(func.count()).select_from(User)) or 0)


def list_users() -> list[dict]:
    with session_scope() as session:
        User, Session = models().AppUser, models().UserSession
        Creator = aliased(User)
        active_sessions = (
            select(func.count())
            .where(Session.user_id == User.id, Session.expires_at > func.now())
            .correlate(User)
            .scalar_subquery()
        )
        rows = session.execute(
            select(
                User.id, User.email, User.name, User.role, User.is_active,
                User.created_at, User.last_login_at,
                Creator.email.label("created_by_email"),
                active_sessions.label("active_sessions"),
            ).outerjoin(Creator, Creator.id == User.created_by)
            .order_by(User.created_at)
        ).mappings().all()
        return [dict(row) for row in rows]


def get_user(user_id: int) -> dict | None:
    with session_scope() as session:
        user = session.get(models().AppUser, user_id)
        return _row({column.name: getattr(user, column.name)
                     for column in user.__table__.columns} if user else None)


def by_email(email: str) -> dict | None:
    """Including the hash: only the sign-in path uses this."""
    with session_scope() as session:
        User = models().AppUser
        user = session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        return ({column.name: getattr(user, column.name) for column in user.__table__.columns}
                if user else None)


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

    with session_scope() as session:
        User = models().AppUser
        user = User(
            email=email, name=name, role=role,
            password_hash=hash_password(password), created_by=created_by,
        )
        session.add(user)
        session.flush()
        row = {column.name: getattr(user, column.name)
               for column in user.__table__.columns}
    log.info("user created: %s (%s)", email, role)
    return _row(row)


def update_user(user_id: int, **fields: Any) -> dict | None:
    """Change a name, role, password or active flag. Unset fields are left alone."""
    sets = {}
    if "name" in fields and fields["name"] is not None:
        if not str(fields["name"]).strip():
            raise AuthError("a name is required")
        sets["name"] = str(fields["name"]).strip()
    if fields.get("role") is not None:
        if fields["role"] not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        sets["role"] = fields["role"]
    if fields.get("is_active") is not None:
        sets["is_active"] = bool(fields["is_active"])
    if fields.get("password"):
        sets["password_hash"] = hash_password(fields["password"])
    if not sets:
        return get_user(user_id)

    with session_scope() as session:
        User = models().AppUser
        session.execute(update(User).where(User.id == user_id).values(**sets))
        user = session.get(User, user_id)
        row = ({column.name: getattr(user, column.name) for column in user.__table__.columns}
               if user else None)
    # A password change or a deactivation must end the sessions it was meant to
    # stop; leaving them alive makes both changes advisory.
    if row is not None and (fields.get("password") or fields.get("is_active") is False):
        revoke_all(user_id)
    return _row(row)


def delete_user(user_id: int) -> bool:
    with session_scope() as session:
        User = models().AppUser
        result = session.execute(delete(User).where(User.id == user_id))
        return bool(result.rowcount)


def admin_count(exclude: int | None = None) -> int:
    """Active administrators, optionally ignoring one. Used to refuse the
    change that would leave the deployment with nobody able to add a person."""
    with session_scope() as session:
        User = models().AppUser
        conditions = [User.role == "admin", User.is_active.is_(True)]
        if exclude is not None:
            conditions.append(User.id != exclude)
        return int(session.scalar(select(func.count()).select_from(User).where(*conditions)) or 0)


# ------------------------------------------------------------------ sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def recent_failures(email: str) -> int:
    with session_scope() as session:
        Attempt = models().LoginAttempt
        cutoff = datetime.now(UTC) - timedelta(minutes=LOCKOUT_MINUTES)
        return int(session.scalar(select(func.count()).select_from(Attempt).where(
            func.lower(Attempt.email) == email.strip().lower(), Attempt.at > cutoff,
        )) or 0)


def prune_login_attempts() -> int:
    """Drop attempts past the window. Called with the other housekeeping."""
    with session_scope() as session:
        Attempt = models().LoginAttempt
        cutoff = datetime.now(UTC) - timedelta(minutes=LOCKOUT_MINUTES)
        result = session.execute(delete(Attempt).where(Attempt.at < cutoff))
        return int(result.rowcount or 0)


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
        with session_scope() as session:
            session.add(models().LoginAttempt(
                email=email.strip()[:254], client=(user_agent or "")[:200],
            ))
        raise AuthError("wrong email or password")
    if not user["is_active"]:
        raise AuthError("this account has been deactivated")

    token = secrets.token_urlsafe(32)
    with session_scope() as session:
        UserSession, User = models().UserSession, models().AppUser
        session.add(UserSession(
            token_hash=_token_hash(token), user_id=user["id"],
            expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS),
            user_agent=(user_agent or "")[:200],
        ))
        session.execute(update(User).where(User.id == user["id"]).values(last_login_at=func.now()))
    # A success clears the record: the person proved it was them, and a stale
    # count would lock them out on their next typo.
    with session_scope() as session:
        Attempt = models().LoginAttempt
        session.execute(delete(Attempt).where(func.lower(Attempt.email) == email.strip().lower()))
    return token, _row(user)


#: A real hash of a value nobody knows, so an unknown address costs the same
#: scrypt work as a known one.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def session_user(token: str | None) -> dict | None:
    """The signed-in user for a cookie value, or None."""
    if not token:
        return None
    with session_scope() as session:
        UserSession, User = models().UserSession, models().AppUser
        row = session.execute(
            select(User).join(UserSession, User.id == UserSession.user_id).where(
                UserSession.token_hash == _token_hash(token),
                UserSession.expires_at > func.now(), User.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row_dict = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        session.execute(update(UserSession).where(
            UserSession.token_hash == _token_hash(token),
        ).values(last_seen_at=func.now(), expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS)))
        return _row(row_dict)


def sign_out(token: str | None) -> None:
    if token:
        with session_scope() as session:
            session.execute(delete(models().UserSession).where(
                models().UserSession.token_hash == _token_hash(token),
            ))


def revoke_all(user_id: int) -> int:
    with session_scope() as session:
        Session = models().UserSession
        result = session.execute(delete(Session).where(Session.user_id == user_id))
        return int(result.rowcount or 0)


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
    with session_scope() as session:
        Token = models().ApiToken
        row = Token(
            token_hash=_token_hash(secret), prefix=secret[:len(TOKEN_PREFIX) + 6],
            user_id=user_id, name=name, expires_at=expires,
        )
        session.add(row)
        session.flush()
        return secret, {column.name: getattr(row, column.name) for column in row.__table__.columns
                        if column.name not in {"token_hash", "user_id"}}


def list_tokens(user_id: int) -> list[dict]:
    with session_scope() as session:
        Token = models().ApiToken
        return [dict(row) for row in session.execute(select(
            Token.id, Token.prefix, Token.name, Token.created_at,
            Token.expires_at, Token.last_used_at,
        ).where(Token.user_id == user_id).order_by(Token.created_at.desc())).mappings()]


def delete_token(token_id: int, user_id: int) -> bool:
    """Scoped to the owner: a token id is not a capability to revoke it."""
    with session_scope() as session:
        Token = models().ApiToken
        result = session.execute(delete(Token).where(Token.id == token_id, Token.user_id == user_id))
        return bool(result.rowcount)


def token_user(secret: str | None) -> dict | None:
    """The user behind a bearer token, or None."""
    if not secret or not secret.startswith(TOKEN_PREFIX):
        return None
    with session_scope() as session:
        Token, User = models().ApiToken, models().AppUser
        row = session.execute(select(User).join(Token, User.id == Token.user_id).where(
            Token.token_hash == _token_hash(secret), User.is_active.is_(True),
            (Token.expires_at.is_(None) | (Token.expires_at > func.now())),
        )).scalar_one_or_none()
        if row is None:
            return None
        row_dict = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        session.execute(update(Token).where(Token.token_hash == _token_hash(secret)).values(
            last_used_at=func.now(),
        ))
        return _row(row_dict)


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

    with session_scope() as session:
        Meta = models().Meta
        secret = secrets.token_urlsafe(32)
        statement = pg_insert(Meta).values(key=_SETUP_KEY, value=secret)
        session.execute(statement.on_conflict_do_nothing(index_elements=[Meta.key]))
        return str(session.scalar(select(Meta.value).where(Meta.key == _SETUP_KEY)))


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
        with session_scope() as session:
            session.add(models().LoginAttempt(email=_SETUP_PRINCIPAL, client="setup"))
        raise AuthError("that is not the setup token for this deployment")


def clear_setup_token() -> None:
    """Drop it once it has been used. It authorises exactly one thing, and that
    thing has now happened; leaving the value in the table is a standing secret
    that nothing will ever check again."""
    with session_scope() as session:
        Meta = models().Meta
        session.execute(delete(Meta).where(Meta.key == _SETUP_KEY))


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
    with session_scope() as session:
        # A transaction-scoped lock closes the check-then-insert race between
        # two first-run requests.  The lock is deliberately held through the
        # account, session, and setup-secret writes.
        User, Meta, UserSession = models().AppUser, models().Meta, models().UserSession
        session.scalar(select(func.pg_advisory_xact_lock(0x47534649525354)))
        if session.scalar(select(func.count()).select_from(User)) > 0:
            raise SetupAlreadyClaimed("this deployment already has users")
        if not _EMAIL.match(email):
            raise AuthError(f"{email!r} does not look like an email address")
        if not name:
            raise AuthError("a name is required")
        stored = session.scalar(select(Meta.value).where(Meta.key == _SETUP_KEY))
        expected = configured or (str(stored) if stored is not None else "")
        if not hmac.compare_digest(setup_secret.strip(), expected):
            raise AuthError("that is not the setup token for this deployment")

        row = User(email=email, name=name, role="admin", password_hash=hash_password(password))
        session.add(row)
        session.flush()
        user = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        session.add(UserSession(
            token_hash=_token_hash(token), user_id=user["id"],
            expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS),
            user_agent=(user_agent or "")[:200],
        ))
        session.execute(update(User).where(User.id == user["id"]).values(last_login_at=func.now()))
        session.execute(delete(Meta).where(Meta.key == _SETUP_KEY))
        user = {key: user[key] for key in
                ("id", "email", "name", "role", "is_active", "created_at", "last_login_at")}
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
    with session_scope() as session:
        Session = models().UserSession
        result = session.execute(delete(Session).where(Session.expires_at < func.now()))
        return int(result.rowcount or 0)
