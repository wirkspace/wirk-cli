"""wirk import: bring a team's tracker into WIRK (docs/plans/importers.md §2).

The dry run reads the source and WIRK and writes nothing to either; it leaves the setup request a person applies with
`wirk admin --request`, the importer's own token and a map template, all in <config>/import/. Every later run writes
as <person>-<source>-import. WIRK's index is the only state, so running again is how a run resumes.
"""

from collections import Counter
import fcntl
import json
import os
import secrets
import sys
from pathlib import Path

from .. import grammar
from ..client import Failure, Service, config_dir, digest, new_token, read_token, saved_url
from ..grammar import UsageError, command
from ..help import HELP
from . import github, render, wirk

SOURCES = {"github": github}
DEFAULT_STATUSES = {"open": "open", "in_progress": "in_progress", "completed": "completed", "cancelled": "cancelled"}
ATTENTION = {"skipped", "blocked", "ambiguous", "missing", "error", "would archive", "archived"}


class Stop(Exception):
    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.fix = fix


def main(words: list, options: dict, transport=None) -> int:
    if not words:
        print(HELP["import"])
        return 0
    try:
        return Run(words, options, transport).go()
    except (Stop, github.Stop) as stop:
        print(f"Error import: {stop}" + (f"\n  {stop.fix}" if stop.fix else ""), file=sys.stderr)
        return 2


