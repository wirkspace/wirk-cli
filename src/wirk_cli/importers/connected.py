"""Account-connected GitHub imports through the existing import command."""

import hashlib
import json
import os
import secrets
import stat
import sys
import time

from ..client import Failure, Service, digest, read_token, saved_url
from ..grammar import command
from .render import Stop
from .wirk import WirkError

ROUTE = "/v2/import"
OLD_ROUTE = "No such route: the service answers /health and /v2/…"
ACTIVE = {"requested", "previewing", "queued", "running"}
WAIT_SECONDS = 2
FOREGROUND_SECONDS = 20 * 60


def dispatch(run):
    """Return None only when discovery expressly selects the old local importer."""
    if run.source != "github":
        return None
    folder = run.folder
    url = saved_url(folder.parent)
    if url is None:
        raise Stop("WIRK is not set up on this machine", "wirk login")
    token = read_token(folder.parent / "agent-token")
    service = Service(url, token, run.transport)
    status_body = {"format": "json", **({"workspace_id": run.pairs["workspace_id"]}
                                          if "workspace_id" in run.pairs else {})}
    status = service.post("/v2/status", status_body)
    if not status["ok"]:
        error = status["errors"][0]
        raise Stop(error["message"], error.get("hint", ""))
    run.preflight_status = status
    you = status["data"]["you"]
    workspace = you["wirkspace"]["id"]
    discovery = service.post(ROUTE, {"action": "status", "provider": "github",
                                     "workspace_id": workspace, "selection": run.selection, "format": "json"})
    if not discovery["ok"]:
        error = discovery["errors"][0]
        if error.get("code") == "not_available" and error.get("message") == OLD_ROUTE:
            return None
        raise WirkError(error)
    mode = discovery["data"].get("mode")
    if (mode == "local" and discovery["data"].get("reason") == "no_connection"
            and discovery["data"].get("provider") == "github"
            and discovery["data"].get("workspace", {}).get("id") == workspace):
        print("GitHub import: local source mode", file=sys.stderr)
        return None
    if mode != "connected" or discovery["data"].get("delegated") is not True:
        raise Stop("the import service did not identify a usable source mode",
                   "Check the GitHub connection in WIRK.", "import_mode_unknown")
    connection = discovery["data"]["connection"]["id"]
    hosted = Hosted(run, service, url, token, you, workspace, connection)
    return hosted.go()


