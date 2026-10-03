"""`wirk import linear` against a live scratch WIRK service (set WIRK_TEST_URL and WIRK_TEST_ADMIN_TOKEN_FILE; skipped
otherwise), with Linear answered by synthetic pages: public teams only, the setup a person applies, the import, links and
their fallbacks, projects, milestones and initiatives with their links, an issue archived in Linear and then restored there,
and a re-run that writes nothing."""

import json
import os

import httpx
import pytest

from test_contract import ADMIN, URL, admin, person_with_agents
from test_import_linear import KEY, FakeLinear, comment, issue, ref
from wirk_cli import cli
from wirk_cli.importers import linear

pytestmark = pytest.mark.skipif(not (URL and ADMIN), reason="no scratch WIRK service configured")


def source():
    issues = [issue(1, description="token lin_api_" + "Z" * 40, project={"id": "p-1"}, projectMilestone={"id": "m-1"}),
              issue(2, parent=ref(1)),
              issue(3, state={"id": "st-ENG-Done"}, completedAt="2026-09-05T00:00:00.000Z"),
              issue(4, archivedAt="2026-03-01T00:00:00.000Z"), issue(5, "SEC")]
    relations = [{"id": "r-1", "type": "blocks", "issue": ref(1), "relatedIssue": ref(3)},
                 {"id": "r-2", "type": "blocks", "issue": ref(2), "relatedIssue": ref(1)}]
    return FakeLinear(issues, relations, [comment(1, 1, "Looks right"), comment(2, 1, "Agreed", parent={"id": "c-1"})])


@pytest.fixture
def imported(tmp_path, monkeypatch, capsys):
    tag = os.urandom(3).hex()
    space = admin({"request_id": f"l{tag}-space", "operations": [{"op": "wirkspace.create", "name": f"Linear {tag}"}]})
    workspace = space["data"]["results"][0]["workspace"]
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(tmp_path / "wirk"))
    monkeypatch.chdir(tmp_path)
    name = person_with_agents(capsys, tmp_path / "wirk", workspace)
    fake = source()
    monkeypatch.setattr(linear, "TRANSPORT", httpx.MockTransport(fake))
    (tmp_path / "wirk" / "import").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "wirk" / "import" / "linear-key").write_text(KEY)
    os.chmod(tmp_path / "wirk" / "import" / "linear-key", 0o600)

    def run(*argv):
        code = cli.main(["import", "linear", *argv])
        return code, capsys.readouterr().out

    code, out = run("--dry-run")
    assert code == 0, out
    setup = json.loads((tmp_path / "wirk" / "import" / "linear-setup.json").read_text())
    assert setup["operations"][0]["id"] == f"{name}-linear-import" and setup["operations"][-1]["op"] == "token.add"
    admin(setup)  # what the person's wirk admin --request sends
    return {"run": run, "dir": tmp_path / "wirk", "fake": fake}


def query(folder, body):
    token = (folder / "agent-token").read_text().strip()
    return httpx.post(f"{URL}/v2/query", json={**body, "format": "json"}, headers={"Authorization": f"Bearer {token}"},
                      timeout=30).json()


def item(folder, key, archived=False, kind="issue"):
    cards = query(folder, {"fields": {"text": f"Linear {kind} [{key}]", **({"archived": True} if archived else {})}})["data"]["cards"]
    if not cards:
        return None
    return query(folder, {"fetch": [cards[0]["id"]], "depth": "full", "max_bytes": 65536})["data"]["results"][0]


def test_linear_import_links_archives_and_restores_then_writes_nothing(imported):
    code, out = imported["run"]()
    assert code == 0 and "created: 15" in out and "archived: 2" in out, out  # 4 issues, a discussion and 10 around them
    folder = imported["dir"]
    one, two, three = (item(folder, f"ENG-{n}") for n in (1, 2, 3))
    assert "lin_api_" not in one["body"] and item(folder, "SEC-5") is None
    assert ("contributes_to", one["id"]) in {(link["type"], link["to"]) for link in two.get("links_out", [])}  # 2 under 1
    assert ("requires", two["id"]) in {(link["type"], link["to"]) for link in one.get("links_out", [])}  # 2 blocks 1
    assert "Blocked by: [ENG-1] (kept as related: completed here)" in three["body"] and three["fields"]["status"] == "completed"
    assert item(folder, "ENG-4") is None and item(folder, "ENG-4", archived=True) is not None
    plan, beta, retention = (item(folder, "Checkout v2", kind="project"), item(folder, "Checkout v2 · Beta", kind="milestone"),
                             item(folder, "Data retention", kind="project"))
    reliability = item(folder, "Reliability 2026", kind="initiative")
    assert item(folder, "Secret thing", kind="project") is None and item(folder, "Secret plan", kind="initiative") is None
    out_of = lambda view: {(link["type"], link["to"]) for link in view.get("links_out", [])}
    assert ("contributes_to", plan["id"]) in out_of(beta) and ("contributes_to", beta["id"]) in out_of(one)
    assert ("requires", beta["id"]) in out_of(retention) and plan["kind"] == "work" and reliability["kind"] == "doc"
    assert plan["id"] in {link["id"] for link in reliability.get("links_out", [])}
    code, out = imported["run"]("--json")
    assert {o["outcome"] for o in json.loads(out)["data"]["outcomes"]} == {"current"}
    imported["fake"].lists["issues"][3]["archivedAt"] = None  # restored in Linear
    code, out = imported["run"]()
    assert code == 0 and "restored: 1" in out and item(folder, "ENG-4") is not None
