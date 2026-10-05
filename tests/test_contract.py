"""The CLI against a live scratch WIRK service (set WIRK_TEST_URL and WIRK_TEST_ADMIN_TOKEN_FILE; skipped otherwise).

The fixture makes a fresh agent token with `wirk login`, registers its digest through the service's administration
route as the scratch service's administrator would, and then drives every command through the real network path.
"""

import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
from pathlib import Path

import httpx
import pytest

from wirk_cli import cli

URL = os.environ.get("WIRK_TEST_URL")
ADMIN = os.environ.get("WIRK_TEST_ADMIN_TOKEN_FILE")
pytestmark = pytest.mark.skipif(not (URL and ADMIN), reason="no scratch WIRK service configured")


def admin(body):
    token = Path(ADMIN).read_text().strip()
    answer = httpx.post(f"{URL}/v2/admin", json={**body, "format": "json"},
                        headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    assert answer["ok"], answer
    return answer


def person_with_agents(capsys, directory, workspace, role="editor", person_role="administrator", rows=True):
    """A person and their agents' principal; the agents' token is made by `wirk login` in `directory`. Without rows
    the agents have no membership of their own and act with their person's role, like the agents `wirk login` makes
    through browser approval."""
    name = "t" + secrets.token_hex(4)
    os.environ["WIRK_CONFIG_DIR"] = str(directory)
    assert cli.main(["login", "--url", URL]) == 0
    digest = re.search(r"\b[0-9a-f]{64}\b", capsys.readouterr().out).group(0)
    admin({"request_id": f"{name}-people", "workspace_id": workspace, "operations": [
        {"op": "principal.create", "id": name, "kind": "person", "name": f"Tester {name}"},
        {"op": "principal.create", "id": f"{name}-agents", "kind": "agent", "name": f"Agents of {name}", "person_id": name},
        {"op": "token.add", "principal_id": f"{name}-agents", "sha256": digest, "label": "test"},
        *([{"op": "member.set", "principal_id": f"{name}-agents", "role": role}] if rows else []),
        {"op": "member.set", "principal_id": name, "role": person_role}]})
    return name


def newcomer(tmp_path, monkeypatch, capsys, rows=True):
    """A fresh person (an administrator) and their agents in a fresh wirkspace, logged in through `wirk login`."""
    tag = secrets.token_hex(4)
    space = admin({"request_id": f"s{tag}-space", "operations": [{"op": "wirkspace.create", "name": f"Scratch {tag}"}]})
    workspace = space["data"]["results"][0]["workspace"]
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(tmp_path / "wirk"))
    monkeypatch.chdir(tmp_path)
    name = person_with_agents(capsys, tmp_path / "wirk", workspace, rows=rows)
    return {"name": name, "workspace": workspace, "dir": tmp_path / "wirk", "tmp": tmp_path}


@pytest.fixture
def agent(tmp_path, monkeypatch, capsys):
    """Agents with a membership row of their own (editor), so they act only through it."""
    return newcomer(tmp_path, monkeypatch, capsys)


