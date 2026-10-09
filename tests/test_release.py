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
    assert "SHA256SUMS" in text and "./.github/workflows/publish-release.yml" in text
    assert "vars.PYPI_PUBLISH == 'true'" in text and "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@" in text and "environment: pypi" in text
    assert "GITHUB_REF_NAME" in text and "pytest" in text
    for use in re.findall(r"uses: (\S+)", text):
        assert use == "./.github/workflows/publish-release.yml" or re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), use


def test_public_qualification_precedes_promotion_and_trusted_pypi():
    caller = yaml.safe_load(WORKFLOW.read_text())
    shared = yaml.safe_load((WORKFLOW.parent / "publish-release.yml").read_text())
    assert caller["concurrency"]["cancel-in-progress"] is False
    assert caller["jobs"]["pypi"]["needs"] == "publish"
    assert "workflow_dispatch" in caller.get("on", caller.get(True))
    steps = shared["jobs"]["publish"]["steps"]
    commands = [step["run"] for step in steps if "run" in step]
    assert [command.split()[2] for command in commands] == ["publish", "qualify", "promote"]
    checkout = steps[0]["with"]
    assert checkout["repository"] == "${{ job.workflow_repository }}"
    assert checkout["ref"] == "${{ job.workflow_sha }}"
    artifact = next(step for step in steps if step.get("uses", "").startswith("actions/download-artifact"))
    assert artifact["with"] == {"name": "dist", "path": "dist"}
    assert shared["jobs"]["verify"]["permissions"] == {"contents": "read"}
    assert "permissions" not in shared and "permissions" not in shared["jobs"]["publish"]
    guard = shared["jobs"]["publish"]["if"]
    assert "inputs.verify_tag == ''" in guard
    assert "github.event_name == 'push'" in guard
    assert "startsWith(github.ref, 'refs/tags/')" in guard
    assert "GH_TOKEN" not in str(shared["jobs"]["verify"])
    token_steps = [step for step in steps if "GH_TOKEN" in step.get("env", {})]
    assert [step["run"].split()[2] for step in token_steps] == ["publish", "promote"]
    assert "concurrency" not in shared


def test_every_workflow_parses_as_yaml():
    """GitHub rejects a workflow it cannot parse, and only says so once the workflow runs."""
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "jobs" in yaml.safe_load(workflow.read_text()), workflow.name
