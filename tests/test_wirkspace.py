"""wirk wirkspace [NAME]: one deliberate command chooses a chat's wirkspace; nothing else changes it."""

import json
import os
import uuid

import httpx
import pytest

from conftest import PERSON_TOKEN, URL, envelope, refusal, write_token
from wirk_cli import cli, client

CHAT, OTHER_CHAT = str(uuid.UUID(int=21)), str(uuid.UUID(int=22))
SALES, SUPPORT = "wsp_5a1e5000" + "0" * 24, "wsp_50990000" + "1" * 24
REACH = {SALES: "Sales", SUPPORT: "Support"}


def service(reach=REACH, refuse=None):
    """A fake service where the caller reaches `reach`: status names the wirkspace sent, or refuses as the service
    does when none is sent and there are several; every other route answers with what it was sent."""
    def answer(request):
        body, route = json.loads(request.content), request.url.path
        if refuse and route in refuse:
            return refuse[route](request)
        if route == "/v2/login":
            done = body.get("format") == "json" and body["for"] != "actor"
            return envelope(data={"state": "logged_in"}) if done else envelope("Logged in as agent-chat")
        given = body.get("workspace_id")
        if route == "/v2/status" and body.get("format") == "json":
            if given is None and len(reach) > 1:
                return envelope("Error choose_wirkspace", ok=False, status=422, errors=[{
                    "code": "choose_wirkspace", "message": f"you belong to {len(reach)} wirkspaces — add workspace_id:",
                    "choices": [{"key": key, "name": f"{key[4:12]} {name}"} for key, name in reach.items()]}])
            found = given or next(iter(reach))
            if found not in reach:
                return refusal("not_available", "Workspace is unavailable")
            return envelope(data={"you": {"principal": "agent-chat", "wirkspace": {"id": found, "name": reach[found]}}})
        return envelope(f"sent to {given}")
    return answer