def wirk(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def as_person(monkeypatch, capsys, name, *argv):
    """Run a person's command as `name`, as if at their terminal typing what it asks; the first call makes and
    registers their person token the way an administrator would."""
    typed = []
    monkeypatch.setattr(cli, "at_terminal", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: typed.append(prompt) or prompt.split('"')[1])
    if not (Path(os.environ["WIRK_CONFIG_DIR"]) / "person-token").exists():
        code, out, err = wirk(capsys, "login", "--person")
        digest = re.search(r"\b[0-9a-f]{64}\b", out).group(0)
        admin({"request_id": f"{name}-person-token", "operations": [
            {"op": "token.add", "principal_id": name, "sha256": digest, "label": "test person"}]})
    return wirk(capsys, *argv, "--person")


def created(out):
    return re.search(r"created ([0-9a-f]{8})", out).group(1)


def test_login_status_and_the_context_header(agent, capsys):
    code, out, err = wirk(capsys, "login")
    assert code == 0 and out.count("Logged in") == 1 and out.count("status") == 1, out  # one line says what is next
    code, out, err = wirk(capsys, "status")
    assert code == 0 and f"{agent['name']}-agents" in out and "Your client reports: wirk-cli" in out
    assert "context_ignored" not in out


def test_write_fetch_list_and_search(agent, capsys):
    code, out, err = wirk(capsys, "write", "new", "Retry failed webhooks", "owner=me", "--criterion", "Backs off",
                          "--body", "Failed deliveries are retried with backoff.")
    assert code == 0, out + err
    created = re.search(r"created ([0-9a-f]{8})", out).group(1)
    code, out, err = wirk(capsys, "query", created)
    assert code == 0 and "Retry failed webhooks" in out
    code, out, err = wirk(capsys, "query", "owner=me")
    assert code == 0 and created in out and re.search(r"of \d+", out)
    code, out, err = wirk(capsys, "query", "about=webhooks backoff")
    assert code == 0 and created in out
    code, out, err = wirk(capsys, "query", "text=deliveries")
    assert code == 0 and created in out


def test_a_progress_note_linked_to_two_items_and_a_status_change(agent, capsys):
    ids = []
    for title in ("Parent one", "Parent two"):
        code, out, err = wirk(capsys, "write", "new", title, "kind=work")
        ids.append(re.search(r"created ([0-9a-f]{8})", out).group(1))
    code, out, err = wirk(capsys, "write", "new", "Progress", "--body", "Did it.", "--link", f"related_to:{ids[0]}",
                          "--link", f"related_to:{ids[1]}")
    assert code == 0 and out.count("linked") == 2
    code, out, err = wirk(capsys, "write", "edit", f"{ids[0]}@1", "status=cancelled")
    assert code == 0, out + err
    code, out, err = wirk(capsys, "write", "edit", f"{ids[0]}@1", "status=open")
    assert code == 1 and "r2" in out  # the stale revision is refused with the current one


def test_completion_needs_evidence_and_takes_it(agent, capsys):
    code, out, err = wirk(capsys, "write", "new", "Ship it", "kind=work")
    item = re.search(r"created ([0-9a-f]{8})", out).group(1)
    code, out, err = wirk(capsys, "write", "edit", f"{item}@1", "status=completed")
    assert code == 1 and "reason_required" in out and "evidence" in out
    code, out, err = wirk(capsys, "write", "edit", f"{item}@1", "status=completed", "--evidence", "tests/test_ship.py passes")
    assert code == 0, out + err


def test_every_read_continuation_runs_as_printed(agent, capsys):
    for n in range(25):
        wirk(capsys, "write", "new", f"Card {n}", "kind=work")
    code, out, err = wirk(capsys, "query", "kind=work", "limit=5")
    lines = [line for line in out.split("\n") if re.match(r"^\s*(?:\d+ )?more[^:]*: ", line) or line.startswith("next: ")]
    assert lines, out
    for line in lines:
        command = line.split(": ", 1)[1]
        code, out, err = wirk(capsys, *shlex.split(command))
        assert code == 0, (line, out, err)


def test_an_identical_resend_applies_once(agent, capsys):
    argv = ["write", "new", "Once only", "--request-id", f"{agent['name']}-once"]
    assert wirk(capsys, *argv)[0] == 0
    code, out, err = wirk(capsys, *argv)
    assert code == 0 and ("stored receipt" in out or "nothing was applied again" in out or "changed nothing" in out)
    code, out, err = wirk(capsys, "query", "text=Once only")
    assert len(re.findall(r"^[0-9a-f]{8} · ", out, re.M)) == 1


def test_upload_attach_and_download(agent, capsys, tmp_path):
    (tmp_path / "log.txt").write_text("all 12 tests pass\n")
    code, out, err = wirk(capsys, "upload", "log.txt", "--description", "Test log")
    assert code == 0, out + err
    attach = re.search(r"attach with: (.+)", out).group(1)
    code, out, err = wirk(capsys, *shlex.split(attach))
    assert code == 0, out + err
    item = re.search(r"created ([0-9a-f]{8})", out).group(1)
    code, out, err = wirk(capsys, "query", item, "depth=all")
    file_id = re.search(r"\bfile ([0-9a-f]{8})\b", out).group(1)  # the short ID the Files block shows
    code, out, err = wirk(capsys, "download", item, file_id, "-o", "copy.txt")
    assert code == 0 and (tmp_path / "copy.txt").read_text() == "all 12 tests pass\n"


def test_status_shows_the_wirkspace_context(agent, capsys):
    code, out, err = wirk(capsys, "status")
    assert code == 0 and "context" in out


def test_an_agent_whose_role_may_review_decides_and_a_person_may_decide_as_themselves(agent, capsys, monkeypatch):
    """Decision 85: an editor's review is refused by its role; a reviewer agent decides with its own token, and a
    person decides as themselves with --person."""
    proposals = []
    for title in ("Nightly cleanup", "Weekly cleanup"):
        code, out, err = wirk(capsys, "write", "new", title, "kind=work")
        code, out, err = wirk(capsys, "write", "edit", f"{created(out)}@1", "status=cancelled", "--propose", "--reason",
                              "Idle 30 days")
        assert code == 0 and "Proposed" in out, out + err
        proposal = re.search(r"proposal ([0-9a-f]{8}) r(\d+)", out)
        proposals.append((f"{proposal[1]}@{proposal[2]}", "accept", "--reason", "Idle indeed"))
    code, out, err = wirk(capsys, "review", *proposals[0])  # these agents have their own editor row
    assert code == 1 and "your role (editor) cannot review" in out and "accepted" not in out, out + err
    person_with_agents(capsys, agent["tmp"] / "other", agent["workspace"], role="reviewer", person_role="editor")
    code, out, err = wirk(capsys, "review", *proposals[0])  # another person's agents, with the reviewer role
    assert code == 0 and "accepted" in out, out + err
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(agent["dir"]))
    code, out, err = as_person(monkeypatch, capsys, agent["name"], "review", *proposals[1])
    assert code == 0 and "accepted" in out, out + err


