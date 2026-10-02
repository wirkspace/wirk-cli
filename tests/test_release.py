"""The release workflow: a v* tag builds, tests and attaches the wheel, sdist and SHA256SUMS to a GitHub Release,
and publishes to PyPI by trusted publishing only once the project's variable allows it. No stored token anywhere."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_workflow():
    text = WORKFLOW.read_text()
    assert re.search(r"tags:\s*\[\s*['\"]v\*['\"]\s*\]", text)
    assert "secrets." not in text  # trusted publishing and the job's own token only
    assert "SHA256SUMS" in text and "gh release create" in text and "--verify-tag" in text
    assert re.search(r"if: \$\{\{ vars\.PYPI_PUBLISH == 'true' \}\}", text) and "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@" in text and "environment: pypi" in text
    assert "GITHUB_REF_NAME" in text and "pytest" in text
    for use in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), use  # actions pinned by commit


def test_every_workflow_parses_as_yaml():
    """GitHub rejects a workflow it cannot parse, and only says so once the workflow runs."""
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "jobs" in yaml.safe_load(workflow.read_text()), workflow.name
