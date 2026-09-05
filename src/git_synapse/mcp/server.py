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
import time
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from git_synapse.analysis import calls, predict
from git_synapse.analysis import query as q
from git_synapse.db.engine import apply_schema, wait_for_database
from git_synapse.stats.registry import (
    BY_KEY, DEFAULT_MEASURE, FREE_LOOKUP_HIT_RATE, MEASURED_ON, MEASURES,
)

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

EVERY CROSS-REPO EDGE CARRIES EVIDENCE. There is no statistical tier: an edge \
exists only where a dependency is declared in a manifest, or where a version \
bump was observed and resolved to the upstream commit it consumed. Both were \
measured at AUC 0.88 in sample against real dependency propagation, 0.69 held \
out in time. Act on them, but the number is an in-sample bound, not a \
guarantee. The `evidence` field says which of the two a row rests on.

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
them with caveats.

IF GIT SYNAPSE ITSELF IS WRONG, say so with `report_gap`: data missing that should be \
indexed, a value that contradicts the repository, something correct once and now \
stale, a tool that failed, a repository or path not covered. Every improvement \
made on the first day of use came from someone noticing exactly that. Reports go \
to a defect log that feeds no measure or score, so this is not a way to suppress \
a suggestion or to disagree with a number -- it is for when the tool is broken.\
"""

def _text_of(result: Any) -> Any:
    """The reply as the agent receives it: parsed JSON when it is JSON, else text."""
    import json as _json

    parts = [getattr(block, "text", None) for block in getattr(result, "content", []) or []]
    text = "\n".join(p for p in parts if p)
    if not text:
        return None
    try:
        return _json.loads(text)
    except ValueError:
        return {"text": text}


def _error_text(payload: Any) -> str:
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])[:500]
    return str(payload)[:500]


class _RecordingServer(MCPServer):
    """An MCP server that records what was called and what it returned.

    Overriding the one dispatch point rather than decorating fourteen tools:
    a tool added later is recorded without anyone remembering to, which is the
    only way this stays true. Recording is best-effort and never changes what
    the caller gets back -- a telemetry failure must not become a tool failure.
    """

    async def call_tool(self, name, arguments, context=None):
        started = time.monotonic()
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception as exc:
            calls.record("mcp", name, status="error", arguments=arguments,
                         duration_ms=int((time.monotonic() - started) * 1000),
                         error=f"{type(exc).__name__}: {exc}")
            raise
        # structured_content is populated only when a tool declares an output
        # schema; every other reply arrives as text blocks, which is what the
        # agent actually reads. Fall back to those so the log records what was
        # returned rather than a null.
        payload = getattr(result, "structured_content", None)
        if payload is None:
            payload = _text_of(result)
        # A tool that returns {"error": ...} succeeded at the protocol level and
        # failed at the only level a reader cares about. Count it as an error.
        failed = bool(getattr(result, "is_error", False)) or (
            isinstance(payload, dict) and "error" in payload)
        calls.record(
            "mcp", name,
            status="error" if failed else "ok",
            arguments=arguments,
            result=payload,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=_error_text(payload) if failed else None,
        )
        return result


server = _RecordingServer(
    name="git-synapse",
    title="Git Synapse change coupling",
    version="1.0.0",
    instructions=INSTRUCTIONS,
)


def _round(value: Any, places: int = 4) -> Any:
    """Round floats for compact output; pass everything else through."""
    return round(value, places) if isinstance(value, float) else value


#: A pair whose last co-change is older than this is reported as stale. Roughly
#: two release cycles here: long enough that an active relationship will have
#: fired at least once, short enough to catch a refactor that finished last year.
STALE_AFTER_DAYS = 270


#: Directory names that hold machine-written or third-party code. Matched as
#: whole path segments: a substring test flagged `pkg/genetics/` as generated and
#: missed a top-level `vendor/`, both of which matter.
#: `swagger` and `openapi` are deliberately absent: they are real package names
#: in Kubernetes-derived code (apiserver/pkg/endpoints/openapi/openapi.go is
#: hand-written), and the generated artefacts they produce are already caught by
#: filename below. Suppressing a real file is the expensive error.
_GENERATED_DIRS = frozenset(
    {"gen", "generated", "vendor", "node_modules", "mocks", ".gen", "dist",
     "__generated__"}
)

#: Filename shapes that mark generated output.
_GENERATED_FILE_PARTS = (
    "zz_generated", ".pb.go", "_pb2.py", ".gen.go", ".generated.", ".min.js",
    "embedded_spec.go", "swagger.json", "swagger.yaml", "openapi.json",
)

#: Exact filenames that are always lockfiles or checksums.
_LOCKFILES = frozenset(
    {"go.sum", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
     "poetry.lock", "Gemfile.lock", "composer.lock"}
)

#: Below this many co-changes the interval around a probability is wider than
#: the probability, so quoting a percentage implies precision that is not there.
#: A pair that co-changed twice and never apart scores 1.0 and sorts above
#: everything real -- observed sending a reviewer at four unrelated files. These
#: are withheld by default rather than labelled, because a label only works on a
#: reader who is already sceptical.
THIN_SUPPORT = 5
MIN_REPORTABLE_SUPPORT = 3


def _classify_partner(path: str, own_path: str, n_ab: int) -> tuple[list[str], bool]:
    """Label a partner, and say whether it is worth the reader's attention.

    Ranking without judging pushed the filtering onto the reader: results padded
    with 5% generated swagger files and the caller's own test file, which the
    caller already knows about. Returns the labels and whether the row carries
    information beyond what the caller can see for themselves.
    """
    labels: list[str] = []
    segments = path.split("/")
    base = segments[-1]
    lower_base = base.lower()

    if (
        base in _LOCKFILES
        or any(seg.lower() in _GENERATED_DIRS for seg in segments[:-1])
        or any(part in lower_base for part in _GENERATED_FILE_PARTS)
        or (lower_base.startswith("mock_") or lower_base.startswith("mocks_"))
    ):
        labels.append("generated")

    stem = own_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem and (
        base.startswith(f"{stem}_test.") or base.startswith(f"test_{stem}.")
        or base.startswith(f"{stem}.test.") or base.startswith(f"{stem}.spec.")
    ):
        labels.append("own_test")

    if n_ab < THIN_SUPPORT:
        labels.append("thin_support")

    # A sibling variant: same filename, different parent. This is where the tool
    # genuinely discovers rather than confirms -- an amd/nvidia pair, a per-cloud
    # or per-arch copy that must be edited in lockstep.
    own_dir, _, own_base = own_path.rpartition("/")
    p_dir, _, p_base = path.rpartition("/")
    if own_base and own_base == p_base and own_dir != p_dir:
        labels.append("sibling_variant")

    informative = not ({"generated", "own_test"} & set(labels))
    if n_ab < MIN_REPORTABLE_SUPPORT:
        informative = False
    return labels, informative


def _describe_currency(
    days: int | None, trend: str | None, deleted: bool = False
) -> str | None:
    """Say whether a coupling still appears to hold.

    A lifetime score is silent about currency: a pair that co-changed forty times
    and stopped two years ago outranks one that co-changed eight times last month.

    Deletion is checked first and stated most loudly, because it is both the
    strongest signal and the one age misses. A file removed in a refactor keeps
    every co-change it ever had, and its coupling can be recent -- the case that
    prompted this returned a partner deleted 50 days ago whose last co-change was
    also 50 days ago, so no age threshold would have caught it. There are 38,716
    such partners in this corpus, and an agent cannot edit any of them.
    """
    if deleted:
        return (
            "DELETED -- this file no longer exists at HEAD. The coupling is "
            "historical; do not try to edit it. If the behaviour moved, find "
            "where it moved to."
        )
    # Age outranks trend. The drift window is wider than the staleness threshold,
    # so a pair can carry a trend label while its last co-change is a year old;
    # returning the label first made the stale branch unreachable for those.
    if days is not None and days > STALE_AFTER_DAYS:
        return (
            f"STALE -- last co-changed {days} days ago; treat as historical, "
            "verify the path still exists"
        )
    if trend == "decaying":
        return "DECAYING -- weaker lately than historically; likely a finished refactor"
    if trend == "emerging":
        return "emerging -- stronger lately than historically"
    if days is None:
        return None
    if days <= 30:
        return f"current -- co-changed {days} days ago"
    return f"co-changed {days} days ago"


def _describe_confidence(confidence: float | None, n_ab: int) -> str:
    """Turn a conditional probability into a sentence a model can act on."""
    if not confidence:
        return "no directional signal"
    pct = f"{confidence:.0%}"
    if n_ab < MIN_REPORTABLE_SUPPORT:
        return f"{pct} of the time, but on only {n_ab} shared commits -- weak evidence"
    # The same percentage means different things at different support. 90% of
    # three commits and 90% of three hundred read identically before this, which
    # is the overconfidence that sent a reviewer at files their own reading had
    # already ruled out.
    hedge = f" (on {n_ab} shared commits, so treat as provisional)" if n_ab < THIN_SUPPORT else ""
    if confidence >= 0.7:
        verdict = "very likely needs updating too" if not hedge else "may need updating too"
        return f"changes together {pct} of the time -- {verdict}{hedge}"
    if confidence >= 0.4:
        return f"changes together {pct} of the time -- worth checking{hedge}"
    return f"changes together {pct} of the time -- occasional{hedge}"


@server.tool(
    name="coupled_files",
    title="Find files that change together",
    description=(
        "Given a file, return the files that have historically changed in the same "
        "commits, ranked by statistical association. Call this before editing to "
        "find the code that usually has to change alongside it. Read each "
        "partner's `currency` field before acting: it flags partners that no "
        "longer exist at HEAD, and coupling that has decayed since. A high score "
        "on a deleted file or a finished refactor is history, not live coupling."
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
        measure: which association measure ranks the results. Leave it unset
            unless you have a reason: the default is ``confidence_ab``, the only
            one chosen by backtest rather than by taste, and it asks exactly
            what this tool is for -- given A changed, how often did B? The
            symmetric measures (``npmi``, ``jaccard``, ``log_likelihood_ratio``)
            answer "is this association surprising", which is a better question
            for exploring a codebase and a worse one for predicting a change.
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

    shaped = []
    for pr in partners:
        labels, informative = _classify_partner(
            pr["path"] or "", target["path"], pr["n_ab"]
        )
        shaped.append((pr, labels, informative))

    mirrors = [x for x in shaped if "sibling_variant" in x[1]]
    noise = [x for x in shaped if not x[2]]
    lead = None
    if mirrors:
        lead = (
            f"{len(mirrors)} sibling variant(s) share this filename in another "
            "directory. Parallel copies are the case this tool finds that reading "
            "one file does not: check whether the edit applies to each."
        )
    elif noise:
        lead = (
            f"{len(noise)} of {len(shaped)} partners are this file's own tests or "
            "generated output, marked `informative: false`. They co-change by "
            "construction and tell you nothing you did not already know."
        )

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
        "summary": lead,
        "partners": [
            {
                "path": p["path"],
                "repo": p["repo"],
                "labels": labels or None,
                "informative": informative,
                "score": _round(p.get("score")),
                "co_changes": p["n_ab"],
                "partner_total_changes": p["n_other"] if p["path"] else None,
                "probability_also_changes": _round(p.get("confidence_out"), 3),
                "probability_reverse": _round(p.get("confidence_in"), 3),
                "log_likelihood_ratio": _round(p.get("log_likelihood_ratio"), 2),
                "npmi": _round(p.get("npmi"), 3),
                "jaccard": _round(p.get("jaccard"), 3),
                "last_co_change": str(p["last_co_change"]) if p.get("last_co_change") else None,
                "days_since_co_change": p.get("days_since_co_change"),
                "trend": p.get("trend"),
                "deleted": bool(p.get("is_deleted")),
                "currency": _describe_currency(
                    p.get("days_since_co_change"),
                    p.get("trend"),
                    bool(p.get("is_deleted")),
                ),
                "interpretation": _describe_confidence(p.get("confidence_out"), p["n_ab"]),
            }
            for p, labels, informative in shaped
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
        "guidance": _evidence_guidance(rows, "upstream"),
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
    # An empty result has two very different meanings and used to render as the
    # same bare []. Traversal follows validated edges only, so a repository that
    # declares no internal dependencies has nothing to walk -- that is "there was
    # nothing to search", not "I searched and found nothing".
    explanation = None
    if not rows:
        side = "target_repo_id" if upstream else "source_repo_id"
        counts = q.query_one(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE is_declared OR has_bump_history) AS validated
            FROM repo_impact WHERE {side} = %s
            """,
            (target["id"],),
        ) or {"total": 0, "validated": 0}
        if counts["validated"] == 0 and counts["total"] > 0:
            explanation = (
                f"No chains, because none of this repository's {counts['total']} "
                f"{'upstream' if upstream else 'downstream'} edges is validated. "
                "Chain traversal follows declared and bump-backed edges only, so "
                "there was nothing to walk -- this is not evidence that no "
                "multi-hop relationship exists. This repository declares no "
                "internal dependencies in its manifests."
            )
        elif counts["total"] == 0:
            explanation = (
                "No chains, and no edges of any kind recorded for this repository."
            )
        else:
            explanation = (
                f"No chains found. {counts['validated']} validated edge(s) exist "
                "but none extends to a second hop above the confidence floor."
            )

    return {
        "repo": target["full_name"],
        "direction": "upstream" if upstream else "downstream",
        "explanation": explanation,
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
               round(adoption_seconds / 86400.0, 2) AS adoption_days
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
                "adoption_days": float(r["adoption_days"]) if r["adoption_days"] is not None else None,
            }
            for r in bumps
        ],
        # The prose is what a model quotes, so it must not claim more than the
        # structured fields beside it. This branch tested the bump count alone
        # and so reported a bump-backed pair as "declares", promoting it to the
        # top evidence tier on the strength of nothing.
        "interpretation": _describe_repo_pair(a, b, declared, impact),
    }


