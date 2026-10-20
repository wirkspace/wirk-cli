"""wirk login: the machine makes its own token, binds it to one address, and shows only its digest."""

import hashlib
import http.client
import json
import os
import re
import socket
import stat
import struct
import time
import webbrowser

import httpx
import pytest

from conftest import TOKEN, envelope, refusal
from wirk_cli import cli


@pytest.fixture(autouse=True)
def no_display(monkeypatch):
    """No browser unless a test gives one: these tests never open a real browser."""
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 50000 10.0.0.2 22")
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail(f"opened {url}"))


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


NO_BROWSER = ("No browser on this machine. Open this link in your browser on any computer and approve:\n"
              "https://app.wirk.life/device?code=BDFG-HJKL\nCode BDFG-HJKL · for your agents · expires 14:05 UTC\n")


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
    assert code == 0 and NO_BROWSER in out and "Logged in as alice-agents" in out
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


@pytest.fixture
def interrupted_wait(monkeypatch):
    class Listener:
        closed = False

        def get(self, interval):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    listener = Listener()
    monkeypatch.setattr("time.sleep", listener.get)
    return listener


@pytest.mark.parametrize("with_listener", [True, False], ids=["listener", "sleep"])
@pytest.mark.parametrize("rerun", ["wirk login", "wirk login --person"])
def test_interrupted_approval_wait_stops_cleanly(capsys, interrupted_wait, with_listener, rerun):
    asks = [(None, {}, {"ok": True, "data": {"state": "pending", "interval": 5}})]
    try:
        code = cli.approve(asks, interrupted_wait if with_listener else None, rerun)
    except KeyboardInterrupt:
        pytest.fail("approval wait leaked KeyboardInterrupt")
    assert code == 130
    assert capsys.readouterr() == ("", "Stopped waiting. The approval link and this machine's token stay valid "
                                   f"until the code expires; after approving, run: {rerun}\n")


@pytest.mark.parametrize("person", [False, True])
def test_interrupted_login_closes_the_listener(fresh, capsys, monkeypatch, interrupted_wait, at_terminal, person):
    monkeypatch.setattr(cli, "browser_here", lambda: True)
    monkeypatch.setattr("wirk_cli.loopback.Listener", lambda: interrupted_wait)
    monkeypatch.setattr(cli, "show_link", lambda asks, listener: listener)
    argv = ["--person"] if person else []
    try:
        code, out, err, requests = login(argv, device(["pending", "pending"]), capsys)
    except KeyboardInterrupt:
        pytest.fail("login leaked KeyboardInterrupt")
    assert interrupted_wait.closed and code == 130
    assert out == "" and err == ("Stopped waiting. The approval link and this machine's token stay valid "
                                "until the code expires; after approving, run: wirk login"
                                + (" --person" if person else "") + "\n")
    assert (fresh / "agent-token").exists()
    if person:
        assert (fresh / "person-token").exists()


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


# ---------------------------------------------------------------- one-click login (wirk-core docs/plans/one-click-login.md)

VALUE = "Zx9_-" + "q" * 17  # the shape of the one-time value the service makes (22 URL-safe characters)


