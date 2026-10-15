"""Hosted GitHub import through the existing command and agent token."""

import json
from copy import deepcopy

import httpx
import pytest

from conftest import TOKEN, envelope, refusal
from wirk_cli import cli
from wirk_cli.importers import github

WORKSPACE = {"id": "wsp_" + "1" * 32, "name": "Scratch"}
READY = {
    "mode": "connected", "provider": "github", "request_id": "preview-1", "job_id": "gim_one",
    "state": "ready", "workspace": WORKSPACE,
    "source": {"connection_id": "conn_one", "name": "acme",
               "repositories": [{"id": 12, "full_name": "acme/repo"}]},
    "principal": "alice-github-import", "snapshot_at": "2026-10-15T10:00:25Z",
    "expires_at": "2026-10-15T11:00:00Z", "snapshot_digest": "a" * 64,
    "run_token": "nonce", "summary": {"issues": 2, "comments": 3,
                                    "outcomes": {"would create": 2}, "omissions": ["Projects are omitted"],
                                    "notes": [], "mapping": {}},
    "progress": {}, "receipt": {"request_id": "preview-1", "stored": True},
    "next": "run", "error": None,
}


class Hosted:
    def __init__(self, mode="connected"):
        self.mode = mode
        self.requests = []
        self.raw_requests = []
        self.job = deepcopy(READY)
        self.preview_calls = 0
        self.run_calls = 0
        self.drop_preview = 0
        self.drop_run = 0
        self.pause_once = False

    def __call__(self, request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        self.raw_requests.append((request.url.path, request.content))
        if request.url.path == "/v2/status":
            return envelope(data={"you": {"person": "alice", "principal": "alice-agents",
                                          "wirkspace": WORKSPACE}})
        assert request.url.path == "/v2/import"
        if body["action"] == "status" and "selection" in body:
            if self.mode == "local":
                return envelope(data={"mode": "local", "reason": "no_connection",
                                      "provider": "github", "workspace": WORKSPACE})
            if self.mode == "denied":
                return refusal("delegation_required", "Enable agent imports in your GitHub connection.",
                               status=403)
            if self.mode == "generic404":
                return refusal("not_available", "No matching source", status=404)
            if self.mode == "oldroute":
                return refusal("not_available", "No such route: the service answers /health and /v2/…",
                               status=404)
            if len(body["selection"]) > 10:
                return refusal("unsupported_import_scope", "Too many repositories", status=422)
            return envelope(data={"mode": "connected", "provider": "github",
                                  "connection": {"id": "conn_one", "name": "acme"},
                                  "workspace": WORKSPACE, "delegated": True})
        if body["action"] == "preview":
            self.preview_calls += 1
            if self.drop_preview:
                self.drop_preview -= 1
                raise httpx.ReadTimeout("lost accepted preview response")
            return envelope(data={**self.job, "state": "requested", "snapshot_at": None,
                                  "snapshot_digest": None, "run_token": None, "summary": None,
                                  "receipt": {"request_id": body["request_id"], "stored": True},
                                  "next": "wait"}, status=202)
        if body["action"] == "run":
            self.run_calls += 1
            if self.drop_run:
                self.drop_run -= 1
                raise httpx.ReadTimeout("lost accepted run response")
            if self.pause_once:
                self.pause_once = False
                self.job.update(state="paused", next="resume", run_token="resume-nonce",
                                error={"code": "write_outcome_unknown",
                                       "message": "A write needs receipt reconciliation.",
                                       "hint": "Run the same command."})
                return envelope(data={**self.job, "receipt": {"request_id": body["request_id"], "stored": True}}, status=202)
            self.job.update(state="completed", run_token=None, next="done", request_id=body["request_id"],
                            progress={"issues": 2, "comments": 3, "outcomes": {"created": 2}})
            return envelope(data={**self.job, "receipt": {"request_id": body["request_id"], "stored": True}},
                            status=202)
        return envelope(data={**self.job, "receipt": None})


@pytest.fixture
def hosted(home, capsys, monkeypatch):
    fake = Hosted()
    monkeypatch.setattr(github, "gh", lambda *args: pytest.fail("connected mode called gh"))

    def run(*args):
        code = cli.main(["import", "github", *args], transport=httpx.MockTransport(fake))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return fake, run, home


def test_clean_agent_previews_and_runs_exact_frozen_job(hosted):
    fake, run, home = hosted
    code, output, error = run("acme/repo", "--dry-run", "--json")
    preview = json.loads(output)
    assert code == 0 and "requested" in error and preview["ok"]
    assert preview["data"]["state"] == "ready"
    assert preview["data"]["summary"]["issues"] == 2
    assert fake.preview_calls == 1 and fake.run_calls == 0
    folder = home / "import"
    assert not (folder / "wirk-token-github-import").exists()
    assert not (folder / "github-setup.json").exists()
    assert not (folder / "github-map.json").exists()
    code, output, error = run("acme/repo", "--json")
    completed = json.loads(output)
    assert code == 0 and completed["data"]["state"] == "completed"
    assert completed["data"]["summary"]["outcomes"] == {"would create": 2}
    assert completed["data"]["progress"]["outcomes"] == {"created": 2}
    assert fake.preview_calls == 1 and fake.run_calls == 1
    sent = [body for route, body in fake.requests if route == "/v2/import"]
    assert sent[-1]["action"] == "run"
    assert sent[-1]["job_id"] == "gim_one"
    assert sent[-1]["snapshot_digest"] == "a" * 64
    assert sent[-1]["run_token"] == "nonce"


def test_actual_requires_identical_ready_preview_without_creating_one(hosted):
    fake, run, _ = hosted
    code, output, error = run("acme/repo", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "preview_required"
    assert "wirk import github acme/repo --dry-run" in error
    assert fake.preview_calls == fake.run_calls == 0


@pytest.mark.parametrize("mode", ["denied", "generic404"])
def test_denial_and_generic_404_never_fall_back_to_local(hosted, mode):
    fake, run, home = hosted
    fake.mode = mode
    code, output, error = run("acme/repo", "--dry-run", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] in {"delegation_required", "not_available"}
    assert not (home / "import" / "wirk-token-github-import").exists()
    assert fake.preview_calls == 0


def test_connected_limit_applies_after_discovery(hosted):
    fake, run, _ = hosted
    choices = tuple(f"acme/repo-{n}" for n in range(11))
    code, output, _ = run(*choices, "--dry-run", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "unsupported_import_scope"
    assert len([b for _, b in fake.requests if b.get("action") == "status" and "selection" in b]) == 1
    assert fake.preview_calls == 0


def test_connected_refuses_custom_map_and_overwrite_before_preview(hosted):
    fake, run, home = hosted
    (home / "import").mkdir(mode=0o700)
    (home / "import" / "github-map.json").write_text('{"status":{"open":"todo"}}')
    for option in ("--overwrite", "map=custom.json", None):
        args = ["acme/repo", "--dry-run", "--json"]
        if option:
            args.append(option)
        code, output, _ = run(*args)
        assert code == 2 and json.loads(output)["errors"][0]["code"] == "unsupported_import_option"
    assert fake.preview_calls == 0


def test_failed_and_paused_jobs_are_nonzero_and_keep_next_action(hosted):
    fake, run, _ = hosted
    fake.job.update(state="failed", snapshot_at=None, snapshot_digest=None, run_token=None,
                    summary=None, next="preview", error={"code": "import_limit",
                    "message": "Selection exceeds the limit.", "hint": "Choose fewer repositories."})
    code, output, _ = run("acme/repo", "--dry-run", "--json")
    assert code == 2 and json.loads(output)["data"]["next"] == "preview"
    assert fake.run_calls == 0


def test_unknown_preview_reuses_identical_request_id_and_body(hosted):
    fake, run, home = hosted
    fake.drop_preview = 2
    code, output, error = run("acme/repo", "--dry-run", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "outcome_unknown"
    assert "same command" in error
    code, output, _ = run("acme/repo", "--dry-run", "--json")
    assert code == 0 and json.loads(output)["data"]["state"] == "ready"
    previews = [body for _, body in fake.requests if body.get("action") == "preview"]
    assert len(previews) == 3 and previews[0] == previews[1] == previews[2]
    raw = [content for route, content in fake.raw_requests if route == "/v2/import"
           and json.loads(content).get("action") == "preview"]
    assert raw[0] == raw[1] == raw[2]
    files = list((home / "import").glob("github-connected-*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0


def test_unknown_run_reuses_exact_request_and_does_not_make_new_preview(hosted):
    fake, run, _ = hosted
    assert run("acme/repo", "--dry-run")[0] == 0
    fake.drop_run = 2
    code, output, _ = run("acme/repo", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "outcome_unknown"
    code, output, _ = run("acme/repo", "--json")
    assert code == 0 and json.loads(output)["data"]["state"] == "completed"
    runs = [body for _, body in fake.requests if body.get("action") == "run"]
    assert len(runs) == 3 and runs[0] == runs[1] == runs[2]
    raw = [content for route, content in fake.raw_requests if route == "/v2/import"
           and json.loads(content).get("action") == "run"]
    assert raw[0] == raw[1] == raw[2]
    assert fake.preview_calls == 1


def test_paused_resume_is_explicit_and_uses_new_nonce_and_request(hosted):
    fake, run, _ = hosted
    assert run("acme/repo", "--dry-run")[0] == 0
    fake.pause_once = True
    code, output, _ = run("acme/repo", "--json")
    assert code == 2 and json.loads(output)["data"]["next"] == "resume"
    assert fake.run_calls == 1
    code, output, _ = run("acme/repo", "--json")
    assert code == 0 and json.loads(output)["data"]["state"] == "completed"
    runs = [body for _, body in fake.requests if body.get("action") == "run"]
    assert len(runs) == 2 and runs[0]["request_id"] != runs[1]["request_id"]
    assert runs[1]["run_token"] == "resume-nonce"


def test_unseen_pending_preview_cannot_be_confirmed(hosted):
    fake, run, home = hosted
    fake.drop_preview = 2
    assert run("acme/repo", "--dry-run", "--json")[0] == 2
    code, output, _ = run("acme/repo", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "preview_required"
    assert fake.run_calls == 0


def test_status_poll_does_not_start_execution_or_refresh_source(hosted, monkeypatch):
    from wirk_cli.importers import connected
    fake, run, _ = hosted
    fake.job["state"] = "requested"
    monkeypatch.setattr(connected, "FOREGROUND_SECONDS", 0)
    code, output, error = run("acme/repo", "--dry-run", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "import_pending"
    assert "same command" in error
    actions = [body["action"] for route, body in fake.requests if route == "/v2/import"]
    assert actions == ["status", "preview"]
    assert fake.run_calls == 0


@pytest.mark.parametrize("selection", [("acme",), ("acme", "acme/repo")])
def test_connected_owner_or_mixed_scope_refuses_before_preview(hosted, selection):
    fake, run, _ = hosted
    code, output, _ = run(*selection, "--dry-run", "--json")
    assert code == 2 and json.loads(output)["errors"][0]["code"] == "unsupported_import_scope"
    assert fake.preview_calls == 0
