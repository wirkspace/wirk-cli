"""`wirk import` as a person and an agent meet it (docs/plans/importers.md §2): the dry run, the setup request, the
importer's own token, the runs after, the report and the exit status."""

import json
import os
import stat

import httpx
import pytest

from import_fakes import FakeWirk, refusal
from test_import_github import FakeGh, node, page, ref, repo
from test_import_linear import KEY
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


def test_without_file_storage_nothing_is_read_or_written_and_the_person_is_told_why(world):
    world["fake"].no_files = "No file store is configured"
    for argv in (("github", "acme", "--dry-run"), ("github", "acme")):
        code, out, err = world["run"](*argv)
        assert code == 2 and "files_unavailable" in err and "file storage" in err and "WIRK administrator" in err
        assert "Nothing was read from GitHub or written" in err
    assert not world["gh"].calls and not world["fake"].writes()
    assert not (world["home"] / "import" / "github-setup.json").exists()
    code, out, err = world["run"]("github", "acme", "--json")
    answer = json.loads(out)
    assert code == 2 and not answer["ok"] and answer["errors"][0]["code"] == "files_unavailable"


def test_file_storage_lost_during_a_run_stops_it_once(world):
    world["run"]("github", "acme", "--dry-run")
    register(world)
    hint = "ask your WIRK administrator to make file storage available"
    world["fake"].files = lambda body: refusal("files_unavailable", "No file store is configured", 503, hint=hint)
    code, out, err = world["run"]("github", "acme", "--json")
    answer = json.loads(out)
    assert code == 2 and not answer["ok"] and answer["errors"][0] == {
        "code": "files_unavailable", "count": 1, "message": "No file store is configured", "hint": hint}
    assert not any(o["outcome"] == "error" for o in answer["data"]["outcomes"])
    assert err.endswith(f"Error import: files_unavailable: No file store is configured\n  {hint}\n")


def test_when_every_issue_errors_the_json_names_the_error_once_with_its_count(world):
    world["run"]("github", "acme", "--dry-run")
    register(world)
    world["fake"].write = lambda body: refusal("invalid_input", "operations[0] is not accepted")
    code, out, err = world["run"]("github", "acme", "--json")
    answer = json.loads(out)
    assert code == 1 and not answer["ok"]
    assert answer["errors"] == [{"code": "invalid_input", "count": 2, "message": "operations[0] is not accepted"}]
    code, out, err = world["run"]("github", "acme")
    assert "errors: 2 invalid_input: operations[0] is not accepted" in out


def test_the_setup_is_a_persons_step_and_says_so(world):
    code, out, err = world["run"]("github", "acme", "--dry-run")
    setup = next(line for line in out.split("\n") if line.startswith("setup: "))
    assert "a person who administers" in setup and "wirk login --person" in setup and "an agent cannot" in setup
    answer = json.loads(world["run"]("github", "acme", "--dry-run", "--json")[1])
    assert "wirk login --person" in answer["data"]["summary"]["setup_by"]
    assert "wirk login --person" in world["run"]()[1] and "agent cannot" in world["run"]()[1]


def test_a_blocked_dry_run_prints_one_json_answer(world):
    fake = world["fake"]
    fake.as_whom = "bob-github-import"
    line = render.line1("GitHub", "issue", "3000000001", "2026-09-30T12:00:00Z", "0" * 12)
    fake.write({"request_id": "bob-1", "operations": [{"op": "item.create", "data": {"title": "Issue 1", "body": line, "work": {}}}]})
    fake.as_whom = None
    code, out, err = world["run"]("github", "acme", "--dry-run", "--json")
    answer = json.loads(out)  # one document
    assert code == 2 and not answer["ok"] and "bob-github-import" in answer["errors"][0]["message"]


