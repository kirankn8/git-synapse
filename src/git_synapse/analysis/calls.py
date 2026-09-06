"""Recording and reading what callers asked for.

Two surfaces consume this product -- agents over MCP, and the UI over HTTP --
and neither left a trace, so "is anything using this?" had no answer and "what
did it ask for, and what did it get back?" had no answer either.

Writes never happen on the request path. A caller hands a row to a bounded
queue and returns; one daemon thread batches them into Postgres. If the queue
fills, rows are dropped and counted rather than blocking a reply: telemetry
that slows the thing it measures is a bad trade, and telemetry that can wedge
it is a worse one.
"""
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

from sqlalchemy import Numeric, cast, delete, desc, func, literal, select
from sqlalchemy.dialects.postgresql import INTERVAL

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

#: Bounded so a database outage cannot grow the process without limit. At the
#: rates this sees, reaching it means the flusher is stuck, not that traffic is
#: high, and dropping is the right answer either way.
_QUEUE_MAX = 2000

#: How much of a reply to keep. This is a log record, so the default keeps the
#: whole body for anything of a readable size; a thousand-row table is cut, with
#: its true size and row count still recorded, because storing every one would
#: make the log larger than the data it describes. Raise or lower with
#: CALL_LOG_BODY_BYTES.
PREVIEW_BYTES = int(os.environ.get("CALL_LOG_BODY_BYTES") or 65536)

#: Pruning bounds. This table grows with traffic; everything else here grows
#: with history, which is much slower.
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
    """Queue one call. Never raises, never blocks.

    ``result_bytes`` and ``result_rows`` may be given when the caller already
    knows them -- an HTTP reply is bytes on the wire, and measuring it by
    re-encoding the parsed body would report a different number than the client
    received.
    """
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
        # Deliberately blind: this runs on every request and every tool call,
        # and there is no failure here worth turning into a caller's failure.
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
    """A bounded preview of a reply, plus its true size and row count.

    Size and shape are recorded in full even when the body is cut, because
    "what came back" is usually a question about how much, not about which.
    """
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
        # Whatever went wrong, there is nowhere left to report it and nothing
        # left to protect: the alternative is a traceback on every shutdown.
        pass


def prune() -> int:
    """Drop rows past the retention bounds. Returns how many went."""
    with session_scope() as session:
        CallLog = models().CallLog
        cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
        oldest_kept = select((func.max(CallLog.id) - KEEP_ROWS).label("oldest")).scalar_subquery()
        result = session.execute(delete(CallLog).where(
            (CallLog.at < cutoff) | (CallLog.id <= oldest_kept),
        ))
        return int(result.rowcount or 0)


# --------------------------------------------------------------------- reads

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
        row = session.execute(select(
            func.count().label("calls"),
            func.count().filter(CallLog.surface == "mcp").label("mcp_calls"),
            func.count().filter(CallLog.surface == "http").label("http_calls"),
            func.count().filter(CallLog.status == "error").label("errors"),
            func.count(func.distinct(CallLog.client)).label("clients"),
            func.round(cast(func.percentile_cont(0.5).within_group(CallLog.duration_ms), Numeric), 1).label("p50_ms"),
            func.round(cast(func.percentile_cont(0.95).within_group(CallLog.duration_ms), Numeric), 1).label("p95_ms"),
            func.max(CallLog.at).label("last_call"),
        ).where(*conditions)).mappings().one()
    return {**dict(row), "hours": hours, "surface": surface, "dropped": dropped()}


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
        rows = [dict(row) for row in session.execute(select(
            CallLog.surface, CallLog.name,
            func.count().label("calls"),
            func.count().filter(CallLog.status == "error").label("errors"),
            func.round(cast(func.avg(CallLog.duration_ms), Numeric), 1).label("avg_ms"),
            func.max(CallLog.duration_ms).label("max_ms"),
            func.round(cast(func.avg(CallLog.result_rows), Numeric), 1).label("avg_rows"),
            func.max(CallLog.at).label("last_call"),
        ).where(*conditions).group_by(CallLog.surface, CallLog.name)
          .order_by(desc("calls")).limit(limit)).mappings()]
    if surface == "http" or status:
        return rows

    # A tool nobody has called is the interesting row, and it cannot appear in a
    # table built from calls. Without this the page showed two tools and read as
    # "this server has two tools".
    seen = {r["name"] for r in rows if r["surface"] == "mcp"}
    idle = [
        {"surface": "mcp", "name": name, "calls": 0, "errors": 0,
         "avg_ms": None, "max_ms": None, "avg_rows": None, "last_call": None}
        for name in known_mcp_tools() if name not in seen
    ]
    return rows + idle


def timeline(hours: int = 24) -> list[dict]:
    """Calls per hour, so the shape of the traffic is visible.

    Every bucket in the window is returned, including the empty ones: a chart
    drawn only from hours that had traffic silently closes the gaps and turns
    an outage into a smooth line.
    """
    with session_scope() as session:
        CallLog = models().CallLog
        count = max(1, min(hours, 168))
        buckets = select(func.generate_series(
            func.date_trunc("hour", func.now()) - cast(literal(f"{count - 1} hours"), INTERVAL),
            func.date_trunc("hour", func.now()), cast(literal("1 hour"), INTERVAL),
        ).label("hour")).subquery("buckets")
        rows = session.execute(select(
            buckets.c.hour,
            func.count(CallLog.id).label("calls"),
            func.count(CallLog.id).filter(CallLog.surface == "mcp").label("mcp"),
            func.count(CallLog.id).filter(CallLog.surface == "http").label("http"),
            func.count(CallLog.id).filter(CallLog.status == "error").label("errors"),
        ).outerjoin(CallLog, func.date_trunc("hour", CallLog.at) == buckets.c.hour)
          .group_by(buckets.c.hour).order_by(buckets.c.hour)).mappings().all()
        return [dict(row) for row in rows]


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
        return [dict(row) for row in session.execute(select(
            CallLog.id, CallLog.at, CallLog.surface, CallLog.name, CallLog.method,
            CallLog.status, CallLog.duration_ms, CallLog.result_rows,
            CallLog.result_bytes, CallLog.error, CallLog.client,
        ).where(*conditions).order_by(CallLog.at.desc(), CallLog.id.desc()).limit(limit)).mappings()]


def detail(call_id: int) -> dict | None:
    """One call in full: what was asked, and what came back."""
    with session_scope() as session:
        row = session.get(models().CallLog, call_id)
        return ({column.name: getattr(row, column.name) for column in row.__table__.columns}
                if row else None)
