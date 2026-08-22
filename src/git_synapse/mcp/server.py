"""MCP server: exposes change-coupling intelligence to coding agents.

This is the interface the project exists for. An agent about to edit a file asks
``coupled_files`` and gets back the files that history says must usually change
with it, ranked by a statistically defensible measure and accompanied by the
evidence -- co-change counts, conditional probabilities and significance.

Two transports:

* **stdio** -- for agents that launch the server as a subprocess, which is how
  Claude Code and most desktop clients connect.
* **streamable-http** -- for the containerised deployment, where the server is a
  long-lived service that several agents share.

Every tool returns plain JSON-serialisable dicts with a short ``interpretation``
string, because a model consuming raw floats does better with an explicit
statement of what the number means than with the number alone.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from git_synapse.analysis import predict
from git_synapse.analysis import query as q
from git_synapse.db.engine import apply_schema, wait_for_database
from git_synapse.stats.registry import BY_KEY, DEFAULT_MEASURE, MEASURES

log = logging.getLogger("git_synapse.mcp")

INSTRUCTIONS = """\
Git Synapse answers "if I change this, what else has to change?" from the commit \
history of an entire GitHub organisation. Full guide: AGENTS.md in the git-synapse \
repository.

WITHIN a repository, call `coupled_files` before editing a file. Read \
`probability_also_changes` together with `co_changes` -- a score is meaningless \
without its support count. `explain_pair` gives the full statistical case for one \
relationship, including the commits that produced it.

ACROSS repositories, this prevents the most common incomplete change: patching a \
symptom in a downstream repo when the defect belongs upstream. Before fixing a \
bug, call `upstream_repos` on the repo you are editing. If it names a dependency \
with bump history and a short propagation lag, the fix may belong there instead. \
`impact_of_change` is the opposite direction. `coupling_chain` follows multi-hop \
paths such as signer -> packager -> runtime.

TRUST THE EVIDENCE TIER, NOT THE SCORE. Cross-repo results carry an `evidence` \
field, and the tiers are on DIFFERENT SCALES -- never sort them into one list:
  * `declared` / `bump-backed` -- structural or ground-truth evidence, measured at \
    AUC ~0.93 against real dependency propagation. Act on these.
  * `discovery` -- statistical only, unvalidated, and prone to flagging merely \
    busy repositories. A lead to verify, not a fact.

WHEN NOT TO ACT. High coupling is a prompt to look, not a mandate to edit. Naive \
use of this data makes an agent worse, not better:
  * A score of 1.000 on 2 shared commits is arithmetic, not evidence. Always \
    check `co_changes`; under 3 is noise.
  * go.mod/go.sum, package.json/package-lock.json and CHANGELOG/Makefile top \
    almost every ranking. They are build artefacts, not design coupling.
  * Generated files (embedded_spec.go, *_gen.go, vendor/) couple to everything \
    upstream of them. Change the source, then regenerate.
  * A file with thousands of partners tells you nothing specific.

REPORT THE EVIDENCE, not just the conclusion: cite support counts alongside \
percentages. If these tools are unavailable, say so -- a fabricated coupling \
claim is worse than none.