def _describe_repo_pair(a: dict, b: dict, declared: str | None, impact: dict | None) -> str:
    """One sentence that never outruns the evidence behind it."""
    bumps = (impact or {}).get("bump_count") or 0
    if declared and bumps:
        return f"{b['name']} declares {a['name']} and has bumped it {bumps} times"
    if declared:
        return (
            f"{b['name']} declares {a['name']} in a manifest, but no bump has "
            "been observed between them"
        )
    if bumps:
        return (
            f"{b['name']} has bumped {a['name']} {bumps} times. No manifest at "
            "HEAD declares it, so this is bump-backed rather than declared"
        )
    return "no declared dependency and no observed manifest bumps between these repositories"


def _resolve_repo(name: str) -> dict | None:
    """Resolve a repository by exact name, then by unambiguous suffix.

    A substring match is deliberately not a resolution. Falling back to the first
    of them meant a typo answered confidently about a different repository --
    "telem" resolved to telemetry, "contr" to contracts, and an empty string to whichever
    repository happened to have the most commits, complete with evidence tiers
    and nothing marking it as a guess.
    """
    if not (name or "").strip():
        return None
    key = name.strip().lower()
    matches = q.list_repos(search=name, limit=8)
    exact = [
        r for r in matches
        if r["name"].lower() == key or r["full_name"].lower() == key
    ]
    if exact:
        return exact[0]
    suffix = [r for r in matches if r["full_name"].lower().endswith(f"/{key}")]
    return suffix[0] if len(suffix) == 1 else None


