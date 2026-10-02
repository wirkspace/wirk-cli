"""Shared fixtures: a private configuration directory and a fake service that answers with v2 envelopes."""

import json

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
