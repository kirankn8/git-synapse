"""FastAPI application: the REST API and the static web UI."""

from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from git_synapse import auth
from git_synapse.analysis import calls
from git_synapse.api import routes
from git_synapse.api.routes import router
from git_synapse.config import get_config
from git_synapse.db.engine import apply_schema, close_pool, wait_for_database

log = logging.getLogger(__name__)


def configure_logging() -> None:
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
    )


def _announce_access() -> None:
    """Say plainly whether this deployment asks anyone to sign in."""
    try:
        email = auth.ensure_admin()
    except auth.AuthError as exc:
        log.error("%s; no administrator was created", exc)
        return
    if email:
        log.info("sign-in required; administrator is %s", email)
    else:
        log.warning(
            "no ADMIN_EMAIL and ADMIN_PASSWORD set: this deployment answers "
            "every request without asking who is calling")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wait for Postgres and apply the schema before serving traffic."""
    configure_logging()
    wait_for_database()
    apply_schema()
    from git_synapse.ingest.pipeline import reconcile_stale_runs

    reconcile_stale_runs()
    _announce_access()
    log.info("git-synapse api ready")
    yield
    close_pool()


app = FastAPI(
    title="Git Synapse",
    version="1.0.0",
    summary="Change-coupling statistics over git history.",
    description=(
        "Ranks the files that historically change together, using 31 association "
        "measures computed from commit co-occurrence. Built so a coding agent can "
        "ask 'I am editing X, what else must change?' and get a statistically "
        "grounded answer."
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

_cfg = get_config()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cfg.server.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


_UNLOGGED = ("/api/calls", "/api/health", "/api/openapi.json", "/api/docs",
             "/api/auth/")

_ALWAYS_OPEN = ("/api/auth/", "/api/health")


@app.middleware("http")
async def _require_caller(request, call_next):
    """Turn the access policy into a closed door, or not, per request."""
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(_ALWAYS_OPEN):
        return await call_next(request)
    if auth.access_mode("dashboard") != "required":
        return await call_next(request)
    if routes.caller(request) is None:
        return JSONResponse(status_code=401, content={"detail": "sign in to continue"})
    return await call_next(request)


@app.middleware("http")
async def _record_calls(request, call_next):
    """Record every API call: what was asked, how it went, how long it took."""
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(_UNLOGGED):
        return await call_next(request)

    started = time.monotonic()
    status = "ok"
    error = None
    try:
        response = await call_next(request)
    except Exception as exc:                      # pragma: no cover - re-raised
        calls.record("http", path, method=request.method, status="error",
                     duration_ms=int((time.monotonic() - started) * 1000),
                     arguments=dict(request.query_params), error=str(exc),
                     client=request.headers.get("user-agent"))
        raise
    if response.status_code >= 400:
        status, error = "error", f"HTTP {response.status_code}"

    route = request.scope.get("route")
    name = request.scope.get("root_path", "") + getattr(route, "path", "") or path
    if not name.startswith("/api"):
        name = "/api" + name

    body, response = await _replay_body(response)
    parsed, rows = _decode(body)
    calls.record(
        "http", name, method=request.method, status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        arguments=dict(request.query_params) or None,
        result=parsed,
        result_bytes=len(body),
        result_rows=rows,
        error=error, client=request.headers.get("user-agent"),
    )
    return response


async def _replay_body(response):
    """Read a response's body and hand back one that can still be sent."""
    from starlette.responses import Response as _Response

    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)
    replayed = _Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
    return body, replayed


def _decode(body: bytes):
    """The reply as data plus its row count, or as text when it is not JSON."""
    import json as _json

    if not body:
        return None, None
    try:
        parsed = _json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"non_json": body[:200].decode("utf-8", "replace")}, None
    rows = None
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                rows = len(value)
                break
    elif isinstance(parsed, list):
        rows = len(parsed)
    return parsed, rows


@app.exception_handler(KeyError)
async def _key_error_handler(_request, exc: KeyError) -> JSONResponse:
    """Surface an unknown measure name as a 400 rather than a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc).strip("'\"")})


_web_root = _cfg.server.web_root
if _web_root.is_dir():
    app.mount("/static", StaticFiles(directory=str(_web_root / "static")), name="static")

    _INDEX = _web_root / "index.html"

    def _asset_version() -> str:
        """A token that changes whenever a served asset changes."""
        stamp = 0.0
        for name in ("static/app.js", "static/style.css", "static/graph.js"):
            asset = _web_root / name
            if asset.is_file():
                stamp = max(stamp, asset.stat().st_mtime)
        return str(int(stamp))

    def _shell() -> HTMLResponse:
        html = _INDEX.read_text(encoding="utf-8")
        version = _asset_version()
        html = re.sub(r'(/static/[\w.-]+?)(\?v=[^"\']*)?(["\'])',
                      lambda m: f"{m.group(1)}?v={version}{m.group(3)}", html)
        return HTMLResponse(
            html, headers={"Cache-Control": "no-store, must-revalidate"}
        )

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return _shell()

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(str(_web_root / "static" / "favicon.svg"))

    SPA_ROUTES = (
        "sources", "accounts",
        "repos", "insights", "activity", "measures", "jobs", "feedback",
        # Reachable while signed out: the shell renders the sign-in form.
        "people", "tokens",
    )

    @app.get("/{segment}", include_in_schema=False)
    async def spa_root(segment: str):
        if segment not in SPA_ROUTES:
            raise HTTPException(status_code=404, detail=f"no route /{segment}")
        return _shell()

    @app.get("/{segment}/{rest:path}", include_in_schema=False)
    async def spa_nested(segment: str, rest: str):
        if segment not in SPA_ROUTES:
            raise HTTPException(status_code=404, detail=f"no route /{segment}")
        return _shell()

else:  # pragma: no cover - only hit in a misconfigured deployment
    log.warning("web root %s not found; UI will not be served", _web_root)
