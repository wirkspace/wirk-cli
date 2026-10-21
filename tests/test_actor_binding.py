"""Each agent chat signs in as itself under the machine's one approval; a chat started by another is not its launcher."""

import json
import uuid

from conftest import TOKEN, URL, envelope, refusal, write_token
from wirk_cli import client

CODEX_A, CODEX_B, CLAUDE = str(uuid.UUID(int=11)), str(uuid.UUID(int=12)), str(uuid.UUID(int=13)).upper()


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
