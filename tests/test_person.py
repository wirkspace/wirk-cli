"""Person commands need a person at a terminal who types a confirmation naming the action, and are absent from agent text."""

import json
import os
import pty
import select
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import PERSON_TOKEN, write_token


@pytest.mark.parametrize("argv", [["login", "--person"], ["review", "c4a1e902@1", "accept", "--reason", "Fine", "--person"],
                                  ["admin", "show", "account"], ["admin", "--request", "batch.json"]])
def test_without_a_terminal_person_commands_refuse_before_anything(run, home, argv, tmp_path):
    (tmp_path / "batch.json").write_text('{"request_id": "a-1", "operations": []}')
    write_token(home / "person-token", PERSON_TOKEN)
    code, out, err, fake = run(argv)
    assert code == 2 and fake.requests == [] and "terminal" in err


def serve():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
            payload = json.dumps({"ok": True, "text": "Reviewed", "errors": [], "notices": [],
                                  "page": {"complete": True, "next_cursor": None}, "timing_ms": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # the system picks a free port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", seen, server


def at_terminal(argv, typed, env):
    """Run the CLI with a pseudo-terminal as stdin and stdout; type `typed` at the prompt."""
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, "-m", "wirk_cli", *argv], stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    output, sent = b"", False
    while True:
        ready, _, _ = select.select([master], [], [], 10)
        if not ready:
            break
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not sent and b"Type \"" in output:
            os.write(master, typed.encode() + b"\n")
            sent = True
    process.wait(timeout=10)
    os.close(master)
    return process.returncode, output.decode(errors="replace")


@pytest.mark.parametrize("typed, decided", [("accept c4a1e902", True), ("accept", False), ("", False), ("yes", False)])
def test_at_a_terminal_only_the_exact_confirmation_proceeds(home, typed, decided):
    url, seen, server = serve()
    (home / "config.json").write_text(json.dumps({"service_url": url}))
    write_token(home / "person-token", PERSON_TOKEN)
    env = {**os.environ, "WIRK_CONFIG_DIR": str(home)}
    code, output = at_terminal(["review", "c4a1e902@1", "accept", "--reason", "Checked it", "--person"], typed, env)
    server.shutdown()
    assert 'Type "accept c4a1e902"' in output
    if decided:
        assert code == 0 and len(seen) == 1 and seen[0][1] == f"Bearer {PERSON_TOKEN}"
    else:
        assert code == 2 and seen == []


def test_agent_help_names_the_persons_decision_and_nothing_else_of_theirs(run):
    code, out, err, fake = run(["--help"])
    person_lines = [line for line in out.split("\n") if "--person" in line]
    assert person_lines and all("review" in line or "login" in line for line in person_lines)
    assert "admin" not in out and "person-token" not in out


def serve_login():
    """A service whose /v2/login answers a pending person grant, then logged in."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            done = sum(1 for path, sent in seen if sent.get("format") == "json") > 1
            data = ({"state": "logged_in", "principal": "alice", "kind": "person", "account": "Acme"} if done else
                    {"state": "pending", "code": "BDFG-HJKL", "url": "https://app.wirk.life/device",
                     "expires_at": "2026-10-02T14:05:00Z", "interval": 1})
            text = "Logged in as alice" if done else ("Type this code at https://app.wirk.life/device: BDFG-HJKL\n"
                                                      "It gives this terminal everything you can do\nafter approving: login")
            payload = json.dumps({"ok": True, **({"data": data} if body.get("format") == "json" else {"text": text}),
                                  "errors": [], "notices": [], "page": {"complete": True, "next_cursor": None}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", seen, server


def test_person_login_shows_the_code_to_type_and_never_a_link_with_it(home, tmp_path):
    url, seen, server = serve_login()
    directory = tmp_path / "person-config"
    env = {**os.environ, "WIRK_CONFIG_DIR": str(directory), "BROWSER": "true"}
    code, output = at_terminal(["login", "--person", "--url", url], "person token", env)
    server.shutdown()
    assert code == 0, output
    assert "BDFG-HJKL" in output and "https://app.wirk.life/device" in output and "code=" not in output
    assert seen[0][1]["for"] == "person" and (directory / "person-token").exists()