def in_chat(monkeypatch, chat=CHAT):
    """An agent chat that has already run wirk login: its own token beside the machine's."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", chat)
    path = client.actor_path(URL, chat)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_token(path, "wirk_" + chat.replace("-", "")[-43:].rjust(43, "c"))
    return path.with_suffix(".wirkspace")


@pytest.fixture
def chat(home, monkeypatch):
    return in_chat(monkeypatch)


def sent(fake):
    return [json.loads(request.content).get("workspace_id") for request in fake.requests]


def test_wirkspace_alone_lists_what_you_reach_until_you_choose(run, chat):
    code, out, err, fake = run(["wirkspace"], service())
    assert code == 0 and "Sales (5a1e5000)" in out and "Support (50990000)" in out
    assert "Choose: wirk wirkspace NAME" in out and sent(fake) == [None]


def test_with_one_wirkspace_it_says_it_is_your_only_one(run, chat):
    code, out, err, fake = run(["wirkspace"], service({SALES: "Sales"}))
    assert code == 0 and out.strip() == "Wirkspace: Sales (5a1e5000) · your only one"


def test_choosing_saves_it_and_wirkspace_alone_prints_it(run, chat):
    code, out, err, fake = run(["wirkspace", "sales"], service())
    assert code == 0 and out.strip() == "Now working in Sales (5a1e5000), none before"
    code, out, err, fake = run(["wirkspace"], service())
    assert code == 0 and out.strip() == "Wirkspace: Sales (5a1e5000) · chosen in this chat" and sent(fake) == [None]
    code, out, err, fake = run(["wirkspace", "50990000"], service())
    assert code == 0 and out.strip() == "Now working in Support (50990000), previously Sales (5a1e5000)"
    assert run(["wirkspace", SALES], service())[1].strip() == "Now working in Sales (5a1e5000), previously Support (50990000)"


def test_the_choice_belongs_to_this_chat_only(run, home, chat, monkeypatch):
    run(["wirkspace", "Sales"], service())
    in_chat(monkeypatch, OTHER_CHAT)
    code, out, err, fake = run(["query", "5c1e7a90"], service())
    assert sent(fake) == [None]


def test_after_choosing_every_command_sends_it(run, chat, tmp_path):
    run(["wirkspace", "Sales"], service())
    (tmp_path / "note.txt").write_text("hello")
    files = {"/v2/files": lambda request: refusal("not_available", "stop here")}
    for argv in (["status"], ["query", "5c1e7a90"], ["write", "new", "Title"], ["review", "c4a1e902@1", "accept",
                 "--reason", "Checked"], ["show", "status", "--no-open"], ["upload", "note.txt"],
                 ["download", "5c1e7a90", "file_1"]):
        code, out, err, fake = run(argv, service(refuse=files))
        assert sent(fake) == [SALES], argv


def test_login_and_a_show_revoke_never_get_it(run, home, chat):
    run(["wirkspace", "Sales"], service())
    code, out, err, fake = run(["login"], service())
    assert code == 0 and fake.requests and set(sent(fake)) == {None}
    code, out, err, fake = run(["show", "--revoke", "https://wirk.test/v/abc", "--no-open"], service())
    assert sent(fake) == [None]
    write_token(home / "person-token", PERSON_TOKEN)
    assert cli.connect(None, person=True).wirkspace is None  # admin and review --person are a person's own


def test_an_explicit_workspace_id_wins_and_changes_nothing_saved(run, chat):
    run(["wirkspace", "Sales"], service())
    code, out, err, fake = run(["query", "5c1e7a90", f"workspace_id={SUPPORT}"], service())
    assert sent(fake) == [SUPPORT] and chat.read_text() == SALES


def test_a_request_file_without_one_gets_the_choice_and_a_file_with_one_keeps_it(run, chat, tmp_path):
    run(["wirkspace", "Sales"], service())
    (tmp_path / "plain.json").write_text(json.dumps({"request_id": "w-1", "operations": []}))
    (tmp_path / "named.json").write_text(json.dumps({"request_id": "w-2", "operations": [], "workspace_id": SUPPORT}))
    assert sent(run(["write", "--request", "plain.json"], service())[3]) == [SALES]
    assert sent(run(["write", "--request", "named.json"], service())[3]) == [SUPPORT]


def no_answer(request):
    raise httpx.ReadTimeout("no answer", request=request)


def test_both_retry_hints_carry_the_wirkspace_sent(run, chat, tmp_path):
    run(["wirkspace", "Sales"], service())
    timeout = {"/v2/write": no_answer}
    code, out, err, fake = run(["write", "new", "Title", "--request-id", "w-1"], service(refuse=timeout))
    assert code == 1 and f"--request-id w-1 workspace_id={SALES}" in err
    assert f"wirk query receipt=w-1 workspace_id={SALES}" in err
    (tmp_path / "plain.json").write_text(json.dumps({"request_id": "w-2", "operations": []}))
    code, out, err, fake = run(["write", "--request", "plain.json"], service(refuse=timeout))
    assert f"wirk write --request plain.json workspace_id={SALES}" in err
    assert f"wirk query receipt=w-2 workspace_id={SALES}" in err
    run(["wirkspace", "Support"], service())  # a new choice: the printed retry still goes where the first attempt went
    code, out, err, fake = run(["write", "--request", "plain.json", f"workspace_id={SALES}"], service())
    assert sent(fake) == [SALES]


def test_the_receipt_hint_names_the_wirkspace_sent_and_the_retry_adds_none_the_line_gave(run, chat, tmp_path):
    run(["wirkspace", "Sales"], service())
    timeout = {"/v2/write": no_answer}
    code, out, err, fake = run(["write", "new", "Title", f"workspace_id={SUPPORT}", "--request-id", "w-1"],
                               service(refuse=timeout))
    assert code == 1 and "--request-id w-1:" in err and f"wirk query receipt=w-1 workspace_id={SUPPORT}" in err
    assert SALES not in err
    (tmp_path / "named.json").write_text(json.dumps({"request_id": "w-2", "operations": [], "workspace_id": SUPPORT}))
    code, out, err, fake = run(["write", "--request", "named.json"], service(refuse=timeout))
    assert "wirk write --request named.json:" in err and f"wirk query receipt=w-2 workspace_id={SUPPORT}" in err
    assert SALES not in err


def test_an_uncertain_upload_prints_a_retry_upload_takes(run, chat, tmp_path):
    """upload takes no workspace_id=, so its retry stays as it was; the receipt still names where it went."""
    run(["wirkspace", "Sales"], service())
    (tmp_path / "note.txt").write_text("hello")
    def files(request):
        if "confirm" in json.loads(request.content):
            return no_answer(request)
        return envelope(data={"present": True})
    code, out, err, fake = run(["upload", "note.txt", "--request-id", "u-1"], service(refuse={"/v2/files": files}))
    assert code == 1 and "--request-id u-1:" in err and f"wirk query receipt=u-1 workspace_id={SALES}" in err


def test_a_choice_no_longer_reachable_says_so_and_how_to_choose(run, chat):
    run(["wirkspace", "Sales"], service())
    code, out, err, fake = run(["wirkspace"], service({SUPPORT: "Support"}))
    assert code == 1 and "5a1e5000 is no longer reachable" in err and "Choose: wirk wirkspace NAME" in err
    assert "Support (50990000)" in err
    assert sent(fake) == [None]
    code, out, err, fake = run(["wirkspace", "Support"], service({SUPPORT: "Support"}))
    assert code == 0 and out.strip() == "Now working in Support (50990000), previously 5a1e5000"


def test_a_helper_sharing_the_chat_moves_its_launchers_next_write(run, chat):
    """The known limit, as behaviour: an Agent-tool helper runs with its chat's session ID, so it shares the choice."""
    run(["wirkspace", "Sales"], service())
    run(["wirkspace", "Support"], service())  # the helper's command
    assert sent(run(["write", "new", "Title"], service())[3]) == [SUPPORT]  # the launcher's next write