def _evidence_guidance(rows: list[dict], direction: str) -> str:
    """State the composition of the result before the reader reads the scores.

    The score mixes both tiers, so it cannot say which evidence a row rests on.
    That has to be stated, not left to be inferred from a number.
    """
    if not rows:
        return f"No {direction} edges recorded for this repository."
    declared = sum(1 for r in rows if r["is_declared"])
    bumped = len(rows) - declared
    return (
        f"{declared} declared, {bumped} bump-backed. Every edge rests on a "
        "dependency declared in a manifest or on an observed version bump; "
        "there is no statistical tier. `score` ranks within that evidence, so "
        "it orders the list rather than giving a probability."
    )


def _impact_row(row: dict, name: str) -> dict:
    """Serialise one impact row with an explicit evidence tier."""
    if row["is_declared"]:
        tier, note = "declared", "declared dependency; measured AUC 0.88 in sample"
    else:
        # Unreachable by construction -- an edge with neither is never written --
        # but stated rather than assumed, so a stale row is legible.
        tier, note = ("bump-backed", "observed manifest bumps; ground truth") \
            if row["has_bump_history"] else ("none", "no evidence recorded")
    return {
        "repo": name,
        "score": round(float(row["score"]), 4),
        "evidence": tier,
        "note": note,
        "bump_count": row["bump_count"],
        "median_adoption_days": (
            round(float(row["median_adoption_days"]), 2)
            if row["median_adoption_days"] is not None else None
        ),
    }