def visit(port, path, host=None):
    """A browser's GET to the listener, with the Host it would send (or another)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
    answer = connection.getresponse()
    result = answer.status, dict(answer.getheaders()), answer.read().decode()
    connection.close()
    return result


@pytest.fixture
def listener():
    from wirk_cli.loopback import Listener
    found = Listener()
    yield found
    found.close()


def test_the_listener_takes_the_value_only_from_done_on_its_own_host(listener, capsys):
    assert listener.server_address[0] == "127.0.0.1" and listener.port >= 1024
    status, headers, page = visit(listener.port, f"/done?c={VALUE}")
    assert status == 200 and "Connected. You can close this tab." in page and listener.get(1) == VALUE
    assert headers["Content-Security-Policy"] == "default-src 'none'" and "src=" not in page and "href=" not in page
    out, err = capsys.readouterr()
    assert VALUE not in out + err


@pytest.mark.parametrize("path, host", [
    (f"/done?c={VALUE}", "localhost:{port}"),           # DNS rebinding: another name for this address
    (f"/done?c={VALUE}", "evil.example:{port}"),
    (f"/done?c={VALUE}", "127.0.0.1"),
    ("/favicon.ico", None), ("/", None), (f"/other?c={VALUE}", None), ("/done", None),
    ("/done?c=short", None), ("/done?c=" + "a" * 65, None), ("/done?c=%3Cscript%3E" + "a" * 20, None),
])
def test_anything_else_is_404_and_waiting_continues(listener, capsys, path, host):
    status, headers, page = visit(listener.port, path, host and host.format(port=listener.port))
    assert status == 404 and listener.get(0.05) is None
    status, headers, page = visit(listener.port, f"/done?c={VALUE}")  # the real value still arrives afterwards
    assert status == 200 and listener.get(1) == VALUE
    out, err = capsys.readouterr()
    assert VALUE not in out + err


def test_an_idle_connection_blocks_neither_the_click_nor_close(listener):
    idle = socket.create_connection(("127.0.0.1", listener.port))  # a browser's preconnect that sends nothing
    try:
        start = time.monotonic()
        assert visit(listener.port, f"/done?c={VALUE}")[0] == 200 and listener.get(1) == VALUE
        listener.close()
        assert time.monotonic() - start < 1
    finally:
        idle.close()


def test_a_connection_reset_before_the_answer_prints_nothing(listener, capfd):
    for _ in range(50):
        client = socket.create_connection(("127.0.0.1", listener.port))
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with a reset
        client.sendall(f"GET /done?c={VALUE} HTTP/1.1\r\nHost: 127.0.0.1:{listener.port}\r\n\r\n".encode())
        client.close()
    time.sleep(0.5)
    listener.close()
    assert capfd.readouterr() == ("", "")


def test_the_listener_closes(listener):
    port = listener.port
    listener.close()
    with pytest.raises(OSError):
        visit(port, f"/done?c={VALUE}")


LOGGED_IN_DATA = {"state": "logged_in", "principal": "alice-agents", "kind": "agent", "person": "alice", "account": "Acme"}
WRONG = "W" * 22


def one_click(interval=5, url="https://app.wirk.life/device?code=BDFG-HJKL"):
    """Core's /v2/login: pending until the token comes back with the value the approval sent to the listener."""
    state = {"done": False, "completes": [], "then": None}

    def answer(request):
        body = json.loads(request.content)
        if "complete" in body:
            state["completes"].append(body["complete"])
            state["done"] = state["done"] or body["complete"] == VALUE
            if state["then"] and not state["done"]:
                state["then"]()
        if state["done"]:
            return envelope(LOGGED_IN) if body["format"] == "text" else envelope(data=LOGGED_IN_DATA)
        return envelope(data={"state": "pending", "code": "BDFG-HJKL", "url": url,
                              "expires_at": "2026-10-02T14:05:00Z", "interval": interval})
    return answer, state


@pytest.fixture
def browser(monkeypatch, clock):
    """A display here, and a browser whose person clicks Connect: the approval brings `clicks` to the listener.
    Waiting on the listener runs on the fake clock, so a give-up costs no real time."""
    from wirk_cli.loopback import Listener
    wait = Listener.get

    def get(self, seconds):
        clock["t"] += seconds
        return wait(self, 0.01)
    monkeypatch.setattr(Listener, "get", get)
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("read from the terminal"))
    seen = {"urls": [], "clicks": [VALUE]}

    def click(value):
        port = int(re.search(r"port=(\d+)", seen["urls"][-1]).group(1))
        assert visit(port, f"/done?c={value}")[0] == 200

    def open_(url):
        seen["urls"].append(url)
        for value in seen["clicks"]:
            click(value)
        return True
    monkeypatch.setattr("webbrowser.open", open_)
    seen["click"] = click
    return seen


