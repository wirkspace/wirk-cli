"""The independent review's findings, each pinned by a test."""

import ast
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from conftest import TOKEN, envelope, write_token
from wirk_cli import cli, client, context, grammar


# 1. The file check follows the file, not its name: hard links, /dev/fd and standard input.

def test_a_hard_link_to_a_token_is_refused(run, home, tmp_path):
    os.link(home / "agent-token", tmp_path / "innocent.txt")
    code, out, err, fake = run(["write", "new", "T", "--body-file", "innocent.txt"])
    assert code == 2 and fake.requests == [] and TOKEN not in out + err


def test_dev_fd_and_standard_input_holding_a_token_are_refused(run, home, monkeypatch):
    with open(home / "agent-token", "rb") as stream:
        code, out, err, fake = run(["write", "new", "T", "--body-file", f"/dev/fd/{stream.fileno()}"])
        assert code == 2 and fake.requests == []
        monkeypatch.setattr("sys.stdin", open(home / "agent-token"))
        code, out, err, fake = run(["write", "new", "T", "--body-file", "-"])
        assert code == 2 and fake.requests == [] and TOKEN not in out + err


def test_every_file_opened_goes_through_the_check():
    """In cli.py a named file is opened only by `readable` (to read) and `download` (to create its target)."""
    allowed = {cli: {"readable", "download"}, client: {"read_token", "new_token", "saved_url", "save_url"}}
    for module, functions in allowed.items():
        tree = ast.parse(inspect.getsource(module))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                owner = getattr(getattr(call.func, "value", None), "id", None)
                if name in ("open", "read_text", "read_bytes", "write_text", "fdopen") and owner != "webbrowser":
                    assert function.name in functions, (module.__name__, function.name, name)


# 2. A malformed token never reaches a header or an error.

@pytest.mark.parametrize("value", ["wirk_abc\ndef0123456789012345678901234567890", "has a space " + "x" * 40, "é" * 40])
def test_a_malformed_token_file_is_refused_without_its_content(run, home, value):
    write_token(home / "agent-token", value)
    code, out, err, fake = run(["status"])
    assert code == 1 and fake.requests == [] and "invalid_token_file" in err and value.strip() not in out + err


def test_transport_errors_print_only_the_error_type(run):
    def leak(request):
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {TOKEN}'")
    code, out, err, fake = run(["status"], leak)
    assert code == 1 and "LocalProtocolError" in err and TOKEN not in out + err


# 3. Download never deletes a file it did not create.

def test_download_keeps_a_file_that_appears_before_it_writes(run, tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("precious")
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)  # the file appears after any check by name

    def answer(request):
        if request.url.host == "storage.test":
            return httpx.Response(200, content=b"hello")
        return envelope(data={"download": {"url": "https://storage.test/o?s=1", "filename": "f.txt", "bytes": 5,
                                           "sha256": "0" * 64}})
    code, out, err, fake = run(["download", "5c1e7a90", "file_4e1f"], answer)
    assert code != 0 and (tmp_path / "f.txt").read_text() == "precious"


# 4. Every printed command is quoted by one rule, = included.

@pytest.mark.parametrize("value, quoted", [("=sum", "'=sum'"), ("it's", "'it'\\''s'"), ("my file.txt", "'my file.txt'"),
                                           ("plain", "plain")])
def test_one_quoting_rule(value, quoted):
    assert grammar.shell_word(value) == quoted


def test_printed_commands_use_it(run, tmp_path):
    with pytest.raises(grammar.UsageError, match=r"wirk query '=sum'"):
        grammar.revision("=sum", "write edit")
    with pytest.raises(grammar.UsageError, match=r"wirk query 'about=it'\\''s done'"):
        grammar.query_body(["it's", "done"])
    (tmp_path / "=odd name.txt").write_text("x")

    def refuse(request):
        if request.url.host == "storage.test":
            return httpx.Response(403)
        return envelope(data={"present": False, "put": {"url": "https://storage.test/o", "headers": {}, "expires_at": "x"}})
    code, out, err, fake = run(["upload", "=odd name.txt", "--request-id", "u 1"], refuse)
    assert "wirk upload '=odd name.txt' --request-id 'u 1'" in err


# 5. A piped agent sees login's link at once.

