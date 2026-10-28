"""Shared fixtures: a private configuration directory and a fake service that answers with v2 envelopes."""

import json
import os
import time

import httpx
import pytest

from wirk_cli import cli

URL = "https://wirk.test"
TOKEN = "wirk_" + "a" * 43
PERSON_TOKEN = "wirk_" + "b" * 43


def envelope(text="done", *, ok=True, errors=(), data=None, status=200):
    body = {"ok": ok, "errors": list(errors), "notices": [], "page": {"complete": True, "next_cursor": None},
            "timing_ms": {"total": 1.0}}
    body.update({"data": data} if data is not None else {"text": text})
    return httpx.Response(status, json=body)


def refusal(code, message, *, status=422, hint=None, text=None):
    problem = {"code": code, "message": message, **({"hint": hint} if hint else {})}
    return envelope(text or f"Error {code}: {message}", ok=False, errors=[problem], status=status)


class Fake:
    """Answers every request with `answer(request)` and keeps what was sent."""

    def __init__(self, answer=None):
        self.requests = []
        self.answer = answer or (lambda request: envelope())

    def __call__(self, request):
        self.requests.append(request)
        return self.answer(request)

    def bodies(self):
        return [json.loads(request.content) for request in self.requests]


def write_token(path, value):
    path.write_text(value)
    path.chmod(0o600)


def process_state(pid):
    """What the system's ps says of PID, asked without subprocess, whose next start would reap it on its own."""
    read, write = os.pipe()
    asked = os.posix_spawn("/bin/ps", ["ps", "-o", "stat=", "-p", str(pid)], dict(os.environ),
                           file_actions=[(os.POSIX_SPAWN_DUP2, write, 1)])
    os.close(write)
    with os.fdopen(read) as out:
        text = out.read().strip()
    os.waitpid(asked, 0)
    return text


def exited(pid):
    """Whether PID exits within 10 s and stays unreaped, a zombie (Z) to ps, until its parent reaps it."""
    deadline = time.monotonic() + 10
    while not process_state(pid).startswith("Z"):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture
def home(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    (directory / "config.json").write_text(json.dumps({"service_url": URL}))
    write_token(directory / "agent-token", TOKEN)
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(directory))
    monkeypatch.chdir(tmp_path)
    return directory


@pytest.fixture
def run(home, capsys):
    """Run the CLI against a fake service; returns (exit code, stdout, stderr, fake)."""
    def go(argv, answer=None):
        fake = Fake(answer)
        code = cli.main(argv, transport=httpx.MockTransport(fake))
        captured = capsys.readouterr()
        return code, captured.out, captured.err, fake
    return go


@pytest.fixture(autouse=True)
def outside_any_chat(monkeypatch):
    """Tests run outside an agent chat unless they set one: the IDs of the chat running them never reach them."""
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "GROK_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                 "CLAUDE_CODE_MESSAGING_TOKEN"):
        monkeypatch.delenv(name, raising=False)  # nor the inbox of the chat running them: no test can reach a real chat
    monkeypatch.setenv("WIRK_NO_UPDATE", "1")  # nothing in the suite updates an install; test_update.py turns it on