@server.tool(
    name="coupled_directories",
    title="Which other directories move with this one",
    description=(
        "Directory-level coupling: given a directory (or any file inside one), "
        "which other directories in the repository historically change with it. "
        "Use this when the question is about a package or subsystem rather than a "
        "single file -- coupled_files cannot see inside a file or across a package, "
        "and this is the closest available answer. Note that a parent and its own "
        "child will always score high; that is arithmetic, not a finding."
    ),
)
def coupled_directories(
    repo: str, path: str, limit: int = 15, measure: str = DEFAULT_MEASURE
) -> dict:
    """Directories that historically change together with this one.

    Args:
        repo: repository name.
        path: a directory path, or a file path whose directory is used.
        limit: maximum partners.
        measure: ranking measure.
    """
    target = _resolve_repo(repo)
    if target is None:
        return {"error": f"no repository matching {repo!r}"}

    cleaned = (path or "").strip().strip("/")
    row = q.query_one(
        "SELECT id, path, file_count, change_count FROM directory"
        " WHERE repo_id = %s AND path = %s",
        (target["id"], cleaned),
    )
    if row is None:
        # Accept a file path and use its directory, which is what a caller
        # editing a file will naturally pass.
        f = q.resolve_file(target["full_name"], path)
        if f is not None:
            row = q.query_one(
                "SELECT id, path, file_count, change_count FROM directory"
                " WHERE repo_id = %s AND path = %s",
                (target["id"], f["dir_path"]),
            )
    if row is None:
        return {
            "error": f"no directory {path!r} in repository {target['full_name']!r}",
            "hint": "pass a directory path, or any file path inside it",
        }

    try:
        spec = BY_KEY[q._safe_order(measure)]
    except KeyError as exc:
        return {"error": str(exc)}

    # Ancestors and descendants are excluded by the query: a parent changes
    # whenever its child does, so it scores 1.000 by construction. What comes
    # back is only directories that could have moved independently and did not.
    partners = q.coupled_directories(row["id"], spec.key, limit)
    shaped = []
    for pr in partners:
        other = pr["path"] or ""
        shaped.append({
            "path": other,
            "relation": "sibling-or-unrelated",
            "informative": True,
            "score": _round(pr.get("score")),
            "co_changes": pr["n_ab"],
            "probability_also_changes": _round(pr.get("confidence_out"), 3),
            "its_total_changes": pr.get("n_other"),
            "files": pr.get("file_count"),
        })
    return {
        "directory": {
            "repo": target["full_name"],
            "path": row["path"],
            "files": row["file_count"],
            "total_changes": row["change_count"],
        },
        "measure": {"key": spec.key, "label": spec.label},
        "summary": (
            f"{len(shaped)} partners outside this directory's own subtree. Its "
            "parents and children are excluded: a parent changes whenever its "
            "child does, so it scores 1.000 by construction and says nothing."
        ) if shaped else "No directory coupling recorded outside its own subtree.",
        "partners": shaped,
    }


