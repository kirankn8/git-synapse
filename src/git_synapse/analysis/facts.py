"""Identifiers a commit carries in plain text: issue keys, PR references and trailers.

These are stored as they are written, with the kind and the field they came from.
Nothing here decides that two commits sharing a key are one change; that is a
question a reader asks, and the facts let them ask it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Uppercase prefix, dash, digits. Deliberately organisation-agnostic.
ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-[0-9]+)\b")

#: A pull-request reference as GitHub writes it in a subject.
PR_REF = re.compile(r"\(#([0-9]+)\)")

#: Trailers worth keeping: machine-written identity, not prose.
_TRAILERS = ("change-id", "co-authored-by", "signed-off-by", "reviewed-on", "fixes")


@dataclass(frozen=True)
class Fact:
    kind: str
    key: str


def extract(subject: str | None, body: str | None) -> list[Fact]:
    """Every identifier this commit states, in the order found."""
    facts: list[Fact] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, key: str) -> None:
        key = key.strip()
        if key and (kind, key) not in seen:
            seen.add((kind, key))
            facts.append(Fact(kind, key))

    for match in ISSUE_KEY.finditer(subject or ""):
        add("issue_key", match.group(1))
    for match in PR_REF.finditer(subject or ""):
        add("pr_ref", match.group(1))
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name.strip().lower() in _TRAILERS and value.strip():
            add("trailer", f"{name.strip().lower()}: {value.strip()}"[:300])
    return facts


def backfill(conn: object | None = None, batch: int = 2000) -> int:
    """Store facts for commits ingested before extraction existed."""
    from git_synapse.db.orm import models, session_scope

    def run(session: object) -> int:
        Commit, Fact = models().Commit, models().CommitFact
        written = 0
        pending = session.query(Commit.id).outerjoin(
            Fact, Fact.commit_id == Commit.id).filter(Fact.commit_id.is_(None)).all()
        ids = [row[0] for row in pending]
        for start in range(0, len(ids), batch):
            rows = session.query(Commit.id, Commit.repo_id, Commit.subject, Commit.body).filter(
                Commit.id.in_(ids[start:start + batch])).all()
            for commit_id, repo_id, subject, body in rows:
                for fact in extract(subject, body):
                    session.add(Fact(commit_id=commit_id, repo_id=repo_id,
                                     kind=fact.kind, key=fact.key))
                    written += 1
            session.commit()
        return written

    if conn is not None:
        return run(conn)
    with session_scope() as session:
        return run(session)
