"""Each agent chat signs in as itself under the machine's one approval; a chat started by another is not its launcher."""

import json
import uuid

import pytest

from conftest import TOKEN, URL, envelope, refusal, write_token
from wirk_cli import client

CODEX_A, CODEX_B, CLAUDE = str(uuid.UUID(int=11)), str(uuid.UUID(int=12)), str(uuid.UUID(int=13)).upper()
GROK = str(uuid.UUID(int=14)).upper()


def service(refuse=()):
    """A fake service: enrols any chat token except those in `refuse`, and says who a bearer is."""
    def answer(request):
        if request.url.path == "/v2/login":
            if json.loads(request.content)["for"] == "actor":
                child = request.headers["wirk-actor-authorization"][7:]
                if child in refuse:
                    return refusal("not_authorized", "This actor credential cannot be enrolled under that parent",
                                   status=403)
                return envelope(f"Logged in as agent-{child[-6:]} · agent of alice · account Acme\nnext: status")
            if json.loads(request.content).get("format") == "json":
                return envelope(data={"state": "logged_in", "principal": "alice-agents", "kind": "agent"})
            return envelope("Logged in as alice-agents · agent of alice · account Acme\nnext: status")
        return envelope("You: " + request.headers["authorization"][7:])
    return answer


def chat_token(*ids):
    return client.read_token(client.actor_path(URL, ",".join(i.lower() for i in ids)))


