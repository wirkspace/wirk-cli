"""wirk login: the machine makes its own token, binds it to one address, and shows only its digest."""

import hashlib
import json
import os
import re
import stat

import httpx
import pytest

from conftest import envelope, refusal
from wirk_cli import cli


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    directory = tmp_path / "wirk"
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(directory))
    return directory


def without_web_sign_in(answer):
    """A service that offers no browser approval, so login checks the token with status (the digest flow)."""
    return lambda request: (refusal("not_available", "This service has no web sign-in", status=404)
                            if request.url.path == "/v2/login" else answer(request))


def login(argv, answer, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return answer(request)
    code = cli.main(["login", *argv], transport=httpx.MockTransport(handler))
    out = capsys.readouterr()
    return code, out.out, out.err, requests


def logged_in(request):
    return envelope(data={"you": {"principal": "alice-agents", "wirkspace": {"id": "wsp_1a2b3c4d5e6f", "name": "Acme"}}})


def test_first_login_makes_a_token_and_shows_only_its_digest(fresh, capsys):
    code, out, err, requests = login([], without_web_sign_in(lambda r: refusal("unauthenticated", "Bearer token is invalid", status=401)), capsys)
    token = (fresh / "agent-token").read_text()
    assert re.fullmatch(r"wirk_[A-Za-z0-9_-]{43}", token)
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o700 and stat.S_IMODE(os.stat(fresh / "agent-token").st_mode) == 0o600
    assert json.loads((fresh / "config.json").read_text()) == {"service_url": "https://api.wirk.life"}
    assert code == 0 and hashlib.sha256(token.encode()).hexdigest() in out and token not in out + err
    assert str(requests[-1].url) == "https://api.wirk.life/v2/status"
    assert json.loads(requests[-1].content) == {"format": "json", "max_bytes": 1024}


def test_a_registered_token_logs_in(fresh, capsys):
    code, out, err, requests = login([], without_web_sign_in(logged_in), capsys)
    assert code == 0 and "Logged in to https://api.wirk.life as alice-agents" in out and "wirk status" in out


def test_rerunning_keeps_the_token_and_new_replaces_it(fresh, capsys):
    login([], without_web_sign_in(logged_in), capsys)
    first = (fresh / "agent-token").read_text()
    login([], without_web_sign_in(logged_in), capsys)
    assert (fresh / "agent-token").read_text() == first
    login(["--new"], without_web_sign_in(logged_in), capsys)
    assert (fresh / "agent-token").read_text() != first


def test_no_wirkspace_prints_the_services_words(fresh, capsys):
    code, out, err, requests = login([], without_web_sign_in(lambda r: refusal("no_wirkspace", "You are not in a wirkspace yet",
                                                            status=403, hint="Ask an administrator to add you")), capsys)
    assert "You are not in a wirkspace yet" in out + err and "Ask an administrator to add you" in out + err


@pytest.mark.parametrize("url", ["http://api.wirk.life", "https://api.wirk.life/v2", "https://u:p@api.wirk.life",
                                 "ftp://api.wirk.life", "https://api.wirk.life?x=1"])
def test_unsafe_addresses_are_refused(fresh, capsys, url):
    code, out, err, requests = login(["--url", url], without_web_sign_in(logged_in), capsys)
    assert code == 2 and requests == [] and not (fresh / "agent-token").exists()


def test_loopback_http_is_allowed_for_a_local_service(fresh, capsys):
    code, out, err, requests = login(["--url", "http://127.0.0.1:8765"], without_web_sign_in(logged_in), capsys)
    assert code == 0 and str(requests[-1].url) == "http://127.0.0.1:8765/v2/status"


def test_the_token_is_bound_to_its_address(fresh, capsys):
    login([], without_web_sign_in(logged_in), capsys)
    code, out, err, requests = login(["--url", "https://wirk.example.org"], without_web_sign_in(logged_in), capsys)
    assert code == 2 and requests == [] and "--new" in err
    code, out, err, requests = login(["--url", "https://wirk.example.org", "--new"], without_web_sign_in(logged_in), capsys)
    assert code == 0 and str(requests[-1].url) == "https://wirk.example.org/v2/status"


def test_a_planted_temporary_file_or_link_is_never_written_through(fresh, capsys, tmp_path):
    fresh.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (fresh / "agent-token.new").symlink_to(victim)
    code, out, err, requests = login([], without_web_sign_in(logged_in), capsys)
    assert code != 0 and victim.read_text() == "keep" and not (fresh / "agent-token").exists()


# ---------------------------------------------------------------- device approval through POST /v2/login (sign-up plan §4)

PENDING_TEXT = ("Approve this machine: https://app.wirk.life/device?code=BDFG-HJKL\n"
                "Code BDFG-HJKL · for your agents · expires 14:05 UTC\nafter approving: login")


LOGGED_IN = "Logged in as alice-agents · agent of alice · account Acme\nnext: status"  # the service's words


def device(states, *, text_logged_in=LOGGED_IN):
    """A service whose /v2/login answers pending until `states` runs out, then logged in."""
    queue = list(states)

    def answer(request):
        body = json.loads(request.content)
        if request.url.path != "/v2/login":
            raise AssertionError(request.url.path)
        state = queue[0] if queue else "logged_in"
        if body.get("format") == "json" and queue:
            queue.pop(0)
        if state == "logged_in":
            return envelope(text_logged_in) if body.get("format") != "json" else envelope(data={
                "state": "logged_in", "principal": "alice-agents", "kind": "agent", "person": "alice", "account": "Acme"})
        if body.get("format") != "json":
            return envelope(PENDING_TEXT)
        return envelope(data={"state": "pending", "code": "BDFG-HJKL", "url": "https://app.wirk.life/device?code=BDFG-HJKL",
                              "expires_at": "2026-10-02T14:05:00Z", "interval": 5})
    return answer


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 0.0, "slept": []}
    monkeypatch.setattr("time.monotonic", lambda: now["t"])

    def sleep(seconds):
        now["slept"].append(seconds)
        now["t"] += seconds
    monkeypatch.setattr("time.sleep", sleep)
    return now


