"""One host chat keeps its actor bearer; a new chat cannot use the shared parent silently."""

import json
import uuid

from conftest import TOKEN, URL, envelope, write_token
from wirk_cli import client

THREAD_A = str(uuid.UUID(int=11))
THREAD_B = str(uuid.UUID(int=12))


def answer(request):
    if request.url.path == "/v2/login":
        body = json.loads(request.content)
        if body["for"] == "actor":
            assert request.headers.get("wirk-actor-authorization", "").startswith("Bearer wirk_")
            return envelope(data={"state": "logged_in", "principal": "agent-11111111111111111111111111111111",
                                  "kind": "agent", "person": "alice", "parent": "alice-agents"})
        if body.get("format") == "text":
            return envelope("Logged in as alice-agents · agent of alice · account Acme\nnext: status")
        return envelope(data={"state": "logged_in", "principal": "alice-agents", "kind": "agent",
                              "person": "alice", "account": "Acme"})
    assert request.url.path == "/v2/status"
    return envelope(data={"you": {"principal": "agent-11111111111111111111111111111111",
                                  "wirkspace": {"id": "wsp_example", "name": "Acme"}}})


def test_bound_read_requires_explicit_login_and_two_chats_get_separate_secrets(run, home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_A)
    code, out, err, fake = run(["status"], answer)
    assert code == 1 and "actor_setup_required" in err and "wirk login" in err and fake.requests == []
    code, out, err, fake = run(["login"], answer)
    assert code == 0 and "agent-1111" in out
    secret_a = client.read_token(client.actor_path(client.actor_base(URL, ("thread", THREAD_A)),
                                                   "alice-agents", "alice"))
    assert secret_a != TOKEN
    code, out, err, fake = run(["status"], answer)
    assert code == 0 and fake.requests[-1].headers["authorization"] == f"Bearer {secret_a}"
    code, out, err, fake = run(["login"], answer)
    assert code == 0
    assert client.read_token(client.actor_path(client.actor_base(URL, ("thread", THREAD_A)),
                                               "alice-agents", "alice")) == secret_a
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_B)
    code, out, err, fake = run(["status"], answer)
    assert code == 1 and "actor_setup_required" in err and fake.requests == []
    code, out, err, fake = run(["login"], answer)
    assert code == 0
    secret_b = client.read_token(client.actor_path(client.actor_base(URL, ("thread", THREAD_B)),
                                                   "alice-agents", "alice"))
    assert secret_b != secret_a


def test_parent_token_rotation_reuses_same_chat_actor_secret(run, home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_A)
    assert run(["login"], answer)[0] == 0
    path = client.actor_path(client.actor_base(URL, ("thread", THREAD_A)), "alice-agents", "alice")
    child = client.read_token(path)
    write_token(home / "agent-token", "wirk_" + "z" * 43)
    code, out, err, fake = run(["login"], answer)
    assert code == 0 and client.read_token(path) == child
    enrolled = [request for request in fake.requests if request.url.path == "/v2/login" and
                json.loads(request.content).get("for") == "actor"]
    assert enrolled and enrolled[-1].headers["authorization"] == "Bearer " + "wirk_" + "z" * 43
    assert enrolled[-1].headers["wirk-actor-authorization"] == "Bearer " + child


def test_no_host_binding_keeps_explicit_legacy_connection(run, home, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("WIRK_ACTOR_HOME", raising=False)
    code, out, err, fake = run(["status"], answer)
    assert code == 0 and fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"


def test_bound_actor_token_loss_requires_setup_instead_of_global_fallback(run, home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_A)
    assert run(["login"], answer)[0] == 0
    path = client.actor_path(client.actor_base(URL, ("thread", THREAD_A)), "alice-agents", "alice")
    path.unlink()
    code, out, err, fake = run(["status"], answer)
    assert code == 1 and "actor_setup_required" in err and fake.requests == []


def test_actor_home_separates_service_origins(tmp_path):
    binding = ("home", str(tmp_path))
    a = client.actor_path(client.actor_base("https://one.invalid", binding), "alice-agents", "alice")
    b = client.actor_path(client.actor_base("https://two.invalid", binding), "alice-agents", "alice")
    assert a != b


def test_concurrent_first_actor_setup_reuses_one_secret(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path = tmp_path / "actor-token"
    started, release = threading.Event(), threading.Event()
    original, calls = client.new_token, []

    def delayed(target):
        calls.append(target)
        started.set()
        assert release.wait(5)
        return original(target)

    monkeypatch.setattr(client, "new_token", delayed)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.actor_secret, path)
        assert started.wait(5)
        second = pool.submit(client.actor_secret, path)
        release.set()
        assert first.result() == second.result() == client.read_token(path)
    assert len(calls) == 1