FAKE_PROGRAMS = {
    # ps answers from files a test writes: the process table, or every process's arguments.
    "ps": '[ -f "$WAKE_FAKES/ps-fails" ] && exit 1\n'
          'case "$*" in *args=*) cat "$WAKE_FAKES/ps-args" ;; *) cat "$WAKE_FAKES/ps" ;; esac 2>/dev/null\nexit 0\n',
    # lsof answers the two questions the listener asks, from a file per process: its directory, or its Unix sockets.
    "lsof": 'echo "lsof $*" >> "$WAKE_FAKES/lsof-calls"\ncase "$*" in\n'
            '  "-a -p $3 -d cwd -Fn") cat "$WAKE_FAKES/lsof-$3" 2>/dev/null ;;\n'
            '  "-a -p $3 -U -Fn") cat "$WAKE_FAKES/lsof-unix-$3" 2>/dev/null ;;\n  *) exit 9 ;;\nesac\nexit 0\n',
    "launchctl": 'echo "launchctl $*" >> "$WAKE_FAKES/calls"\n[ -f "$WAKE_FAKES/jobs-fail" ] && exit 5\n'
                 'case "$1" in print) [ -f "$WAKE_FAKES/loaded" ] ;; bootstrap) touch "$WAKE_FAKES/loaded" ;;\n'
                 '  bootout) rm -f "$WAKE_FAKES/loaded" ;; esac\n',
    "systemctl": 'echo "systemctl $*" >> "$WAKE_FAKES/calls"\n[ -f "$WAKE_FAKES/jobs-fail" ] && exit 5\nexit 0\n',
}
# A harness that answers `agents --json` with the sessions a test lists (after a delay a test may set, keeping the
# environment it was asked in and running what a test says happens meanwhile), and otherwise
# keeps what it was started with, shows itself to ps by its arguments, says what a test gave it and
# exits with the code a test gave it.
HARNESS = ('if [ "$1" = agents ]; then env > "$WAKE_FAKES/agents-env"; sh "$WAKE_FAKES/agents-meanwhile" 2>/dev/null\n'
           '  sleep "$(cat "$WAKE_FAKES/agents-delay" 2>/dev/null || echo 0)"\n'
           '  cat "$WAKE_FAKES/agents" 2>/dev/null; exit "$(cat "$WAKE_FAKES/agents-exit" 2>/dev/null || echo 0)"; fi\n'
           'mkdir -p "$WAKE_FAKES/runs"; run="$WAKE_FAKES/runs/$(printf %04d "$(ls "$WAKE_FAKES/runs" | wc -l)")"\n'
           'mkdir "$run"; echo "$0" > "$run/program"; pwd > "$run/cwd"\n'
           'printf "%s\\n" "$@" > "$run/args"; env > "$run/env"; echo "$$ $0 $*" >> "$WAKE_FAKES/ps-args"\n'
           'cat "$WAKE_FAKES/says" 2>/dev/null; exit "$(cat "$WAKE_FAKES/exit" 2>/dev/null || echo 0)"\n')


class Fakes:
    """The programs the listener uses, faked on PATH, with what they were asked and what they ran."""

    def __init__(self, root):
        self.root, self.bin = root, root / "bin"

    def write(self, name, text):
        (self.root / name).write_text(text)

    def ps(self, *rows):
        """Rows of (pid, ppid, started, program), as ps -A -o pid=,ppid=,lstart=,comm= prints them."""
        self.write("ps", "".join(f"{pid:>6} {ppid:>6} {started} {program}\n" for pid, ppid, started, program in rows))

    def args(self, *rows):
        self.write("ps-args", "".join(f"{pid} {args}\n" for pid, args in rows))

    def agents(self, *rows):
        """The sessions Claude Code lists, as rows of (session ID, pid, status)."""
        self.write("agents", json.dumps([{"pid": pid, "cwd": "/", "kind": "interactive", "startedAt": 0, "sessionId": session,
                                          "name": "chat", "status": status} for session, pid, status in rows]))

    def cwd(self, pid, directory):
        self.write(f"lsof-{pid}", f"p{pid}\nfcwd\nn{directory}\n")

    def sockets(self, pid, *paths):
        """The Unix sockets lsof names for PID: an anonymous one, then each path given (a bound socket's)."""
        self.write(f"lsof-unix-{pid}", f"p{pid}\nf0\ntunix\nn->0x1\n"
                   + "".join(f"f{n}\ntunix\nn{path}\n" for n, path in enumerate(paths, 7)))

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def runs(self):
        """What each harness run was started with: program, directory, arguments and environment."""
        found = []
        for run in sorted((self.root / "runs").glob("*")):
            env = dict(line.split("=", 1) for line in (run / "env").read_text().splitlines() if "=" in line)
            found.append({"program": (run / "program").read_text().strip(), "cwd": (run / "cwd").read_text().strip(),
                          "args": (run / "args").read_text().splitlines(), "env": env})
        return found


@pytest.fixture(autouse=True)
def fakes(tmp_path_factory, monkeypatch):
    """No test installs a real background job, reads the real process table or starts a real harness: launchctl,
    systemctl, ps, lsof, claude and codex are fakes on PATH, home and the state folder are temporary, and the system
    is macOS unless a test says otherwise."""
    from wirk_cli import wake
    root = tmp_path_factory.mktemp("fakes")
    found = Fakes(root)
    found.bin.mkdir()
    for name, text in {**FAKE_PROGRAMS, "claude": HARNESS, "codex": HARNESS}.items():
        (found.bin / name).write_text("#!/bin/sh\n" + text)
        (found.bin / name).chmod(0o755)
    monkeypatch.setenv("WAKE_FAKES", str(root))
    monkeypatch.setenv("PATH", f"{found.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    # a harness's own files are the temporary home's, never the real
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wake, "SYSTEM", "darwin")
    monkeypatch.setattr(wake, "LSOF", str(found.bin / "lsof"))  # by path, as the listener runs it: never the real one
    monkeypatch.setattr(wake, "PROC", root / "proc")  # nor the real /proc
    return found
