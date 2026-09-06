"""Resolve every source, and nothing else.

Discovery is cheap -- one listing per source -- and the scheduler only runs it
once at the start of a run that then takes hours to ingest. Separating them
converges the source list in the time the budget allows rather than the time
the ingest takes; the scheduler picks up whatever has appeared on its next pass.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/work/src")

from git_synapse.ingest import accounts, pipeline  # noqa: E402


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def budget() -> tuple[int, int]:
    import httpx

    try:
        core = httpx.get("https://api.github.com/rate_limit",
                         timeout=15).json()["resources"]["core"]
        return core["remaining"], max(0, int(core["reset"] - time.time()))
    except Exception:  # noqa: BLE001
        return 0, 300


def owning() -> int:
    accounts.refresh_repo_counts()
    return sum(1 for a in accounts.list_accounts(enabled_only=True) if a["repo_count"])


deadline = time.time() + 9 * 3600
while time.time() < deadline:
    total = len(accounts.list_accounts(enabled_only=True))
    have = owning()
    log(f"{have}/{total} sources own repositories")
    if have >= total:
        log("every source resolved")
        break

    remaining, reset_in = budget()
    if remaining < 3:
        wait = min(reset_in + 20, deadline - time.time())
        if wait <= 0:
            break
        log(f"budget spent; waiting {int(wait)}s")
        time.sleep(wait)
        continue

    try:
        found = pipeline.discover(trigger="converge")
        log(f"discovery selected {len(found)} repositories "
            f"(budget was {remaining})")
    except Exception as exc:  # noqa: BLE001
        log(f"discovery raised: {str(exc)[:150]}")
        time.sleep(45)

log(f"final: {owning()}/{len(accounts.list_accounts(enabled_only=True))} sources resolved")
