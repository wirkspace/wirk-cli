"""wirk signup: an agent with no person registers with WorkOS as an anonymous agent, then makes its own unclaimed
Free account for this machine's new token. The service names its sign-in provider; nothing here names one."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import TOKEN, URL, envelope, refusal
from wirk_cli import cli

AUTHKIT = "https://signin.example"  # whatever the service names; the CLI knows no provider address of its own
ASSERTION, CLAIM, ACCESS = "eyJhbGciOi.assertion.sig", "claim-token-never-kept", "eyJhbGciOi.access.sig"
CLAIM_URL = "https://app.wirk.life/claim?c=" + "c" * 32
SIGNED_UP = (f"Logged in as person-1a2b3c4d-agents · agent of Unclaimed · account Unclaimed account\n"
             f"Show your person this link to claim the account: {CLAIM_URL}\n"
             "Deleted on 2026-12-22 unless claimed (after 3 days if nothing is written) · next: status")


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    directory = tmp_path / "wirk"
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(directory))
    monkeypatch.setattr(cli, "machine_name", lambda: "build-box")
    return directory


def world(directory, *, provider=AUTHKIT, metadata=None, identity=None, token=None, status=None, login=None):
    """The service and AuthKit, each answering as given or as it would. The login handler also records which token
    the agent token file held when the service was asked."""
    held = []

    def answer(request):
        where = (f"{request.url.scheme}://{request.url.host}" + (f":{request.url.port}" if request.url.port else ""),
                 request.method, request.url.path)
        if where[1:] == ("GET", "/.well-known/oauth-protected-resource"):
            return metadata(request) if metadata else httpx.Response(
                200, json={"resource": where[0], "authorization_servers": [provider]})
        if where == (provider, "POST", "/agent/identity"):
            return identity(request) if identity else httpx.Response(200, json={
                "identity": {"assertion": ASSERTION}, "claim": {"token": CLAIM},
                "scopes": {"pre_claim": ["wirk:signup"], "post_claim": []}})
        if where == (provider, "POST", "/oauth2/token"):
            return token(request) if token else httpx.Response(200, json={
                "access_token": ACCESS, "token_type": "Bearer", "expires_in": 300})
        if where[1:] == ("POST", "/v2/status"):
            return status(request) if status else refusal("unauthenticated", "Bearer token is invalid", status=401)
        if where[1:] == ("POST", "/v2/login"):
            held.append((directory / "agent-token").read_text() if (directory / "agent-token").exists() else None)
            if login:
                return login(request)
            if json.loads(request.content)["format"] == "json":
                return envelope(data={"state": "logged_in", "principal": "person-1a2b3c4d-agents", "unclaimed": True,
                                      "claim": {"url": CLAIM_URL, "expires_at": "2026-12-22T10:00:00Z"}})
            return envelope(SIGNED_UP)
        raise AssertionError(f"unexpected {request.method} {request.url}")
    return answer, held


def signup(argv, answer, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return answer(request)
    code = cli.main(["signup", *argv], transport=httpx.MockTransport(handler))
    out = capsys.readouterr()
    return code, out.out, out.err, requests


def paths(requests):
    return [(request.method, f"{request.url.scheme}://{request.url.host}{request.url.path}") for request in requests]


def test_signup_saves_token_first_and_prints_claim_link(fresh, capsys):
    answer, held = world(fresh)
    code, out, err, requests = signup([], answer, capsys)
    token = (fresh / "agent-token").read_text()
    assert code == 0 and out == SIGNED_UP + "\n" and err == ""
    assert paths(requests) == [("GET", "https://api.wirk.life/.well-known/oauth-protected-resource"),
                               ("POST", f"{AUTHKIT}/agent/identity"), ("POST", f"{AUTHKIT}/oauth2/token"),
                               ("POST", "https://api.wirk.life/v2/login")]
    discover, identity, exchange, login = requests
    assert json.loads(identity.content) == {"type": "anonymous"}
    assert exchange.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(exchange.content.decode()) == {"grant_type": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
                                                   "assertion": [ASSERTION]}  # no resource: aud stays the client ID
    assert all("authorization" not in r.headers for r in (discover, identity, exchange))  # the WIRK token stays home
    assert json.loads(login.content) == {"for": "signup", "label": "build-box", "format": "text"}
    assert login.headers["authorization"] == f"Bearer {token}" and held == [token]  # saved before the service is asked
    assert login.headers["wirk-agent-registration"] == f"Bearer {ACCESS}"
    assert json.loads((fresh / "config.json").read_text()) == {"service_url": "https://api.wirk.life"}
    kept = "".join(path.read_text() for path in fresh.rglob("*") if path.is_file())
    assert not any(secret in kept + out + err for secret in (ASSERTION, CLAIM, ACCESS))


def test_signup_json_is_the_services_answer(fresh, capsys):
    code, out, err, requests = signup(["--json"], world(fresh)[0], capsys)
    answer = json.loads(out)
    assert code == 0 and answer["data"]["claim"]["url"] == CLAIM_URL and json.loads(requests[-1].content)["format"] == "json"


def test_signup_uses_the_saved_service_and_its_named_provider(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "machine_name", lambda: "build-box")
    (home / "agent-token").unlink()
    code, out, err, requests = signup([], world(home)[0], capsys)
    assert code == 0 and [url for _, url in paths(requests)] == [
        f"{URL}/.well-known/oauth-protected-resource", f"{AUTHKIT}/agent/identity", f"{AUTHKIT}/oauth2/token",
        f"{URL}/v2/login"]


@pytest.mark.parametrize("status", [
    lambda r: envelope(data={"you": {"principal": "alice-agents", "wirkspace": {"id": "wsp_1", "name": "Acme"}}}),
    lambda r: refusal("choose_wirkspace", "Choose a wirkspace", status=409),
], ids=["accepted", "accepted-several-wirkspaces"])
def test_signup_keeps_existing_token(home, capsys, status):
    code, out, err, requests = signup([], world(home, status=status)[0], capsys)
    assert code == 1 and (home / "agent-token").read_text() == TOKEN
    assert "This machine already signs in to WIRK" in err and f"remove {home / 'agent-token'} first" in err
    assert paths(requests) == [("POST", f"{URL}/v2/status")]  # nothing reaches WorkOS


def test_signup_replaces_a_token_the_service_does_not_accept(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "machine_name", lambda: "build-box")
    answer, held = world(home)
    code, out, err, requests = signup([], answer, capsys)
    token = (home / "agent-token").read_text()
    assert code == 0 and token != TOKEN and held == [token] and requests[0].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("metadata", [
    lambda r: httpx.Response(404, json={"detail": "Not Found"}),
    lambda r: httpx.Response(200, json={"resource": "https://api.wirk.life", "authorization_servers": []}),
    lambda r: httpx.Response(200, text="<html>"),
    lambda r: httpx.Response(200, json={"authorization_servers": ["http://signin.example"]}),  # not https
    lambda r: httpx.Response(200, json={"authorization_servers": ["https://signin.example/x?y=1"]}),
    lambda r: httpx.Response(200, json={"authorization_servers": [7]}),
], ids=["404", "empty", "not-json", "http", "path", "not-a-string"])
def test_signup_needs_a_service_that_names_an_https_provider(fresh, capsys, metadata):
    code, out, err, requests = signup([], world(fresh, metadata=metadata)[0], capsys)
    assert code == 1 and "This service does not offer agent sign-up" in err and "wirk login" in err
    assert len(requests) == 1 and not (fresh / "agent-token").exists()


def test_a_loopback_provider_may_use_http(fresh, capsys):
    """A local service and a local provider, as a developer runs them."""
    local = "http://127.0.0.1:8899"
    fresh.mkdir(mode=0o700)
    (fresh / "config.json").write_text(json.dumps({"service_url": "http://127.0.0.1:8765"}))
    code, out, err, requests = signup([], world(fresh, provider=local)[0], capsys)
    assert code == 0 and str(requests[1].url) == f"{local}/agent/identity"


@pytest.mark.parametrize("identity, token", [
    (lambda r: httpx.Response(503, text="down"), None),
    (None, lambda r: httpx.Response(502, text="bad gateway")),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), None),
    (lambda r: httpx.Response(429, json={}), None),
], ids=["identity-503", "token-502", "unreachable", "429"])
def test_workos_unavailable_says_to_run_it_again(fresh, capsys, identity, token):
    code, out, err, requests = signup([], world(fresh, identity=identity, token=token)[0], capsys)
    assert code == 1 and "WorkOS did not answer" in err and "Run the same command again in a minute" in err
    assert not (fresh / "agent-token").exists() and "/v2/login" not in [r.url.path for r in requests]


@pytest.mark.parametrize("identity, token", [
    (lambda r: httpx.Response(403, json={"error": "agent_registration_disabled"}), None),
    (None, lambda r: httpx.Response(400, json={"error": "invalid_grant"})),
    (lambda r: httpx.Response(200, json={"identity": {}}), None),
    (None, lambda r: httpx.Response(200, json={"token_type": "Bearer"})),
], ids=["identity-403", "token-400", "no-assertion", "no-access-token"])
def test_workos_refusals_stop_before_the_service(fresh, capsys, identity, token):
    code, out, err, requests = signup([], world(fresh, identity=identity, token=token)[0], capsys)
    assert code == 1 and "WorkOS did not register this agent" in err
    assert not (fresh / "agent-token").exists() and "/v2/login" not in [r.url.path for r in requests]


def test_the_services_refusal_is_printed(fresh, capsys):
    closed = lambda r: refusal("not_available", "Agent sign-up is not open on this service", status=404,
                               hint="Ask your person to sign up at https://app.wirk.life, then run wirk login")
    code, out, err, requests = signup([], world(fresh, login=closed)[0], capsys)
    assert code == 1 and out == "Error not_available: Agent sign-up is not open on this service\n"


@pytest.mark.parametrize("argv", [["now"], ["--email", "a@example.com"], ["--url", "https://wirk.example.org"]])
def test_signup_takes_no_words_or_options(fresh, capsys, argv):
    code, out, err, requests = signup(argv, world(fresh)[0], capsys)
    assert code == 2 and requests == [] and not fresh.exists()


def test_signup_help(capsys):
    assert cli.main(["signup", "--help"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("usage: wirk signup [--json]") and "claim" in text and "wirk login" in text
    assert cli.main(["--help"]) == 0 and "signup" in capsys.readouterr().out
