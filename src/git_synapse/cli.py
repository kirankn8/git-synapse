"""Command-line interface.

Every operation the scheduler and the API can perform is also available here,
so a deployment can be driven entirely from ``docker compose run --rm cli``.
"""

from __future__ import annotations

import logging
import sys

import typer
from rich.console import Console
from rich.table import Table

from git_synapse.analysis import query as q
from git_synapse.analysis import crossrepo, depbump, lagged, mining, predict
from git_synapse.analysis.aggregate import rebuild_repo, repos_needing_aggregation
from git_synapse.analysis.score import score_repo
from git_synapse.config import get_config
from git_synapse.db.engine import apply_schema, connection, query, wait_for_database
from git_synapse.ingest import accounts, pipeline
from git_synapse.stats.registry import DEFAULT_MEASURE, families

app = typer.Typer(
    name="git-synapse",
    help="Change-coupling statistics over git history.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _setup(verbose: bool = False) -> None:
    cfg = get_config()
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        stream=sys.stderr,
    )
    wait_for_database()
    apply_schema()


@app.command("init")
def init() -> None:
    """Create the schema. Idempotent, and run automatically by every service."""
    _setup()
    console.print("[green]schema applied[/green]")


@app.command("discover")
def discover() -> None:
    """Fetch the org's repository list from GitHub without cloning anything."""
    _setup()
    records = pipeline.discover(trigger="manual")
    table = Table(title=f"{len(records)} repositories selected", box=None)
    for col in ("repository", "language", "size (MB)", "mode", "visibility"):
        table.add_column(col)
    from git_synapse.ingest.gitops import choose_clone_mode

    for rec in sorted(records, key=lambda r: -(r.disk_usage_kb or 0))[:40]:
        blobless = choose_clone_mode(rec.disk_usage_kb)
        table.add_row(
            rec.full_name,
            rec.primary_language or "-",
            f"{(rec.disk_usage_kb or 0) / 1024:.1f}",
            "blobless" if blobless else "full",
            "private" if rec.is_private else "public",
        )
    console.print(table)
    if len(records) > 40:
        console.print(f"[dim]... and {len(records) - 40} more[/dim]")