def test_an_agent_run_login_opens_the_browser_and_one_click_logs_in(fresh, capsys, browser):
    answer, state = one_click()
    code, out, err, requests = login([], answer, capsys)
    token = (fresh / "agent-token").read_text()
    [url] = browser["urls"]
    port = int(url.rsplit("=", 1)[1])
    assert url == f"https://app.wirk.life/device?code=BDFG-HJKL&port={port}"
    assert code == 0 and out == (f"Approve this machine in your browser: {url}\n"
                                 "Code BDFG-HJKL · for your agents · expires 14:05 UTC\n" + LOGGED_IN + "\n")
    assert state["completes"] == [VALUE] and VALUE not in out + err and token not in out + err
    assert all(r.headers["authorization"] == f"Bearer {token}" for r in requests)
    with pytest.raises(OSError):  # closed once the login finished
        visit(port, f"/done?c={VALUE}")


def test_a_refused_value_keeps_the_listener_waiting(fresh, capsys, browser):
    answer, state = one_click()
    browser["clicks"] = [WRONG]  # say a page on another site navigated here first
    state["then"] = lambda: browser["click"](VALUE)
    code, out, err, requests = login([], answer, capsys)
    assert code == 0 and state["completes"] == [WRONG, VALUE] and "Logged in as alice-agents" in out
    assert VALUE not in out + err and WRONG not in out + err


def test_giving_up_before_the_click_closes_the_listener(fresh, capsys, browser):
    browser["clicks"] = []
    answer, state = one_click()
    code, out, err, requests = login([], answer, capsys)
    port = int(browser["urls"][0].rsplit("=", 1)[1])
    assert code == 1 and "login_pending" in err and state["completes"] == []
    with pytest.raises(OSError):
        visit(port, f"/done?c={VALUE}")


@pytest.mark.parametrize("place", ["ssh", "linux without a display", "browser fails"])
def test_without_a_browser_the_link_has_no_port_and_login_polls(fresh, capsys, clock, monkeypatch, place):
    made = []
    if place != "ssh":
        monkeypatch.delenv("SSH_CONNECTION")
    if place == "linux without a display":
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr("wirk_cli.loopback.Listener", lambda: pytest.fail("listened"))
    if place == "browser fails":
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr("webbrowser.open", lambda url: made.append(url) and False)
    code, out, err, requests = login([], device(["pending"]), capsys)
    assert code == 0 and out == NO_BROWSER + LOGGED_IN + "\n" and clock["slept"] == [5]
    assert all("complete" not in json.loads(r.content) for r in requests)
    if made:  # the browser was asked, refused, and the listener closed
        with pytest.raises(OSError):
            visit(int(made[0].rsplit("=", 1)[1]), f"/done?c={VALUE}")


PERSON_IN = "Logged in as alice · registered as a person · account Acme\nnext: status"


def pair():
    """Core's /v2/login for two tokens: the person's (code CDFG-MNPQ, typed on a page without it) and the agents'."""
    sent = {}

    def answer(request):
        body, token = json.loads(request.content), request.headers["authorization"][7:]
        person = body["for"] == "person"
        sent.setdefault(body["for"], []).append(body)
        done = any(b.get("complete") == VALUE for b in sent[body["for"]])
        if done:
            return envelope(PERSON_IN if person else LOGGED_IN) if body["format"] == "text" else envelope(data={
                "state": "logged_in", "principal": "alice" if person else "alice-agents"})
        return envelope(data={"state": "pending", "code": "CDFG-MNPQ" if person else "BDFG-HJKL",
                              "url": "https://app.wirk.life/device" + ("" if person else "?code=BDFG-HJKL"),
                              "expires_at": "2026-10-02T14:05:00Z", "interval": 5})
    return answer, sent


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(cli, "at_terminal", lambda: None)


def test_person_login_makes_both_tokens_and_one_click_approves_the_pair(fresh, capsys, browser, at_terminal):
    answer, sent = pair()
    code, out, err, requests = login(["--person"], answer, capsys)
    [url] = browser["urls"]
    port = int(url.rsplit("=", 1)[1])
    assert url == f"https://app.wirk.life/device?code=CDFG-MNPQ&code=BDFG-HJKL&port={port}"
    assert code == 0 and out == (f"Approve this machine in your browser: {url}\n"
                                 "Code CDFG-MNPQ · for you · expires 14:05 UTC\n"
                                 "Code BDFG-HJKL · for your agents · expires 14:05 UTC\n" + PERSON_IN + "\n" + LOGGED_IN + "\n")
    for wants in ("person", "agent"):
        asked = [body for body in sent[wants] if body["format"] == "json"]
        assert [body.get("complete") for body in asked] == [None, VALUE]  # one ask each, then one complete each
    tokens = {name: (fresh / name).read_text() for name in ("person-token", "agent-token")}
    assert len(set(tokens.values())) == 2 and all(stat.S_IMODE(os.stat(fresh / name).st_mode) == 0o600 for name in tokens)
    assert {r.headers["authorization"][7:] for r in requests} == set(tokens.values())
    assert VALUE not in out + err and not any(token in out + err for token in tokens.values())


