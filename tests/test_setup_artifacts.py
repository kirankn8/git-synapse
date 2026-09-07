import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_local_installers_are_present_and_shell_script_parses() -> None:
    shell = ROOT / "scripts/install.sh"
    powershell = ROOT / "scripts/install.ps1"
    assert shell.is_file()
    assert powershell.is_file()
    subprocess.run(["bash", "-n", str(shell)], check=True)
    assert "docker compose run --rm cli admin setup-token" in shell.read_text()
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
    assert "ADMIN_SETUP_TOKEN" in secrets
    assert "post-install,post-upgrade" in migration
    assert "/api/health" in api
    assert "jsonpath='{.data.ADMIN_SETUP_TOKEN}'" in notes


def test_setup_docs_explain_both_paths_and_secret_location() -> None:
    docs = (ROOT / "docs/setup.html").read_text()
    assert "scripts/install.sh" in docs
    assert "helm upgrade --install git-synapse" in docs
    assert "Kubernetes Secret" in docs
    assert "docker compose run --rm cli admin setup-token" in docs


def test_github_pages_publishes_docs_root() -> None:
    workflow = (ROOT / ".github/workflows/pages.yml").read_text()
    index = (ROOT / "docs/index.html").read_text()
    assert "actions/deploy-pages" in workflow
    assert "path: docs" in workflow
    assert "setup.html" in index
