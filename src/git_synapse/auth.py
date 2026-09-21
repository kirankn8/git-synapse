"""Who may read this dashboard, and how that is proven."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

# scrypt cost: N=2**15, r=8, p=1 is about 32MB per hash.
_N, _R, _P, _DKLEN = 2**15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024

SESSION_DAYS = 14

ROLES = ("admin", "member")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD = 12


MAX_FAILURES = 10
LOCKOUT_MINUTES = 15


class AuthError(Exception):
    """Something a caller did wrong: bad credentials, duplicate email, weak password."""


class TooManyAttempts(AuthError):
    """Refused for now, not refused outright."""



def hash_password(password: str) -> str:
    """Hash a password, with its parameters recorded alongside it."""
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



def _row(user: dict | None) -> dict | None:
    """A user as the API returns it. Never the hash, on any path."""
    if user is None:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


def count_users() -> int:
    with session_scope() as session:
        User = models().AppUser
        return session.query(User).count()


def list_users() -> list[dict]:
    with session_scope() as session:
        User, Session = models().AppUser, models().UserSession
        users = session.query(User).order_by(User.created_at).all()
        creators = {row.id: row.email for row in users}
        now = datetime.now(UTC)
        return [{
            "id": row.id, "email": row.email, "name": row.name,
            "role": row.role, "is_active": row.is_active,
            "created_at": row.created_at, "last_login_at": row.last_login_at,
            "created_by_email": creators.get(row.created_by),
            "active_sessions": session.query(Session).filter_by(user_id=row.id).filter(
                Session.expires_at > now,
            ).count(),
        } for row in users]


def get_user(user_id: int) -> dict | None:
    with session_scope() as session:
        user = session.get(models().AppUser, user_id)
        return _row({column.name: getattr(user, column.name)
                     for column in user.__table__.columns} if user else None)


def by_email(email: str) -> dict | None:
    """Including the hash: only the sign-in path uses this."""
    with session_scope() as session:
        User = models().AppUser
        user = next((row for row in session.query(User).all()
                     if row.email.lower() == email.strip().lower()), None)
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
        user = session.get(User, user_id)
        if user is not None:
            for key, value in sets.items():
                setattr(user, key, value)
        row = ({column.name: getattr(user, column.name) for column in user.__table__.columns}
               if user else None)
    if row is not None and (fields.get("password") or fields.get("is_active") is False):
        revoke_all(user_id)
    return _row(row)


def delete_user(user_id: int) -> bool:
    with session_scope() as session:
        user = session.get(models().AppUser, user_id)
        if user is None:
            return False
        session.delete(user)
        return True


def admin_count(exclude: int | None = None) -> int:
    """Active administrators, optionally ignoring one."""
    with session_scope() as session:
        User = models().AppUser
        return sum(1 for user in session.query(User).all()
                   if user.role == "admin" and user.is_active and
                   (exclude is None or user.id != exclude))



def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def recent_failures(email: str) -> int:
    with session_scope() as session:
        Attempt = models().LoginAttempt
        cutoff = datetime.now(UTC) - timedelta(minutes=LOCKOUT_MINUTES)
        return sum(1 for attempt in session.query(Attempt).all()
                   if attempt.email.lower() == email.strip().lower() and attempt.at > cutoff)


def prune_login_attempts() -> int:
    """Drop attempts past the window. Called with the other housekeeping."""
    with session_scope() as session:
        Attempt = models().LoginAttempt
        cutoff = datetime.now(UTC) - timedelta(minutes=LOCKOUT_MINUTES)
        rows = [row for row in session.query(Attempt).all() if row.at < cutoff]
        for row in rows:
            session.delete(row)
        return len(rows)


def sign_in(email: str, password: str, user_agent: str | None = None) -> tuple[str, dict]:
    """Verify credentials and open a session. Returns (token, user)."""
    if recent_failures(email) >= MAX_FAILURES:
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
        row = session.get(User, user["id"])
        if row is not None:
            row.last_login_at = datetime.now(UTC)
    with session_scope() as session:
        Attempt = models().LoginAttempt
        for row in session.query(Attempt).all():
            if row.email.lower() == email.strip().lower():
                session.delete(row)
    return token, _row(user)


# Checked against unknown emails so both paths cost the same scrypt work.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def session_user(token: str | None) -> dict | None:
    """The signed-in user for a cookie value, or None."""
    if not token:
        return None
    with session_scope() as session:
        UserSession, User = models().UserSession, models().AppUser
        session_row = session.get(UserSession, _token_hash(token))
        row = session.get(User, session_row.user_id) if session_row else None
        if row is None or session_row.expires_at <= datetime.now(UTC) or not row.is_active:
            return None
        row_dict = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        session_row.last_seen_at = datetime.now(UTC)
        session_row.expires_at = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
        return _row(row_dict)


def sign_out(token: str | None) -> None:
    if token:
        with session_scope() as session:
            row = session.get(models().UserSession, _token_hash(token))
            if row is not None:
                session.delete(row)


def revoke_all(user_id: int) -> int:
    with session_scope() as session:
        Session = models().UserSession
        rows = session.query(Session).filter_by(user_id=user_id).all()
        for row in rows:
            session.delete(row)
        return len(rows)



#: Recognisable in a log or a paste, and greppable in a leaked file.
TOKEN_PREFIX = "gss_"  # noqa: S105 - a prefix, not a secret


def create_token(user_id: int, name: str, days: int | None = None) -> tuple[str, dict]:
    """Mint a personal token. Returns (secret, row); the secret is not stored."""
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
        return [{"id": row.id, "prefix": row.prefix, "name": row.name,
                 "created_at": row.created_at, "expires_at": row.expires_at,
                 "last_used_at": row.last_used_at}
                for row in session.query(Token).filter_by(user_id=user_id)
                .order_by(Token.created_at.desc()).all()]


def delete_token(token_id: int, user_id: int) -> bool:
    """Scoped to the owner: a token id is not a capability to revoke it."""
    with session_scope() as session:
        Token = models().ApiToken
        row = session.query(Token).filter_by(id=token_id, user_id=user_id).one_or_none()
        if row is None:
            return False
        session.delete(row)
        return True


def token_user(secret: str | None) -> dict | None:
    """The user behind a bearer token, or None."""
    if not secret or not secret.startswith(TOKEN_PREFIX):
        return None
    with session_scope() as session:
        Token, User = models().ApiToken, models().AppUser
        token_row = session.query(Token).filter_by(token_hash=_token_hash(secret)).one_or_none()
        row = session.get(User, token_row.user_id) if token_row else None
        if row is None or not row.is_active or (
            token_row.expires_at is not None and token_row.expires_at <= datetime.now(UTC)
        ):
            return None
        row_dict = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        token_row.last_used_at = datetime.now(UTC)
        return _row(row_dict)



def ensure_admin() -> str | None:
    """Make the account named in the environment exist, and return its email.

    This is the only way a deployment gets its first user. With both variables
    set the account is created, or its password brought back into line, every
    time the process starts -- which is also how a forgotten password is reset.
    With neither set nothing is created, and a deployment with no users answers
    every read without asking who is calling.
    """
    from git_synapse.config import get_config

    cfg = get_config().server
    email, password = cfg.admin_email.strip(), cfg.admin_password
    if not email or not password:
        return None
    if not _EMAIL.match(email):
        raise AuthError(f"ADMIN_EMAIL {email!r} does not look like an email address")

    with session_scope() as session:
        User = models().AppUser
        row = session.query(User).filter(func.lower(User.email) == email.lower()).one_or_none()
        if row is None:
            session.add(User(email=email, name=email.split("@")[0], role="admin",
                             password_hash=hash_password(password)))
            log.info("administrator %s created from the environment", email)
            return email
        # An operator who edits the variable means it: bring the stored account back
        # to what the file says, including the role, and leave everything else.
        if not verify_password(password, row.password_hash):
            row.password_hash = hash_password(password)
            log.info("administrator %s password reset from the environment", email)
        row.role = "admin"
        row.is_active = True
    return email


ACCESS_MODES = ("required", "open")


def access_mode(surface: str) -> str:
    """Whether `dashboard` or `mcp` currently requires a caller to identify."""
    from git_synapse.analysis import settings

    if count_users() == 0:
        return "open"
    value = settings.effective(f"{surface}_auth", "required")
    return value if value in ACCESS_MODES else "required"


def prune_sessions() -> int:
    """Drop expired sessions. Called from the ingest run, like the call log."""
    with session_scope() as session:
        Session = models().UserSession
        now = datetime.now(UTC)
        rows = [row for row in session.query(Session).all()
                if row.expires_at is not None and row.expires_at < now]
        for row in rows:
            session.delete(row)
        return len(rows)
