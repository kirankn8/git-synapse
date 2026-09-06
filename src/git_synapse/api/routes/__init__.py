"""REST endpoints. One router, grouped by resource."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from git_synapse import auth, vault
from git_synapse.analysis import calls, mining, predict, settings
from git_synapse.analysis import query as q
from git_synapse.config import get_config, live_cron
from git_synapse.db.engine import query_one, scalar
from git_synapse.ingest import accounts, pipeline
from git_synapse.ingest.accounts import AccountError
from git_synapse.ingest.sources import SourceError
from git_synapse.stats.registry import DEFAULT_MEASURE
from git_synapse.vault import VaultError

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & meta
# ---------------------------------------------------------------------------


@router.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness probe. Reports database reachability without failing the check."""
    try:
        db_ok = scalar("SELECT 1") == 1
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"status": "degraded", "database": False, "error": str(exc)}
    return {"status": "ok", "database": db_ok}


@router.get("/overview", tags=["meta"])
def overview() -> dict:
    """Headline counts across the whole corpus."""
    return q.overview()


@router.get("/measures", tags=["meta"])
def measures() -> dict:
    """The full catalogue of association measures, with guidance on each."""
    catalog = q.measure_catalog()
    return {
        "default": DEFAULT_MEASURE,
        "count": len(catalog),
        "measures": catalog,
    }


@router.get("/config", tags=["meta"])
def config() -> dict:
    """Effective tuning parameters, so the UI can explain what it is showing."""
    cfg = get_config()
    return {
        # Accounts are configured in the database; this is only the seed value
        # a fresh deployment adopts on its first discovery.
        "default_org": cfg.github.org,
        "max_files_per_commit": cfg.ingest.max_files_per_commit,
        "min_pair_support": cfg.ingest.min_pair_support,
        "include_merges": cfg.ingest.include_merges,
        "rename_similarity": cfg.ingest.rename_similarity,
        "blobless_threshold_kb": cfg.ingest.blobless_threshold_kb,
        "recency_half_life_days": cfg.analysis.recency_half_life_days,
        "crossrepo_enabled": cfg.crossrepo.enabled,
        "chain_min_confidence": cfg.crossrepo.chain_min_confidence,
        "chain_max_depth": cfg.crossrepo.chain_max_depth,
        # The value in force, not the seed: a schedule set from the UI is
        # stored, and reporting the environment's here would tell the reader a
        # cadence nothing runs on.
        "refresh_cron": live_cron("refresh"),
        "discover_cron": live_cron("discover"),
        "scheduler_timezone": cfg.schedule.timezone,
        "scheduler_enabled": cfg.schedule.enabled,
        # Which of these the UI may change, and whether each is currently
        # overridden or still coming from the environment.
        "editable": {
            name: {"value": live_cron(name.removesuffix("_cron")),
                   "overridden": settings.get(name) is not None,
                   "from_env": getattr(cfg.schedule, "cron" if name == "refresh_cron"
                                       else "discover_cron")}
            for name in settings.SCHEDULES
        },
        # Never the token itself -- only whether one is present and how it got
        # here, so the UI can say "set" without being able to read it back.
        "github_token": _token_status(),
    }


def _token_status() -> dict:
    """Whether a token is configured, and from where. Never its value."""
    gh = get_config().github
    from pathlib import Path as _Path

    path = _Path(gh.token_file) if gh.token_file else None
    file_has = bool(path and path.is_file() and path.read_text(encoding="utf-8").strip())
    return {
        "present": bool(gh.current_token()),
        "source": "file" if file_has else ("environment" if gh.token else "none"),
        # A host-managed file is authoritative and rotates on its own; saying so
        # stops a reader pasting a token that will be ignored.
        "editable_here": not file_has,
    }


class SettingIn(BaseModel):
    """One operational setting. Blank clears the override."""

    value: str = Field(default="", max_length=200)


@router.get("/settings", tags=["meta"])
def list_settings(request: Request) -> dict:
    """The operational settings the UI may change, and what they are now."""
    _require(request)
    cfg = get_config().schedule
    return {
        "access": {
            surface: auth.access_mode(surface) for surface in ("dashboard", "mcp")
        },
        "settings": [
            {
                "name": name,
                "value": live_cron(name.removesuffix("_cron")),
                "overridden": settings.get(name) is not None,
                "from_env": cfg.cron if name == "refresh_cron" else cfg.discover_cron,
            }
            for name in settings.SCHEDULES
        ]
    }


