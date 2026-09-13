"""Replay history and measure whether coupling suggestions would have helped.

    docker compose run --rm -v "$PWD:/repo" --entrypoint python cli /repo/scripts/backtest.py --top 5
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from git_synapse.analysis import backtest as bt
from git_synapse.db.engine import apply_schema, wait_for_database
from git_synapse.db.orm import models, session_scope
from git_synapse.stats.registry import DEFAULT_MEASURE

console = Console()


def main(
    repo: str = typer.Option("", "--repo", "-r", help="Restrict to one repository."),
    measure: str = typer.Option("", "--measure", "-m", help="Comma-separated measures."),
    k: int = typer.Option(5, "--top", "-k", help="How many suggestions may be offered."),
    min_support: int = typer.Option(2, "--min-support", help="Ignore pairs seen fewer times."),
    limit: int = typer.Option(0, "--limit", help="Stop after this many commits."),
    seeding: str = typer.Option("all", "--seeding",
                                help="all: every changed file seeds a prompt. "
                                     "obscure: one prompt per commit, seeded by its least-changed file."),
    grep_sample: int = typer.Option(0, "--grep-sample",
                                    help="Also score a content-grep baseline on this many prompts."),
) -> None:
    wait_for_database()
    apply_schema()

    repo_id = None
    if repo:
        with session_scope() as session:
            row = session.query(models().Repo).filter(
                (models().Repo.full_name == repo) | (models().Repo.name == repo)
            ).first()
        if not row:
            console.print(f"[red]no repository {repo!r}[/red]")
            raise typer.Exit(1)
        repo_id = row.id

    keys = tuple(m.strip() for m in measure.split(",") if m.strip()) or (DEFAULT_MEASURE,)
    result = bt.run(repo_id, keys, k=k, min_support=min_support,
                    limit=limit or None, grep_sample=grep_sample, seeding=seeding)
    if not result.prompts:
        console.print("[yellow]not enough history to replay; ingest more commits first[/yellow]")
        return

    seeded = "" if result.seeding == "all" else f", {result.seeding} seeds"
    table = Table(title=f"backtest: {result.prompts:,} prompts over {result.commits_scored:,} "
                        f"commits (top-{k}{seeded})", box=None, title_style="bold")
    for column in ("measure", "hit rate", "95% CI", "lift", "unsolved", "MRR", ""):
        table.add_column(column, justify="left" if column == "measure" else "right")
    for s in result.baselines + result.scores:
        base = s in result.baselines
        table.add_row(
            s.label if base else s.measure,
            f"{s.hit_rate:.1%}",
            f"{s.ci_low:.1%}-{s.ci_high:.1%}",
            "-" if base else f"{s.lift:.2f}x",
            "-" if base else f"{s.hard_hit_rate:.1%}",
            "-" if base else f"{s.mrr:.3f}",
            "rare-item bias" if s.rare_item_bias else "",
            style="dim" if base else None,
        )
    console.print(table)
    console.print(f"[bold]{result.verdict}[/bold]")
    best = result.best
    if best is not None and best.unaided_prompts:
        low, high = best.unaided_ci
        console.print(
            f"[cyan]neither the free rules nor the New Hire solved "
            f"{best.unaided_prompts:,} of the {result.sampled:,} sampled prompts; "
            f"{best.measure} answered {best.unaided_hit_rate:.1%} of those "
            f"({low:.1%}-{high:.1%})[/cyan]")
    if not result.conclusive:
        console.print("[dim]hit rate = share of prompts where a correct file appeared in the "
                      "top k. Prompts from one commit are not independent, so the true "
                      "interval is wider.[/dim]")


if __name__ == "__main__":
    typer.run(main)
