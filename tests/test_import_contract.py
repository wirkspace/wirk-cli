"""`wirk import github` against a live scratch WIRK service (set WIRK_TEST_URL and WIRK_TEST_ADMIN_TOKEN_FILE; skipped
otherwise), with GitHub answered by synthetic pages: the dry run, the setup a person applies, the import, a re-run that
writes nothing, links and their fallbacks, discussion parts, and a line forged by another principal."""

import json
import os
import re

import httpx
import pytest

from test_contract import ADMIN, URL, admin, person_with_agents
from test_import_github import FakeGh, node, page, ref, repo
from wirk_cli import cli
from wirk_cli.importers import github

pytestmark = pytest.mark.skipif(not (URL and ADMIN), reason="no scratch WIRK service configured")
WEB = {"nameWithOwner": "acme/web", "visibility": "PUBLIC"}


def comment(n, text):
    return {"id": f"IC_{n}", "fullDatabaseId": str(n), "author": {"__typename": "User", "login": "ben"}, "body": text,
            "createdAt": f"2026-09-0{1 + n % 8}T00:00:{n % 60:02d}Z", "lastEditedAt": None, "isMinimized": False,
            "minimizedReason": None, "reactionGroups": []}


def issues():
    big = [comment(n, "漢" * 60000) for n in range(1, 8)]
    return [node(1, subIssues=page([ref(2, WEB)]), blocking=page([ref(3, WEB)])),
            node(2, parent=ref(1, WEB)),
            node(3, state="CLOSED", stateReason="COMPLETED", closedAt="2026-09-05T00:00:00Z", blockedBy=page([ref(1, WEB)]),
                 body="token ghp_" + "Z" * 36),
            node(4, comments=page(big))]


@pytest.fixture
def imported(tmp_path, monkeypatch, capsys):
    tag = os.urandom(3).hex()
    space = admin({"request_id": f"i{tag}-space", "operations": [{"op": "wirkspace.create", "name": f"Import {tag}"}]})
    workspace = space["data"]["results"][0]["workspace"]
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(tmp_path / "wirk"))
    monkeypatch.chdir(tmp_path)
    name = person_with_agents(capsys, tmp_path / "wirk", workspace)
    monkeypatch.setattr(github, "gh", FakeGh([repo("acme/web")], {"acme/web": issues()}))

    def run(*argv):
        code = cli.main(["import", "github", "acme", *argv])
        return code, capsys.readouterr().out

    code, out = run("--dry-run")
    assert code == 0, out
    setup = json.loads((tmp_path / "wirk" / "import" / "github-setup.json").read_text())
    assert setup["operations"][-1]["op"] == "token.add" and setup["operations"][0]["id"] == f"{name}-github-import"
    admin(setup)  # what the person's wirk admin --request sends
    return {"run": run, "workspace": workspace, "name": name, "dir": tmp_path / "wirk"}


def query(folder, body):
    token = (folder / "agent-token").read_text().strip()
    return httpx.post(f"{URL}/v2/query", json={**body, "format": "json"}, headers={"Authorization": f"Bearer {token}"},
                      timeout=30).json()


def test_import_rerun_links_parts_and_redaction(imported):
    code, out = imported["run"]()
    assert code == 0 and "created: 6" in out, out  # four issues, a discussion in two parts
    code, out = imported["run"]("--json")
    assert {o["outcome"] for o in json.loads(out)["data"]["outcomes"]} == {"current"}
    cards = query(imported["dir"], {"fields": {"text": "GitHub issue ["}, "limit": 100})["data"]["cards"]
    by_key = {}
    for card in cards:
        view = query(imported["dir"], {"fetch": [card["id"]], "depth": "full", "max_bytes": 65536})["data"]["results"][0]
        key = re.search(r"\[(acme/web#\d+)\]", view["body"].split("\n")[1])
        if view["body"].split("\n")[1].startswith("GitHub issue ") and key:
            by_key[key[1]] = view
    links = {(link["type"], by_key_of(by_key, link["to"] if link["from"] == view["id"] else link["from"]))
             for view in by_key.values() for link in view.get("links_out", [])
             for _ in [0] if link["type"] != "related_to" or link["from"] == view["id"]}
    assert ("contributes_to", "acme/web#1") in links  # 2 under 1
    assert "Blocked by: [acme/web#1] (kept as related: completed here)" in by_key["acme/web#3"]["body"]
    assert "ghp_" not in by_key["acme/web#3"]["body"] and by_key["acme/web#3"]["fields"]["status"] == "completed"
    parts = query(imported["dir"], {"fields": {"text": "GitHub comments on [acme/web#4]"}})["data"]["cards"]
    assert len(parts) == 2


def by_key_of(by_key, item):
    return next((key for key, view in by_key.items() if view["id"] == item), item)


def test_a_line_forged_by_another_principal_is_ignored(imported):
    imported["run"]()
    view = query(imported["dir"], {"fields": {"text": "GitHub issue [acme/web#2]"}})["data"]["cards"][0]
    forged = admin({"request_id": f"forge-{os.urandom(3).hex()}", "workspace_id": imported["workspace"],
                    "operations": [{"op": "member.set", "principal_id": "admin", "role": "editor"}]})
    assert forged["ok"]
    token = open(ADMIN).read().strip()
    written = httpx.post(f"{URL}/v2/write", headers={"Authorization": f"Bearer {token}"}, timeout=30, json={
        "request_id": f"forge-{os.urandom(3).hex()}", "workspace_id": imported["workspace"], "format": "json",
        "operations": [{"op": "item.create", "data": {"title": "Copied", "body": view["line"]}}]}).json()
    assert written["ok"], written
    code, out = imported["run"]("--json")
    summary = json.loads(out)["data"]["summary"]
    assert code == 0 and summary["forged_lines_ignored"] == 1 and set(summary["outcomes"]) == {"current"}