@server.tool(
    name="module_context",
    title="Which module owns this file, and what depends on that module",
    description=(
        "For a monorepo, resolve the file to its own module and report both "
        "directions of the manifest graph: what that module declares, and which "
        "other modules declare it. Use this when the repository has several "
        "modules -- changing a shared one is a change to everything that declares "
        "it, and that is a fact from the manifest rather than a correlation."
    ),
)
def module_context(repo: str, path: str) -> dict:
    """Resolve a file to its module and give both directions of the graph.

    Args:
        repo: repository name.
        path: file path relative to the repository root.
    """
    target = _resolve_repo(repo)
    if target is None:
        return {"error": f"no repository matching {repo!r}"}

    # Every other path-taking tool rejects an unknown path. This one answered
    # "it is a leaf", which an agent reads as "nothing depends on this".
    if q.resolve_file(target["full_name"], path) is None:
        return {
            "error": f"no file {path!r} found in repository {target['full_name']!r}",
            "hint": "call search_files to find the right path",
        }

    ctx = q.module_context(target["id"], path)
    if not ctx["modules"]:
        return {
            "repo": target["full_name"],
            "path": path,
            "multi_module": False,
            "note": "single-module repository; there is no internal module graph",
        }

    owning = ctx["owning_module"]
    guidance = []
    if ctx["declared_by"]:
        guidance.append(
            f"'{owning or '(root)'}' is declared by "
            f"{', '.join(ctx['declared_by'])} -- a change to its exported surface "
            "is a change to those modules too."
        )
    if ctx["declares"]:
        guidance.append(
            f"'{owning or '(root)'}' declares {', '.join(ctx['declares'])}; if the "
            "behaviour you need belongs to one of those, change it there."
        )
    if not guidance:
        guidance.append(
            f"'{owning or '(root)'}' neither declares nor is declared by another "
            "module here, so it is a leaf."
        )

    return {
        "repo": target["full_name"],
        "path": path,
        "multi_module": True,
        "owning_module": owning or "(root)",
        "declares": ctx["declares"],
        "declared_by": ctx["declared_by"],
        "all_modules": ctx["modules"],
        "guidance": " ".join(guidance),
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
        chosen = exact[0] if exact else None
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
    chosen = exact[0] if exact else None
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
    name="report_gap",
    title="Report a defect in Git Synapse itself",
    description=(
        "Report that Git Synapse's own data or tooling is wrong: something missing "
        "that should be indexed, a value that contradicts the repository, data "
        "that was correct once and no longer is, a tool that failed, or a "
        "repository or path that should be covered and is not. "
        "Use this when you notice Git Synapse is the problem -- not to disagree with a "
        "coupling score, and not to suppress a suggestion. Reports go to a defect "
        "log that feeds no measure, score or ranking."
    ),
)
def report_gap(
    kind: str,
    detail: str,
    severity: str = "medium",
    tool: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    expected: str | None = None,
    observed: str | None = None,
) -> dict:
    """File a defect against Git Synapse.

    Args:
        kind: one of ``missing_data``, ``wrong_data``, ``stale_data``,
            ``tool_error``, ``coverage_gap``, ``suggestion``.
        detail: what is wrong, concretely enough to reproduce. Required.
        severity: ``low``, ``medium`` or ``high``. High means it would mislead
            someone into a wrong change.
        tool: the Git Synapse tool involved, if any.
        repo: repository the problem concerns.
        path: file path the problem concerns.
        expected: what the repository or history actually shows.
        observed: what Git Synapse returned instead.

    Returns:
        The report id and how many times this same defect has been seen. A
        repeat increments the count rather than creating a duplicate, so the
        count is a priority signal.
    """
    try:
        result = q.record_feedback(
            kind=kind,
            detail=detail,
            severity=severity,
            tool=tool,
            args={"repo": repo, "path": path},
            repo=repo,
            path=path,
            expected=expected,
            observed=observed,
        )
    except ValueError as exc:
        return {"error": str(exc), "valid_kinds": list(q.FEEDBACK_KINDS)}

    return {
        "recorded": True,
        "id": result["id"],
        "occurrences": result["occurrences"],
        "note": (
            f"Seen {result['occurrences']} times; first on {result['first_seen'][:10]}."
            if result["deduplicated"]
            else "First report of this defect."
        ),
        "reminder": (
            "This is a defect log for Git Synapse, not a correction to the coupling "
            "data. Nothing you write here changes a score."
        ),
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
    """Return the measure catalogue, with what each one measured.

    No measure is recommended. Which question you want asked depends on what you
    are doing, and only you know that: predicting what else must change is a
    different question from finding out what a codebase is shaped like. What is
    reported instead is how each fared at the first of those, next to what the
    same corpus yields with no history at all -- so a measure that does worse
    than looking at the file's test is visible as such.
    """
    return {
        "default": DEFAULT_MEASURE,
        "measured_on": MEASURED_ON,
        "free_lookup_hit_rate": FREE_LOOKUP_HIT_RATE,
        "measures": [
            {
                "key": s.key,
                "label": s.label,
                "family": s.family,
                "formula": s.formula,
                "summary": s.summary,
                "hit_rate": s.hit_rate,
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


def tool_names() -> list[str]:
    """Every tool this server exposes, in registration order."""
    return sorted(server._tool_manager._tools)


def _publish_tool_inventory() -> None:
    """Record what this server offers, so the UI can show never-called tools.

    Written by the MCP process rather than read by the API, which would mean the
    API importing this module to answer a question about a different container.
    A tool nobody calls is the interesting case -- without the inventory the
    activity page shows two tools and implies there are two.
    """
    from git_synapse.db.engine import set_watermark

    try:
        set_watermark("mcp_tools", ",".join(tool_names()))
        log.info("published %d tool name(s)", len(tool_names()))
    except Exception:
        log.warning("could not publish the tool inventory", exc_info=True)


def _guarded_app(host: str):
    """The MCP app, behind a token when this deployment asks for one.

    Wrapping the ASGI app rather than calling `server.run` is what makes the
    administrator's switch mean something here: without it the setting would be
    a control that changes nothing, which is worse than not offering it.

    The mode is read per request, so turning it on applies to the next call
    rather than the next restart.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from git_synapse import auth

    # `host` is not decoration: it configures the transport's allowed-Host
    # check. Omitted, every request from anywhere but localhost came back 421,
    # which reads as a protocol fault rather than a rejected Host header.
    app = server.streamable_http_app(host=host)

    async def gate(request, call_next):
        if auth.access_mode("mcp") != "required":
            return await call_next(request)
        header = request.headers.get("authorization") or ""
        user = (auth.token_user(header[7:].strip())
                if header.lower().startswith("bearer ") else None)
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"error": "this MCP server requires a token",
                         "hint": "create one under API tokens in the dashboard, "
                                 "then send it as: Authorization: Bearer gss_..."},
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=gate)
    return app


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
    _publish_tool_inventory()

    if args.transport == "stdio":
        # A subprocess on the caller's own machine: they already have whatever
        # access the token would grant, so a token here protects nothing.
        log.info("git-synapse mcp server on stdio")
        server.run(transport="stdio")
    elif args.transport == "sse":
        log.info("git-synapse mcp server (sse) on %s:%s", args.host, args.port)
        server.run(transport="sse", host=args.host, port=args.port)
    else:
        import uvicorn

        log.info("git-synapse mcp server (streamable http) on %s:%s/mcp", args.host, args.port)
        uvicorn.run(_guarded_app(args.host), host=args.host, port=args.port,
                    log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