@router.put("/settings/{name}", tags=["meta"])
def put_setting(name: str, body: SettingIn, request: Request) -> dict:
    """Store an operational setting, or clear it back to the environment's.

    Validated here rather than at the scheduler: a cron that does not parse
    would otherwise be accepted, stored, and then silently ignored once a
    minute by a process the reader is not watching.
    """
    # Changing the schedule affects the corpus; changing the access policy
    # affects who can see it. Both are an administrator's call.
    _require(request, admin=True)
    if name not in settings.WRITABLE:
        raise HTTPException(404, f"{name!r} is not a settable option")

    value = body.value.strip()
    if name.endswith("_auth"):
        if value and value not in auth.ACCESS_MODES:
            raise HTTPException(422, f"access must be one of {', '.join(auth.ACCESS_MODES)}")
        if name == "dashboard_auth" and value == "open":
            log.warning("dashboard sign-in switched OFF by %s", _require(request)["email"])
        if value:
            settings.set(name, value)
        else:
            settings.clear(name)
        return {"name": name, "value": auth.access_mode(name[:-5]), "overridden": bool(value)}
    if not value:
        settings.clear(name)
        return {"name": name, "value": live_cron(name.removesuffix("_cron")),
                "overridden": False}

    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(value, timezone=get_config().schedule.timezone)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"not a five-field cron expression: {exc}") from exc

    settings.set(name, value)
    return {"name": name, "value": value, "overridden": True}


# ---------------------------------------------------------------------------
# Sign-in, and the people who may sign in
# ---------------------------------------------------------------------------

#: The session cookie. Host-only, so it is never sent to a sibling subdomain.
COOKIE = "gs_session"


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=auth.MIN_PASSWORD, max_length=200)
    role: str = "member"


class FirstUser(NewUser):
    """The first administrator, who has nobody to be authorised by.

    The setup token stands in for the admin who does not exist yet. It proves
    the caller can read the deployment's console or its secret store, which is
    the same thing being an administrator will later mean.
    """

    setup_token: str = Field(min_length=1, max_length=200)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=auth.MIN_PASSWORD, max_length=200)


def _set_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        COOKIE, token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,          # script cannot read it, so XSS cannot lift it
        samesite="lax",         # sent on navigation, not on a cross-site POST
        secure=secure,          # only withheld on plain http, where it is moot
        path="/",
    )


@router.get("/auth/me", tags=["auth"])
def whoami(request: Request) -> dict:
    """Who is signed in, and whether anyone exists yet.

    Answers for the signed-out caller too: the UI needs to know whether to show
    a sign-in form or a first-run setup screen, and that must not require being
    signed in already.
    """
    user = caller(request)
    needs_setup = auth.count_users() == 0
    return {
        "user": user,
        "authenticated": user is not None,
        "needs_setup": needs_setup,
        # Where to tell the reader to look for the setup token, which differs:
        # a minted one is in the API log, a configured one is wherever the
        # deployment keeps its secrets. Never the token itself -- the whole
        # point is that reading it requires access this endpoint does not.
        "setup_token_minted": needs_setup and auth.setup_token_is_minted(),
        # The UI needs this to know whether a signed-out visitor should see a
        # sign-in form or the dashboard.
        "auth_required": auth.access_mode("dashboard") == "required",
    }


@router.post("/auth/login", tags=["auth"])
def login(body: Credentials, request: Request, response: Response) -> dict:
    try:
        token, user = auth.sign_in(body.email, body.password,
                                   request.headers.get("user-agent"))
    except auth.TooManyAttempts as exc:
        # 429, not 401: the credentials were never examined, and telling the
        # caller to wait is different from telling them they are wrong.
        raise HTTPException(429, str(exc)) from exc
    except auth.AuthError as exc:
        # 401 rather than 400: the credentials were the problem, and the client
        # distinguishes the two.
        raise HTTPException(401, str(exc)) from exc
    _set_cookie(response, token, request.url.scheme == "https")
    return {"user": user}


@router.post("/auth/logout", tags=["auth"])
def logout(request: Request, response: Response) -> dict:
    auth.sign_out(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"signed_out": True}


