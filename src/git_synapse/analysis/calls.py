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
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

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
        rows = session.query(CallLog).order_by(CallLog.id.desc()).all()
        keep_ids = {row.id for row in rows[:KEEP_ROWS]}
        doomed = [row for row in rows if row.at < cutoff or row.id not in keep_ids]
        for row in doomed:
            session.delete(row)
        return len(doomed)


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
        rows = session.query(CallLog).filter(*conditions).all()
    durations = sorted(row.duration_ms for row in rows if row.duration_ms is not None)
    return {"calls": len(rows), "mcp_calls": sum(row.surface == "mcp" for row in rows),
            "http_calls": sum(row.surface == "http" for row in rows),
            "errors": sum(row.status == "error" for row in rows),
            "clients": len({row.client for row in rows if row.client is not None}),
            "p50_ms": round(statistics.quantiles(durations, n=100, method="inclusive")[49], 1) if len(durations) > 1 else (durations[0] if durations else None),
            "p95_ms": round(statistics.quantiles(durations, n=100, method="inclusive")[94], 1) if len(durations) > 1 else (durations[0] if durations else None),
            "last_call": max((row.at for row in rows), default=None),
            "hours": hours, "surface": surface, "dropped": dropped()}


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
        calls = session.query(CallLog).filter(*conditions).all()
        grouped: dict[tuple[str, str], list[Any]] = {}
        for call in calls:
            grouped.setdefault((call.surface, call.name), []).append(call)
        rows = []
        for (call_surface, call_name), group in grouped.items():
            durations = [row.duration_ms for row in group if row.duration_ms is not None]
            result_rows = [row.result_rows for row in group if row.result_rows is not None]
            rows.append({"surface": call_surface, "name": call_name, "calls": len(group),
                         "errors": sum(row.status == "error" for row in group),
                         "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
                         "max_ms": max(durations, default=None),
                         "avg_rows": round(sum(result_rows) / len(result_rows), 1) if result_rows else None,
                         "last_call": max((row.at for row in group), default=None)})
        rows.sort(key=lambda row: row["calls"], reverse=True)
        rows = rows[:limit]
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
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(hours=count - 1)
        rows = session.query(CallLog).filter(CallLog.at >= start).all()
        result = []
        for offset in range(count):
            bucket = start + timedelta(hours=offset)
            group = [row for row in rows if row.at.replace(minute=0, second=0, microsecond=0) == bucket]
            result.append({"hour": bucket, "calls": len(group),
                           "mcp": sum(row.surface == "mcp" for row in group),
                           "http": sum(row.surface == "http" for row in group),
                           "errors": sum(row.status == "error" for row in group)})
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
        rows = session.query(CallLog).filter(*conditions).order_by(
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