class Run:
    def __init__(self, words: list, options: dict, transport):
        source, rest = words[0], words[1:]
        if source not in SOURCES:
            raise UsageError(f"import takes {', '.join(SOURCES)}; Linear and Jira come later")
        selection, pairs = grammar.split(rest)
        if set(pairs) - {"workspace_id", "map"}:
            raise UsageError(f"import takes workspace_id= and map=, not {', '.join(sorted(set(pairs) - {'workspace_id', 'map'}))}")
        if not selection:
            raise UsageError(f"import {source} needs what to bring: an owner, or owner/repo")
        self.source, self.module, self.selection, self.pairs = source, SOURCES[source], selection, pairs
        self.options, self.transport = options, transport
        self.dry_run, self.json = bool(options.get("--dry-run")), bool(options.get("--json"))
        self.folder = config_dir() / "import"
        self.token_file = self.folder / f"wirk-token-{source}-import"
        self.setup_file, self.map_file = self.folder / f"{source}-setup.json", self.folder / f"{source}-map.json"
        self.again = command("wirk", "import", source, *rest, *(["--dry-run"] if self.dry_run else []))

    # ---------------------------------------------------------------- the run

    def go(self) -> int:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.folder / f"{self.source}.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Stop(f"another wirk import {self.source} is running on this machine; wait for it to finish") from None
            service, registered = self.connect()
            self.mapped = mapped = self.read_map()
            adapter = self.module.GitHub()
            adapter.check()
            census, records = adapter.read(self.selection)
            ctx = adapter.context(census.selected)
            plan = render.plan_fields(records, adapter.selections(records), mapped.get("field_limit", 50), adapter.source)
            importer = wirk.Importer(wirk.Wirk(service, self.pairs.get("workspace_id")), ctx, users=mapped.get("users", {}),
                                     statuses={**DEFAULT_STATUSES, **mapped.get("status", {})}, plan=plan,
                                     withheld=adapter.withheld(ctx), download=adapter.download, dry_run=self.dry_run,
                                     overwrite=bool(self.options.get("--overwrite")), progress=self.progress)
            you = importer.start(me=self.principal)
            self.check_statuses(importer)
            outcomes = importer.run(records)
            if self.dry_run and importer.index.blocked:  # the plan is shown, but no setup until a person decides
                self.report(census, importer, outcomes, you, setup=None)
                raise Stop("another importer for this source already holds some of these issues: " + ", ".join(
                    f"{name} made {count}" for name, count in importer.index.blocked.items()),
                    "run the import as that principal, or import into another wirkspace (workspace_id=)")
            setup = self.write_setup(importer, plan, records, registered) if self.dry_run else None
            self.report(census, importer, outcomes, you, setup)
            return 1 if any(o.outcome == "error" for o in outcomes) else 0

    def connect(self) -> tuple[Service, bool]:
        """The importer's own token once a person registered it; until then the dry run reads with the agent's."""
        directory = config_dir()
        url = saved_url(directory)
        if url is None:
            raise Stop("WIRK is not set up on this machine", "wirk login")
        agent = Service(url, read_token(directory / "agent-token"), self.transport)
        status = agent.post("/v2/status", self.scoped({"format": "json", "max_bytes": 1024}))
        if not status["ok"]:
            raise Stop(status["errors"][0]["message"], status["errors"][0].get("hint", ""))
        you = status["data"]["you"]
        person = you.get("person") or you["principal"]
        self.principal = f"{person}-{self.source}-import"
        if len(self.principal) > 40:
            raise Stop(f"{self.principal} is longer than WIRK's 40 characters for a principal",
                       "ask your administrator for a shorter person ID")
        self.workspace = you["wirkspace"]["id"]
        if self.token_file.exists():
            mine = Service(url, read_token(self.token_file), self.transport)
            answer = mine.post("/v2/status", self.scoped({"format": "json", "max_bytes": 1024}))
            if answer["ok"]:
                return mine, True
        if not self.dry_run:
            fix = (f"a person applies the setup: wirk admin --request {self.setup_file}" if self.setup_file.exists()
                   else command("wirk", "import", self.source, *self.selection, "--dry-run"))
            raise Stop(f"the {self.source} importer is not set up for this wirkspace yet", fix)
        return agent, False

    def scoped(self, body: dict) -> dict:
        return {**body, **({"workspace_id": self.pairs["workspace_id"]} if "workspace_id" in self.pairs else {})}

    def read_map(self) -> dict:
        path = Path(self.pairs["map"]) if "map" in self.pairs else self.map_file
        if not path.exists():
            if "map" in self.pairs:
                raise Stop(f"no map file at {path}")
            return {}
        try:
            mapped = json.loads(path.read_text())
        except ValueError as error:
            raise Stop(f"{path} is not JSON: {error}") from None
        if not isinstance(mapped, dict) or set(mapped) - {"users", "field_limit", "status"}:
            raise Stop(f"{path} holds users, field_limit and status only")
        return mapped

    def check_statuses(self, importer) -> None:
        wanted = {**DEFAULT_STATUSES, **self.mapped.get("status", {})}
        missing = sorted(set(wanted.values()) - set(importer.vocab.get("status", [])))
        if missing:
            raise Stop(f"this wirkspace's status has no {', '.join(missing)}; its options are "
                       f"{', '.join(importer.vocab.get('status', []))}",
                       f'map each meaning to one in {self.map_file}: {{"status": {{"open": "…", "completed": "…"}}}}')

    def progress(self, done: int, total: int) -> None:
        if done % 100 == 0:
            print(f"{self.source}: {done} of {total} issues", file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- what the dry run leaves for the person

    def write_setup(self, importer, plan: dict, records: list, registered: bool) -> Path | None:
        limit = self.mapped.get("field_limit", 50)
        fields = [{"op": "field.create", "field": {
            "key": field.key, "name": field.name, "applies_to": "item", "selection": field.selection, "required": False,
            "options": [{"key": key, "name": name, "order": order} for order, (name, key) in enumerate(field.options.items(), 1)]}}
            for field in plan.values() if render.small(field, limit) and field.key not in importer.vocab]
        operations = fields
        if not registered:
            if not self.token_file.exists():
                new_token(self.token_file)
            token = read_token(self.token_file)
            operations = [{"op": "principal.create", "id": self.principal, "kind": "agent", "name": f"{self.module.SOURCE} importer",
                           "person_id": self.principal.rsplit(f"-{self.source}-import", 1)[0]},
                          {"op": "member.set", "principal_id": self.principal, "role": "editor"}, *fields,
                          {"op": "token.add", "principal_id": self.principal, "sha256": digest(token), "label": f"{self.source} import"}]
        if not self.map_file.exists():
            users = sorted({user for record in records for user, _ in record.assignees})
            self.map_file.write_text(json.dumps({"users": dict.fromkeys(users), "field_limit": 50, "status": DEFAULT_STATUSES},
                                                indent=1) + "\n")
        if not operations:
            return None
        self.setup_file.write_text(json.dumps({"request_id": f"{self.source}-import-setup-{secrets.token_hex(4)}",
                                               "workspace_id": self.workspace, "operations": operations}, indent=1) + "\n")
        return self.setup_file

    # ---------------------------------------------------------------- the report

    def report(self, census, importer, outcomes: list, you: dict, setup: Path | None) -> None:
        counts = Counter(o.outcome for o in outcomes)
        attention = [o for o in outcomes if o.outcome in ATTENTION or o.message]
        limit = self.mapped.get("field_limit", 50)
        large = {plan.key: len(plan.options) for plan in importer.plan.values() if len(plan.options) > limit}
        summary = {"source": self.module.SOURCE, "principal": self.principal, "dry_run": self.dry_run, "selected": census.selected,
                   "skipped_not_public": census.skipped, "issues": census.issues, "comments": census.comments,
                   "pull_requests_skipped": census.pulls, "outcomes": dict(counts), "text": dict(importer.counts),
                   "header_only": large, "missing_options": {k: sorted(v) for k, v in importer.missing.items()},
                   "fields_not_set_up": sorted(importer.unset), "forged_lines_ignored": importer.index.forged,
                   "blocked": dict(importer.index.blocked), "notes": census.notes, "setup": str(setup) if setup else None}
        if self.json:
            print(json.dumps({"ok": not counts["error"], "data": {"summary": summary, "outcomes": [o.__dict__ for o in outcomes]},
                              "errors": []}, ensure_ascii=False))
            return
        space = you["wirkspace"]
        lines = [f"{self.module.SOURCE} {' '.join(self.selection)} → wirkspace {space['name']} ({space['id'].split('_')[-1][:8]}) "
                 f"as {self.principal}" + (" · dry run, nothing written" if self.dry_run else ""),
                 f"selection: {len(census.selected)} repositories · {census.issues} issues · {census.comments} comments · "
                 f"pull requests skipped: {census.pulls}"]
        if census.skipped:
            lines += [f"skipped, not public: " + ", ".join(f"{name} ({count} issues)" for name, count in census.skipped)
                      + ". Everyone in the wirkspace would read them. To include them, name them:",
                      "  " + command("wirk", "import", self.source, *self.selection, *(name for name, _ in census.skipped), "--dry-run")]
        lines.append("items: " + (" · ".join(f"{outcome}: {count}" for outcome, count in sorted(counts.items())) or "none"))
        fields = [f"header only: {key} ({count} values, over the limit of {limit})" for key, count in large.items()]
        fields += [f"missing options in {key}: {', '.join(sorted(values)[:5])}" for key, values in importer.missing.items()]
        fields += [f"not set up yet: {', '.join(sorted(importer.unset))}"] if importer.unset else []
        if fields:
            lines.append("fields: " + " · ".join(fields))
        planned = getattr(importer, "planned", set())
        lines.append(f"links: {len(planned)} planned · {len(importer.notes)} kept as related (the notes say why) · "
                     f"{importer.counts['linked']} written this run")
        lines.append(f"files: attachments stored {importer.counts['attachments']} · left as links {importer.counts['left as links']} · "
                     f"images on other hosts left as links {census.external_images}")
        text = importer.counts
        lines.append(f"text: {text['redacted']} possible credentials redacted · {text['guarded']} [github markers guarded · "
                     f"{text['neutralized']} comment headings neutralized · {text['withheld']} references withheld")
        lines += [f"note: {note}" for note in census.notes]
        if importer.index.forged:
            lines.append(f"ignored: {importer.index.forged} items with a provenance line the importer did not write")
        if setup:
            lines.append(f"setup: a person who administers the account reviews {setup} and runs: "
                         f"wirk admin --request {setup}")
        if outcomes:
            first = next((o.key for o in outcomes if o.kind == "issue"), None)
            if first:
                lines.append(f"find one: wirk query text='{self.module.SOURCE} issue [{first}]'")
        for outcome in attention[:50]:
            lines.append("  " + outcome.line())
        if len(attention) > 50:
            lines.append(f"  {len(attention) - 50} more: add --json for every outcome")
        print("\n".join(lines))

