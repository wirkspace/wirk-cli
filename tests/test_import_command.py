"""`wirk import` as a person and an agent meet it (docs/plans/importers.md §2): the dry run, the setup request, the
importer's own token, the runs after, the report and the exit status."""

import json
import os
import stat

import httpx
import pytest

from import_fakes import FakeWirk
from test_import_github import FakeGh, node, page, ref, repo
from wirk_cli import cli
from wirk_cli.importers import github, render

AGENT = "wirk_" + "a" * 43


@pytest.fixture
def world(home, monkeypatch, capsys):
    """An agent's CLI configuration, a fake WIRK that knows the agent's token, and a fake GitHub."""
    fake = FakeWirk(principal="alice-github-import", person="alice", fields={})
    fake.tokens = {AGENT: "alice-agents"}
    gh = FakeGh([repo("acme/web"), repo("acme/api", "PRIVATE", issues=812)],
                {"acme/web": [node(1, labels=page([{"name": "bug", "description": ""}])), node(2)]})
    monkeypatch.setattr(github, "gh", gh)
    monkeypatch.setattr(github.time, "sleep", lambda seconds: None)

    def run(*argv):
        code = cli.main(["import", *argv], transport=httpx.MockTransport(fake))
        out = capsys.readouterr()
        return code, out.out, out.err

    return {"fake": fake, "gh": gh, "run": run, "home": home}


def register(world):
    """What a person's `wirk admin --request` does with the setup file, done straight on the fake."""
    setup = json.loads((world["home"] / "import" / "github-setup.json").read_text())
    token = (world["home"] / "import" / "wirk-token-github-import").read_text().strip()
    operations = [op["op"] for op in setup["operations"]]
    assert operations[0] == "principal.create" and operations[1] == "member.set" and operations[-1] == "token.add"
    for op in setup["operations"]:
        if op["op"] == "field.create":
            world["fake"].definitions[op["field"]["key"]] = {o["key"]: "active" for o in op["field"]["options"]}
    world["fake"].tokens[token] = "alice-github-import"
    return setup


def test_without_a_source_it_lists_the_sources_and_steps(world):
    code, out, _ = world["run"]()
    assert code == 0 and "wirk import github" in out and "--dry-run" in out and "wirk admin --request" in out


def test_the_dry_run_writes_the_setup_the_token_and_the_map_and_nothing_else(world):
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 0, out + err
    assert not world["fake"].writes()
    folder = world["home"] / "import"
    token = folder / "wirk-token-github-import"
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600
    setup = register(world)
    assert setup["operations"][0] == {"op": "principal.create", "id": "alice-github-import", "kind": "agent",
                                      "name": "GitHub importer", "person_id": "alice"}
    assert {op["field"]["key"] for op in setup["operations"] if op["op"] == "field.create"} == {"repository", "label"}
    assert json.loads((folder / "github-map.json").read_text())["field_limit"] == 50
    assert "skipped, not public: acme/api (812 issues)" in out and "wirk import github acme acme/api --dry-run" in out
    assert "would create: 2" in out and f"wirk admin --request {folder / 'github-setup.json'}" in out
    assert "files: attachments stored 0 · left as links 0 · images on other hosts left as links 0" in out
    assert "0 email addresses typed in text, kept as written" in out
    assert token.read_text().strip() not in out + err


def test_a_run_before_the_setup_says_what_to_do(world):
    code, out, err = world["run"]("github", "acme")
    assert code == 2 and "wirk import github acme --dry-run" in err
    world["run"]("github", "acme", "--dry-run")
    code, out, err = world["run"]("github", "acme")
    assert code == 2 and "wirk admin --request" in err and not world["fake"].writes()


def test_after_the_setup_it_imports_as_the_importer_and_a_rerun_is_current(world):
    world["run"]("github", "acme", "--dry-run")
    register(world)
    code, out, err = world["run"]("github", "acme")
    assert code == 0, out + err
    assert "created: 2" in out and "find one: wirk query text='GitHub issue [acme/web#1]'" in out
    assert {snap["revisions"][0]["by"] for snap in world["fake"].items.values()} == {"alice-github-import"}
    code, out, err = world["run"]("github", "acme", "--json")
    answer = json.loads(out)
    assert code == 0 and answer["ok"] and {o["outcome"] for o in answer["data"]["outcomes"]} == {"current"}
    assert answer["data"]["summary"]["issues"] == 2


def test_another_importers_items_stop_the_dry_run(world):
    fake = world["fake"]
    fake.as_whom = "bob-github-import"
    line = render.line1("GitHub", "issue", "3000000001", "2026-09-30T12:00:00Z", "0" * 12)
    fake.write({"request_id": "bob-1", "operations": [{"op": "item.create", "data": {"title": "Issue 1", "body": line, "work": {}}}]})
    fake.as_whom = None
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 2 and "bob-github-import" in out + err
    assert "blocked: 1" in out and "would create: 1" in out  # the plan is printed before the stop
    assert not (world["home"] / "import" / "github-setup.json").exists()


def test_a_second_run_on_the_same_machine_waits_for_none(world):
    import fcntl
    folder = world["home"] / "import"
    folder.mkdir(mode=0o700, exist_ok=True)
    with open(folder / "github.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 2 and "another wirk import github" in err


def test_a_status_the_wirkspace_lacks_stops_with_the_map_to_fix(world):
    world["fake"].definitions["status"] = {"todo": "active", "done": "completed"}
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 2 and "status" in err and "github-map.json" in err


def test_unknown_words_and_sources_are_usage_errors(world):
    assert world["run"]("gitlab", "acme")[0] == 2
    assert world["run"]("github", "acme", "colour=blue")[0] == 2
    assert world["run"]("github")[0] == 2


def test_naming_a_private_repository_warns_who_will_read_it(world):
    warning = "acme/api is private on GitHub: everyone in the wirkspace will read its 812 issues"
    code, out, err = world["run"]("github", "acme", "acme/api", "--dry-run")
    assert code == 0 and warning in out
    register(world)
    code, out, err = world["run"]("github", "acme", "acme/api")
    assert code == 0 and warning in out
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert "private on GitHub" not in out


def test_a_run_that_stops_still_prints_what_it_did(world):
    world["run"]("github", "acme", "--dry-run")
    register(world)
    fake, write = world["fake"], world["fake"].write

    def write_until_the_service_goes(body):
        if fake.items:  # the first issue went in; then WIRK cannot be reached
            raise httpx.ConnectError("refused")
        return write(body)
    fake.write = write_until_the_service_goes
    code, out, err = world["run"]("github", "acme")
    assert code == 2 and "created: 1" in out and "service_unavailable" in err
    code, out, err = world["run"]("github", "acme", "--json")
    answer = json.loads(out)
    assert code == 2 and not answer["ok"] and answer["errors"][0]["code"] == "service_unavailable"
    assert answer["data"]["outcomes"][0]["outcome"] == "current"
