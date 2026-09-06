"""Add a diverse corpus, then converge it, pacing against the request budget.

Adding costs nothing: a repository URL is parsed and written as a source with
a one-name allowlist, and no host is contacted until discovery. Discovery is
where the budget goes -- one page per owner, which for an ordinary org is one
request -- so the loop below discovers what it can, waits out a refill when
GitHub says no, and ingests whatever has arrived.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app/src")

from git_synapse.db.engine import query, query_one  # noqa: E402
from git_synapse.ingest import accounts  # noqa: E402


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def budget() -> tuple[int, int]:
    """(remaining, seconds until refill) for GitHub; free to ask."""
    import httpx

    try:
        d = httpx.get("https://api.github.com/rate_limit", timeout=15).json()
        core = d["resources"]["core"]
        return core["remaining"], max(0, int(core["reset"] - time.time()))
    except Exception as exc:  # noqa: BLE001
        log(f"could not read budget: {exc}")
        return 0, 60


def add_all(path: str) -> None:
    urls = [ln.strip() for ln in open(path) if ln.strip() and not ln.startswith("#")]
    added = failed = existing = 0
    for url in urls:
        try:
            before = {a["id"] for a in accounts.list_accounts()}
            row = accounts.add_from_url(url)
            if row["id"] in before:
                existing += 1
            else:
                added += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log(f"  could not add {url}: {str(exc)[:100]}")
    log(f"sources: {added} added, {existing} already present, {failed} refused")


def pending_accounts() -> list[dict]:
    """Sources that have produced no repository yet."""
    return [a for a in accounts.list_accounts(enabled_only=True)
            if not a["repo_count"]]


def main() -> None:
    add_all("/scratch/corpus.txt")   # already added; re-running is a no-op

    from git_synapse.ingest import pipeline

    for cycle in range(1, 60):
        pend = pending_accounts()
        total = len(accounts.list_accounts(enabled_only=True))
        log(f"cycle {cycle}: {total - len(pend)}/{total} sources resolved")
        if not pend:
            log("every source has repositories; discovery is done")
            break

        remaining, reset_in = budget()
        gh = [a for a in pend if a["host"] == "github.com"]
        if gh and remaining < 5:
            log(f"budget spent ({remaining}); waiting {reset_in + 30}s for a refill")
            time.sleep(reset_in + 30)
            continue

        try:
            found = pipeline.discover(trigger="corpus-expansion")
            log(f"discovery returned {len(found)} repositories")
        except Exception as exc:  # noqa: BLE001
            log(f"discovery raised: {str(exc)[:160]}")
            time.sleep(60)

    counts = query_one(
        "SELECT count(*) FILTER (WHERE is_enabled) AS active,"
        " count(*) FILTER (WHERE is_enabled AND commit_count > 0) AS ingested"
        " FROM repo")
    log(f"repositories: {counts['active']} active, {counts['ingested']} with history")

    for host in ("github.com", "gitlab.com", "bitbucket.org"):
        n = query("SELECT count(*) AS n FROM repo WHERE host=%s AND is_enabled",
                  (host,))[0]["n"]
        log(f"  {host:16} {n}")


if __name__ == "__main__":
    main()
