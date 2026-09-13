"""Who may read this deployment, and how that is proven.

The dashboard is derived from public repositories but is not itself public: it
says which repositories an organisation tracks, where its coupling is weakest,
and which files one person alone understands. These cover the parts that would
be quietly wrong rather than loudly broken -- a password that verifies when it
should not, a session that outlives the account, a door that can be walked
around.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from git_synapse import auth


def _email() -> str:
    return f"pytest-{uuid4().hex[:10]}@example.com"


@pytest.fixture
def person(db):
    """One member, removed afterwards so the deployment goes back to open."""
    user = auth.create_user(_email(), "Test Person", "a-sufficiently-long-pass")
    yield user
    auth.delete_user(user["id"])


# ------------------------------------------------------------------ hashing

def test_a_password_verifies_only_against_itself():
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", stored)
    assert not auth.verify_password("Correct horse battery staple", stored)
    assert not auth.verify_password("", stored)


def test_two_hashes_of_one_password_differ():
    """A per-password salt: identical passwords must not produce identical
    rows, or the table tells an attacker who to attack once."""
    a = auth.hash_password("the same password twice")
    b = auth.hash_password("the same password twice")
    assert a != b
    assert auth.verify_password("the same password twice", a)
    assert auth.verify_password("the same password twice", b)


def test_the_hash_records_its_own_cost():
    """Stored parameters mean the cost can be raised later without
    invalidating every existing password."""
    algo, n, r, p, salt, digest = auth.hash_password("a long enough password").split("$")
    assert algo == "scrypt" and int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1
    assert salt and digest


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "scrypt$x$y$z$q$w", "md5$1$1$1$a$b"])
def test_a_malformed_hash_fails_closed(garbage):
    """A corrupt or foreign row must reject, never raise and never accept."""
    assert auth.verify_password("anything", garbage) is False


def test_a_short_password_is_refused_where_it_is_set():
    with pytest.raises(auth.AuthError, match="at least"):
        auth.hash_password("short")


# ------------------------------------------------------------------- people

def test_a_person_can_sign_in_and_the_session_finds_them(person):
    token, user = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    assert user["email"] == person["email"]
    assert "password_hash" not in user, "the hash must never leave this module"
    assert auth.session_user(token)["id"] == person["id"]


def test_an_unknown_address_and_a_wrong_password_are_the_same_answer(person):
    """A different message, or a faster one, tells an attacker which addresses
    are real. The dummy hash makes the unknown path do the same work."""
    with pytest.raises(auth.AuthError) as wrong:
        auth.sign_in(person["email"], "not-the-password")
    with pytest.raises(auth.AuthError) as missing:
        auth.sign_in("nobody@example.com", "not-the-password")
    assert str(wrong.value) == str(missing.value) == "wrong email or password"


def test_a_deactivated_person_cannot_sign_in_and_their_sessions_end(person):
    token, _ = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    assert auth.session_user(token) is not None

    auth.update_user(person["id"], is_active=False)
    assert auth.session_user(token) is None, "deactivation must end live sessions"
    with pytest.raises(auth.AuthError, match="deactivated"):
        auth.sign_in(person["email"], "a-sufficiently-long-pass")


def test_changing_a_password_ends_every_session(person):
    """Otherwise the change is advisory: whoever knew the old password is still
    signed in, which is the situation the change was meant to end."""
    token, _ = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    auth.update_user(person["id"], password="a-different-long-password")
    assert auth.session_user(token) is None


def test_signing_out_ends_that_session_only(person):
    first, _ = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    second, _ = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    auth.sign_out(first)
    assert auth.session_user(first) is None
    assert auth.session_user(second) is not None


def test_an_expired_session_is_nobody(person):
    from datetime import UTC, datetime, timedelta

    from git_synapse.db.orm import models, session_scope

    token, _ = auth.sign_in(person["email"], "a-sufficiently-long-pass")
    with session_scope() as session:
        session.query(models().UserSession).filter_by(user_id=person["id"]).update(
            {models().UserSession.expires_at: datetime.now(UTC) - timedelta(hours=1)},
            synchronize_session=False,
        )
    assert auth.session_user(token) is None
    assert auth.prune_sessions() >= 1


def test_a_duplicate_address_is_refused_whatever_its_case(person):
    with pytest.raises(auth.AuthError, match="already has an account"):
        auth.create_user(person["email"].upper(), "Someone Else", "another-long-password")


@pytest.mark.parametrize("email", ["not-an-email", "@example.com", "a@b", "a b@c.com"])
def test_an_address_that_cannot_be_one_is_refused(db, email):
    with pytest.raises(auth.AuthError, match="email address"):
        auth.create_user(email, "Name", "a-sufficiently-long-pass")


def test_a_role_outside_the_two_is_refused(db):
    with pytest.raises(auth.AuthError, match="role must be"):
        auth.create_user(_email(), "Name", "a-sufficiently-long-pass", role="superuser")


def test_a_nameless_person_is_refused(db):
    with pytest.raises(auth.AuthError, match="name is required"):
        auth.create_user(_email(), "   ", "a-sufficiently-long-pass")


def test_updating_nothing_leaves_the_person_alone(person):
    same = auth.update_user(person["id"])
    assert same["name"] == person["name"] and same["role"] == person["role"]


def test_updating_rejects_what_creating_rejects(person):
    with pytest.raises(auth.AuthError, match="role must be"):
        auth.update_user(person["id"], role="wizard")
    with pytest.raises(auth.AuthError, match="name is required"):
        auth.update_user(person["id"], name="  ")


def test_admin_count_can_ignore_one(db):
    a = auth.create_user(_email(), "A", "a-sufficiently-long-pass", role="admin")
    b = auth.create_user(_email(), "B", "a-sufficiently-long-pass", role="admin")
    try:
        assert auth.admin_count() >= 2
        assert auth.admin_count(exclude=a["id"]) >= 1
    finally:
        auth.delete_user(a["id"])
        auth.delete_user(b["id"])


# ------------------------------------------------------------------- tokens

def test_a_token_acts_as_the_person_who_made_it(person):
    secret, row = auth.create_token(person["id"], "laptop agent")
    assert secret.startswith(auth.TOKEN_PREFIX)
    assert row["prefix"] in secret and len(row["prefix"]) < len(secret)

    who = auth.token_user(secret)
    assert who["id"] == person["id"] and who["role"] == person["role"]
    assert "password_hash" not in who


def test_the_token_secret_is_not_recoverable(person):
    """Stored as a hash: whoever holds the database cannot use the tokens in
    it, and a listing is not a set of working credentials."""
    from git_synapse.db.orm import models, session_scope

    secret, _ = auth.create_token(person["id"], "a token")
    with session_scope() as session:
        row = session.query(models().ApiToken).filter_by(user_id=person["id"]).first()
    assert secret not in str(row.__dict__)
    assert row.token_hash != secret


@pytest.mark.parametrize("bad", [None, "", "not-a-token", "gss_wrong", "Bearer x"])
def test_a_token_that_is_not_one_is_nobody(db, bad):
    assert auth.token_user(bad) is None


def test_a_revoked_token_stops_working(person):
    secret, row = auth.create_token(person["id"], "temporary")
    assert auth.token_user(secret) is not None
    assert auth.delete_token(row["id"], person["id"])
    assert auth.token_user(secret) is None


def test_a_token_cannot_be_revoked_by_someone_else(person, db):
    other = auth.create_user(_email(), "Other", "a-sufficiently-long-pass")
    try:
        _, row = auth.create_token(person["id"], "mine")
        assert not auth.delete_token(row["id"], other["id"]), \
            "a token id must not be a capability to revoke it"
    finally:
        auth.delete_user(other["id"])


def test_an_expired_token_is_nobody(person):
    from datetime import UTC, datetime, timedelta

    from git_synapse.db.orm import models, session_scope

    secret, _ = auth.create_token(person["id"], "short-lived", days=1)
    with session_scope() as session:
        session.query(models().ApiToken).filter_by(user_id=person["id"]).update(
            {models().ApiToken.expires_at: datetime.now(UTC) - timedelta(days=1)},
            synchronize_session=False,
        )
    assert auth.token_user(secret) is None


def test_a_token_dies_with_its_owner(db):
    user = auth.create_user(_email(), "Temp", "a-sufficiently-long-pass")
    secret, _ = auth.create_token(user["id"], "goes away")
    auth.delete_user(user["id"])
    assert auth.token_user(secret) is None


def test_a_nameless_token_is_refused(person):
    with pytest.raises(auth.AuthError, match="name"):
        auth.create_token(person["id"], "  ")


def test_first_admin_claim_is_validated_and_single_use(db):
    secret = auth.setup_token()
    with pytest.raises(auth.AuthError, match="name"):
        auth.claim_first_admin("first@example.com", " ",
                               "a-sufficiently-long-pass", secret)
    with pytest.raises(auth.AuthError, match="setup token"):
        auth.claim_first_admin("first@example.com", "First",
                               "a-sufficiently-long-pass", "wrong")
    user = auth.create_user("already@example.com", "Already",
                            "a-sufficiently-long-pass")
    with pytest.raises(auth.SetupAlreadyClaimed):
        auth.claim_first_admin("first@example.com", "First",
                               "a-sufficiently-long-pass", secret)
    auth.delete_user(user["id"])


# ------------------------------------------------------------ access policy

def test_with_nobody_registered_the_door_is_open(db):
    """Requiring sign-in when no account exists would lock the first
    administrator out of the screen that creates them."""
    assert auth.count_users() == 0, "this test needs a deployment with no users"
    assert auth.access_mode("dashboard") == "open"


def test_once_someone_exists_the_default_is_to_require_it(person):
    assert auth.access_mode("dashboard") == "required"


def test_an_administrator_can_open_and_close_the_door(person):
    from git_synapse.analysis import settings

    try:
        settings.set("dashboard_auth", "open")
        assert auth.access_mode("dashboard") == "open"
        settings.set("dashboard_auth", "required")
        assert auth.access_mode("dashboard") == "required"
    finally:
        settings.clear("dashboard_auth")


def test_a_stored_mode_that_is_not_one_falls_closed(person):
    """Written by hand, or by a future version. Falling open would turn a typo
    into a public dashboard."""
    from git_synapse.analysis import settings

    try:
        settings.set("dashboard_auth", "whatever")
        assert auth.access_mode("dashboard") == "required"
    finally:
        settings.clear("dashboard_auth")


# ------------------------------------------------------- guessing a password

def test_a_password_cannot_be_guessed_at_machine_speed(person):
    """scrypt costs about 70ms an attempt, which throttles one attacker on one
    thread and does nothing about a thousand in parallel."""
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.sign_in(person["email"], "not-the-password")

    with pytest.raises(auth.TooManyAttempts, match="too many failed attempts"):
        auth.sign_in(person["email"], "not-the-password")

    # Refused even with the right password: the point is that credentials are
    # no longer being examined at all.
    with pytest.raises(auth.TooManyAttempts):
        auth.sign_in(person["email"], "a-sufficiently-long-pass")


def test_the_lockout_is_scoped_to_one_address(person, db):
    """Otherwise a handful of guesses at one account closes the door on
    everyone, which is a denial of service rather than a defence."""
    other = auth.create_user(_email(), "Other", "a-sufficiently-long-pass")
    try:
        for _ in range(auth.MAX_FAILURES + 1):
            with pytest.raises(auth.AuthError):
                auth.sign_in(person["email"], "wrong")
        # The other account still works.
        token, _ = auth.sign_in(other["email"], "a-sufficiently-long-pass")
        assert token
    finally:
        auth.delete_user(other["id"])


def test_signing_in_clears_the_count(person):
    """A stale count would lock someone out on their next typo, long after
    they proved it was them."""
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            auth.sign_in(person["email"], "wrong")
    assert auth.recent_failures(person["email"]) == auth.MAX_FAILURES - 1

    auth.sign_in(person["email"], "a-sufficiently-long-pass")
    assert auth.recent_failures(person["email"]) == 0


def test_attempts_past_the_window_stop_counting(person):
    from datetime import UTC, datetime, timedelta

    from git_synapse.db.orm import models, session_scope

    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.sign_in(person["email"], "wrong")
    with session_scope() as session:
        session.query(models().LoginAttempt).filter_by(email=person["email"]).update(
            {models().LoginAttempt.at: datetime.now(UTC) - timedelta(minutes=auth.LOCKOUT_MINUTES + 1)},
            synchronize_session=False,
        )
    assert auth.recent_failures(person["email"]) == 0
    assert auth.prune_login_attempts() >= auth.MAX_FAILURES
    # And the door opens again.
    assert auth.sign_in(person["email"], "a-sufficiently-long-pass")[0]


def test_deleting_a_user_who_is_not_there_reports_it_rather_than_raising(db):
    """The caller is a DELETE endpoint: "nobody by that id" is a 404 it renders,
    not an exception it has to catch."""
    from git_synapse import auth

    assert auth.delete_user(999_999_999) is False


def test_the_first_admin_can_be_claimed_before_the_schema_row_exists(scratch_db):
    """The claim locks the schema row to serialise two simultaneous first-run
    requests. On a database where bootstrap has not written it yet, the lock
    has to be created rather than waited for."""
    from git_synapse import auth
    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        for model in (models().UserSession, models().ApiToken, models().LoginAttempt):
            session.query(model).delete(synchronize_session=False)
        session.query(models().AppUser).delete(synchronize_session=False)
        row = session.get(models().Meta, "schema_version")
        if row is not None:
            session.delete(row)

    token = auth.setup_token()
    _session_token, user = auth.claim_first_admin(
        "first@example.com", "First Admin", "a-long-password-1234", token)
    assert user["email"] == "first@example.com"
    auth.delete_user(user["id"])

    with session_scope() as session:
        assert session.get(models().Meta, "schema_version") is not None
