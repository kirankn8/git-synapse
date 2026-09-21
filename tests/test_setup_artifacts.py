import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_local_installers_are_present_and_shell_script_parses() -> None:
    shell = ROOT / "scripts/install.sh"
    powershell = ROOT / "scripts/install.ps1"
    assert shell.is_file()
    assert powershell.is_file()
    subprocess.run(["bash", "-n", str(shell)], check=True)
    assert "ADMIN_EMAIL" in shell.read_text(), "the installer must say how to require a sign-in"
    assert ".env" in shell.read_text()
    assert "releases/latest" in shell.read_text()


def test_helm_chart_has_secret_migration_and_health_contract() -> None:
    chart = ROOT / "charts/git-synapse"
    assert (chart / "Chart.yaml").is_file()
    assert (chart / "values.yaml").is_file()
    secrets = (chart / "templates/secrets.yaml").read_text()
    migration = (chart / "templates/migration-job.yaml").read_text()
    api = (chart / "templates/api.yaml").read_text()
    notes = (chart / "templates/NOTES.txt").read_text()
    # A cluster is shared, so the chart must ship an account rather than an
    # open deployment, and must say where its password is.
    assert "ADMIN_EMAIL" in secrets and "ADMIN_PASSWORD" in secrets
    assert "post-install,post-upgrade" in migration
    assert "/api/health" in api
    assert "jsonpath='{.data.ADMIN_PASSWORD}'" in notes


def test_setup_docs_explain_both_paths_and_secret_location() -> None:
    docs = (ROOT / "docs/setup.html").read_text()
    assert "scripts/install.sh" in docs
    assert "helm upgrade --install git-synapse" in docs
    assert "Kubernetes Secret" in docs
    assert "ADMIN_EMAIL" in docs and "ADMIN_PASSWORD" in docs


def test_github_pages_publishes_docs_root() -> None:
    """The workflow makes its own Pages site.

    Asking it to was once wrong: a private repository on the free plan has no
    Pages at all, so the step failed on that and the request was taken out.
    Once the repository is public the step is the only thing that turns the
    site on, and without it every run failed at Configure Pages while the
    README sent people to an address that answered 404.
    """
    workflow = (ROOT / ".github/workflows/pages.yml").read_text()
    index = (ROOT / "docs/index.html").read_text()
    assert "actions/deploy-pages" in workflow
    assert "enablement: true" in workflow
    assert "path: docs" in workflow
    assert "setup.html" in index


def test_the_published_pages_only_link_inside_themselves_or_out_to_github() -> None:
    """A relative link up out of docs/ breaks once the site is deployed.

    Only docs/ is published, so ../README.md resolves above the site root. It
    did not 404 either: it landed on the owner's user site and served whatever
    happened to be there, which is worse than an error because it looks like a
    page. Links either stay inside docs/ or name github.com outright.
    """
    for name in ("index.html", "setup.html"):
        page = (ROOT / "docs" / name).read_text()
        escaping = re.findall(r'href="(\.\./[^"]*)"', page)
        assert not escaping, f"docs/{name} links above the published root: {escaping}"


def test_the_landing_page_introduces_the_project_and_leads_to_the_guide() -> None:
    """The site root is where the README sends a stranger, so it has to say what
    this is before it asks them to install anything."""
    index = (ROOT / "docs/index.html").read_text()
    assert 'href="setup.html"' in index, "the landing page never reaches the guide"
    assert "coupl" in index.lower(), "the landing page never says what the tool does"
    assert "git clone" in index, "the landing page shows no way to start"
    readme = (ROOT / "README.md").read_text()
    assert "kirankn8.github.io/git-synapse" in readme, "the README never links the site"


def _requirement_names(lines: list[str]) -> set[str]:
    """Distribution names, stripped of version pins, extras and markers."""
    found = set()
    for line in lines:
        entry = line.strip().strip('",')
        if not entry or entry.startswith("#"):
            continue
        found.add(re.split(r"[><=;\[]", entry)[0].strip().lower())
    return found


def test_the_two_dependency_lists_name_the_same_distributions() -> None:
    """requirements.txt builds the image; pyproject.toml builds the package."""
    requirements = _requirement_names(
        (ROOT / "requirements.txt").read_text().splitlines()
    )
    pyproject = (ROOT / "pyproject.toml").read_text()
    declared = _requirement_names(
        pyproject.split("dependencies = [", 1)[1].split("\n]", 1)[0].splitlines()
    )
    assert requirements == declared, {
        "only in requirements.txt": sorted(requirements - declared),
        "only in pyproject.toml": sorted(declared - requirements),
    }
