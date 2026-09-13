"""Recording and reading what callers asked for."""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import distinct, func, literal_column

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

_QUEUE_MAX = 2000

PREVIEW_BYTES = int(os.environ.get("CALL_LOG_BODY_BYTES") or 65536)

KEEP_DAYS = 30
KEEP_ROWS = 200_000

_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_lock = threading.Lock()
_dropped = 0


def record(
    surface: str,
    name: str,
    *,
    status: str = "ok",
    duration_ms: int = 0,
    arguments: Any = None,
    result: Any = None,
    error: str | None = None,
    client: str | None = None,
    method: str | None = None,
    result_bytes: int | None = None,
    result_rows: int | None = None,
) -> None:
    """Queue one call. Never raises, never blocks."""
    global _dropped
    try:
        preview, size, rows = _summarise(result)
        size = result_bytes if result_bytes is not None else size
        rows = result_rows if result_rows is not None else rows
        row = {
            "surface": surface,
            "name": name[:200],
            "method": method,
            "status": status,
            "duration_ms": int(duration_ms),
            "arguments": _json_or_none(arguments),
            "result_preview": preview,
            "result_bytes": size,
            "result_rows": rows,
            "error": (error or None) and str(error)[:500],
            "client": (client or None) and str(client)[:200],
        }
        _ensure_worker()
        _queue.put_nowait(row)
    except queue.Full:
        with _lock:
            _dropped += 1
    except Exception:  # pragma: no cover
        log.debug("could not record a call", exc_info=True)


def dropped() -> int:
    """How many rows were discarded because the queue was full."""
    with _lock:
        return _dropped


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)[:8000]
    except (TypeError, ValueError):
        return json.dumps({"unserialisable": str(type(value))})


def _summarise(result: Any) -> tuple[str | None, int | None, int | None]:
    """A bounded preview of a reply, plus its true size and row count."""
    if result is None:
        return None, None, None
    try:
        body = json.dumps(result, default=str)
    except (TypeError, ValueError):
        body = str(result)
    rows = None
    if isinstance(result, dict):
        for value in result.values():
            if isinstance(value, list):
                rows = len(value)
                break
    elif isinstance(result, list):
        rows = len(result)
    if len(body) > PREVIEW_BYTES:
        return (json.dumps({"truncated": True, "bytes": len(body),
                            "head": body[:PREVIEW_BYTES]}), len(body), rows)
    return body, len(body), rows


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_flush_forever, name="call-log",
                                   daemon=True)
        _worker.start()


def _flush_forever() -> None:  # pragma: no cover - exercised via _flush_once
    while True:
        try:
            _flush_once(block=True)
        except Exception:
            log.debug("call log flush failed", exc_info=True)
            time.sleep(5.0)


def _flush_once(block: bool = False, limit: int = 200) -> int:
    """Drain up to `limit` queued rows into one insert. Returns rows written."""
    batch: list[dict[str, Any]] = []
    try:
        first = _queue.get(timeout=2.0) if block else _queue.get_nowait()
        batch.append(first)
    except queue.Empty:
        return 0
    while len(batch) < limit:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break

    with session_scope() as session:
        CallLog = models().CallLog
        entries = []
        for row in batch:
            values = dict(row)
            values["arguments"] = (json.loads(values["arguments"])
                                    if values["arguments"] is not None else None)
            values["result_preview"] = (json.loads(values["result_preview"])
                                         if values["result_preview"] is not None else None)
            entries.append(CallLog(**values))
        session.add_all(entries)
    return len(batch)


@atexit.register
def _drain_at_exit() -> None:  # pragma: no cover - process teardown
    try:
        while _flush_once():
            pass
    except Exception:  # noqa: BLE001, S110 - the process is exiting
        pass


def prune() -> int:
    """Drop rows past the retention bounds. Returns how many went."""
    with session_scope() as session:
        CallLog = models().CallLog
        cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
        # The KEEP_ROWS-th newest id; every older id is past the row bound.
        threshold = (session.query(CallLog.id).order_by(CallLog.id.desc())
                     .offset(KEEP_ROWS - 1).limit(1).scalar())
        doomed = CallLog.at < cutoff
        if threshold is not None:
            doomed = doomed | (CallLog.id < threshold)
        return session.query(CallLog).filter(doomed).delete(synchronize_session=False)



def known_mcp_tools() -> list[str]:
    """The tools the MCP server published at startup, called or not."""
    from git_synapse.db.engine import get_watermark

    raw = get_watermark("mcp_tools") or ""
    return [name for name in raw.split(",") if name]