def test_a_new_chat_is_told_to_log_in_then_acts_as_itself(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    code, out, err, fake = run(["status"], service())
    assert code == 1 and "actor_setup_required" in err and "wirk login" in err and fake.requests == []
    code, out, err, fake = run(["login"], service())
    assert code == 0 and "Logged in as agent-" in out
    assert [json.loads(r.content)["for"] for r in fake.requests][-1] == "actor"  # no browser, no person
    mine = chat_token(CODEX_A)
    assert mine != TOKEN and run(["status"], service())[1].strip() == f"You: {mine}"


def test_a_claude_code_session_alone_gets_its_own_chat_identity(run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    assert "actor_setup_required" in run(["status"], service())[2]
    assert run(["login"], service())[0] == 0
    assert run(["status"], service())[1].strip() == f"You: {chat_token(CLAUDE)}"


def test_a_worker_started_from_a_chat_never_borrows_its_launchers_identity(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    assert run(["login"], service())[0] == 0
    launcher = chat_token(CODEX_A)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)  # a Claude Code worker inherits the Codex chat's ID
    assert "actor_setup_required" in run(["status"], service())[2]
    assert run(["login"], service())[0] == 0
    worker = chat_token(CODEX_A, CLAUDE)
    assert worker != launcher and run(["status"], service())[1].strip() == f"You: {worker}"


def test_two_chats_differ_and_a_resumed_chat_keeps_its_token(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    run(["login"], service())
    first = chat_token(CODEX_A)
    assert run(["login"], service())[0] == 0 and chat_token(CODEX_A) == first
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_B)
    run(["login"], service())
    assert chat_token(CODEX_B) != first


def test_mcp_names_the_codex_thread_and_shell_and_mcp_agree(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    assert client.actor_binding(CODEX_A.upper()) == client.actor_binding() == f"{CODEX_A},{CLAUDE.lower()}"
    monkeypatch.delenv("CODEX_THREAD_ID")
    assert client.actor_binding(CODEX_B) == f"{CODEX_B},{CLAUDE.lower()}"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not a session")
    assert client.actor_binding() is None and client.actor_binding("../../etc") is None


def test_a_refused_chat_token_is_replaced_by_a_fresh_enrolment(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    run(["login"], service())
    old = chat_token(CODEX_A)
    code, out, err, fake = run(["login"], service(refuse={old}))
    assert code == 0 and "Logged in as agent-" in out
    assert chat_token(CODEX_A) != old
    sent = [r.headers["wirk-actor-authorization"][7:] for r in fake.requests if json.loads(r.content)["for"] == "actor"]
    assert sent == [old, chat_token(CODEX_A)]


def test_a_renewed_machine_token_rebinds_the_same_chat_token(run, home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    run(["login"], service())
    child = chat_token(CODEX_A)
    write_token(home / "agent-token", "wirk_" + "z" * 43)
    code, out, err, fake = run(["login"], service())
    assert code == 0 and chat_token(CODEX_A) == child
    assert fake.requests[-1].headers["authorization"] == "Bearer wirk_" + "z" * 43


def test_without_a_host_chat_the_machines_shared_identity_is_used(run):
    code, out, err, fake = run(["status"], service())
    assert code == 0 and out.strip() == f"You: {TOKEN}"


def test_a_lost_chat_token_asks_for_login_rather_than_falling_back_to_the_machine(run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    run(["login"], service())
    client.actor_path(URL, CLAUDE.lower()).unlink()
    code, out, err, fake = run(["status"], service())
    assert code == 1 and "actor_setup_required" in err and fake.requests == []


def test_login_help_says_chats_sign_in_as_themselves_and_new_is_for_a_machine(run):
    code, out, err, fake = run(["login", "--help"])
    text = " ".join(out.split())
    assert code == 0 and "chat" in text and "signs in as itself" in text and "--new" in text
    assert "a new machine token" in text


def test_an_import_in_a_chat_uses_the_chats_token_never_the_machines(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    code, out, err, fake = run(["import", "github", "acme/repo", "--dry-run"], service())
    assert code != 0 and "actor_setup_required" in out + err and fake.requests == []
    run(["login"], service())
    code, out, err, fake = run(["import", "github", "acme/repo", "--dry-run"],
                               lambda request: refusal("not_available", "stop here", status=404))
    assert fake.requests and {r.headers["authorization"] for r in fake.requests} == {f"Bearer {chat_token(CODEX_A)}"}


def test_a_replaced_chat_token_is_said_to_be_a_new_agent(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    run(["login"], service())
    code, out, err, fake = run(["login"], service(refuse={chat_token(CODEX_A)}))
    assert code == 0 and "new agent" in out


def test_a_malformed_mcp_thread_never_hides_the_codex_thread(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    assert client.actor_binding("not-a-thread") == client.actor_binding() == CODEX_A


def test_chat_tokens_are_private_files_in_a_private_folder(run, home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    run(["login"], service())
    path = client.actor_path(URL, CLAUDE.lower())
    assert path.parent == home / "actors" and path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def bearers(fake) -> list[str]:
    """The bearer of each chat enrolment sent."""
    return [r.headers["authorization"][7:] for r in fake.requests if json.loads(r.content)["for"] == "actor"]


def test_a_worker_logs_in_with_its_launchers_token(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    code, out, err, fake = run(["login"], service())
    assert code == 0 and bearers(fake) == [TOKEN]  # the launcher itself enrols under the machine
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)  # a worker the first chat started on another host
    code, out, err, fake = run(["login"], service())
    assert code == 0 and bearers(fake) == [chat_token(CODEX_A)]
    assert fake.requests[-1].headers["wirk-actor-authorization"] == f"Bearer {chat_token(CODEX_A, CLAUDE)}"


def test_no_or_two_launcher_tokens_fall_back_to_the_machine(run, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    code, out, err, fake = run(["login"], service())  # neither part has a token: no launcher is known
    assert code == 0 and bearers(fake) == [TOKEN]
    monkeypatch.delenv("CODEX_THREAD_ID")
    run(["login"], service())
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    run(["login"], service())
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    code, out, err, fake = run(["login"], service())  # both parts have one: which started it is unknown
    assert code == 0 and bearers(fake) == [TOKEN]


@pytest.mark.parametrize("reason", ["This chat's WIRK token was revoked; see your account page",
                                    "This chat's WIRK token went 90 days unused"])
def test_a_refused_launcher_says_why_and_signs_in_nowhere(run, monkeypatch, reason):
    monkeypatch.setenv("CODEX_THREAD_ID", CODEX_A)
    run(["login"], service())
    launcher = chat_token(CODEX_A)
    def refusing(request):
        if request.headers["authorization"] == f"Bearer {launcher}":
            return refusal("unauthenticated", reason, status=401,
                           hint="Run: wirk login   (in this chat; it signs in as a new agent)")
        return service()(request)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    code, out, err, fake = run(["login"], refusing)
    assert code == 1 and reason in out and "started this one" in out
    assert bearers(fake) == [launcher]  # never enrolled under the machine instead
    assert chat_token(CODEX_A) == launcher


def test_a_grok_session_alone_gets_its_own_chat_identity(run, monkeypatch):
    monkeypatch.setenv("GROK_SESSION_ID", GROK)
    assert "actor_setup_required" in run(["status"], service())[2]
    assert run(["login"], service())[0] == 0
    assert run(["status"], service())[1].strip() == f"You: {chat_token(GROK)}"


def test_a_grok_session_started_from_a_claude_code_chat_is_filed_under_it(run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE)
    assert run(["login"], service())[0] == 0
    launcher = chat_token(CLAUDE)
    monkeypatch.setenv("GROK_SESSION_ID", GROK)  # a Grok session the Claude Code chat started
    code, out, err, fake = run(["login"], service())
    assert code == 0 and bearers(fake) == [launcher]
    assert fake.requests[-1].headers["wirk-actor-authorization"] == f"Bearer {chat_token(CLAUDE, GROK)}"


def test_a_malformed_grok_session_falls_back_to_the_machine(run, monkeypatch):
    monkeypatch.setenv("GROK_SESSION_ID", "not a session")
    code, out, err, fake = run(["status"], service())
    assert code == 0 and out.strip() == f"You: {TOKEN}"