@router.post("/auth/setup", tags=["auth"], status_code=201)
def setup(body: FirstUser, request: Request, response: Response) -> dict:
    """Create the first administrator, once, for whoever holds the setup token.

    A password in the environment would sit in a shell history, a compose file
    and every process listing; this asks for one at the console instead. The
    endpoint refuses as soon as a single user exists, so it cannot be used to
    add a second administrator later.

    The window between a migrated database and a claimed account is the one
    moment nothing is signed in, and the screen that ends it is by necessity
    reachable without signing in. The token is what stops a stranger who
    reaches the port first from becoming the administrator of the deployment.
    """
    if auth.count_users() > 0:
        raise HTTPException(409, "this deployment already has users")
    try:
        auth.check_setup_token(body.setup_token)
    except auth.TooManyAttempts as exc:
        raise HTTPException(429, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(403, str(exc)) from exc
    try:
        user = auth.create_user(body.email, body.name, body.password, role="admin")
        token, _ = auth.sign_in(body.email, body.password,
                                request.headers.get("user-agent"))
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    # It authorised the one thing it exists for. Keeping it would leave a
    # standing secret in the table that nothing will ever check again.
    auth.clear_setup_token()
    _set_cookie(response, token, request.url.scheme == "https")
    return {"user": user}


def caller(request: Request) -> dict | None:
    """Whoever is making this request: a browser session, or a bearer token.

    Both carry a person, so everything downstream -- roles, the call log, the
    audit of who added whom -- works the same either way.
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        user = auth.token_user(header[7:].strip())
        if user is not None:
            return user
    return auth.session_user(request.cookies.get(COOKIE))


def _require(request: Request, admin: bool = False) -> dict:
    user = caller(request)
    if user is None:
        # With sign-in switched off there is still nobody to attribute an
        # administrative act to, so these endpoints always need a caller.
        raise HTTPException(401, "sign in to continue")
    if admin and user["role"] != "admin":
        raise HTTPException(403, "only an administrator can do that")
    return user


class NewToken(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    #: Optional lifetime. A token that never expires is a key left in a door.
    days: int | None = Field(default=None, ge=1, le=730)


@router.get("/auth/tokens", tags=["auth"])
def my_tokens(request: Request) -> dict:
    me = _require(request)
    return {"tokens": auth.list_tokens(me["id"]), "prefix": auth.TOKEN_PREFIX}


@router.post("/auth/tokens", tags=["auth"], status_code=201)
def mint_token(body: NewToken, request: Request) -> dict:
    """Create a personal token. The secret is returned once and never again.

    It carries the maker's identity and role, so it can do what they can do and
    nothing more, and it stops working when their account does.
    """
    me = _require(request)
    try:
        secret, row = auth.create_token(me["id"], body.name, body.days)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"token": secret, "detail": row,
            "note": "Copy this now — it is stored only as a hash and cannot be shown again."}


@router.delete("/auth/tokens/{token_id}", tags=["auth"])
def revoke_token(token_id: int, request: Request) -> dict:
    me = _require(request)
    if not auth.delete_token(token_id, me["id"]):
        raise HTTPException(404, f"token {token_id} not found")
    return {"deleted": token_id}


@router.get("/users", tags=["auth"])
def list_users(request: Request) -> dict:
    """Everyone may see who has access; only an administrator may change it."""
    _require(request)
    return {"users": auth.list_users(), "roles": list(auth.ROLES)}


@router.post("/users", tags=["auth"], status_code=201)
def create_user(body: NewUser, request: Request) -> dict:
    me = _require(request, admin=True)
    try:
        return auth.create_user(body.email, body.name, body.password,
                                role=body.role, created_by=me["id"])
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/users/{user_id}", tags=["auth"])
def patch_user(user_id: int, body: UserPatch, request: Request) -> dict:
    _require(request, admin=True)
    if auth.get_user(user_id) is None:
        raise HTTPException(404, f"user {user_id} not found")
    # Demoting or deactivating the last administrator leaves a deployment
    # nobody can add a person to, and no way back except the database.
    losing_admin = body.role == "member" or body.is_active is False
    if losing_admin and auth.admin_count(exclude=user_id) == 0:
        raise HTTPException(409, "this is the only administrator")
    try:
        return auth.update_user(user_id, **body.model_dump(exclude_unset=True))
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/users/{user_id}", tags=["auth"])
def delete_user(user_id: int, request: Request) -> dict:
    me = _require(request, admin=True)
    if user_id == me["id"]:
        raise HTTPException(409, "you cannot remove your own account")
    if auth.get_user(user_id) is None:
        raise HTTPException(404, f"user {user_id} not found")
    # No "last administrator" check here, unlike the patch above: the caller is
    # an active administrator by definition, and cannot be the person being
    # removed, so one always remains. Demotion is the case that needs guarding,
    # because there you can demote yourself.
    auth.delete_user(user_id)
    return {"deleted": user_id}


# ---------------------------------------------------------------------------
# Accounts: the orgs and users whose repositories get scanned
# ---------------------------------------------------------------------------


class AccountIn(BaseModel):
    """A new source to scan, described field by field.

    Most callers want :class:`SourceIn` instead, which takes a URL. This is the
    explicit form, for a caller that already knows every part.
    """

    login: str = Field(min_length=1, max_length=200)
    kind: str = "org"
    provider: str = "github"
    host: str = "github.com"
    api_url: str | None = None
    enabled: bool = True
    include_private: bool = True
    #: A fork's history is its parent's history, so tracking both stores the
    #: same commits twice and ranks a second copy of every coupling as if it
    #: were independent evidence.
    include_forks: bool = False
    include_archived: bool = True
    only_repos: list[str] = Field(default_factory=list)
    skip_repos: list[str] = Field(default_factory=list)


class AccountPatch(BaseModel):
    """Partial update. Unset fields are left as they are."""

    login: str | None = None
    kind: str | None = None
    api_url: str | None = None
    enabled: bool | None = None
    include_private: bool | None = None
    include_forks: bool | None = None
    include_archived: bool | None = None
    only_repos: list[str] | None = None
    skip_repos: list[str] | None = None


class SourceIn(BaseModel):
    """Something a person pasted, plus what they chose to track from it.

    No filter flags. In a URL-driven flow the URL and the ticks are the whole
    answer: a repository someone named is one they want, fork or not, and a
    toggle beside it could only contradict them. Filters apply to the one case
    where nothing was named -- tracking a whole owner -- and there the answer
    is fixed rather than asked, because a fork's history is its parent's.
    """

    url: str = Field(min_length=1, max_length=2000)
    #: Which repositories under the owner. An empty list means *everything*,
    #: including repositories created later -- the one intent an allowlist
    #: cannot express. Omitted entirely for a repository URL, which names one.
    repos: list[str] | None = None
    #: This source's own access token, for a private repository. Stored
    #: encrypted; never returned. Empty leaves the deployment-wide credential
    #: in the environment as the only one.
    token: str = Field(default="", max_length=500)


class ResolveIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    #: Which page of an owner's repositories. Every host caps a listing at a
    #: hundred, so a large organisation is fetched a page at a time while the
    #: reader is already looking at the first one.
    page: int = Field(default=1, ge=1, le=500)
    #: Tried but not stored. The whole point of a lookup is to find out whether
    #: a credential works before committing to it.
    token: str = Field(default="", max_length=500)


@router.get("/accounts", tags=["accounts"])
def list_accounts(enabled_only: bool = False) -> dict:
    """Every configured source, with how many repositories it has produced."""
    rows = accounts.list_accounts(enabled_only)
    return {"count": len(rows), "kinds": list(accounts.KINDS), "accounts": rows}


@router.post("/accounts/resolve", tags=["accounts"])
def resolve_source(body: ResolveIn) -> dict:
    """What is at this URL, and what could be tracked from it.

    Reads only. A repository URL comes back as one already-fetched repository
    to confirm; an owner URL comes back with the repositories under it, for a
    person to tick. Nothing is written until :func:`create_source`.
    """
    try:
        return {**accounts.resolve_url(body.url, token=body.token, page=body.page),
                # Whether a token *could* be kept, so the form knows to offer.
                "can_store_token": vault.available()}
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AccountError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"could not read {body.url}: {exc}") from exc


@router.post("/accounts/from-url", tags=["accounts"], status_code=201)
def create_source(body: SourceIn) -> dict:
    """Track what a person pasted and chose. Discovery picks it up next run."""
    try:
        return accounts.add_from_url(body.url, repos=body.repos, token=body.token)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except AccountError as exc:
        raise HTTPException(409, str(exc)) from exc
    except VaultError as exc:
        # A token was offered and cannot be kept. Refusing is the point: the
        # alternative is silently adding the source without it, which then
        # fails to clone for a reason nothing on screen explains.
        raise HTTPException(422, str(exc)) from exc


@router.post("/accounts", tags=["accounts"], status_code=201)
def create_account(body: AccountIn) -> dict:
    """Add a source field by field. Discovery picks it up on the next run."""
    try:
        return accounts.add_account(**body.model_dump())
    except AccountError as exc:
        raise HTTPException(409, str(exc)) from exc


class CredentialIn(BaseModel):
    #: Empty clears it, which is the only way to take a token back out.
    token: str = Field(default="", max_length=500)


@router.put("/accounts/{account_id}/credential", tags=["accounts"])
def set_credential(account_id: int, body: CredentialIn) -> dict:
    """Replace or clear one source's access token. Never returns it."""
    if accounts.get_account(account_id) is None:
        raise HTTPException(404, f"account {account_id} not found")
    try:
        return accounts.set_credential(account_id, body.token) or {}
    except VaultError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/accounts/{account_id}", tags=["accounts"])
def get_account(account_id: int) -> dict:
    row = accounts.get_account(account_id)
    if row is None:
        raise HTTPException(404, f"account {account_id} not found")
    return row


@router.patch("/accounts/{account_id}", tags=["accounts"])
def patch_account(account_id: int, body: AccountPatch) -> dict:
    """Update an account's login or filters."""
    if accounts.get_account(account_id) is None:
        raise HTTPException(404, f"account {account_id} not found")
    try:
        return accounts.update_account(account_id, **body.model_dump(exclude_unset=True))
    except AccountError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/accounts/{account_id}", tags=["accounts"])
def delete_account(account_id: int) -> dict:
    """Stop scanning an account. Its repositories and statistics are kept."""
    if not accounts.remove_account(account_id):
        raise HTTPException(404, f"account {account_id} not found")
    return {"deleted": account_id}


# ---------------------------------------------------------------------------
# Callers: what asked for what, and what came back
# ---------------------------------------------------------------------------


@router.get("/calls/summary", tags=["calls"])
def calls_summary(
    hours: int = Query(24, ge=1, le=8760),
    surface: str | None = Query(None, pattern="^(mcp|http)$"),
    status: str | None = Query(None, pattern="^(ok|error)$"),
) -> dict:
    """Volume, failures and latency over a window, for the operator view.

    Honours ``surface``: without it the figures ignored the filter the reader
    had just set, so the page looked broken -- the list narrowed and everything
    above it stayed the same.
    """
    return {
        "summary": calls.summary(hours, surface=surface),
        "by_name": calls.by_name(surface=surface, hours=hours, status=status),
        "mcp_tools": len(calls.known_mcp_tools()),
    }


@router.get("/calls/timeline", tags=["calls"])
def calls_timeline(hours: int = Query(24, ge=1, le=168)) -> dict:
    """Calls per hour, empty hours included, for the activity chart."""
    return {"buckets": calls.timeline(hours)}


@router.get("/calls", tags=["calls"])
def list_calls(
    surface: str | None = Query(None, pattern="^(mcp|http)$"),
    name: str | None = None,
    status: str | None = Query(None, pattern="^(ok|error)$"),
    hours: int | None = Query(None, ge=1, le=8760),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """The call list, newest first, without payloads."""
    rows = calls.recent(surface=surface, name=name, status=status,
                        hours=hours, limit=limit)
    return {"count": len(rows), "calls": rows}


@router.get("/calls/{call_id}", tags=["calls"])
def get_call(call_id: int) -> dict:
    """One call in full: the arguments given, and the reply that went back."""
    row = calls.detail(call_id)
    if row is None:
        raise HTTPException(404, f"call {call_id} not found")
    return row


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


@router.get("/repos", tags=["repos"])
def list_repos(
    search: str | None = None,
    language: str | None = None,
    status: str | None = None,
    order_by: str = "commit_count",
    descending: bool = True,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    account_id: int | None = None,
) -> dict:
    """List repositories with ingest state and history summary."""
    rows = q.list_repos(search, language, status, order_by, descending, limit,
                        offset, account_id)
    return {"count": len(rows), "repos": rows}


@router.get("/overview/shape", tags=["meta"])
def corpus_shape() -> dict:
    """Distributions behind the headline numbers, for the landing page."""
    return q.corpus_shape()


@router.get("/repos/languages", tags=["repos"])
def languages() -> dict:
    return {"languages": q.repo_languages()}


@router.get("/repos/{repo_id}", tags=["repos"])
def get_repo(repo_id: int) -> dict:
    repo = q.get_repo(repo_id)
    if repo is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    return repo


@router.get("/repos/{repo_id}/files", tags=["repos"])
def repo_files(
    repo_id: int,
    search: str | None = None,
    extension: str | None = None,
    min_changes: int = 0,
    order_by: str = "change_count",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = q.search_files(search, repo_id, extension, min_changes, order_by, limit, offset)
    return {"count": len(rows), "files": rows}


@router.get("/repos/{repo_id}/tree", tags=["repos"])
def repo_tree(
    repo_id: int,
    path: str = "",
    limit: int = Query(1000, ge=1, le=5000),
) -> dict:
    """One level of a repository's tree, for browsing it as it is laid out."""
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    tree = q.directory_tree(repo_id, path.strip("/"), limit)
    if tree["directory"] is None and tree["path"]:
        raise HTTPException(404, f"no directory {path!r} in repository {repo_id}")
    return tree


@router.get("/repos/{repo_id}/directories", tags=["repos"])
def repo_directories(repo_id: int, limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {"directories": q.directories(repo_id, limit)}


@router.get("/repos/{repo_id}/extensions", tags=["repos"])
def repo_extensions(repo_id: int) -> dict:
    return {"extensions": q.file_extensions(repo_id)}


@router.get("/repos/{repo_id}/hotspots", tags=["repos"])
def repo_hotspots(repo_id: int, limit: int = Query(25, ge=1, le=200)) -> dict:
    return {"hotspots": q.hotspots(repo_id, limit)}


@router.get("/repos/{repo_id}/pairs", tags=["coupling"])
def repo_pairs(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(50, ge=1, le=1000),
    min_support: int = Query(3, ge=1),
) -> dict:
    """Strongest couplings inside one repository."""
    return {
        "measure": measure,
        "pairs": q.strongest_pairs(repo_id, measure, limit, min_support),
    }


@router.get("/repos/{repo_id}/graph", tags=["coupling"])
def repo_graph(
    repo_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(150, ge=1, le=2000),
    min_support: int = Query(2, ge=1),
    center_file_id: int | None = None,
    min_score: float | None = None,
) -> dict:
    """Node/edge graph of the strongest couplings, for the force-directed view."""
    return q.coupling_graph(repo_id, measure, limit, min_support, center_file_id, min_score)


# ---------------------------------------------------------------------------
# Files & coupling
# ---------------------------------------------------------------------------


@router.get("/files", tags=["files"])
def search_files(
    search: str | None = None,
    repo_id: int | None = None,
    extension: str | None = None,
    min_changes: int = 0,
    order_by: str = "change_count",
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    rows = q.search_files(search, repo_id, extension, min_changes, order_by, limit, offset)
    return {"count": len(rows), "files": rows}


@router.get("/files/resolve", tags=["files"])
def resolve_file(path: str, repo: str | None = None, repo_id: int | None = None) -> dict:
    """Look a file up by repo and path, following renames through the alias table.

    Name the repository either way: ``repo`` for humans and agents, ``repo_id``
    for the UI, whose URLs address files by path so they survive a re-ingest.
    """
    if (repo is None) == (repo_id is None):
        raise HTTPException(400, "give exactly one of repo or repo_id")
    row = q.resolve_file(repo, path, repo_id)
    if row is None:
        raise HTTPException(404, f"no file {path!r} in repository {repo or repo_id!r}")
    return row


@router.get("/files/{file_id}", tags=["files"])
def get_file(file_id: int) -> dict:
    row = q.get_file(file_id)
    if row is None:
        raise HTTPException(404, f"file {file_id} not found")
    return row


@router.get("/files/{file_id}/coupled", tags=["coupling"])
def coupled(
    file_id: int,
    measure: str = DEFAULT_MEASURE,
    limit: int = Query(25, ge=1, le=1000),
    min_support: int = Query(1, ge=1),
    min_score: float | None = None,
) -> dict:
    """Files that historically change together with this one, ranked.

    The central question of the product: "I am editing this, what else must
    change?"
    """
    if q.get_file(file_id) is None:
        raise HTTPException(404, f"file {file_id} not found")
    return {
        "file_id": file_id,
        "measure": measure,
        "partners": q.coupled_files(file_id, measure, limit, min_support, min_score),
    }


@router.get("/files/{file_id}/commits", tags=["files"])
def file_commits(file_id: int, limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"commits": q.file_commits(file_id, limit)}


@router.get("/files/{file_id}/authors", tags=["files"])
def file_authors(file_id: int, limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"authors": q.file_authors(file_id, limit)}


@router.get("/pairs/{file_a_id}/{file_b_id}", tags=["coupling"])
def pair_detail(file_a_id: int, file_b_id: int) -> dict:
    """Full contingency table and every measure for one pair."""
    row = q.pair_detail(file_a_id, file_b_id)
    if row is None:
        raise HTTPException(404, "no recorded coupling between those files")
    return row


@router.get("/pairs/{file_a_id}/{file_b_id}/commits", tags=["coupling"])
def pair_commits(
    file_a_id: int, file_b_id: int, limit: int = Query(25, ge=1, le=200)
) -> dict:
    """The commits where both files changed -- the evidence behind the score."""
    return {"commits": q.co_change_commits(file_a_id, file_b_id, limit)}


@router.get("/directories/{dir_id}/coupled", tags=["coupling"])
def coupled_dirs(
    dir_id: int, measure: str = DEFAULT_MEASURE, limit: int = Query(25, ge=1, le=500)
) -> dict:
    """Directories that change with this one, and which directory it is.

    The identity is returned because without it the page is an orphan: it can
    rank partners but cannot say whose directory this is, so a reader arriving
    from anywhere has no way back up to the repository or its account.
    """

    row = query_one(
        """
        SELECT d.id, d.path, d.repo_id, r.name AS repo, r.full_name, r.account_id
          FROM directory d JOIN repo r ON r.id = d.repo_id
         WHERE d.id = %(dir)s
        """,
        {"dir": dir_id},
    )
    if row is None:
        raise HTTPException(404, f"no directory {dir_id}")
    return {"measure": measure, "directory": row,
            "partners": q.coupled_directories(dir_id, measure, limit)}


@router.get("/pairs", tags=["coupling"])
def top_pairs(
    measure: str = DEFAULT_MEASURE,
    repo_id: int | None = None,
    limit: int = Query(50, ge=1, le=1000),
    min_support: int = Query(3, ge=1),
) -> dict:
    """Strongest couplings, optionally across the whole org."""
    return {
        "measure": measure,
        "pairs": q.strongest_pairs(repo_id, measure, limit, min_support),
    }


@router.get("/hotspots", tags=["files"])
def hotspots(repo_id: int | None = None, limit: int = Query(25, ge=1, le=200)) -> dict:
    return {"hotspots": q.hotspots(repo_id, limit)}


# ---------------------------------------------------------------------------
# Impact prediction
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/impact", tags=["impact"])
def repo_impact(
    repo_id: int,
    direction: str = Query("downstream", pattern="^(downstream|upstream)$"),
    limit: int = Query(20, ge=1, le=200),
    declared_only: bool = False,
) -> dict:
    """What else to look at when changing this repository.

    ``downstream`` is what a change here forces others to update; ``upstream`` is
    where a change here may actually belong.
    """
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    rows = (
        predict.upstream_of(repo_id, limit=limit)
        if direction == "upstream"
        else predict.impact_for(repo_id, limit=limit, declared_only=declared_only)
    )
    return {"repo_id": repo_id, "direction": direction, "edges": rows}


@router.get("/repos/{repo_id}/impact-chains", tags=["impact"])
def repo_impact_chains(
    repo_id: int,
    direction: str = Query("downstream", pattern="^(downstream|upstream)$"),
    max_depth: int = Query(3, ge=1, le=5),
    min_score: float = Query(0.3, ge=0.0, le=1.0),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    """Transitive impact chains. Every hop carries evidence; see impact_graph."""
    if q.get_repo(repo_id) is None:
        raise HTTPException(404, f"repository {repo_id} not found")
    fn = predict.upstream_chains if direction == "upstream" else predict.impact_chains
    rows = fn(repo_id, max_depth=max_depth, min_score=min_score, limit=limit)
    return {
        "repo_id": repo_id,
        "direction": direction,
        "chains": [
            {
                "depth": c["depth"],
                "path_score": float(c["path_score"]),
                "repos": list(c["repo_names"] or []),
                "repo_ids": list(c["path"]),
                "hops": [float(h) for h in (c["hops"] or [])],
                "lags": [float(x) if x is not None else None for x in (c.get("lags") or [])],
            }
            for c in rows
        ],
    }


@router.get("/impact/graph", tags=["impact"])
def impact_graph(
    min_score: float = Query(0.4, ge=0.0, le=1.0),
    limit: int = Query(400, ge=1, le=3000),
) -> dict:
    """Repository-level impact graph, for the force-directed view.

    There is no evidence filter because there is nothing to filter: every row in
    ``repo_impact`` comes from a declared dependency or an observed version
    bump, by construction in :func:`git_synapse.analysis.predict.rebuild`.
    """
    from git_synapse.db.engine import query as raw

    edges = raw(
        """
        SELECT i.source_repo_id AS source, i.target_repo_id AS target,
               i.score, i.is_declared, i.has_bump_history, i.bump_count,
               i.median_adoption_days
        FROM repo_impact i
        WHERE i.score >= %(min_score)s
        ORDER BY i.score DESC
        LIMIT %(limit)s
        """,
        {"min_score": min_score, "limit": limit},
    )
    ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    nodes = raw(
        """
        SELECT r.id, r.name AS basename, r.full_name AS path,
               COALESCE(r.primary_language,'') AS dir_path,
               r.primary_language AS extension, r.commit_count AS change_count,
               FALSE AS is_deleted
        FROM repo r WHERE r.id = ANY(%(ids)s)
        """,
        {"ids": ids},
    ) if ids else []
    return {"nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)}}


@router.get("/repos/{consumer_id}/bumps/{dep_id}", tags=["impact"])
def repo_pair_bumps(consumer_id: int, dep_id: int,
                    limit: int = Query(100, ge=1, le=500)) -> dict:
    """Every version bump one repository made to another, newest first.

    The aggregate above it says "13 bumps, median lag 41.8 days", which is a
    summary of something and never shows the something. This is the evidence:
    which version, on what date, and the upstream commit it consumed where that
    could be resolved.
    """
    from git_synapse.db.engine import query as raw

    rows = raw(
        """
        SELECT b.dep_name, b.dep_version, b.manifest, b.ecosystem, b.resolution,
               b.bumped_at, b.consumer_sha, b.dep_sha,
               round(b.adoption_seconds / 86400.0, 1)::float8 AS adoption_days,
               dc.sha AS upstream_sha, dc.subject AS upstream_subject,
               dc.committed_at AS upstream_at
          FROM dep_bump b
          LEFT JOIN commit dc ON dc.id = b.dep_commit_id
         WHERE b.consumer_repo_id = %(consumer)s AND b.dep_repo_id = %(dep)s
      ORDER BY b.bumped_at DESC NULLS LAST
         LIMIT %(limit)s
        """,
        {"consumer": consumer_id, "dep": dep_id, "limit": limit},
    )
    return {"consumer_repo_id": consumer_id, "dep_repo_id": dep_id,
            "count": len(rows), "bumps": rows}


@router.get("/repos/{repo_id}/dependencies", tags=["impact"])
def repo_dependencies(repo_id: int) -> dict:
    """Declared dependencies and observed bumps for one repository."""
    from git_synapse.db.engine import query as raw

    declared = raw(
        """
        SELECT d.dep_name, d.dep_version, d.manifest, d.ecosystem, d.dep_repo_id,
               r.name AS dep_repo
        FROM repo_dependency d
        LEFT JOIN repo r ON r.id = d.dep_repo_id
        WHERE d.consumer_repo_id = %(repo)s
        ORDER BY (d.dep_repo_id IS NULL), d.dep_name
        """,
        {"repo": repo_id},
    )
    bumps = raw(
        """
        SELECT rd.name AS dep_repo, b.dep_repo_id, count(*) AS bumps,
               round((percentile_cont(0.5) WITHIN GROUP (ORDER BY b.adoption_seconds)
                      / 86400.0)::numeric, 2)::float8 AS median_adoption_days,
               max(b.bumped_at) AS last_bump
        FROM dep_bump b
        LEFT JOIN repo rd ON rd.id = b.dep_repo_id
        WHERE b.consumer_repo_id = %(repo)s AND b.dep_repo_id IS NOT NULL
        GROUP BY 1, 2 ORDER BY bumps DESC
        """,
        {"repo": repo_id},
    )
    return {"declared": declared, "bumps": bumps}


# ---------------------------------------------------------------------------
# Directional / lagged analysis
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mining: modules, drift, risk
# ---------------------------------------------------------------------------


@router.get("/repos/{repo_id}/modules", tags=["mining"])
def repo_modules(repo_id: int, limit: int = Query(20, ge=1, le=200)) -> dict:
    """De-facto modules that cut across the declared directory structure."""
    return {"modules": mining.cross_directory_modules(repo_id, limit)}


@router.get("/drift", tags=["mining"])
def drift(
    trend: str = Query("emerging", pattern="^(emerging|decaying|stable)$"),
    repo_id: int | None = None,
    limit: int = Query(30, ge=1, le=300),
) -> dict:
    """Pairs whose coupling is strengthening or decaying over time."""
    return {"trend": trend, "pairs": mining.drifting_pairs(repo_id, trend, limit)}


@router.get("/risk", tags=["mining"])
def risk(repo_id: int | None = None, limit: int = Query(30, ge=1, le=300)) -> dict:
    """Files where churn, coupling and concentrated ownership coincide."""
    return {"files": mining.risky_files(repo_id, limit)}


@router.get("/mining/overview", tags=["mining"])
def mining_overview() -> dict:
    """Counts for the mining layer."""
    from git_synapse.db.engine import query_one as one

    return one(
        """
        SELECT
          -- cluster_id restarts at 0 in every repository, so a module is
          -- identified by the (repo_id, cluster_id) pair. Counting cluster_id
          -- alone collapsed 4,394 modules down to 559.
          (SELECT count(*) FROM (SELECT DISTINCT repo_id, cluster_id
                                   FROM file_cluster) m)                 AS modules,
          (SELECT count(*) FROM file_cluster)                            AS clustered_files,
          (SELECT count(*) FROM (SELECT DISTINCT repo_id, cluster_id
                                   FROM file_cluster
                                  WHERE dirs_spanned > 1) m)             AS cross_dir_modules,
          (SELECT count(*) FROM pair_drift WHERE trend='emerging')       AS emerging,
          (SELECT count(*) FROM pair_drift WHERE trend='decaying')       AS decaying,
          (SELECT count(*) FROM pair_drift WHERE trend='stable')         AS stable,
          (SELECT count(*) FROM file_risk)                               AS risk_scored,
          (SELECT count(*) FROM repo_impact)                             AS impact_edges,
          (SELECT count(*) FROM repo_impact WHERE is_declared)           AS declared_edges,
          (SELECT count(*) FROM repo_impact WHERE has_bump_history)      AS bump_edges,
          (SELECT count(*) FROM dep_bump)                                AS dep_bumps,
          (SELECT count(*) FROM repo_dependency
            WHERE dep_repo_id IS NOT NULL)                               AS declared_deps
        """
    ) or {}

# ---------------------------------------------------------------------------
# Feedback: defects in Git Synapse reported by the sessions using it
# ---------------------------------------------------------------------------


@router.get("/feedback", tags=["feedback"])
def feedback(
    status: str = "open",
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """Defects reported against Git Synapse, most-hit first.

    ``status=all`` is explicit rather than an empty string, because the client's
    query-string builder drops empty values and the filter would silently fall
    back to "open".
    """
    return {
        "summary": q.feedback_summary(),
        "reports": q.list_feedback(None if status == "all" else status, kind, limit),
    }


@router.post("/feedback/{feedback_id}/resolve", tags=["feedback"])
def resolve_feedback(feedback_id: int, status: str, resolution: str = "") -> dict:
    """Close or reclassify a report. A human action, not an agent's."""
    try:
        ok = q.resolve_feedback(feedback_id, status, resolution)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"report {feedback_id} not found")
    return {"id": feedback_id, "status": status}


# ---------------------------------------------------------------------------
# Ingest control
# ---------------------------------------------------------------------------


@router.get("/runs", tags=["ingest"])
def runs(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"runs": q.recent_runs(limit)}


@router.get("/runs/{run_id}", tags=["ingest"])
def run_detail(run_id: int) -> dict:
    row = q.run_detail(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} not found")
    return row


@router.post("/ingest/refresh", tags=["ingest"])
def trigger_refresh(
    background: BackgroundTasks,
    force_full: bool = False,
    skip_discovery: bool = False,
) -> dict:
    """Kick off an ingest run in the background.

    Returns immediately; poll ``/api/runs`` for progress. A full ingest of a
    large org takes tens of minutes, far longer than any sane HTTP timeout.
    """
    # Reconciles abandoned runs first, so a killed container cannot block
    # ingestion forever.
    running = pipeline.active_run()
    if running:
        raise HTTPException(
            409,
            f"run {running['id']} ({running['trigger']}) is already in progress,"
            f" started {running['started_at']:%Y-%m-%d %H:%M UTC}",
        )

    def _job() -> None:
        records = pipeline.load_repo_records() if skip_discovery else None
        pipeline.run_ingest(records=records, trigger="api", force_full=force_full)

    background.add_task(_job)
    return {"status": "started", "force_full": force_full, "skip_discovery": skip_discovery}