@pytest.mark.parametrize("first", ["person", "agent"])
@pytest.mark.parametrize("with_listener", [True, False], ids=["bound", "unbound"])
def test_partial_pair_names_the_remaining_grants_own_approval_page(
        fresh, capsys, browser, at_terminal, monkeypatch, first, with_listener):
    browser["clicks"] = []  # one code was approved on the typed page, not through the paired loopback link
    if not with_listener:
        monkeypatch.setattr("webbrowser.open", lambda url: False)  # pair was requested, then browser failed to open
    calls = {"person": 0, "agent": 0}

    def answer(request):
        body = json.loads(request.content)
        wants = body["for"]
        if body["format"] == "json":
            calls[wants] += 1
        done = calls[wants] >= (2 if wants == first else 4)
        if done:
            return envelope(PERSON_IN if wants == "person" else LOGGED_IN) if body["format"] == "text" else envelope(
                data={"state": "logged_in", "principal": "alice" if wants == "person" else "alice-agents"})
        code = "CDFG-MNPQ" if wants == "person" else "BDFG-HJKL"
        url = "https://app.wirk.life/device" + ("" if wants == "person" else f"?code={code}")
        return envelope(data={"state": "pending", "code": code, "url": url,
                              "expires_at": "2026-10-02T14:05:00Z", "interval": 5})

    code, out, err, requests = login(["--person"], answer, capsys)
    remaining = "agent" if first == "person" else "person"
    remaining_code = "BDFG-HJKL" if remaining == "agent" else "CDFG-MNPQ"
    if with_listener:
        port = int(browser["urls"][0].rsplit("=", 1)[1])
        url = f"https://app.wirk.life/device?code={remaining_code}&port={port}"
    else:
        assert browser["urls"] == []
        url = "https://app.wirk.life/device?code=BDFG-HJKL" if remaining == "agent" else "https://app.wirk.life/device"
    label = "your agents" if remaining == "agent" else "your own sign-in"
    consumed_code = "CDFG-MNPQ" if remaining == "agent" else "BDFG-HJKL"
    assert consumed_code not in url
    assert code == 0 and not err
    assert out.count(f"Still waiting for {label}: {url}") == 1
    assert out.count(f"Code {remaining_code}") >= 1
    assert PERSON_IN in out and LOGGED_IN in out
    assert all("complete" not in json.loads(request.content) for request in requests)


