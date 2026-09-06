"""Nothing a user or an agent reads may describe a previous version.

They have never seen it. "The table that used to sit here ranked repositories
by co-change" costs a reader a paragraph and tells them nothing they can act
on, and an agent handed the same sentence spends tokens on it.

This covers the runtime surfaces: the MCP instructions and tool descriptions,
the API's own documentation and its error messages, and the CLI's help. The UI
is covered by tests/ui/layout.mjs, which reads what the pages actually render
rather than what the source contains -- most of that prose is assembled at
runtime.

It also covers the prose docs. The distinction there is not "no history" but
*whose* history: a rejected alternative stated as one -- "the obvious repair is
to widen the unit; measured, that construction is unsound" -- is rationale a
maintainer needs, and reads as true whenever it is read. "The table that used to
sit here" is a diff against a version the reader never saw, and rots the moment
it is written. The first survives this check by construction; the second is what
it looks for.

Code comments are not covered. They are read beside the code they annotate, by
someone who can see the current state next to them.
"""
from __future__ import annotations

import re

import pytest

#: Narrow on purpose. "Best used to confirm candidates" is employed-to, and
#: "where the reduction no longer holds" is about the maths -- both are real
#: sentences in this product and both are correct. Only phrasing that can mean
#: nothing except a previous version belongs here.
CHANGELOG = re.compile(
    r"used to (sit|be|show|live|say|appear|rank|have)"
    r"|(was|were) (tried|removed|replaced|dropped)"
    r"|we (tried|removed|used to|no longer|found)"
    r"|previously (shown|ranked|listed|here|credited)"
    r"|in an earlier (version|release)"
    r"|has been replaced"
    r"|old version",
    re.IGNORECASE,
)


def _offending(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if CHANGELOG.search(line)]


def test_the_mcp_instructions_describe_the_tool_not_its_history():
    """An agent is handed this on every connection and pays for every token."""
    from git_synapse.mcp import server

    assert not _offending(server.INSTRUCTIONS)


def test_no_mcp_tool_description_describes_a_previous_version():
    from git_synapse.mcp import server

    leaked = {
        name: _offending(tool.description)
        for name, tool in server.server._tool_manager._tools.items()
        if _offending(tool.description)
    }
    assert not leaked, leaked


def test_no_api_endpoint_documents_a_previous_version():
    """These are rendered at /api/docs, which is a page users open."""
    from git_synapse.api.main import app

    leaked = {
        getattr(route, "path", "?"): _offending(getattr(route, "description", ""))
        for route in app.routes
        if _offending(getattr(route, "description", ""))
    }
    assert not leaked, leaked


def test_no_error_message_explains_what_the_code_used_to_do():
    """A message shown when something has already gone wrong is the worst place
    to spend a reader's attention on history."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    leaked = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Only strings handed to a caller: exception arguments and returns.
            if isinstance(node, ast.Raise) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"HTTPException", "AuthError", "GitError"}
            ):
                for text in ast.walk(node):
                    if (
                        isinstance(text, ast.Constant)
                        and isinstance(text.value, str)
                        and CHANGELOG.search(text.value)
                    ):
                        leaked.append(f"{path.name}: {text.value[:80]}")
    assert not leaked, leaked


@pytest.mark.parametrize("doc", [
    "README.md", "DESIGN.md", ".env.example",
    "tests/ui/README.md", "skills/git-synapse-mcp/SKILL.md",
])
def test_no_document_narrates_its_own_past(doc):
    """A reader of the docs has never seen the previous version either.

    DESIGN.md is included, which is not a claim that it may not discuss what
    was rejected -- that is most of its job. The distinction is tense. "The
    obvious repair is to widen the unit; measured, that construction is
    unsound" is a fact about the problem and reads as true whenever it is read.
    "The table that used to sit here" is a diff against a build nobody can see,
    and rots the moment it is written. Only the second shape is matched.
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / doc
    if not path.exists():  # pragma: no cover - every one of these is committed
        pytest.skip(f"{doc} is not present")
    assert not _offending(path.read_text(encoding="utf-8"))