def test_an_unknown_or_repeated_name_is_refused_listing_what_you_reach(run, chat):
    code, out, err, fake = run(["wirkspace", "Marketing"], service())
    assert code == 2 and "Marketing" in err and "Sales (5a1e5000)" in err and "Support (50990000)" in err
    assert not chat.exists()
    twins = {SALES: "Sales", SUPPORT: "sales"}
    code, out, err, fake = run(["wirkspace", "Sales"], service(twins))
    assert code == 2 and "5a1e5000" in err and "50990000" in err and not chat.exists()
    assert run(["wirkspace", "50990000"], service(twins))[0] == 0 and chat.read_text() == SUPPORT


def test_outside_a_chat_setting_is_refused_and_showing_works(run):
    code, out, err, fake = run(["wirkspace", "Sales"], service())
    assert code == 2 and "chat" in err and "workspace_id=" in err
    code, out, err, fake = run(["wirkspace"], service())
    assert code == 0 and "Sales (5a1e5000)" in out and "workspace_id=" in out


def test_a_chat_that_has_not_logged_in_is_told_to(run, home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CHAT)
    code, out, err, fake = run(["wirkspace", "Sales"], service())
    assert code == 1 and "actor_setup_required" in err and fake.requests == []


def test_the_choice_file_holds_only_the_id_is_private_and_is_never_written_through_a_link(run, chat, tmp_path):
    run(["wirkspace", "Sales"], service())
    assert chat.read_text() == SALES and chat.stat().st_mode & 0o777 == 0o600
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("keep")
    chat.unlink()
    chat.symlink_to(elsewhere)
    code, out, err, fake = run(["query", "5c1e7a90"], service())
    assert code == 1 and "insecure" in err and fake.requests == []  # a planted link is refused, never read
    assert run(["wirkspace", "Support"], service())[0] == 1 and elsewhere.read_text() == "keep"
    chat.unlink()
    (chat.parent / (chat.name + ".new")).symlink_to(elsewhere)  # nor is one planted where the new choice is written
    assert run(["wirkspace", "Support"], service())[0] == 1 and elsewhere.read_text() == "keep" and not chat.exists()


def test_json_and_exit_codes(run, chat):
    code, out, err, fake = run(["wirkspace", "Sales", "--json"], service())
    assert code == 0 and json.loads(out) == {"ok": True, "errors": [],
                                             "data": {"wirkspace": {"id": SALES, "name": "Sales"}, "previous": None}}
    code, out, err, fake = run(["wirkspace", "--json"], service())
    assert code == 0 and json.loads(out)["data"]["wirkspace"] == {"id": SALES, "name": "Sales"}
    code, out, err, fake = run(["wirkspace", "Nowhere", "--json"], service())
    assert code == 2 and json.loads(out)["ok"] is False
    gone = {"/v2/status": lambda request: refusal("no_wirkspace", "agent-chat is not a member of any wirkspace")}
    assert run(["wirkspace", "Sales"], service(refuse=gone))[0] == 1
    assert run(["wirkspace", "Sales", "Support"], service())[0] == 2


def test_a_service_compares_its_wirkspace_so_a_new_choice_is_a_new_connection():
    token = "wirk_" + "a" * 43
    assert not client.Service(URL, token, None, SALES).same(client.Service(URL, token, None, SUPPORT))
    assert client.Service(URL, token, None, SALES).same(client.Service(URL, token, None, SALES))


def test_wirkspace_help_says_what_it_does_and_the_helper_limit(capsys):
    assert cli.main(["wirkspace", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "usage: wirk wirkspace [NAME]" in text and "this chat" in text and "workspace_id=" in text
    assert "helper" in text
    assert cli.main(["show", "--help"]) == 0 and "wirk wirkspace NAME" in capsys.readouterr().out