@pytest.mark.parametrize("with_listener", [True, False], ids=["bound", "unbound"])
@pytest.mark.parametrize("person_completes", [True, False], ids=["completion-and-change", "two-code-change"])
def test_changed_pending_codes_show_current_singleton_recovery_links_once(
        capsys, clock, with_listener, person_completes):
    def pending(wants, code):
        return {"ok": True, "data": {"state": "pending", "code": code,
                "url": "https://app.wirk.life/device" + ("" if wants == "person" else f"?code={code}"),
                "expires_at": "2026-10-02T14:10:00Z", "interval": 5}}

    def done(wants):
        return {"ok": True, "data": {"state": "logged_in", "principal": "alice" if wants == "person" else "alice-agents"}}

    class Service:
        def __init__(self, wants, replies):
            self.wants, self.replies = wants, replies

        def post(self, route, body):
            assert route == "/v2/login" and body["for"] == self.wants
            if body["format"] == "text":
                return {"text": PERSON_IN if self.wants == "person" else LOGGED_IN}
            assert "complete" not in body
            return self.replies.pop(0)

    class Listener:
        port = 54321
        calls = []

        def get(self, seconds):
            self.calls.append(seconds)
            clock["t"] += seconds
            return None

    person_old, agent_old = pending("person", "CDFG-MNPQ"), pending("agent", "BDFG-HJKL")
    person_new, agent_new = pending("person", "CDFG-MNPR"), pending("agent", "BDFG-HJKM")
    person = Service("person", [person_old, done("person") if person_completes else person_new,
                                *([] if person_completes else [done("person")])])
    agent = Service("agent", [agent_old, agent_new, done("agent")])
    listener = Listener() if with_listener else None
    result = cli.approve([(person, {"for": "person", "label": "device"}, person_old),
                          (agent, {"for": "agent", "label": "device"}, agent_old)], listener,
                         "wirk login --person")
    out, err = capsys.readouterr()
    port = "&port=54321" if with_listener else ""
    agent_url = f"https://app.wirk.life/device?code=BDFG-HJKM{port}"
    assert result == 0 and not err
    assert out.count("The approval code changed. Use the current link and code below.") == 1
    assert out.count(f"Still waiting for your agents: {agent_url}") == 1
    if person_completes:
        assert "Still waiting for your own sign-in" not in out
    else:
        person_url = f"https://app.wirk.life/device?code=CDFG-MNPR{port}" if with_listener else \
            "https://app.wirk.life/device"
        assert out.count(f"Still waiting for your own sign-in: {person_url}") == 1
        assert "Code CDFG-MNPR" in out
    assert "Code BDFG-HJKM" in out and "CDFG-MNPQ" not in out and "BDFG-HJKL" not in out
    assert out.count(PERSON_IN) == 1 and out.count(LOGGED_IN) == 1
    assert (listener.calls if listener else clock["slept"]) == [5, 5, 5]


def test_person_login_leaves_a_connected_agent_token_alone(fresh, capsys, browser, at_terminal):
    fresh.mkdir(mode=0o700)
    (fresh / "agent-token").write_text("wirk_" + "c" * 43)
    (fresh / "agent-token").chmod(0o600)
    answer, sent = pair()
    sent["agent"] = [{"complete": VALUE}]  # the agents' token is connected already
    code, out, err, requests = login(["--person"], answer, capsys)
    assert browser["urls"][0].startswith("https://app.wirk.life/device?code=CDFG-MNPQ&port=")
    assert code == 0 and out.endswith(PERSON_IN + "\n") and "for your agents" not in out and LOGGED_IN not in out
    assert (fresh / "agent-token").read_text() == "wirk_" + "c" * 43 and len(sent["agent"]) == 2


def test_person_login_without_a_browser_keeps_the_typed_code(fresh, capsys, clock, at_terminal):
    answer, sent = pair()

    def typed(request):
        if sent.get("person"):  # after the first ask, the person typed the code on the page
            sent["person"].append({"complete": VALUE})
        return answer(request)
    code, out, err, requests = login(["--person"], typed, capsys)
    assert code == 0 and out == ("No browser on this machine. Open this link in your browser on any computer and approve:\n"
                                 "https://app.wirk.life/device\nCode CDFG-MNPQ · for you · expires 14:05 UTC\n" + PERSON_IN + "\n")
    assert not (fresh / "agent-token").exists() and "agent" not in sent


def test_a_rerun_resumes_the_same_grant_on_a_new_port(fresh, capsys, browser):
    """The call gave up before the click; running login again answers the same code, and the click (or the
    approved page's Send to this computer's terminal) reaches the new port."""
    answer, state = one_click()
    browser["clicks"] = []
    assert login([], answer, capsys)[0] == 1
    first = (fresh / "agent-token").read_text()
    browser["clicks"] = [VALUE]
    code, out, err, requests = login([], answer, capsys)
    old, new = (int(url.rsplit("=", 1)[1]) for url in browser["urls"])
    assert code == 0 and old != new and "code=BDFG-HJKL" in browser["urls"][1] and "Logged in as alice-agents" in out
    assert (fresh / "agent-token").read_text() == first and state["completes"] == [VALUE]


STALE = "This machine's token stopped after 90 days unused"


