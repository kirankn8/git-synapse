"""Command-line bootstrap and maintenance. The server does everything else."""

from __future__ import annotations

import logging
import sys

import typer
from rich.console import Console
from rich.table import Table

from git_synapse.config import get_config
from git_synapse.db.engine import apply_schema, wait_for_database
from git_synapse.db.orm import models, session_scope
from git_synapse.ingest import accounts

app = typer.Typer(
    name="git-synapse",
    help="Change-coupling statistics over git history.",
    no_args_is_help=True,
    add_completion=False,
)
account_app = typer.Typer(name="account", help="Manage the sources that get scanned.",
                          no_args_is_help=True)
app.add_typer(account_app)
console = Console()


def _setup() -> None:
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        stream=sys.stderr,
    )
    wait_for_database()
    apply_schema()


@app.command("reset")
def reset(
    yes: bool = typer.Option(False, "--yes", help="Required. Destroys all ingested data."),
) -> None:
    """Drop all ingested data, keeping the schema. Mirrors on disk are untouched."""
    if not yes:
        console.print("[red]refusing without --yes[/red]")
        raise typer.Exit(1)
    _setup()
    with session_scope() as session:
        classes = [getattr(models(), name) for name in (
            "RepoImpact", "DepBump", "RepoDependency", "ModuleDependency", "RepoPackage",
            "FileRisk", "PairDrift", "FileCluster", "AuthorFile", "DirPairMetric", "FilePairMetric",
            "DirPair", "FilePair", "FileDirectory", "Directory", "CommitParent", "RefTag",
            "CommitFile", "Commit", "FileAlias", "File", "Author", "IngestRunRepo", "IngestRun", "Repo",
        )]
        for cls in classes:
            for row in session.query(cls).all():
                session.delete(row)
    console.print("[green]all ingested data removed[/green]")


def _account_rows(rows: list[dict]) -> Table:
    table = Table(box=None)
    for col in ("id", "login", "kind", "enabled", "repos", "filters", "last discovered"):
        table.add_column(col)
    for r in rows:
        filters = ", ".join(
            name for name, on in (
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
    no_archived: bool = typer.Option(False, "--no-archived", help="Skip archived repositories."),
    no_private: bool = typer.Option(False, "--no-private", help="Skip private repositories."),
    only: str = typer.Option("", "--only", help="Comma-separated allowlist of repo names."),
    skip: str = typer.Option("", "--skip", help="Comma-separated denylist of repo names."),
) -> None:
    """Add a source to scan: an org, user, group or workspace."""
    _setup()
    try:
        row = accounts.add_account(
            login, kind=kind,
            include_archived=not no_archived,
            include_private=not no_private,
            only_repos=only, skip_repos=skip,
        )
    except accounts.AccountError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(_account_rows([row]))
    console.print("[green]added[/green] — its repositories arrive on the next refresh, "
                  "or press Run now on the Jobs page")


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
