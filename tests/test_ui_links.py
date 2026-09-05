"""Every in-app link must point at a route that exists.

A commit that renamed the routes left eleven links behind pointing at the old
addresses. All of them 404'd, and the suite stayed green because the smoke test
renders routes without ever following a link. This closes that gap with string
work alone -- no browser, no server, so a rename is caught immediately.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
CSS = Path(__file__).resolve().parents[1] / "web" / "static" / "style.css"
SHELL = Path(__file__).resolve().parents[1] / "web" / "index.html"
MAIN = Path(__file__).resolve().parents[1] / "src" / "git_synapse" / "api" / "main.py"


def _routes() -> list[re.Pattern[str]]:
    """The client's own route table, compiled the way the router compiles it."""
    patterns = re.findall(r"^on\('([^']+)'", APP.read_text(), re.M)
    assert patterns, "no routes found; the regex probably broke"
    return [
        re.compile("^" + re.sub(r"[:*]([a-zA-Z]+)",
                                lambda m: "(.+)" if m.group(0)[0] == "*" else "([^/]+)",
                                p) + "$")
        for p in patterns
    ]


def _fill(template: str) -> str:
    """Replace each `${...}` hole with what it can stand for in a path.

    Holes nest -- a ternary inside a template inside a hole -- so they are
    matched by counting braces rather than with a regex. A hole that follows a
    slash is one segment; one that does not is a suffix or a query fragment and
    contributes nothing a route can be matched against.
    """
    out, i = [], 0
    while i < len(template):
        if template.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(template) and depth:
                depth += (template[j] == "{") - (template[j] == "}")
                j += 1
            inner = template[i + 2:j - 1]
            after_slash = bool(out) and out[-1].endswith("/")
            if after_slash:
                # A path variable carries slashes of its own; anything else is
                # exactly one segment.
                out.append("seg/seg" if "path" in inner else "seg")
            i = j
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


def _links() -> set[str]:
    """Every internal destination the app can navigate to."""
    src = APP.read_text()
    found: set[str] = set()
    raws = re.findall(r"(?:href: *|go\()`([^`]*)`", src)
    raws += re.findall(r"(?:href: *|go\()'(/[^']*)'", src)
    for raw in raws:
        if not raw.startswith("/"):
            continue
        path = _fill(raw).split("?")[0]
        found.add(path.rstrip("/") or "/")
    return found


@pytest.mark.parametrize("link", sorted(_links()))
def test_every_internal_link_matches_a_registered_route(link):
    assert any(rx.match(link) for rx in _routes()), \
        f"{link} is linked but no client route matches it"


def test_every_nav_link_matches_a_registered_route():
    nav = re.search(r'<nav class="mainnav".*?</nav>', SHELL.read_text(), re.S)
    assert nav, "the shell no longer has a main nav"
    for href in re.findall(r'href="(/[^"]*)"', nav.group(0)):
        assert any(rx.match(href) for rx in _routes()), f"nav links to {href}, which has no route"


def test_the_server_serves_every_top_level_route_the_client_claims():
    """A client route the server does not know 404s on reload and on a pasted
    link -- exactly when a shareable URL earns its keep."""
    claimed = {p.split("/")[1] for p in re.findall(r"^on\('(/[^']+)'", APP.read_text(), re.M)}
    claimed.discard("")
    for node in ast.walk(ast.parse(MAIN.read_text())):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SPA_ROUTES":
            served = set(ast.literal_eval(node.value))
            break
    else:
        raise AssertionError("SPA_ROUTES no longer exists in api/main.py")
    assert claimed <= served, f"the SPA routes {sorted(claimed - served)}, which 404 on reload"


def test_script_hidden_elements_are_really_hidden():
    """`el.hidden = true` only sets an attribute. The browser's [hidden] rule is
    display:none, which any author `display` on the same element beats -- the
    measure bar is display:flex, so hiding it changed nothing on screen while
    every assertion on the property passed. One global rule settles it."""
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS.read_text()), \
        "style.css must force [hidden] to display:none, or scripted hiding is a no-op"

    hidden_from_script = re.findall(r"(\w+)\.hidden\s*=", APP.read_text())
    assert hidden_from_script, "nothing hides itself any more; drop this test with the rule"