@pytest.mark.parametrize("refused", [  # the gate's refusal is text with its hint; the route's, for format json, is data
    refusal("unauthenticated", STALE, status=401, hint="Run: wirk login --new",
            text=f"Error unauthenticated: {STALE}\n  Run: wirk login --new"),
    envelope(ok=False, errors=[{"code": "not_authorized", "message": f"{STALE}; make a new one: wirk login --new"}],
             data={}, status=403),
])
def test_a_stale_token_prints_the_services_words_and_stays(fresh, capsys, refused):
    fresh.mkdir(mode=0o700)
    (fresh / "agent-token").write_text(TOKEN)
    (fresh / "agent-token").chmod(0o600)
    code, out, err, requests = login([], lambda request: refused, capsys)
    problem = refused.json()["errors"][0]
    assert code == 1 and len(requests) == 1 and (fresh / "agent-token").read_text() == TOKEN
    assert out + err == f"Error {problem['code']}: {problem['message']}\n" + (f"  {problem['hint']}\n" if "hint" in problem else "")


def no_runnable_browser(url):
    raise webbrowser.Error("could not locate runnable browser")


@pytest.mark.parametrize("fails", [lambda url: False, no_runnable_browser], ids=["false", "raises"])
def test_a_browser_that_does_not_open_closes_the_listener_before_waiting(fresh, capsys, clock, monkeypatch, fails):
    """open() answering False or raising is the no-browser path: the listener is gone before the first wait."""
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setenv("DISPLAY", ":0")
    tried = []
    monkeypatch.setattr("webbrowser.open", lambda url: tried.append(url) or fails(url))

    def sleep(seconds):
        with pytest.raises(OSError):
            visit(int(tried[0].rsplit("=", 1)[1]), f"/done?c={VALUE}")
    monkeypatch.setattr("time.sleep", sleep)
    code, out, err, requests = login([], device(["pending"]), capsys)
    assert code == 0 and tried and out == NO_BROWSER + LOGGED_IN + "\n"


def test_a_pair_whose_browser_does_not_open_shows_both_links(fresh, capsys, clock, monkeypatch, at_terminal):
    """The agents' token was made for the pair; without the browser each login shows its own link, and both finish."""
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    answer, sent = pair()

    def approved_elsewhere(request):
        for wants in ("person", "agent"):
            if len(sent.get(wants, [])) > 1:  # after its first poll, each is approved on another computer
                sent[wants].append({"complete": VALUE})
        return answer(request)
    code, out, err, requests = login(["--person"], approved_elsewhere, capsys)
    assert code == 0 and out == ("No browser on this machine. Open this link in your browser on any computer and approve:\n"
                                 "https://app.wirk.life/device\nCode CDFG-MNPQ · for you · expires 14:05 UTC\n"
                                 "https://app.wirk.life/device?code=BDFG-HJKL\n"
                                 "Code BDFG-HJKL · for your agents · expires 14:05 UTC\n" + PERSON_IN + "\n" + LOGGED_IN + "\n")


def test_person_login_says_when_the_agents_token_is_refused(fresh, capsys, browser, at_terminal):
    fresh.mkdir(mode=0o700)
    (fresh / "agent-token").write_text(TOKEN)
    (fresh / "agent-token").chmod(0o600)
    answer, sent = pair()
    words = f"{STALE}; make a new one: wirk login --new"

    def stale_agents(request):
        if json.loads(request.content)["for"] == "agent":
            return envelope(ok=False, errors=[{"code": "not_authorized", "message": words}], data={}, status=403)
        return answer(request)
    code, out, err, requests = login(["--person"], stale_agents, capsys)
    assert code == 1 and f"Error not_authorized: {words}\n" in out + err and out.endswith(PERSON_IN + "\n")
    assert "code=BDFG-HJKL" not in browser["urls"][0] and (fresh / "agent-token").read_text() == TOKEN


def test_only_a_service_without_web_sign_in_falls_back_to_the_digest(fresh, capsys):
    words = "label must be one line"
    code, out, err, requests = login([], lambda request: refusal("invalid_input", words, status=422,
                                                                 text=f"Error invalid_input: {words}"), capsys)
    assert code == 1 and [r.url.path for r in requests] == ["/v2/login"] and words in out + err
    assert "digest" not in out + err
