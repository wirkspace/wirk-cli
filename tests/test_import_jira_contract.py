"""`wirk import jira` against a live scratch WIRK service (set WIRK_TEST_URL and WIRK_TEST_ADMIN_TOKEN_FILE; skipped
otherwise), with Jira Cloud answered by synthetic REST v3 pages: every project the token browses, a restricted issue skipped,
the setup a person applies, the import, links and their fallbacks, comments, and a re-run that writes nothing."""

import json
import os

import httpx
import pytest

from test_contract import ADMIN, URL, admin, person_with_agents
from test_import_jira import TOKEN, FakeJira, comment, issue, ref
from wirk_cli import cli
from wirk_cli.importers import jira

pytestmark = pytest.mark.skipif(not (URL and ADMIN), reason="no scratch WIRK service configured")
BLOCKS = {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}


def source():
    done = {"name": "Done", "statusCategory": {"key": "done"}}
    return FakeJira([
        issue(1, issuetype={"name": "Epic", "subtask": False, "hierarchyLevel": 1},
              comment={"comments": [comment(1, "## Looks right"), comment(2, "Agreed")], "total": 2, "startAt": 0, "maxResults": 100}),
        issue(2, parent=ref("SEED-1", "20001"), issuelinks=[{"id": "500", "type": BLOCKS, "outwardIssue": ref("SEED-3", "20003")}]),
        issue(3, issuelinks=[{"id": "500", "type": BLOCKS, "inwardIssue": ref("SEED-2", "20002")}]),
        issue(4, status=done, resolution={"name": "Fixed"}, resolutiondate="2026-09-05T00:00:00.000+0000",
              issuelinks=[{"id": "501", "type": BLOCKS, "inwardIssue": ref("SEED-1", "20001")}], description=None),
        issue(9, security={"name": "Staff only"}, summary="Secret plan")])


@pytest.fixture
def imported(tmp_path, monkeypatch, capsys):
    tag = os.urandom(3).hex()
    space = admin({"request_id": f"j{tag}-space", "operations": [{"op": "wirkspace.create", "name": f"Jira {tag}"}]})
    workspace = space["data"]["results"][0]["workspace"]
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(tmp_path / "wirk"))
    monkeypatch.chdir(tmp_path)
    name = person_with_agents(capsys, tmp_path / "wirk", workspace)
    monkeypatch.setattr(jira, "TRANSPORT", httpx.MockTransport(source()))
    (tmp_path / "wirk" / "import").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "wirk" / "import" / "jira-key").write_text(json.dumps({"site": "acme.atlassian.net", "email": "admin@example.com",
                                                                       "token": TOKEN}))
    os.chmod(tmp_path / "wirk" / "import" / "jira-key", 0o600)

    def run(*argv):
        code = cli.main(["import", "jira", *argv])
        return code, capsys.readouterr().out

    code, out = run("--dry-run")
    assert code == 0 and "skipped, not public: SEED-9." in out, out
    setup = json.loads((tmp_path / "wirk" / "import" / "jira-setup.json").read_text())
    assert setup["operations"][0]["id"] == f"{name}-jira-import" and setup["operations"][-1]["op"] == "token.add"
    admin(setup)  # what the person's wirk admin --request sends
    return {"run": run, "dir": tmp_path / "wirk"}


def query(folder, body):
    token = (folder / "agent-token").read_text().strip()
    return httpx.post(f"{URL}/v2/query", json={**body, "format": "json"}, headers={"Authorization": f"Bearer {token}"},
                      timeout=30).json()


def item(folder, text):
    cards = query(folder, {"fields": {"text": text}})["data"]["cards"]
    return query(folder, {"fetch": [cards[0]["id"]], "depth": "full", "max_bytes": 65536})["data"]["results"][0] if cards else None


def test_jira_import_links_comments_and_a_rerun_that_writes_nothing(imported):
    code, out = imported["run"]()
    assert code == 0 and "created: 8" in out, out  # four issues, one discussion and three project docs; the restricted one is skipped
    folder = imported["dir"]
    one, two, three, four = (item(folder, f"Jira issue [SEED-{n}]") for n in (1, 2, 3, 4))
    assert item(folder, "Jira issue [SEED-9]") is None
    out_of = lambda view: {(link["type"], link["to"]) for link in view.get("links_out", [])}
    assert ("contributes_to", one["id"]) in out_of(two) and ("requires", two["id"]) in out_of(three)
    assert "Is blocked by: [SEED-1] (kept as related: completed here)" in four["body"] and four["fields"]["status"] == "completed"
    discussion = item(folder, "Jira comments on [SEED-1]")
    assert "\\## Looks right" in discussion["body"] and "### Ben Sample" in discussion["body"]
    project = item(folder, "Jira project [SEED]")
    assert project["title"] == "Seed project" and "## Versions" in project["body"] and "@example.com" not in json.dumps(project)
    code, out = imported["run"]("--json")
    assert {o["outcome"] for o in json.loads(out)["data"]["outcomes"]} == {"current"}