def test_login_flushes_its_link_to_a_pipe(tmp_path):
    class Pending(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            answer = {"ok": True, "errors": [], "notices": [], "page": {"complete": True, "next_cursor": None}}
            answer.update({"data": {"state": "pending", "code": "BDFG-HJKL", "url": "https://wirk.test/device?code=BDFG-HJKL",
                                    "expires_at": "x", "interval": 5}} if body["format"] == "json" else
                          {"text": "Approve this machine: https://wirk.test/device?code=BDFG-HJKL"})
            payload = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Pending)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {**os.environ, "WIRK_CONFIG_DIR": str(tmp_path / "wirk")}
    process = subprocess.Popen([sys.executable, "-m", "wirk_cli", "login", "--url", f"http://127.0.0.1:{server.server_address[1]}"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        start = time.monotonic()
        line = process.stdout.readline().decode()
        assert "Approve this machine" in line and time.monotonic() - start < 5
    finally:
        process.kill()
        server.shutdown()


# 6. review --request --person names the decision; extra words are refused, not a crash.

def test_review_request_with_words_is_a_usage_error(run, tmp_path):
    (tmp_path / "decide.json").write_text(json.dumps({"request_id": "r-1", "decisions": [
        {"id": "c4a1e902", "revision": 1, "action": "accept", "reason": "Fine"}]}))
    code, out, err, fake = run(["review", "extra", "--request", "decide.json", "--person"])
    assert code == 2 and fake.requests == [] and "Traceback" not in err


def test_the_person_confirmation_for_a_request_names_its_decisions(tmp_path):
    body = {"decisions": [{"id": "c4a1e902", "revision": 1, "action": "accept"}, {"id": "e7b35d16", "revision": 2, "action": "reject"}]}
    assert cli.decision_phrase(body) == "accept c4a1e902 reject e7b35d16"


# 7. The repository value follows the service's rule, so one odd remote never voids the header.

def test_an_odd_remote_gives_a_valid_repo_or_none(tmp_path, monkeypatch):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://git.example.org/acme/api@2"], check=True)
    monkeypatch.chdir(tmp_path)
    header = context.header("wirk-cli", "0.3.0", "0123456789abcdef")
    repo = dict(token.split("=", 1) for token in header.split())["repo"]
    assert "@" not in repo and (repo.startswith("local:") or repo.count("/") == 2)


# 8. People's commands are documented for people only.

def test_readme_has_a_section_for_people_and_login_help_describes_approval(capsys):
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert "## For people" in readme and "--person" in readme.split("## For people", 1)[1]
    assert cli.main(["login", "--help"]) == 0
    text = capsys.readouterr().out
    assert "approve" in text and "--person" not in text


# 9. Simpler: one connection per download, the time zone read once, and --request alone.

def test_download_connects_once_and_show_reads_the_zone_once(run, monkeypatch):
    calls = {"connect": 0, "zone": 0}
    real = cli.connect

    def counting(*args, **kwargs):
        calls["connect"] += 1
        return real(*args, **kwargs)
    monkeypatch.setattr(cli, "connect", counting)
    monkeypatch.setattr(cli, "local_zone", lambda: calls.__setitem__("zone", calls["zone"] + 1) or "UTC")
    run(["download", "5c1e7a90", "file_4e1f"], lambda r: envelope(ok=False, errors=[{"code": "not_available", "message": "x"}]))
    run(["show", "status", "--no-open"])
    assert calls == {"connect": 2, "zone": 1}


@pytest.mark.parametrize("argv", [["write", "new", "T", "--request", "b.json"], ["write", "--request", "b.json", "--request-id", "x"],
                                  ["write", "--request", "b.json", "--link", "related_to:5c1e7a90"],
                                  ["query", "5c1e7a90", "--request", "b.json"]])
def test_request_refuses_shortcut_arguments(run, tmp_path, argv):
    (tmp_path / "b.json").write_text('{"request_id": "w-1", "operations": []}')
    code, out, err, fake = run(argv)
    assert code == 2 and fake.requests == []


# 10. The recheck: a retry that works from a subdirectory, and clean refusals instead of tracebacks.

def test_an_upload_retry_names_the_path_given_and_confirms_the_base_name(run, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x")

    def refuse(request):
        if request.url.host == "storage.test":
            return httpx.Response(403)
        return envelope(data={"present": False, "put": {"url": "https://storage.test/o", "headers": {}, "expires_at": "x"}})
    code, out, err, fake = run(["upload", "sub/f.txt", "--request-id", "u-1", "--description", "a note"], refuse)
    assert "wirk upload sub/f.txt --request-id u-1 --description 'a note'" in err
    code, out, err, fake = run(["upload", "sub/f.txt", "--request-id", "u-2"], lambda r: envelope(data={"present": True}))
    assert fake.bodies()[-1]["confirm"]["filename"] == "f.txt"


def test_download_into_a_missing_folder_is_refused_cleanly(run):
    answer = envelope(data={"download": {"url": "https://storage.test/o", "filename": "f.txt", "bytes": 1, "sha256": "0" * 64}})
    code, out, err, fake = run(["download", "5c1e7a90", "file_4e1f", "-o", "nodir/x"], lambda r: answer)
    assert code == 2 and "nodir/x" in err and "Traceback" not in err


@pytest.mark.parametrize("command, key", [("review", "decisions"), ("admin", "operations")])
@pytest.mark.parametrize("value", ["accept", [1], None, []])
def test_a_person_request_with_malformed_entries_is_refused_cleanly(run, tmp_path, monkeypatch, command, key, value):
    monkeypatch.setattr(cli, "at_terminal", lambda: None)  # as if at a terminal: the check comes before any question
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked before the request was checked"))
    (tmp_path / "decide.json").write_text(json.dumps({"request_id": "r-1", key: value}))
    code, out, err, fake = run([command, "--request", "decide.json", *(["--person"] if command == "review" else [])])
    assert code == 2 and fake.requests == [] and key in err