Scores come from 29 association measures over co-occurrence. NPMI is the default: \
bounded to [-1, 1] and resistant to the rare-item bias that plagues raw PMI. Ask \
for `log_likelihood_ratio` when you need statistical confidence, or \
`confidence_ab` for "will I have to touch it". `list_measures` returns all of \
them with caveats.\
"""

server = MCPServer(
    name="git-synapse",
    title="Git Synapse change coupling",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)


def _round(value: Any, places: int = 4) -> Any:
    """Round floats for compact output; pass everything else through."""
    return round(value, places) if isinstance(value, float) else value


def _describe_confidence(confidence: float | None, n_ab: int) -> str:
    """Turn a conditional probability into a sentence a model can act on."""
    if not confidence:
        return "no directional signal"
    pct = f"{confidence:.0%}"
    if n_ab < 3:
        return f"{pct} of the time, but on only {n_ab} shared commits -- weak evidence"
    if confidence >= 0.7:
        return f"changes together {pct} of the time -- very likely needs updating too"
    if confidence >= 0.4:
        return f"changes together {pct} of the time -- worth checking"
    return f"changes together {pct} of the time -- occasional"


@server.tool(
    name="coupled_files",
    title="Find files that change together",
    description=(
        "Given a file, return the files that have historically changed in the same "
        "commits, ranked by statistical association. Call this before editing to "
        "find the code that usually has to change alongside it."
    ),
)
def coupled_files(
    repo: str,
    path: str,
    measure: str = DEFAULT_MEASURE,
    limit: int = 15,
    min_support: int = 2,
) -> dict:
    """Rank a file's historical change partners.

    Args:
        repo: repository name, either ``name`` or ``owner/name``.
        path: file path relative to the repository root. Renamed paths resolve
            through the alias table, so an old path still works.
        measure: which association measure ranks the results. ``npmi`` is a good
            default; ``log_likelihood_ratio`` favours statistical confidence;
            ``confidence_ab`` favours directional predictability.
        limit: maximum partners to return.
        min_support: ignore partners sharing fewer than this many commits. Raise
            it to suppress coincidental pairs.

    Returns:
        The resolved file, and a ranked list of partners with their scores,
        conditional probabilities and an interpretation of each.
    """
    target = q.resolve_file(repo, path)
    if target is None:
        return {
            "error": f"no file {path!r} found in repository {repo!r}",
            "hint": "call search_files to find the right path",
        }

    try:
        spec = BY_KEY[q._safe_order(measure)]
    except KeyError as exc:
        return {"error": str(exc)}

    partners = q.coupled_files(target["id"], spec.key, limit, min_support)
    return {
        "file": {
            "repo": target["repo"],
            "path": target["path"],
            "total_changes": target["change_count"],
            "authors": target["author_count"],
            "last_changed": str(target["last_change_at"]) if target["last_change_at"] else None,
        },
        "measure": {"key": spec.key, "label": spec.label, "summary": spec.summary},
        "population": target.get("pair_population"),
        "partners": [
            {
                "path": p["path"],
                "repo": p["repo"],
                "score": _round(p.get(spec.key)),
                "co_changes": p["n_ab"],
                "partner_total_changes": p["n_b"] if p["path"] else None,
                "probability_also_changes": _round(p.get("confidence_out"), 3),
                "probability_reverse": _round(p.get("confidence_in"), 3),
                "log_likelihood_ratio": _round(p.get("log_likelihood_ratio"), 2),
                "npmi": _round(p.get("npmi"), 3),
                "jaccard": _round(p.get("jaccard"), 3),
                "last_co_change": str(p["last_co_change"]) if p.get("last_co_change") else None,
                "interpretation": _describe_confidence(p.get("confidence_out"), p["n_ab"]),
            }
            for p in partners
        ],
    }


@server.tool(
    name="explain_pair",
    title="Explain one coupling relationship",
    description=(
        "Return the full statistical case for a single file pair: the 2x2 "
        "contingency table, all 29 association measures, and the actual commits "
        "in which both files changed."
    ),
)
def explain_pair(repo: str, path_a: str, path_b: str, commit_limit: int = 8) -> dict:
    """Produce the complete evidence for one coupling relationship.

    Args:
        repo: repository name.
        path_a: first file path.
        path_b: second file path.
        commit_limit: how many shared commits to include as evidence.
    """
    a = q.resolve_file(repo, path_a)
    b = q.resolve_file(repo, path_b)
    if a is None or b is None:
        missing = path_a if a is None else path_b
        return {"error": f"no file {missing!r} in repository {repo!r}"}

    detail = q.pair_detail(a["id"], b["id"])
    if detail is None:
        return {
            "repo": repo,
            "path_a": a["path"],
            "path_b": b["path"],
            "coupled": False,
            "reason": (
                "these files have never changed in the same commit, or fell below "
                "the minimum support threshold"
            ),
        }

    cells = detail["cells"]
    commits = q.co_change_commits(a["id"], b["id"], commit_limit)

    return {
        "repo": detail["repo"],
        "path_a": detail["path_a"],
        "path_b": detail["path_b"],
        "coupled": True,
        "contingency": {
            "both_changed": cells["a"],
            "only_a_changed": cells["b"],
            "only_b_changed": cells["c"],
            "neither_changed": cells["d"],
            "total_commits": cells["n_total"],
            "expected_by_chance": round(cells["expected"], 2),
        },
        "measures": {
            spec.key: _round(detail.get(spec.key))
            for spec in MEASURES
            if detail.get(spec.key) is not None
        },
        "summary": (
            f"These files changed together {cells['a']} times out of "
            f"{cells['n_total']} commits, versus {cells['expected']:.1f} expected "
            f"by chance -- {detail.get('association_strength', 0):.1f}x more often. "
            f"When {detail['path_a']} changes, {detail['path_b']} changes "
            f"{(detail.get('confidence_ab') or 0):.0%} of the time; the reverse is "
            f"{(detail.get('confidence_ba') or 0):.0%}."
        ),
        "evidence_commits": [
            {
                "sha": c["sha"][:10],
                "subject": c["subject"],
                "author": c["author"],
                "date": str(c["committed_at"]),
                "files_in_commit": c["n_files"],
            }
            for c in commits
        ],
    }


@server.tool(
    name="upstream_repos",
    title="Find repositories a change here may actually belong in",
    description=(
        "Given a repository you are about to change, list the repositories whose "
        "changes historically PRECEDE changes here. Call this before fixing a bug: "
        "if a dependency shows bump history and a short propagation lag, the defect "
        "may belong upstream rather than in the repo you are looking at."
    ),
)
def upstream_repos(repo: str, limit: int = 12) -> dict:
    """List upstream repositories, strongest evidence first.

    Args:
        repo: repository name you are editing.
        limit: maximum results.
    """
    target = _resolve_repo(repo)
    if target is None:
        return {"error": f"no repository matching {repo!r}"}

    rows = predict.upstream_of(target["id"], limit=limit)
    return {
        "repo": target["full_name"],
        "guidance": (
            "Entries marked declared or bump-backed carry structural or "
            "ground-truth evidence and are reliable. Entries marked discovery are "
            "statistical only -- verify before acting on them."
        ),
        "upstream": [_impact_row(r, r["name"]) for r in rows],
    }


@server.tool(
    name="impact_of_change",
    title="What a change here will force others to update",
    description=(
        "Given a repository you are changing, list the repositories that "
        "historically have to be updated afterwards, with the observed propagation "
        "delay. The forward direction of upstream_repos."
    ),
)
def impact_of_change(repo: str, limit: int = 12, declared_only: bool = False) -> dict:
    """List downstream repositories affected by a change here.

    Args:
        repo: repository being changed.
        limit: maximum results.
        declared_only: restrict to declared dependencies, the highest-confidence tier.
    """
    target = _resolve_repo(repo)
    if target is None:
        return {"error": f"no repository matching {repo!r}"}

    rows = predict.impact_for(target["id"], limit=limit, declared_only=declared_only)
    return {
        "repo": target["full_name"],
        "downstream": [_impact_row(r, r["name"]) for r in rows],
    }


@server.tool(
    name="coupling_chain",
    title="Follow multi-hop coupling between repositories",
    description=(
        "Trace transitive coupling chains, e.g. signer -> packager -> runtime. Path "
        "confidence multiplies the per-hop scores, so a weak hop can only weaken a "
        "chain. Only structurally-evidenced hops are traversed."
    ),
)
def coupling_chain(
    repo: str, direction: str = "downstream", max_depth: int = 3, limit: int = 12
) -> dict:
    """Trace coupling chains outward from or inward to a repository.

    Args:
        repo: repository to start from.
        direction: ``downstream`` for what this affects, ``upstream`` for where a
            change here may originate.
        max_depth: maximum hops; 2 gives A -> B -> C.
        limit: maximum chains.
    """
    target = _resolve_repo(repo)
    if target is None:
        return {"error": f"no repository matching {repo!r}"}

    upstream = direction.lower().startswith("up")
    fn = predict.upstream_chains if upstream else predict.impact_chains
    rows = fn(target["id"], max_depth=max_depth, min_score=0.3, limit=limit)

    arrow = " <- " if upstream else " -> "
    return {
        "repo": target["full_name"],
        "direction": "upstream" if upstream else "downstream",
        "chains": [
            {
                "path": arrow.join(c["repo_names"] or []),
                "repos": list(c["repo_names"] or []),
                "hops": c["depth"],
                "path_confidence": round(float(c["path_score"]), 4),
                "hop_scores": [round(float(h), 4) for h in (c["hops"] or [])],
            }
            for c in rows
        ],
    }


@server.tool(
    name="crossrepo_files",
    title="Files in other repositories that change with this file",
    description=(
        "Given a specific file, find files in OTHER repositories that historically "
        "changed as part of the same unit of work -- the same ticket, or the same "
        "burst of activity. Use it when a change looks like it needs a matching "
        "edit somewhere else in the organisation."
    ),
)
def crossrepo_files(repo: str, path: str, limit: int = 12, min_support: int = 2) -> dict:
    """Cross-repository file-level coupling for one file."""
    target = q.resolve_file(repo, path)
    if target is None:
        return {"error": f"no file {path!r} in repository {repo!r}"}

    rows = q.crossrepo_file_partners(target["id"], "npmi", limit, min_support)
    return {
        "file": {"repo": target["repo"], "path": target["path"]},
        "partners": [
            {
                "repo": r["repo_name"],
                "path": r["path"],
                "shared_change_sets": r["n_ab"],
                "ticket_backed": r["n_ab_ticket"],
                "ticket_ratio": round(float(r["ticket_ratio"] or 0), 3),
                "probability_also_changes": _round(r.get("confidence_out"), 3),
                "npmi": _round(r.get("npmi"), 3),
                "last_together": str(r["last_co_change"]) if r.get("last_co_change") else None,
                "evidence": (
                    "ticket-linked" if (r["ticket_ratio"] or 0) >= 0.5
                    else "mostly temporal proximity -- weaker evidence"
                ),
            }
            for r in rows
        ],
    }


@server.tool(
    name="explain_repo_pair",
    title="Explain the coupling between two repositories",
    description=(
        "Full evidence for one repository pair: declared dependency status, observed "
        "manifest bumps with the exact upstream commits, propagation lag, and the "
        "association measures behind the score."
    ),
)
def explain_repo_pair(repo_a: str, repo_b: str) -> dict:
    """Assemble every piece of evidence for one repository pair."""
    from git_synapse.db.engine import query, query_one

    a, b = _resolve_repo(repo_a), _resolve_repo(repo_b)
    if a is None or b is None:
        return {"error": f"unknown repository: {repo_a if a is None else repo_b}"}

    impact = query_one(
        "SELECT * FROM repo_impact WHERE source_repo_id=%s AND target_repo_id=%s",
        (a["id"], b["id"]),
    )
    reverse = query_one(
        "SELECT * FROM repo_impact WHERE source_repo_id=%s AND target_repo_id=%s",
        (b["id"], a["id"]),
    )
    bumps = query(
        """
        SELECT consumer_sha, dep_version, dep_sha, bumped_at,
               round(lag_seconds / 86400.0, 2) AS lag_days
        FROM dep_bump
        WHERE dep_repo_id=%s AND consumer_repo_id=%s
        ORDER BY bumped_at DESC NULLS LAST LIMIT 8
        """,
        (a["id"], b["id"]),
    )
    declared = query_one(
        "SELECT dep_name, dep_version, manifest FROM repo_dependency"
        " WHERE dep_repo_id=%s AND consumer_repo_id=%s",
        (a["id"], b["id"]),
    )

    return {
        "repo_a": a["full_name"],
        "repo_b": b["full_name"],
        "declared_dependency": (
            f"{b['name']} declares {a['name']} in {declared['manifest']}"
            if declared else None
        ),
        "forward": _impact_row(impact, b["name"]) if impact else None,
        "reverse": _impact_row(reverse, a["name"]) if reverse else None,
        "recent_bumps": [
            {
                "consumer_commit": (r["consumer_sha"] or "")[:10],
                "upstream_commit": r["dep_sha"],
                "version": r["dep_version"],
                "when": str(r["bumped_at"]) if r["bumped_at"] else None,
                "lag_days": float(r["lag_days"]) if r["lag_days"] is not None else None,
            }
            for r in bumps
        ],
        "interpretation": (
            f"{b['name']} declares and has bumped {a['name']} "
            f"{impact['bump_count']} times"
            if impact and impact["bump_count"]
            else "no observed manifest bumps between these repositories"
        ),
    }


def _resolve_repo(name: str) -> dict | None:
    """Resolve a repository by exact name, then by fuzzy search."""
    matches = q.list_repos(search=name, limit=8)
    exact = [r for r in matches if r["name"].lower() == name.lower()]
    if exact:
        return exact[0]
    suffix = [r for r in matches if r["full_name"].lower().endswith(f"/{name.lower()}")]
    return suffix[0] if suffix else (matches[0] if matches else None)


def _impact_row(row: dict, name: str) -> dict:
    """Serialise one impact row with an explicit evidence tier."""
    if row["is_declared"]:
        tier, note = "declared", "declared dependency; validated tier (AUC 0.93)"
    elif row["has_bump_history"]:
        tier, note = "bump-backed", "observed manifest bumps; ground truth"
    else:
        tier, note = "discovery", "statistical only; unvalidated, verify before acting"
    return {
        "repo": name,
        "score": round(float(row["score"]), 4),
        "evidence": tier,
        "note": note,
        "bump_count": row["bump_count"],
        "median_lag_days": (
            round(float(row["median_lag_days"]), 2)
            if row["median_lag_days"] is not None else None
        ),
    }


@server.tool(
    name="search_files",
    title="Search for files by path",
    description="Find files by path substring across the corpus or within one repository.",
)
def search_files(
    term: str, repo: str | None = None, limit: int = 20, min_changes: int = 0
) -> dict:
    """Locate files whose path contains ``term``.

    Args:
        term: substring to match against the full path.
        repo: restrict to one repository.
        limit: maximum results.
        min_changes: ignore files changed fewer than this many times.
    """
    repo_id = None
    if repo:
        matches = q.list_repos(search=repo, limit=5)
        exact = [r for r in matches if r["full_name"].lower().endswith(f"/{repo.lower()}")]
        chosen = exact[0] if exact else (matches[0] if matches else None)
        if chosen is None:
            return {"error": f"no repository matching {repo!r}"}
        repo_id = chosen["id"]

    rows = q.search_files(term, repo_id, None, min_changes, "change_count", limit)
    return {
        "count": len(rows),
        "files": [
            {
                "repo": r["repo"],
                "path": r["path"],
                "changes": r["change_count"],
                "authors": r["author_count"],
                "last_changed": str(r["last_change_at"]) if r["last_change_at"] else None,
                "deleted": r["is_deleted"],
            }
            for r in rows
        ],
    }


@server.tool(
    name="file_history",
    title="Recent history and owners of a file",
    description=(
        "Return a file's recent commits and its most frequent authors -- useful for "
        "understanding why a file changes and who to ask about it."
    ),
)
def file_history(repo: str, path: str, limit: int = 15) -> dict:
    """Summarise how a file has evolved and who has worked on it."""
    target = q.resolve_file(repo, path)
    if target is None:
        return {"error": f"no file {path!r} in repository {repo!r}"}

    commits = q.file_commits(target["id"], limit)
    authors = q.file_authors(target["id"], 8)
    return {
        "repo": target["repo"],
        "path": target["path"],
        "total_changes": target["change_count"],
        "insertions": target["insertions"],
        "deletions": target["deletions"],
        "first_changed": str(target["first_change_at"]) if target["first_change_at"] else None,
        "last_changed": str(target["last_change_at"]) if target["last_change_at"] else None,
        "top_authors": [
            {"name": a["display_name"], "commits": a["n_commits"]} for a in authors
        ],
        "recent_commits": [
            {
                "sha": c["sha"][:10],
                "subject": c["subject"],
                "author": c["author"],
                "date": str(c["committed_at"]),
                "change_type": c["change_type"],
                "insertions": c["insertions"],
                "deletions": c["deletions"],
            }
            for c in commits
        ],
    }


@server.tool(
    name="repo_hotspots",
    title="Most-changed files in a repository",
    description=(
        "List the files that change most often, with how many coupling partners "
        "each has. A good way to orient yourself in an unfamiliar repository."
    ),
)
def repo_hotspots(repo: str, limit: int = 20) -> dict:
    """Return churn leaders for one repository."""
    matches = q.list_repos(search=repo, limit=5)
    exact = [r for r in matches if r["full_name"].lower().endswith(f"/{repo.lower()}")]
    chosen = exact[0] if exact else (matches[0] if matches else None)
    if chosen is None:
        return {"error": f"no repository matching {repo!r}"}

    rows = q.hotspots(chosen["id"], limit)
    return {
        "repo": chosen["full_name"],
        "description": chosen["description"],
        "language": chosen["primary_language"],
        "commits_analysed": chosen["commit_count"],
        "files": chosen["file_count"],
        "hotspots": [
            {
                "path": r["path"],
                "changes": r["change_count"],
                "authors": r["author_count"],
                "coupling_partners": r["partner_count"],
                "last_changed": str(r["last_change_at"]) if r["last_change_at"] else None,
            }
            for r in rows
        ],
    }


@server.tool(
    name="list_repositories",
    title="List analysed repositories",
    description="List the repositories in the corpus, with how much history each holds.",
)
def list_repositories(search: str | None = None, limit: int = 40) -> dict:
    """Enumerate available repositories."""
    rows = q.list_repos(search=search, limit=limit)
    return {
        "count": len(rows),
        "repositories": [
            {
                "name": r["full_name"],
                "language": r["primary_language"],
                "description": r["description"],
                "commits": r["commit_count"],
                "files": r["file_count"],
                "coupling_pairs": r["pair_count"],
                "status": r["ingest_status"],
                "has_churn_data": r["has_churn"],
                "last_commit": str(r["last_commit_at"]) if r["last_commit_at"] else None,
            }
            for r in rows
        ],
    }


@server.tool(
    name="list_measures",
    title="List available association measures",
    description=(
        "Describe the 29 association measures, including when each is appropriate "
        "and which ones are biased toward rarely-changed files."
    ),
)
def list_measures() -> dict:
    """Return the measure catalogue with guidance."""
    return {
        "default": DEFAULT_MEASURE,
        "measures": [
            {
                "key": s.key,
                "label": s.label,
                "family": s.family,
                "formula": s.formula,
                "summary": s.summary,
                "recommended": s.recommended,
                "caveat": (
                    "biased toward rarely-changed files; pair with a support threshold"
                    if s.rare_item_bias
                    else (
                        "counts joint absence, so it saturates near 1.0 on commit data"
                        if s.saturates_on_sparse
                        else None
                    )
                ),
            }
            for s in MEASURES
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Git Synapse MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for subprocess clients, http for the containerised service.",
    )
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8081")))
    args = parser.parse_args(argv)

    # stdio speaks JSON-RPC on stdout, so logging must never go there.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
    )

    wait_for_database()
    apply_schema()

    if args.transport == "stdio":
        log.info("git-synapse mcp server on stdio")
        server.run(transport="stdio")
    elif args.transport == "sse":
        log.info("git-synapse mcp server (sse) on %s:%s", args.host, args.port)
        server.run(transport="sse", host=args.host, port=args.port)
    else:
        log.info("git-synapse mcp server (streamable http) on %s:%s/mcp", args.host, args.port)
        server.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