def test_login_waits_for_the_person_to_approve(fresh, capsys, clock):
    code, out, err, requests = login([], device(["pending", "pending"]), capsys)
    token = (fresh / "agent-token").read_text()
    assert code == 0 and PENDING_TEXT in out and "Logged in as alice-agents" in out
    assert clock["slept"] == [5, 5] and token not in out + err
    assert all(r.url.path == "/v2/login" and r.headers["authorization"] == f"Bearer {token}" for r in requests)
    first = json.loads(requests[0].content)
    assert first["for"] == "agent" and "label" in first and token not in first["label"]


def test_logging_in_again_once_approved_prints_one_line(fresh, capsys, clock):
    """Running the installer again logs in again: the machine is approved already, so it says so once."""
    code, out, err, requests = login([], device([]), capsys)
    assert code == 0 and out.count("Logged in as alice-agents") == 1 and clock["slept"] == [] and "Approve" not in out


def test_an_agent_gives_up_after_100_seconds_and_says_how_to_resume(fresh, capsys, clock):
    code, out, err, requests = login([], device(["pending"] * 100), capsys)
    assert code == 1 and "login_pending" in err and "after approving, run: wirk login" in err
    assert 95 <= sum(clock["slept"]) <= 100


def test_a_denied_or_revoked_token_prints_the_services_refusal(fresh, capsys, clock):
    words = "This machine's token was revoked; make a new one: wirk login --new"
    code, out, err, requests = login([], lambda r: refusal("not_authorized", words, status=403,
                                                           text=f"Error not_authorized: {words}"), capsys)
    assert code == 1 and words in out + err and len(requests) == 1


@pytest.mark.parametrize("missing", [
    lambda r: refusal("not_available", "This service has no web sign-in", status=404,
                      hint="Have an administrator register this machine's digest"),
    lambda r: httpx.Response(404, json={"detail": "Not Found"}),
])
def test_without_web_sign_in_the_digest_flow_stays(fresh, capsys, clock, missing):
    def answer(request):
        if request.url.path == "/v2/login":
            return missing(request)
        return refusal("unauthenticated", "Bearer token is invalid", status=401)
    code, out, err, requests = login([], answer, capsys)
    token = (fresh / "agent-token").read_text()
    assert code == 0 and hashlib.sha256(token.encode()).hexdigest() in out and token not in out + err


def test_the_services_next_line_is_the_only_one(fresh, capsys, clock):
    code, out, err, requests = login([], device([]), capsys)
    assert code == 0 and out == LOGGED_IN + "\n"