@app.command("ingest")
def ingest(
    all_repos: bool = typer.Option(False, "--all", help="Discover from GitHub first."),
    repo: list[str] = typer.Option(None, "--repo", "-r", help="Limit to these repo names."),
    force_full: bool = typer.Option(False, "--force-full", help="Ignore watermarks."),
    concurrency: int = typer.Option(0, "--concurrency", "-j", help="Override worker count."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Mirror, parse, load, aggregate and score repositories."""
    _setup(verbose)

    records = None
    if not all_repos:
        records = pipeline.load_repo_records()
        if not records:
            console.print("[yellow]no repositories known yet; discovering[/yellow]")
            records = None
    if repo:
        wanted = {r.lower() for r in repo}
        source = records if records is not None else pipeline.discover()
        records = [r for r in source if r.name.lower() in wanted or r.full_name.lower() in wanted]
        if not records:
            console.print(f"[red]no repositories matched {repo}[/red]")
            raise typer.Exit(1)

    result = pipeline.run_ingest(
        records=records,
        trigger="manual",
        force_full=force_full,
        concurrency=concurrency or None,
    )

    console.print()
    console.print(
        f"[bold]run {result.run_id}[/bold] {result.status} in {result.duration_s:.1f}s: "
        f"[green]{len(result.ok)} ok[/green], [red]{len(result.failed)} failed[/red], "
        f"{result.commits_added} commits added"
    )
    if result.failed:
        table = Table(title="failures", box=None)
        table.add_column("repository")
        table.add_column("error", overflow="fold")
        for r in result.failed[:20]:
            table.add_row(r.full_name, (r.error or "")[:200])
        console.print(table)
        raise typer.Exit(1 if not result.ok else 0)


@app.command("aggregate")
def aggregate(
    repo_id: int = typer.Option(0, "--repo-id", help="One repo; 0 means all stale ones."),
    rescore: bool = typer.Option(True, "--rescore/--no-rescore"),
) -> None:
    """Rebuild the derived pair tables from the atomic facts."""
    _setup()
    targets = [repo_id] if repo_id else repos_needing_aggregation()
    if not targets:
        console.print("[dim]nothing to aggregate[/dim]")
        return
    for rid in targets:
        stats = rebuild_repo(rid)
        line = f"repo {rid}: {stats.file_pairs} file pairs, {stats.dir_pairs} dir pairs"
        if rescore:
            score = score_repo(rid)
            line += f", scored {score.file_pairs}"
        console.print(line)


@app.command("score")
def score(repo_id: int = typer.Option(0, "--repo-id", help="0 means every repo.")) -> None:
    """Recompute the 29 measures from the existing pair counts.

    Cheap, and the command to run after adding a new measure -- no re-clone or
    re-parse is needed because the contingency counts are already stored.
    """
    _setup()
    if repo_id:
        console.print(score_repo(repo_id))
        return
    rows = query("SELECT id, full_name FROM repo WHERE is_enabled ORDER BY id")
    for row in rows:
        stats = score_repo(row["id"])
        console.print(f"{row['full_name']}: {stats.file_pairs} pairs in {stats.duration_s:.1f}s")


@app.command("crossrepo")
def crossrepo_cmd(
    force: bool = typer.Option(False, "--force",
                               help="Re-partition every change set from scratch."),
) -> None:
    """Rebuild cross-repository coupling: change sets, repo pairs, file pairs.

    Incremental by default: only the tickets and authors touched by new commits
    are re-partitioned. Use --force after changing SESSION_GAP_HOURS or
    TICKET_PATTERN, since those alter every change set.
    """
    _setup()
    stats = crossrepo.rebuild(force=force)
    table = Table(box=None, show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("change sets", f"{stats.change_sets:,}")
    table.add_row("  ticket-linked", f"{stats.ticket_sets:,}")
    table.add_row("  temporal sessions", f"{stats.temporal_sets:,}")
    table.add_row("  pair-eligible", f"{stats.eligible_sets:,}")
    table.add_row("repo pairs", f"{stats.repo_pairs:,}")
    table.add_row("cross-repo file pairs", f"{stats.file_pairs:,}")
    table.add_row("duration", f"{stats.duration_s:.1f}s")
    console.print(table)


@app.command("depbump")
def depbump_cmd(
    force: bool = typer.Option(False, "--force", help="Rescan every repo, ignoring watermarks."),
) -> None:
    """Extract dependency-bump edges from manifest history.

    Go pseudo-versions embed the upstream commit they were cut from, so a go.mod
    diff yields a dated, directional, provable propagation edge. Incremental by
    default: only repositories ingested since their last scan are re-walked.
    """
    _setup()
    stats = depbump.rebuild(force=force)
    table = Table(box=None, show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("repos scanned", f"{stats.repos_scanned:,}")
    table.add_row("edges found", f"{stats.edges_found:,}")
    table.add_row("new edges stored", f"{stats.edges_written:,}")
    table.add_row("resolved to a commit", f"{stats.resolved_commits:,}")
    table.add_row("duration", f"{stats.duration_s:.1f}s")
    console.print(table)

    lags = depbump.propagation_lags(limit=15)
    if lags:
        lt = Table(title="observed propagation lag", box=None, title_style="bold")
        for col in ("dependency", "consumer", "bumps", "median lag", "p90 lag", "last"):
            lt.add_column(col)
        for r in lags:
            lt.add_row(r["dep"], r["consumer"], str(r["bumps"]),
                       f"{r['median_lag_days']}d" if r["median_lag_days"] is not None else "-",
                       f"{r['p90_lag_days']}d" if r["p90_lag_days"] is not None else "-",
                       str(r["last_bump"]))
        console.print(lt)


@app.command("lagged")
def lagged_cmd(
    bin_hours: int = typer.Option(0, "--bin-hours", help="Time bin width; 0 uses config."),
) -> None:
    """Compute directed, time-lagged coupling between repositories.

    Bins time, turns each repo into a binary vector over bins, and forms the 2x2
    table between A and B shifted by each lag. All 29 measures then apply, but
    become directional -- which is what distinguishes 'A precedes B' from the
    reverse.
    """
    _setup()
    stats = lagged.rebuild(bin_hours=bin_hours or None)
    table = Table(box=None, show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    table.add_row("repositories", f"{stats.n_repos:,}")
    table.add_row("time bins", f"{stats.n_bins:,} x {stats.bin_hours}h")
    table.add_row("lags evaluated", ", ".join(str(x) for x in stats.lags))
    table.add_row("directed rows", f"{stats.rows_written:,}")
    table.add_row("duration", f"{stats.duration_s:.1f}s")
    console.print(table)


@app.command("mine")
def mine(
    repo_id: int = typer.Option(0, "--repo-id", help="One repo; 0 means all stale ones."),
    force: bool = typer.Option(False, "--force", help="Re-mine every repo, ignoring watermarks."),
) -> None:
    """Rebuild the mining layer: de-facto modules, coupling drift, file risk.

    Incremental by default: only repositories whose history moved since their
    last mining pass are re-processed.
    """
    _setup()
    stats = mining.rebuild(repo_id or None, force=force)
    t = Table(box=None, show_header=False)
    t.add_column("metric", style="dim"); t.add_column("value", justify="right")
    t.add_row("de-facto modules", f"{stats.clusters:,}")
    t.add_row("  cross-directory", f"{stats.cross_directory_clusters:,}")
    t.add_row("clustered files", f"{stats.clustered_files:,}")
    t.add_row("drift rows", f"{stats.drift_rows:,}")
    t.add_row("  emerging", f"{stats.emerging:,}")
    t.add_row("  decaying", f"{stats.decaying:,}")
    t.add_row("risk scored", f"{stats.risk_rows:,}")
    t.add_row("duration", f"{stats.duration_s:.1f}s")
    console.print(t)


@app.command("impact")
def impact_cmd(
    repo: str = typer.Argument(..., help="Repository name."),
    direction: str = typer.Option("upstream", "--direction", "-d",
                                  help="upstream (where a fix may belong) or downstream."),
    limit: int = typer.Option(15, "--limit", "-n"),
) -> None:
    """What else to look at when changing a repository."""
    _setup()
    matches = [r for r in q.list_repos(search=repo, limit=8) if r["name"] == repo] or \
              q.list_repos(search=repo, limit=1)
    if not matches:
        console.print(f"[red]no repository matching {repo!r}[/red]")
        raise typer.Exit(1)
    target = matches[0]
    rows = (predict.upstream_of(target["id"], limit=limit)
            if direction.startswith("up")
            else predict.impact_for(target["id"], limit=limit))
    label = "upstream of" if direction.startswith("up") else "downstream of"
    t = Table(title=f"{label} {target['name']}", box=None, title_style="bold")
    for c in ("score", "evidence", "bumps", "median lag", "repository"):
        t.add_column(c, justify="right" if c in ("score","bumps","median lag") else "left")
    for r in rows:
        ev = "declared" if r["is_declared"] else ("bumps" if r["has_bump_history"] else "discovery")
        style = "green" if ev == "declared" else ("cyan" if ev == "bumps" else "dim")
        t.add_row(f"{r['score']:.3f}", f"[{style}]{ev}[/{style}]", str(r["bump_count"]),
                  f"{r['median_lag_days']:.1f}d" if r["median_lag_days"] is not None else "-",
                  r["name"])
    console.print(t)


@app.command("validate")
def validate_cmd(
    lag: int = typer.Option(1, "--lag"),
    top: int = typer.Option(12, "--top", "-n"),
) -> None:
    """Measure how well each association measure predicts real propagation."""
    _setup()
    from git_synapse.analysis.validate import evaluate
    scored = evaluate(lag_bins=lag, min_bumps=2)[:top]
    if not scored:
        console.print("[yellow]no ground truth; run `git-synapse depbump` and `git-synapse lagged`[/yellow]")
        return
    t = Table(title=f"measure quality at lag={lag} (ground truth: manifest bumps)",
              box=None, title_style="bold")
    for c in ("measure", "AUC", "P@10", "P@25", "dir.acc"):
        t.add_column(c, justify="right" if c != "measure" else "left")
    for s in scored:
        t.add_row(s.measure, f"{s.auc:.4f}", f"{s.precision_at[10]:.2f}",
                  f"{s.precision_at[25]:.2f}",
                  f"{s.directional_accuracy:.3f}" if s.directional_accuracy else "-")
    console.print(t)
    console.print(f"[dim]{scored[0].n_true} true edges among {scored[0].n_candidates:,} "
                  f"candidate ordered pairs[/dim]")


@app.command("chains")
def chains(
    repo: str = typer.Argument(..., help="Repository name to walk outward from."),
    depth: int = typer.Option(3, "--depth", "-d", help="Maximum hops."),
    min_confidence: float = typer.Option(0.15, "--min-confidence", "-c"),
    min_support: int = typer.Option(3, "--min-support"),
    limit: int = typer.Option(15, "--limit", "-n"),
) -> None:
    """Show transitive coupling chains: changing A implies B implies C."""
    _setup()
    matches = [r for r in q.list_repos(search=repo, limit=8) if r["name"] == repo] or \
              q.list_repos(search=repo, limit=1)
    if not matches:
        console.print(f"[red]no repository matching {repo!r}[/red]")
        raise typer.Exit(1)
    target = matches[0]

    rows = q.repo_chains(
        target["id"], max_depth=depth, min_confidence=min_confidence,
        limit=limit, min_support=min_support,
    )
    if not rows:
        console.print(
            f"[yellow]no chains from {target['name']} with per-hop confidence "
            f">= {min_confidence:.0%} and support >= {min_support}[/yellow]"
        )
        console.print("[dim]lower --min-confidence or --min-support to widen the search[/dim]")
        return

    table = Table(title=f"coupling chains from {target['name']}", box=None, title_style="bold")
    table.add_column("chain")
    table.add_column("path conf", justify="right", style="cyan")
    table.add_column("hops", justify="right")
    table.add_column("support", justify="right", style="dim")
    for c in rows:
        names = c["repo_names"]
        hops = [float(h) for h in c["hops"]]
        chain = " ".join(
            f"[bold]{names[i]}[/bold] →{hops[i]:.0%}→" for i in range(len(hops))
        ) + f" [bold]{names[-1]}[/bold]"
        table.add_row(chain, f"{float(c['path_conf']):.2%}", str(c["depth"]),
                      ",".join(str(x) for x in c["supports"]))
    console.print(table)


@app.command("xcoupled")
def xcoupled(
    repo: str = typer.Argument(..., help="Repository name."),
    measure: str = typer.Option(DEFAULT_MEASURE, "--measure", "-m"),
    limit: int = typer.Option(15, "--limit", "-n"),
    min_support: int = typer.Option(3, "--min-support"),
) -> None:
    """Which other repositories change together with this one."""
    _setup()
    matches = [r for r in q.list_repos(search=repo, limit=8) if r["name"] == repo] or \
              q.list_repos(search=repo, limit=1)
    if not matches:
        console.print(f"[red]no repository matching {repo!r}[/red]")
        raise typer.Exit(1)
    target = matches[0]

    rows = q.repo_partners(target["id"], measure, limit, min_support)
    table = Table(title=f"repositories coupled to {target['name']}", box=None, title_style="bold")
    table.add_column(measure, justify="right", style="cyan")
    table.add_column("P(it|this)", justify="right")
    table.add_column("P(this|it)", justify="right")
    table.add_column("shared", justify="right")
    table.add_column("ticket-backed", justify="right", style="dim")
    table.add_column("repository")
    for r in rows:
        table.add_row(
            f"{(r.get('score') or 0):.3f}",
            f"{(r.get('confidence_out') or 0):.0%}",
            f"{(r.get('confidence_in') or 0):.0%}",
            str(r["n_ab"]),
            f"{r['n_ab_ticket']} ({(r['ticket_ratio'] or 0):.0%})",
            r["name"],
        )
    console.print(table)


@app.command("measures")
def measures() -> None:
    """List every association measure with its formula and guidance."""
    for family, specs in families().items():
        table = Table(title=family, box=None, title_justify="left", title_style="bold cyan")
        table.add_column("key", style="green")
        table.add_column("formula", style="dim")
        table.add_column("summary", overflow="fold")
        table.add_column("flags", style="yellow")
        for spec in specs:
            flags = []
            if spec.recommended:
                flags.append("recommended")
            if spec.rare_item_bias:
                flags.append("rare-item bias")
            if spec.saturates_on_sparse:
                flags.append("saturates")
            table.add_row(spec.key, spec.formula, spec.summary, ", ".join(flags))
        console.print(table)
        console.print()


@app.command("coupled")
def coupled(
    repo: str = typer.Argument(..., help="Repository name."),
    path: str = typer.Argument(..., help="File path within the repository."),
    measure: str = typer.Option(DEFAULT_MEASURE, "--measure", "-m"),
    limit: int = typer.Option(15, "--limit", "-n"),
    min_support: int = typer.Option(2, "--min-support"),
) -> None:
    """Show what changes together with a file. The core question, from the shell."""
    _setup()
    target = q.resolve_file(repo, path)
    if target is None:
        console.print(f"[red]no file {path!r} in {repo!r}[/red]")
        raise typer.Exit(1)

    partners = q.coupled_files(target["id"], measure, limit, min_support)
    table = Table(
        title=f"{target['repo']} :: {target['path']}  ({target['change_count']} changes)",
        box=None,
        title_style="bold",
    )
    table.add_column(measure, justify="right", style="cyan")
    table.add_column("P(also|this)", justify="right")
    table.add_column("n_ab", justify="right")
    table.add_column("G2", justify="right", style="dim")
    table.add_column("file")
    for p in partners:
        table.add_row(
            f"{(p.get('score') or 0):.3f}",
            f"{(p.get('confidence_out') or 0):.0%}",
            str(p["n_ab"]),
            f"{(p.get('log_likelihood_ratio') or 0):.1f}",
            p["path"],
        )
    console.print(table)


@app.command("feedback")
def feedback_cmd(
    status: str = typer.Option("open", "--status", help="open|investigating|fixed|wontfix|all"),
    kind: str = typer.Option("", "--kind"),
    resolve: int = typer.Option(0, "--resolve", help="Report id to close."),
    as_status: str = typer.Option("fixed", "--as", help="Status to set when resolving."),
    note: str = typer.Option("", "--note", help="Resolution note."),
) -> None:
    """Review defects that sessions reported against Git Synapse.

    The occurrence count is the priority signal: a gap twenty sessions hit
    matters more than one seen once.
    """
    _setup()
    if resolve:
        if q.resolve_feedback(resolve, as_status, note or f"marked {as_status}"):
            console.print(f"[green]report {resolve} -> {as_status}[/green]")
        else:
            console.print(f"[red]no report {resolve}[/red]")
            raise typer.Exit(1)
        return

    summary = q.feedback_summary()
    st = Table(box=None, show_header=False)
    st.add_column("metric", style="dim"); st.add_column("value", justify="right")
    for key in ("total", "open", "open_high", "fixed", "total_hits", "seen_today"):
        st.add_row(key.replace("_", " "), str(summary.get(key, 0)))
    console.print(st)

    rows = q.list_feedback(None if status == "all" else status, kind or None, 60)
    if not rows:
        console.print("[dim]no reports[/dim]")
        return
    t = Table(title=f"reports ({status})", box=None, title_style="bold")
    for col in ("id", "hits", "sev", "kind", "tool", "where", "detail"):
        t.add_column(col, justify="right" if col in ("id", "hits") else "left",
                     overflow="fold" if col == "detail" else None)
    for r in rows:
        sev = r["severity"]
        style = "red" if sev == "high" else ("yellow" if sev == "medium" else "dim")
        where = "/".join(x for x in (r["repo"], r["path"]) if x) or "-"
        t.add_row(str(r["id"]), str(r["occurrences"]), f"[{style}]{sev}[/{style}]",
                  r["kind"], r["tool"] or "-", where[-38:], (r["detail"] or "")[:70])
    console.print(t)


@app.command("status")
def status() -> None:
    """Corpus summary and recent ingest runs."""
    _setup()
    ov = q.overview()
    table = Table(box=None, show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    for key in (
        "repos", "repos_ready", "repos_failed", "commits", "file_changes",
        "files", "directories", "authors", "file_pairs", "dir_pairs",
    ):
        table.add_row(key.replace("_", " "), f"{ov.get(key, 0):,}")
    table.add_row("mirror size", f"{ov.get('mirror_kb', 0) / 1024 / 1024:.2f} GB")
    console.print(table)

    runs = q.recent_runs(5)
    if runs:
        rt = Table(title="recent runs", box=None, title_style="bold")
        for col in ("id", "kind", "trigger", "status", "started", "duration", "commits"):
            rt.add_column(col)
        for r in runs:
            rt.add_row(
                str(r["id"]), r["kind"], r["trigger"], r["status"],
                r["started_at"].strftime("%Y-%m-%d %H:%M") if r["started_at"] else "-",
                f"{r['duration_s']:.0f}s" if r["duration_s"] else "-",
                str(r["commits_added"]),
            )
        console.print(rt)


@app.command("reset")
def reset(
    yes: bool = typer.Option(False, "--yes", help="Required. Destroys all ingested data."),
) -> None:
    """Drop all ingested data, keeping the schema. Mirrors on disk are untouched."""
    if not yes:
        console.print("[red]refusing without --yes[/red]")
        raise typer.Exit(1)
    _setup()
    with connection() as conn:
        conn.execute(
            "TRUNCATE repo, author, ingest_run RESTART IDENTITY CASCADE"
        )
    console.print("[green]all ingested data removed[/green]")


account_app = typer.Typer(name="account", help="Manage the orgs and users that get scanned.", no_args_is_help=True)
app.add_typer(account_app)


def _account_rows(rows: list[dict]) -> Table:
    """Render accounts as a table, shared by add/list/remove."""
    table = Table(box=None)
    for col in ("id", "login", "kind", "enabled", "repos", "filters", "last discovered"):
        table.add_column(col)
    for r in rows:
        filters = ", ".join(
            name for name, on in (
                ("no forks", not r["include_forks"]),
                ("no archived", not r["include_archived"]),
                ("no private", not r["include_private"]),
            ) if on
        )
        if r["only_repos"]:
            filters = f"only {len(r['only_repos'])}"
        table.add_row(
            str(r["id"]), r["login"], r["kind"],
            "yes" if r["enabled"] else "no",
            str(r.get("live_repo_count", r["repo_count"])),
            filters or "-",
            r["last_discovered_at"].strftime("%Y-%m-%d %H:%M") if r["last_discovered_at"] else "never",
        )
    return table


@account_app.command("add")
def account_add(
    login: str = typer.Argument(..., help="GitHub org or user login."),
    kind: str = typer.Option("org", "--kind", help="org or user."),
    no_forks: bool = typer.Option(False, "--no-forks", help="Skip forked repositories."),
    no_archived: bool = typer.Option(False, "--no-archived", help="Skip archived repositories."),
    no_private: bool = typer.Option(False, "--no-private", help="Skip private repositories."),
    only: str = typer.Option("", "--only", help="Comma-separated allowlist of repo names."),
    skip: str = typer.Option("", "--skip", help="Comma-separated denylist of repo names."),
) -> None:
    """Add an organisation or user to scan."""
    _setup()
    try:
        row = accounts.add_account(
            login, kind=kind,
            include_forks=not no_forks,
            include_archived=not no_archived,
            include_private=not no_private,
            only_repos=only, skip_repos=skip,
        )
    except accounts.AccountError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(_account_rows([row]))
    console.print("[green]added[/green] — run `git-synapse discover` to pick up its repositories")


@account_app.command("list")
def account_list() -> None:
    """List every configured account."""
    _setup()
    rows = accounts.list_accounts()
    if not rows:
        console.print("[yellow]no accounts configured[/yellow] — add one with `git-synapse account add <login>`")
        return
    console.print(_account_rows(rows))


@account_app.command("remove")
def account_remove(account_id: int = typer.Argument(..., help="Account id, from `account list`.")) -> None:
    """Stop scanning an account. Its repositories and statistics are kept."""
    _setup()
    if not accounts.remove_account(account_id):
        console.print(f"[red]account {account_id} not found[/red]")
        raise typer.Exit(1)
    console.print("[green]removed[/green] — its repositories stay, but stop being refreshed")


@account_app.command("enable")
def account_enable(
    account_id: int = typer.Argument(..., help="Account id, from `account list`."),
    off: bool = typer.Option(False, "--off", help="Disable instead of enabling."),
) -> None:
    """Enable or disable an account without deleting it."""
    _setup()
    try:
        row = accounts.update_account(account_id, enabled=not off)
    except accounts.AccountError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(_account_rows([row]))


if __name__ == "__main__":
    app()