def summary(hours: int = 24, surface: str | None = None) -> dict:
    """Headline counts an operator reads first: volume, failures, latency."""
    with session_scope() as session:
        CallLog = models().CallLog
        conditions = [CallLog.at > datetime.now(UTC) - timedelta(hours=hours)]
        if surface:
            conditions.append(CallLog.surface == surface)
        row = session.query(
            func.count(),
            func.count().filter(CallLog.surface == "mcp"),
            func.count().filter(CallLog.surface == "http"),
            func.count().filter(CallLog.status == "error"),
            func.count(distinct(CallLog.client)),
            func.count(CallLog.duration_ms),
            func.percentile_cont(0.5).within_group(CallLog.duration_ms),
            func.percentile_cont(0.95).within_group(CallLog.duration_ms),
            func.min(CallLog.duration_ms),
            func.max(CallLog.at),
        ).filter(*conditions).one()
    total, mcp, http, errors, clients, timed, p50, p95, only, last = row
    return {"calls": total, "mcp_calls": mcp, "http_calls": http, "errors": errors,
            "clients": clients,
            "p50_ms": _percentile(timed, p50, only),
            "p95_ms": _percentile(timed, p95, only),
            "last_call": last, "hours": hours, "surface": surface, "dropped": dropped()}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _percentile(timed: int, value: float | None, only: int | None) -> float | int | None:
    if timed > 1:
        return round(float(value), 1)
    return only


def by_name(surface: str | None = None, hours: int = 24, limit: int = 50,
            status: str | None = None) -> list[dict]:
    """Which tools and routes are actually used, and how well they behave."""
    with session_scope() as session:
        CallLog = models().CallLog
        conditions = [CallLog.at > datetime.now(UTC) - timedelta(hours=hours)]
        if surface:
            conditions.append(CallLog.surface == surface)
        if status:
            conditions.append(CallLog.status == status)
        calls = func.count().label("calls")
        grouped = session.query(
            CallLog.surface, CallLog.name, calls,
            func.count().filter(CallLog.status == "error"),
            func.avg(CallLog.duration_ms), func.max(CallLog.duration_ms),
            func.avg(CallLog.result_rows), func.max(CallLog.at),
        ).filter(*conditions).group_by(CallLog.surface, CallLog.name).order_by(
            calls.desc(), CallLog.surface, CallLog.name).limit(limit).all()
    rows = [{"surface": call_surface, "name": call_name, "calls": count, "errors": errors,
             "avg_ms": _rounded(avg_ms), "max_ms": max_ms,
             "avg_rows": _rounded(avg_rows), "last_call": last}
            for call_surface, call_name, count, errors, avg_ms, max_ms, avg_rows, last in grouped]
    if surface == "http" or status:
        return rows

    seen = {r["name"] for r in rows if r["surface"] == "mcp"}
    idle = [
        {"surface": "mcp", "name": name, "calls": 0, "errors": 0,
         "avg_ms": None, "max_ms": None, "avg_rows": None, "last_call": None}
        for name in known_mcp_tools() if name not in seen
    ]
    return rows + idle


def timeline(hours: int = 24) -> list[dict]:
    """Calls per hour, every hour in the window including the empty ones."""
    count = max(1, min(hours, 168))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=count - 1)
    with session_scope() as session:
        CallLog = models().CallLog
        hour = func.date_trunc(literal_column("'hour'"),
                               func.timezone(literal_column("'UTC'"), CallLog.at))
        counted = {
            bucket.replace(tzinfo=UTC): (calls, mcp, http, errors)
            for bucket, calls, mcp, http, errors in session.query(
                hour, func.count(),
                func.count().filter(CallLog.surface == "mcp"),
                func.count().filter(CallLog.surface == "http"),
                func.count().filter(CallLog.status == "error"),
            ).filter(CallLog.at >= start).group_by(hour).all()
        }
    result = []
    for offset in range(count):
        bucket = start + timedelta(hours=offset)
        calls, mcp, http, errors = counted.get(bucket, (0, 0, 0, 0))
        result.append({"hour": bucket, "calls": calls, "mcp": mcp, "http": http,
                       "errors": errors})
    return result


def recent(
    surface: str | None = None,
    name: str | None = None,
    status: str | None = None,
    limit: int = 100,
    hours: int | None = None,
) -> list[dict]:
    """The call list itself, newest first, without the payloads."""
    with session_scope() as session:
        CallLog = models().CallLog
        conditions = []
        if hours:
            conditions.append(CallLog.at > datetime.now(UTC) - timedelta(hours=hours))
        for column, value in ((CallLog.surface, surface), (CallLog.name, name),
                              (CallLog.status, status)):
            if value:
                conditions.append(column == value)
        rows = session.query(
            CallLog.id, CallLog.at, CallLog.surface, CallLog.name, CallLog.method,
            CallLog.status, CallLog.duration_ms, CallLog.result_rows,
            CallLog.result_bytes, CallLog.error, CallLog.client,
        ).filter(*conditions).order_by(
            CallLog.at.desc(), CallLog.id.desc()).limit(limit).all()
        return [{"id": row.id, "at": row.at, "surface": row.surface, "name": row.name,
                 "method": row.method, "status": row.status, "duration_ms": row.duration_ms,
                 "result_rows": row.result_rows, "result_bytes": row.result_bytes,
                 "error": row.error, "client": row.client} for row in rows]


def detail(call_id: int) -> dict | None:
    """One call in full: what was asked, and what came back."""
    with session_scope() as session:
        row = session.get(models().CallLog, call_id)
        return ({column.name: getattr(row, column.name) for column in row.__table__.columns}
                if row else None)
