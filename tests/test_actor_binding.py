"""One host chat keeps its actor bearer; a new chat cannot use the shared parent silently."""

import errno
import json
import os
import uuid

import pytest

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


def test_current_actor_metadata_is_private_and_never_follows_a_symlink(tmp_path):
    base = tmp_path / "actors"
    base.mkdir()
    target = tmp_path / "other.json"
    target.write_text(json.dumps({"service_url": URL, "principal": "alice-agents", "person": "alice"}))
    current = base / "current.json"
    current.symlink_to(target)
    with pytest.raises(client.Failure) as error:
        client.actor_current(base, URL)
    assert error.value.code == "insecure_actor_file"
    current.unlink()
    current.write_text(target.read_text())
    current.chmod(0o644)
    with pytest.raises(client.Failure) as error:
        client.actor_current(base, URL)
    assert error.value.code == "insecure_actor_file"


def test_concurrent_actor_metadata_save_refuses_symlinked_current(tmp_path):
    base = tmp_path / "actors"
    base.mkdir()
    target = tmp_path / "other.json"
    expected = {"service_url": URL, "principal": "alice-agents", "person": "alice"}
    target.write_text(json.dumps(expected) + "\n")
    (base / "current.json").symlink_to(target)
    (base / "current.json.new").write_text("another writer")
    with pytest.raises(client.Failure) as error:
        client.save_actor_current(base, URL, "alice-agents", "alice")
    assert error.value.code == "insecure_actor_file"
    assert json.loads(target.read_text()) == expected


def test_actor_secret_refuses_planted_lock_symlink(tmp_path):
    path = tmp_path / "actor-token"
    target = tmp_path / "other"
    target.write_text("do not touch")
    path.with_name("actor-token.lock").symlink_to(target)
    with pytest.raises(client.Failure) as error:
        client.actor_secret(path)
    assert error.value.code == "insecure_actor_file"
    assert target.read_text() == "do not touch"


def test_current_actor_metadata_refuses_a_named_pipe_without_waiting(tmp_path):
    base = tmp_path / "actors"
    base.mkdir()
    os.mkfifo(base / "current.json", 0o600)
    with pytest.raises(client.Failure) as error:
        client.actor_current(base, URL)
    assert error.value.code == "insecure_actor_file"


def test_saved_actor_metadata_is_private_and_readable(tmp_path):
    base = tmp_path / "actors"
    client.save_actor_current(base, URL, "alice-agents", "alice")
    current = base / "current.json"
    assert current.stat().st_mode & 0o777 == 0o600
    assert client.actor_current(base, URL) == ("alice-agents", "alice")


@pytest.mark.parametrize("collision", [False, True])
def test_directory_actor_metadata_is_refused_and_its_descriptor_closed(tmp_path, monkeypatch, collision):
    base = tmp_path / "actors"
    base.mkdir()
    current = base / "current.json"
    current.mkdir(mode=0o700)
    if collision:
        (base / "current.json.new").write_text("another writer")
    opened = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(client.os, "open", tracked_open)
    with pytest.raises(client.Failure) as error:
        if collision:
            client.save_actor_current(base, URL, "alice-agents", "alice")
        else:
            client.actor_current(base, URL)
    assert error.value.code == "insecure_actor_file"
    assert str(error.value) == f"{current} must be a private regular file"
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_actor_metadata_wrap_error_refuses_and_closes_descriptor(tmp_path, monkeypatch):
    base = tmp_path / "actors"
    base.mkdir()
    current = base / "current.json"
    current.write_text("{}")
    current.chmod(0o600)
    opened = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def cannot_wrap(_descriptor):
        raise OSError("cannot wrap")

    monkeypatch.setattr(client.os, "open", tracked_open)
    monkeypatch.setattr(client.os, "fdopen", cannot_wrap)
    with pytest.raises(client.Failure) as error:
        client.actor_current(base, URL)
    assert error.value.code == "insecure_actor_file"
    assert str(error.value) == f"{current} must be a private regular file"
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