def test_a_receipt_is_found_by_its_request_id(agent, capsys):
    wirk(capsys, "write", "new", "Receipt me", "--request-id", f"{agent['name']}-rcpt")
    code, out, err = wirk(capsys, "query", f"receipt={agent['name']}-rcpt")
    assert code == 0 and f"{agent['name']}-rcpt" in out and "nothing was applied again" in out, out + err


def test_show_returns_a_link_or_says_it_is_not_live(agent, capsys):
    code, out, err = wirk(capsys, "show", "status", "--no-open")
    if "views_unavailable" in out + err:  # a service with no address for views yet, as api.wirk.life is today
        assert code == 1 and "Traceback" not in err, out + err
    else:
        assert code == 0 and out.startswith("http"), out + err


def test_status_with_a_task(agent, capsys):
    wirk(capsys, "write", "new", "Retry failed webhooks", "kind=work")
    code, out, err = wirk(capsys, "status", "webhook", "retries")
    assert code == 0 and "Relevant to your task" in out, out


def test_cards_print_the_kind_agents_filter_on(agent, capsys):
    code, out, err = wirk(capsys, "write", "new", "A task", "kind=work")
    item = re.search(r"created ([0-9a-f]{8})", out).group(1)
    code, out, err = wirk(capsys, "query", "kind=work")
    assert code == 0 and f"{item} · work ·" in out, out
    code, out, err = wirk(capsys, "query", "proposal=proposed,deferred")
    assert code == 0, out + err


def write_context(capsys, monkeypatch, agent):
    """Agents with rows of their own propose context and a person accepts it; then work contributes to the initiative."""
    proposals = []
    for title, level, body in (("Scratch context", "organization", "Purpose: a dependable public API."),
                               ("API hardening", "initiative", "Goal: no partner loses data.")):
        code, out, err = wirk(capsys, "write", "new", title, "kind=context", f"level={level}", "--body", body)
        assert code == 1 and "requires_review" in out, out + err  # an editor's agents may not add it
        code, out, err = wirk(capsys, "write", "new", title, "kind=context", f"level={level}", "--body", body,
                              "--propose", "--reason", "Agreed with the team")
        assert code == 0 and "Proposed" in out, out + err
        proposals.append("{}@{}".format(*re.search(r"proposal ([0-9a-f]{8}) r(\d+)", out).groups()))
    code, out, err = as_person(monkeypatch, capsys, agent["name"], "review", *proposals, "accept", "--reason", "Agreed")
    assert code == 0 and out.count("accepted") >= 2, out + err
    code, out, err = wirk(capsys, "query", "API hardening")
    initiative = re.search(r"^([0-9a-f]{8}) · ", out, re.M).group(1)
    code, out, err = wirk(capsys, "write", "new", "Retry webhooks", "kind=work", "--link", f"contributes_to:{initiative}")
    assert code == 0, out + err
    return created(out)


def test_context_on_every_status(agent, capsys, monkeypatch):
    write_context(capsys, monkeypatch, agent)
    code, out, err = wirk(capsys, "status")
    assert re.search(r"^Scratch \w+ context · maintained by ", out, re.M) and "not written yet" not in out, out
    assert "dependable public API" in out and "API hardening" in out, out
    code, out, err = wirk(capsys, "query", "kind=context", "state=active")
    assert code == 0 and "API hardening" in out and "· context ·" in out, out


def test_fetched_work_carries_its_context(agent, capsys, monkeypatch):
    item = write_context(capsys, monkeypatch, agent)
    code, out, err = wirk(capsys, "query", item)
    assert "Context" in out and "API hardening" in out, out


def test_agents_acting_for_an_administrator_add_context_directly(tmp_path, monkeypatch, capsys):
    """The agents browser approval makes have no rows of their own and act with their person's role."""
    newcomer(tmp_path, monkeypatch, capsys, rows=False)
    code, out, err = wirk(capsys, "write", "new", "Scratch context", "kind=context", "level=organization",
                          "--body", "Purpose: a dependable public API.")
    assert code == 0 and "created" in out and "Proposed" not in out, out + err
    code, out, err = wirk(capsys, "status")
    assert "dependable public API" in out, out