class Hosted:
    def __init__(self, run, service, url, token, you, workspace, connection):
        self.run, self.service = run, service
        self.workspace = workspace
        scope = [url, digest(token), you.get("person"), you.get("principal"), workspace, connection,
                 [name.casefold() for name in run.selection]]
        self.scope = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        self.path = run.folder / f"github-connected-{self.scope[:24]}.json"
        words = ["wirk", "import", "github", *run.selection]
        if "workspace_id" in run.pairs:
            words.append("workspace_id=" + run.pairs["workspace_id"])
        self.run_command = command(*words)
        self.dry_command = command(*words, "--dry-run")
        self.retry = self.dry_command if run.dry_run else self.run_command

    def go(self):
        self.check_options()
        record = self.load()
        if self.run.dry_run:
            return self.preview(record)
        return self.execute(record)

    def check_options(self):
        if "map" in self.run.pairs or self.run.options.get("--overwrite"):
            raise Stop("connected GitHub imports do not support map= or --overwrite",
                       "Use the website's connected import preview with its standard mapping.",
                       "unsupported_import_option")
        if any("/" not in name for name in self.run.selection):
            raise Stop("connected GitHub imports need explicit OWNER/REPO names",
                       "Name each repository, then preview again.", "unsupported_import_scope")
        if len(self.run.selection) > 10:
            raise Stop("connected GitHub imports support at most ten repositories at once",
                       "Preview a smaller selection.", "unsupported_import_scope")
        path = self.run.map_file
        if path.exists():
            try:
                mapped = json.loads(path.read_text())
            except ValueError:
                raise Stop(f"{path} is not JSON", "Fix or remove this map before importing.",
                           "unsupported_import_option") from None
            defaults = {"status": {"open": "open", "in_progress": "in_progress",
                                   "completed": "completed", "cancelled": "cancelled"},
                        "field_limit": 50}
            if (not isinstance(mapped, dict) or set(mapped) - {"users", "names", "status", "field_limit", "cancelled"}
                    or not isinstance(mapped.get("users", {}), dict)
                    or not isinstance(mapped.get("names", {}), dict)
                    or any(value is not None for value in mapped.get("users", {}).values())
                    or mapped.get("status", defaults["status"]) != defaults["status"]
                    or mapped.get("field_limit", 50) != 50 or mapped.get("cancelled")):
                raise Stop("the saved GitHub map has custom settings that connected imports cannot apply",
                           "Use the standard connected preview, or use a local source with this map.",
                           "unsupported_import_option")

    def unusable_cache(self, message):
        return Stop(message, f"Move or rename {self.path} yourself and keep it for reconciling any pending request; "
                    f"then run: {self.dry_command}", "preview_required")

    def load(self):
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise self.unusable_cache("the saved connected import reference is not a private regular file")
            record = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            raise self.unusable_cache("the saved connected import reference is damaged") from None
        if not isinstance(record, dict) or record.get("scope") != self.scope:
            raise self.unusable_cache(
                "the saved connected import reference does not match this source and destination")
        return record

    def save(self, record):
        """Keep only job metadata, never source bytes or a credential."""
        body = json.dumps(record, separators=(",", ":")).encode()
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def request(self, body):
        """The stored body is the only retry basis after an uncertain write."""
        try:
            answer = self.service.post(ROUTE, body, uncertain=body["request_id"])
        except Failure as failure:
            if failure.code == "outcome_unknown":
                raise Stop("WIRK did not answer whether it accepted this import request",
                           f"Run the same command again: {self.retry}", "outcome_unknown") from None
            raise
        if not answer["ok"]:
            raise WirkError(answer["errors"][0])
        return answer

    def status(self, record):
        body = {"action": "status", "workspace_id": self.workspace, "job_id": record["job_id"], "format": "json"}
        answer = self.service.post(ROUTE, body)
        if not answer["ok"]:
            raise WirkError(answer["errors"][0])
        return answer

    def preview(self, record):
        if record and record.get("preview") and record.get("state") in {"requested", "previewing"}:
            body = record["preview"]
        else:
            body = {"action": "preview", "request_id": "gimp-" + secrets.token_hex(12),
                    "provider": "github", "workspace_id": self.workspace,
                    "repositories": self.run.selection, "format": "json"}
            record = {"scope": self.scope, "preview": body, "state": "requested"}
            self.save(record)
        answer = self.request(body)
        record["job_id"] = answer["data"]["job_id"]
        record["state"] = answer["data"]["state"]
        self.save(record)
        return self.poll(record, answer)

    def execute(self, record):
        if not record or not record.get("job_id") or record.get("state") not in {
                "ready", "queued", "running", "completed", "paused"}:
            raise Stop("a connected import needs a matching ready preview",
                       f"Run: {self.dry_command}", "preview_required")
        answer = self.status(record)
        data = answer["data"]
        expected = sorted(name.casefold() for name in self.run.selection)
        actual = sorted(repo["full_name"].casefold() for repo in data["source"]["repositories"])
        if (data["job_id"] != record["job_id"] or data["workspace"]["id"] != self.workspace
                or actual != expected):
            raise Stop("the saved preview no longer matches this source and destination",
                       f"Run: {self.dry_command}", "basis_changed")
        record["state"] = data["state"]
        self.save(record)
        if data["state"] == "completed":
            return self.show(answer)
        if data["state"] in {"queued", "running"}:
            return self.poll(record, answer)
        if data["state"] == "paused" and data.get("next") == "resolve":
            return self.show(answer)
        if data["state"] not in {"ready", "paused"} or data.get("next") not in {"run", "resume"}:
            raise Stop("this connected preview is no longer ready to import",
                       f"Run: {self.dry_command}", "preview_required")
        if not data.get("snapshot_digest") or not data.get("run_token"):
            raise Stop("this connected preview has no valid run basis",
                       f"Run: {self.dry_command}", "preview_required")
        prior = record.get("run")
        if (prior and record.get("run_state") == "uncertain"
                and prior["snapshot_digest"] == data["snapshot_digest"]
                and prior["run_token"] == data["run_token"]):
            body = prior
        else:
            body = {"action": "run", "request_id": "gimr-" + secrets.token_hex(12),
                    "workspace_id": self.workspace, "job_id": record["job_id"],
                    "snapshot_digest": data["snapshot_digest"], "run_token": data["run_token"], "format": "json"}
            record["run"] = body
            record["run_state"] = "uncertain"
            self.save(record)
        answer = self.request(body)
        record["run_state"] = "accepted"
        record["state"] = answer["data"]["state"]
        self.save(record)
        return self.poll(record, answer)

    def poll(self, record, answer):
        deadline = time.monotonic() + FOREGROUND_SECONDS
        last_state = None
        while answer["data"]["state"] in ACTIVE:
            state = answer["data"]["state"]
            if state != last_state:
                print(f"GitHub import {record['job_id']}: {state}", file=sys.stderr, flush=True)
                last_state = state
            if time.monotonic() >= deadline:
                raise Stop(f"connected GitHub import {record['job_id']} is still {state}",
                           f"Run the same command to continue: {self.retry}", "import_pending")
            time.sleep(WAIT_SECONDS)
            answer = self.status(record)
            record["state"] = answer["data"]["state"]
            self.save(record)
        return self.show(answer)

    def show(self, answer):
        data = answer["data"]
        if self.run.json:
            print(json.dumps(answer, ensure_ascii=False))
        else:
            summary = data.get("summary") or {}
            progress = data.get("progress") or {}
            source = ", ".join(repo["full_name"] for repo in data["source"]["repositories"])
            lines = [f"GitHub {source} → {data['workspace']['name']} as {data['principal']} · connected source"]
            if data["state"] == "ready":
                lines.append(f"Preview ready: {summary.get('issues', 0)} issues, {summary.get('comments', 0)} comments. No items imported.")
                lines.append(f"Snapshot {data.get('snapshot_at') or 'time unavailable'}; expires {data['expires_at']}.")
                lines.append(f"Run {self.run_command} to import exactly this snapshot.")
            elif data["state"] == "completed":
                outcomes = progress.get("outcomes") or {}
                lines.append(f"Import completed · {data['job_id']} · snapshot {data.get('snapshot_at') or 'time unavailable'}")
                lines.append("Actual: " + (", ".join(f"{name} {count}" for name, count in outcomes.items()) or "no counted items"))
                lines.append(f"Run {self.dry_command} for a new snapshot.")
            else:
                lines.append(f"Import {data['state']} · {data['job_id']} · next: {data['next']}")
            if summary.get("omissions"):
                lines.append("Omissions: " + "; ".join(summary["omissions"]))
            if data.get("error"):
                error = data["error"]
                lines.append(f"{error['code']}: {error['message']}")
                if error.get("hint"):
                    lines.append("  " + error["hint"])
            print("\n".join(lines))
        return 2 if data["state"] in {"failed", "paused", "expired"} else 0