@pytest.fixture
def linear_world(home, monkeypatch, capsys):
    """The same CLI configuration, with Linear answered by synthetic pages and the key saved owner-only."""
    from test_import_linear import FakeLinear, issue
    from wirk_cli.importers import linear
    fake = FakeWirk(principal="alice-linear-import", person="alice", fields={})
    fake.tokens = {AGENT: "alice-agents"}
    source = FakeLinear([issue(1, labels={"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"id": "l-bug"}]}),
                         issue(2, "OPS"), issue(3, "SEC")])
    monkeypatch.setattr(linear, "TRANSPORT", httpx.MockTransport(source))
    monkeypatch.setattr(linear.time, "sleep", lambda seconds: None)
    (home / "import").mkdir(mode=0o700)
    (home / "import" / "linear-key").write_text(KEY)
    os.chmod(home / "import" / "linear-key", 0o600)

    def run(*argv):
        code = cli.main(["import", "linear", *argv], transport=httpx.MockTransport(fake))
        out = capsys.readouterr()
        return code, out.out, out.err

    return {"fake": fake, "linear": source, "run": run, "home": home}


def test_linear_public_teams_by_default_then_the_import_as_its_own_importer(linear_world):
    run, fake = linear_world["run"], linear_world["fake"]
    code, out, err = run("--dry-run")
    assert code == 0, out + err
    assert "selection: 2 teams · 2 issues" in out and "skipped, not public: SEC." in out
    assert "wirk import linear ENG OPS SEC --dry-run" in out and "complexity points" in out and "pull requests" not in out
    setup = json.loads((linear_world["home"] / "import" / "linear-setup.json").read_text())
    assert setup["operations"][0]["id"] == "alice-linear-import" and setup["operations"][0]["name"] == "Linear importer"
    assert {"team", "label", "workflow"} <= {op["field"]["key"] for op in setup["operations"] if op["op"] == "field.create"}
    token = (linear_world["home"] / "import" / "wirk-token-linear-import").read_text().strip()
    fake.tokens[token] = "alice-linear-import"
    code, out, err = run()
    assert code == 0, out + err
    assert "created: 12" in out and "find one: wirk query text='Linear issue [ENG-1]'" in out  # 2 issues, 10 around them
    code, out, err = run("ENG", "SEC")
    assert code == 0 and "warning: SEC is private on Linear: everyone in the wirkspace will read its 1 issues" in out
    assert KEY not in out + err


def test_the_help_shows_linear_its_teams_and_where_its_key_lives(world):
    text = world["run"]()[1]
    assert "wirk import linear [TEAM…]" in text and "import/linear-key" in text and "every public team" in text


@pytest.fixture
def jira_world(home, monkeypatch, capsys):
    """The CLI configuration, with Jira Cloud answered by synthetic REST v3 pages and the key saved owner-only."""
    from test_import_jira import BEN, TOKEN, FakeJira, issue as jira_issue
    from wirk_cli.importers import jira
    fake = FakeWirk(principal="alice-jira-import", person="alice", fields={})
    fake.tokens = {AGENT: "alice-agents"}
    source = FakeJira([jira_issue(1, assignee=BEN, labels=["bug"]), jira_issue(2, "OPS"), jira_issue(9, security={"name": "Staff"})])
    monkeypatch.setattr(jira, "TRANSPORT", httpx.MockTransport(source))
    monkeypatch.setattr(jira.time, "sleep", lambda seconds: None)
    (home / "import").mkdir(mode=0o700)
    (home / "import" / "jira-key").write_text(json.dumps({"site": "acme.atlassian.net", "email": "admin@example.com", "token": TOKEN}))
    os.chmod(home / "import" / "jira-key", 0o600)

    def run(*argv):
        code = cli.main(["import", "jira", *argv], transport=httpx.MockTransport(fake))
        out = capsys.readouterr()
        return code, out.out, out.err

    return {"fake": fake, "run": run, "home": home, "token": TOKEN}


def test_jira_every_project_with_restricted_issues_named_then_the_import(jira_world):
    run, fake, home = jira_world["run"], jira_world["fake"], jira_world["home"]
    code, out, err = run("--dry-run")
    assert code == 0, out + err
    assert "selection: 3 projects · 2 issues" in out and "skipped, not public: SEED-9." in out
    assert "wirk import jira OPS OUT SEED SEED-9 --dry-run" in out and "Jira requests" in out
    setup = json.loads((home / "import" / "jira-setup.json").read_text())
    assert setup["operations"][0]["id"] == "alice-jira-import" and setup["operations"][0]["name"] == "Jira importer"
    assert {"workflow", "issue_type", "project", "priority", "label"} <= {op["field"]["key"] for op in setup["operations"]
                                                                           if op["op"] == "field.create"}
    template = json.loads((home / "import" / "jira-map.json").read_text())
    assert template["users"] == {"acc-ben": None} and template["names"] == {"acc-ben": "Ben Sample"}
    assert template["cancelled"] == ["Cannot Reproduce", "Declined", "Duplicate", "Won't Do", "Won't Fix"]  # read back by the run below
    fake.tokens[(home / "import" / "wirk-token-jira-import").read_text().strip()] = "alice-jira-import"
    code, out, err = run()
    assert code == 0 and "created: 5" in out and "find one: wirk query text='Jira issue [SEED-1]'" in out, out + err  # 2 issues, 3 projects
    assert "note: project docs: 3 read" in out
    assert jira_world["token"] not in out + err
    from wirk_cli.help import HELP
    assert "wirk import jira [PROJECT|ISSUE_KEY…]" in HELP["import"] and "import/jira-key" in HELP["import"]



def test_a_map_whose_cancelled_is_not_a_list_of_names_stops_with_the_shape_it_wants(jira_world):
    (jira_world["home"] / "import" / "jira-map.json").write_text(json.dumps({"cancelled": "Won't Do"}))
    code, out, err = jira_world["run"]("--dry-run")
    assert code == 2 and "cancelled" in err and "list" in err


# ---------------------------------------------------------------- the report and the stops, line by line

def test_a_refused_status_stops_with_wirks_hint_in_text_and_json(world):
    world["fake"].status = lambda body: refusal("not_available", "Workspace is unavailable", 404,
                                                hint="wirk status names the wirkspaces you can reach")
    told = "Error import: Workspace is unavailable\n  wirk status names the wirkspaces you can reach\n"
    assert world["run"]("github", "acme", "--dry-run") == (2, "", told)
    code, out, err = world["run"]("github", "acme", "--dry-run", "--json")
    assert (code, err) == (2, told) and json.loads(out) == {"ok": False, "data": {}, "errors": [
        {"code": "import_stopped", "count": 1, "message": "Workspace is unavailable",
         "hint": "wirk status names the wirkspaces you can reach"}]}


def test_a_refused_read_stops_with_wirks_code_and_hint_in_text_and_json(world):
    hint = "wirk status names the wirkspaces you can reach"
    world["fake"].query = lambda body: refusal("not_available", "Workspace is unavailable", 404, hint=hint)
    told = f"Error import: not_available: Workspace is unavailable\n  {hint}\n"
    assert world["run"]("github", "acme", "--dry-run") == (2, "", told)
    code, out, err = world["run"]("github", "acme", "--dry-run", "--json")
    assert (code, err) == (2, told) and json.loads(out) == {"ok": False, "data": {}, "errors": [
        {"code": "not_available", "count": 1, "message": "Workspace is unavailable", "hint": hint}]}


def test_a_status_the_wirkspace_lacks_passes_once_the_map_names_one_it_has(world):
    world["fake"].definitions["status"] = {"open": "active", "completed": "completed", "cancelled": "cancelled"}
    (world["home"] / "import").mkdir(mode=0o700)
    (world["home"] / "import" / "github-map.json").write_text(json.dumps({"status": {"in_progress": "open"}}))
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 0 and "would create: 2" in out, out + err


def test_a_field_over_the_maps_limit_is_header_only_and_not_set_up(world, monkeypatch):
    labels = lambda name: page([{"name": name, "description": ""}])
    monkeypatch.setattr(github, "gh", FakeGh([repo("acme/web")], {"acme/web": [node(1, labels=labels("bug")),
                                                                                node(2, labels=labels("docs"))]}))
    folder = world["home"] / "import"
    folder.mkdir(mode=0o700)
    (folder / "github-map.json").write_text(json.dumps({"field_limit": 1}))
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 0, out + err
    setup = json.loads((folder / "github-setup.json").read_text())
    assert [op["field"]["key"] for op in setup["operations"] if op["op"] == "field.create"] == ["repository"]
    assert next(line for line in out.split("\n") if line.startswith("fields: ")) == (
        "fields: header only: label (2 values, over the limit of 1) · not set up yet: label, repository")
    answer = json.loads(world["run"]("github", "acme", "--dry-run", "--json")[1])
    assert answer["data"]["summary"]["header_only"] == {"label": 2}


def test_an_owner_who_is_not_a_member_is_named_on_the_owners_line(world, monkeypatch):
    monkeypatch.setattr(github, "gh", FakeGh([repo("acme/web")], {"acme/web": [node(1, assignees={"nodes": [{"login": "ben"}]})]}))
    world["run"]("github", "acme", "--dry-run")
    register(world)
    folder = world["home"] / "import"
    (folder / "github-map.json").write_text(json.dumps({"users": {"ben": "carol"}}))
    code, out, err = world["run"]("github", "acme")
    assert code == 0, out + err
    assert (f"owners: carol not in this wirkspace, so their issues have no owner; add them, or change {folder / 'github-map.json'}"
            in out.split("\n"))
    assert [item["revisions"][-1]["work"] for item in world["fake"].mine().values()] == [{}]


def test_a_line_someone_else_wrote_is_reported_as_ignored(world):
    fake = world["fake"]
    fake.as_whom = "bob"
    line = render.line1("GitHub", "issue", "3000000001", "2026-09-30T12:00:00Z", "0" * 12)
    fake.write({"request_id": "bob-1", "operations": [{"op": "item.create", "data": {"title": "Issue 1", "body": line, "work": {}}}]})
    fake.as_whom = None
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 0 and "ignored: 1 items with a provenance line the importer did not write" in out.split("\n")


def test_more_than_fifty_outcomes_that_need_attention_end_with_how_many_more(world, monkeypatch):
    monkeypatch.setattr(github, "gh", FakeGh([repo("acme/web", issues=51)], {"acme/web": [node(n) for n in range(1, 52)]}))
    world["run"]("github", "acme", "--dry-run")
    register(world)
    world["fake"].write = lambda body: refusal("invalid_input", "operations[0] is not accepted")
    code, out, err = world["run"]("github", "acme")
    lines = out.split("\n")
    shown = [line for line in lines if line.startswith("  acme/web#")]
    assert code == 1 and len(shown) == 50 and shown[0] == "  acme/web#1 error: invalid_input: operations[0] is not accepted"
    assert lines[-2:] == ["  1 more: add --json for every outcome", ""]


def test_a_run_with_no_outcomes_has_no_find_one_line(world, monkeypatch):
    monkeypatch.setattr(github, "gh", FakeGh([repo("acme/web", issues=0)], {"acme/web": []}))
    code, out, err = world["run"]("github", "acme", "--dry-run")
    assert code == 0 and "items: none" in out.split("\n") and "find one:" not in out, out + err


def test_a_run_whose_outcomes_hold_no_issue_has_no_find_one_line(linear_world, monkeypatch):
    from test_import_linear import FakeLinear
    from wirk_cli.importers import linear
    monkeypatch.setattr(linear, "TRANSPORT", httpx.MockTransport(FakeLinear([])))
    code, out, err = linear_world["run"]("--dry-run")
    assert code == 0 and "would create: " in out and "find one:" not in out, out + err


def test_an_empty_workspace_id_is_sent_as_given_so_wirk_refuses_it(world):
    status = world["fake"].status
    world["fake"].status = lambda body: (refusal("not_available", "Workspace is unavailable", 404) if body.get("workspace_id") == ""
                                         else status(body))  # as core answers a wirkspace ID that names none
    code, out, err = world["run"]("github", "acme", "workspace_id=", "--dry-run")
    assert (code, out, err) == (2, "", "Error import: Workspace is unavailable\n")
    assert [body for route, body in world["fake"].requests] == [{"format": "json", "max_bytes": 1024, "workspace_id": ""}]
    code, out, err = world["run"]("github", "acme", "workspace_id=", "--dry-run", "--json")  # no hint, no hint key
    assert json.loads(out)["errors"] == [{"code": "import_stopped", "count": 1, "message": "Workspace is unavailable"}]


@pytest.mark.parametrize("which, source", [("linear_world", "Linear"), ("jira_world", "Jira")])
def test_without_file_storage_the_stop_names_the_source_it_did_not_read(which, source, request):
    world = request.getfixturevalue(which)
    world["fake"].no_files = "No file store is configured"
    code, out, err = world["run"]("--dry-run")
    assert code == 2 and f"Nothing was read from {source} or written to WIRK" in err and "GitHub" not in err


@pytest.mark.parametrize("answer, said", [(httpx.Response(401, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"}), "CAPTCHA"),
                                          (httpx.Response(401), "refused the token")])
def test_a_captcha_or_a_dead_token_during_a_project_read_stops_the_jira_run(answer, said, jira_world, monkeypatch):
    from test_import_jira import FakeJira, issue as jira_issue
    from wirk_cli.importers import jira
    source = FakeJira([jira_issue(1)])
    source.answers["/rest/api/3/project/10000/components"] = answer
    monkeypatch.setattr(jira, "TRANSPORT", httpx.MockTransport(source))
    code, out, err = jira_world["run"]("SEED", "--dry-run")
    assert code == 2 and said in err and not jira_world["fake"].writes()
