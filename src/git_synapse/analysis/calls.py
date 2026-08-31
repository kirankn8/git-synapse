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
from typing import Any

from git_synapse.db.engine import execute, query, query_one

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
    except Exception:  # pragma: no cover - recording must never break a caller
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

    values = ",".join(["(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)"] * len(batch))
    params: list[Any] = []
    for row in batch:
        params += [row["surface"], row["name"], row["method"], row["status"],
                   row["duration_ms"], row["arguments"], row["result_preview"],
                   row["result_bytes"], row["result_rows"], row["error"],
                   row["client"]]
    execute(
        "INSERT INTO call_log (surface, name, method, status, duration_ms,"
        " arguments, result_preview, result_bytes, result_rows, error, client)"
        f" VALUES {values}",
        tuple(params),
    )
    return len(batch)


@atexit.register
def _drain_at_exit() -> None:  # pragma: no cover - process teardown
    try:
        while _flush_once():
            pass
    except Exception:
        pass


def prune() -> int:
    """Drop rows past the retention bounds. Returns how many went."""
    return int(execute(
        """
        DELETE FROM call_log
        WHERE at < now() - make_interval(days => %(days)s)
           OR id <= (SELECT max(id) - %(rows)s FROM call_log)
        """,
        {"days": KEEP_DAYS, "rows": KEEP_ROWS},
    ) or 0)


# --------------------------------------------------------------------- reads

def summary(hours: int = 24) -> dict:
    """Headline counts an operator reads first: volume, failures, latency."""
    row = query_one(
        """
        SELECT count(*)                                        AS calls,
               count(*) FILTER (WHERE surface = 'mcp')         AS mcp_calls,
               count(*) FILTER (WHERE surface = 'http')        AS http_calls,
               count(*) FILTER (WHERE status = 'error')        AS errors,
               count(DISTINCT client)                          AS clients,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p50_ms,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 1) AS p95_ms,
               max(at)                                         AS last_call
        FROM call_log WHERE at > now() - make_interval(hours => %(hours)s)
        """,
        {"hours": hours},
    ) or {}
    return {**row, "hours": hours, "dropped": dropped()}


def by_name(surface: str | None = None, hours: int = 24, limit: int = 50) -> list[dict]:
    """Which tools and routes are actually used, and how well they behave."""
    clause = "at > now() - make_interval(hours => %(hours)s)"
    params: dict[str, Any] = {"hours": hours, "limit": limit}
    if surface:
        clause += " AND surface = %(surface)s"
        params["surface"] = surface
    return query(
        f"""
        SELECT surface, name,
               count(*)                                 AS calls,
               count(*) FILTER (WHERE status = 'error') AS errors,
               round(avg(duration_ms)::numeric, 1)      AS avg_ms,
               max(duration_ms)                         AS max_ms,
               round(avg(result_rows)::numeric, 1)      AS avg_rows,
               max(at)                                  AS last_call
        FROM call_log WHERE {clause}
        GROUP BY surface, name
        ORDER BY calls DESC
        LIMIT %(limit)s
        """,
        params,
    )


def recent(
    surface: str | None = None,
    name: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """The call list itself, newest first, without the payloads."""
    clauses, params = ["TRUE"], {"limit": limit}
    for column, value in (("surface", surface), ("name", name), ("status", status)):
        if value:
            clauses.append(f"{column} = %({column})s")
            params[column] = value
    return query(
        f"""
        SELECT id, at, surface, name, method, status, duration_ms,
               result_rows, result_bytes, error, client
        FROM call_log WHERE {' AND '.join(clauses)}
        ORDER BY at DESC, id DESC LIMIT %(limit)s
        """,
        params,
    )


def detail(call_id: int) -> dict | None:
    """One call in full: what was asked, and what came back."""
    return query_one("SELECT * FROM call_log WHERE id = %s", (call_id,))
