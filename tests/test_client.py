"""Transport, errors, output and the token: what reaches the service, what the agent reads, and what never leaks."""

import json
import logging

import httpx
import pytest

from conftest import PERSON_TOKEN, TOKEN, URL, envelope, refusal, write_token
from wirk_cli import cli


def test_text_by_default_and_the_envelope_with_json(run):
    code, out, err, fake = run(["query", "5c1e7a90"], lambda request: envelope("5c1e7a90 · work · open"))
    assert (code, out, err) == (0, "5c1e7a90 · work · open\n", "")
    assert fake.bodies() == [{"fetch": ["5c1e7a90"], "format": "text"}]
    assert fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    code, out, err, fake = run(["query", "5c1e7a90", "--json"], lambda request: envelope(data={"cards": []}))
    assert json.loads(out)["data"] == {"cards": []} and fake.bodies()[0]["format"] == "json"


def test_a_refusal_is_printed_verbatim_and_exits_1(run):
    code, out, err, fake = run(["query", "nope"], lambda r: refusal("no_match", "no readable item is titled \"nope\"",
                                                                    text="Error no_match: no readable item is titled \"nope\""))
    assert (code, out) == (1, "Error no_match: no readable item is titled \"nope\"\n")


def test_a_v2_envelope_is_authoritative_at_any_status(run):
    code, out, err, fake = run(["write", "new", "T"], lambda r: refusal("database_unavailable", "down", status=503,
                                                                        text="Error database_unavailable: down"))
    assert code == 1 and "outcome_unknown" not in out + err and "database_unavailable" in out


def failing(*errors):
    """Raise each transport error in turn, then answer."""
    queue = list(errors)

    def answer(request):
        if queue:
            raise queue.pop(0)
        return envelope("answered")
    return answer


def test_one_identical_resend_when_no_answer_arrives(run):
    code, out, err, fake = run(["write", "new", "T"], failing(httpx.ReadTimeout("slow")))
    assert code == 0 and len(fake.requests) == 2 and fake.requests[0].content == fake.requests[1].content


def test_two_failures_on_a_write_are_an_unknown_outcome_with_the_exact_retry(run):
    code, out, err, fake = run(["write", "new", "T", "--request-id", "w-1"],
                               failing(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")))
    assert code == 1 and len(fake.requests) == 2
    assert f"Error outcome_unknown: {URL} did not answer" in err and "--request-id w-1" in err
    assert "wirk query receipt=w-1" in err


def test_an_unknown_admin_outcome_says_to_run_the_same_command_again(run, home, monkeypatch):
    """wirk admin takes its request ID from the file and has no --request-id; resending the file replays the receipt,
    while query receipt= finds only writes, reviews and uploads."""
    monkeypatch.setattr(cli, "at_terminal", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "admin a-1")
    write_token(home / "person-token", PERSON_TOKEN)
    (home.parent / "batch.json").write_text(json.dumps({"request_id": "a-1", "operations": [
        {"op": "principal.create", "id": "bob", "kind": "person", "name": "Bob"}]}))
    code, out, err, fake = run(["admin", "--request", "batch.json"], failing(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")))
    assert code == 1 and len(fake.requests) == 2 and "Error outcome_unknown" in err
    assert "Run the same command again: wirk admin --request batch.json" in err
    assert "--request-id" not in err and "receipt=" not in err


def test_admin_show_with_a_stray_request_id_fails_cleanly_when_no_answer_comes(run, home, monkeypatch):
    """A show is a read: no outcome is uncertain, whatever words it was given."""
    monkeypatch.setattr(cli, "at_terminal", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "show wirkspace")
    write_token(home / "person-token", PERSON_TOKEN)
    code, out, err, fake = run(["admin", "show", "wirkspace", "request_id=x"],
                               failing(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")))
    assert code == 1 and "Error service_unavailable" in err and "Traceback" not in err


def test_an_unreachable_service_names_the_url_and_suggests_no_other_address(run):
    code, out, err, fake = run(["status"], failing(httpx.ConnectError("refused"), httpx.ConnectError("refused")))
    assert code == 1 and f"Error service_unavailable: could not reach {URL}" in err
    assert f"curl {URL}/health" in err and "--url" not in err


def test_the_json_form_of_a_client_failure_is_an_envelope_on_stdout(run):
    code, out, err, fake = run(["status", "--json"], failing(httpx.ConnectError("refused"), httpx.ConnectError("x")))
    problem = json.loads(out)["errors"][0]
    assert code == 1 and problem["code"] == "service_unavailable" and URL in problem["message"]


@pytest.mark.parametrize("status, location", [(301, "https://elsewhere.test/v2/status"), (302, "https://x.test/"),
                                              (307, "https://x.test/"), (308, "https://x.test/")])
def test_redirects_are_never_followed(run, status, location):
    code, out, err, fake = run(["status"], lambda r: httpx.Response(status, headers={"location": location}))
    assert code == 1 and len(fake.requests) == 1 and location in err and "unexpected_redirect" in err


def test_a_missing_route_is_named(run):
    code, out, err, fake = run(["show", "status", "--no-open"], lambda r: httpx.Response(404, json={"detail": "Not Found"}))
    assert code == 1 and "missing_route" in err and "POST /v2/show" in err


def test_a_page_that_is_not_from_wirk(run):
    code, out, err, fake = run(["status"], lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    assert code == 1 and "service_error" in err and "502" in err
    code, out, err, fake = run(["write", "new", "T", "--request-id", "w-2"],
                               lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    assert "outcome_unknown" in err and "--request-id w-2" in err


def test_not_logged_in(run, home):
    (home / "agent-token").unlink()
    code, out, err, fake = run(["status"])
    assert code == 1 and fake.requests == [] and "Error not_configured" in err and "wirk login" in err


@pytest.mark.parametrize("name", ["agent-token", "person-token"])
def test_unsafe_token_files_are_refused(run, home, tmp_path, name):
    path = home / name
    path.write_text(TOKEN)
    path.chmod(0o644)
    from wirk_cli import client
    with pytest.raises(client.Failure) as refused:
        client.read_token(path)
    assert refused.value.code == "insecure_token_file" and "chmod 600" in refused.value.hint
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_text(TOKEN)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(client.Failure):
        client.read_token(path)


def test_tokens_never_leak(run, home, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "wirk_cli"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    outputs = []
    for argv, answer in [(["status"], None), (["write", "new", "T"], failing(httpx.ReadTimeout("x"), httpx.ReadTimeout("x"))),
                         (["status"], lambda r: httpx.Response(301, headers={"location": "https://x.test"})),
                         (["status"], lambda r: refusal("unauthenticated", "Bearer token is invalid", status=401))]:
        code, out, err, fake = run(argv, answer)
        outputs += [out, err]
    joined = "".join(outputs) + caplog.text
    assert TOKEN not in joined
    for path in home.parent.rglob("*"):
        if path.is_file() and path.name not in ("agent-token", "person-token"):
            assert TOKEN not in path.read_text(errors="replace")


def test_status_sends_the_context_header_and_no_other_route_does(run):
    code, out, err, fake = run(["status"])
    assert fake.requests[0].headers["wirk-context"].startswith("harness=wirk-cli version=")
    code, out, err, fake = run(["query", "5c1e7a90"])
    assert "wirk-context" not in fake.requests[0].headers
