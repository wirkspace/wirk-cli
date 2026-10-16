"""wirk login: the machine makes its own token, binds it to one address, and shows only its digest."""

import hashlib
import http.client
import json
import os
import re
import stat

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
def browser(monkeypatch):
    """A display here, and a browser whose person clicks Connect: the approval brings `clicks` to the listener."""
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


def test_giving_up_before_the_click_closes_the_listener(fresh, capsys, browser, monkeypatch):
    browser["clicks"] = []
    ticks = iter(range(0, 10_000, 30))
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    answer, state = one_click(interval=0.01)
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


def test_person_login_asks_nothing_at_the_terminal(fresh, capsys, browser, at_terminal):
    answer, sent = pair()
    login(["--person"], answer, capsys)  # the browser fixture fails the test on any input()
    assert 'Type "' not in capsys.readouterr().out


def test_a_rerun_resumes_the_same_grant_on_a_new_port(fresh, capsys, browser, monkeypatch):
    """The call gave up before the click; running login again answers the same code, and the click (or the
    approved page's Send to this computer's terminal) reaches the new port."""
    answer, state = one_click(interval=0.01)
    browser["clicks"] = []
    ticks = iter(range(0, 10_000, 30))
    with monkeypatch.context() as patch:
        patch.setattr("time.monotonic", lambda: next(ticks))
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
