"""wirk import: bring a team's tracker into WIRK (docs/plans/importers.md §2).

The dry run reads the source and WIRK and writes nothing to either; it leaves the setup request a person applies with
`wirk admin --request`, the importer's own token and a map template, all in <config>/import/. Every later run writes
as <person>-<source>-import. WIRK's index is the only state, so running again is how a run resumes.
"""

from collections import Counter
import fcntl
import json
import os
import re
import secrets
import sys
from pathlib import Path

from .. import grammar
from ..client import Failure, Service, config_dir, digest, new_token, read_token, saved_url
from ..grammar import UsageError, command
from ..help import HELP
from . import github, jira, linear, render, wirk
from .render import Stop

SOURCES = {"github": github.GitHub, "linear": linear.Linear, "jira": jira.Jira}
DEFAULT_STATUSES = {"open": "open", "in_progress": "in_progress", "completed": "completed", "cancelled": "cancelled"}
ATTENTION = {"skipped", "blocked", "ambiguous", "missing", "error"}
PERSON_STEP = "a person who administers the account applies it, at their own terminal after wirk login --person (an agent cannot)"
CODED = re.compile(r"([a-z][a-z_]*): (.*)", re.S)  # an error outcome's message: WIRK's code, then its words


def main(words: list, options: dict, transport=None) -> int:
    if not words:
        print(HELP["import"])
        return 0
    try:
        return Run(words, options, transport).go()
    except (Stop, wirk.WirkError) as stop:
        if options.get("--json"):
            print(json.dumps({"ok": False, "data": {}, "errors": [problem(stop)]}, ensure_ascii=False))
        tell(stop)
        return 2


def problem(stop: Exception) -> dict:
    return {"code": getattr(stop, "code", None) or "import_stopped", "count": 1, "message": str(stop)}


def errors_of(outcomes: list) -> list:
    """Each kind of error once, with how many issues it hit and its first message."""
    found = {}
    for outcome in outcomes:
        if outcome.outcome == "error":
            coded = CODED.fullmatch(outcome.message)
            code, message = coded.groups() if coded else ("error", outcome.message)
            found.setdefault(code, {"code": code, "count": 0, "message": message})["count"] += 1
    return list(found.values())


def tell(stop: Exception) -> None:
    """Why the run stopped: our Stop, WIRK's refusal or the client's failure, with what helps."""
    code, fix = getattr(stop, "code", None), getattr(stop, "fix", None) or getattr(stop, "hint", None)
    print(f"Error import: {f'{code}: ' if code else ''}{stop}" + (f"\n  {fix}" if fix else ""), file=sys.stderr)


class Run:
    def __init__(self, words: list, options: dict, transport):
        source, rest = words[0], words[1:]
        if source not in SOURCES:
            raise UsageError(f"import takes {', '.join(SOURCES)}")
        selection, pairs = grammar.split(rest)
        if set(pairs) - {"workspace_id", "map"}:
            raise UsageError(f"import takes workspace_id= and map=, not {', '.join(sorted(set(pairs) - {'workspace_id', 'map'}))}")
        self.source, self.adapter, self.selection, self.pairs = source, SOURCES[source](), selection, pairs
        if not selection and self.adapter.needs_selection:
            raise UsageError(f"import {source} needs what to bring: an owner, or owner/repo")
        self.options, self.transport = options, transport
        self.dry_run, self.json = bool(options.get("--dry-run")), bool(options.get("--json"))
        self.folder = config_dir() / "import"
        self.token_file = self.folder / f"wirk-token-{source}-import"
        self.setup_file, self.map_file = self.folder / f"{source}-setup.json", self.folder / f"{source}-map.json"

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
            adapter = self.adapter
            if "cancelled" in mapped:  # Jira's resolutions that mean done work was not done
                adapter.cancelled = mapped["cancelled"]  # checked by the adapter that reads it
            adapter.check()
            census, records = adapter.read(self.selection)
            ctx = adapter.context(census.selected)
            plan = render.plan_fields(records, adapter.selections(records), adapter.source)
            importer = wirk.Importer(wirk.Wirk(service, self.pairs.get("workspace_id")), ctx, users=mapped.get("users", {}),
                                     statuses={**DEFAULT_STATUSES, **mapped.get("status", {})}, plan=plan,
                                     withheld=adapter.withheld(ctx), download=adapter.download, dry_run=self.dry_run,
                                     overwrite=bool(self.options.get("--overwrite")), progress=self.progress)
            you = importer.start(me=self.principal)
            self.check_statuses(importer)
            try:
                outcomes = importer.run(records)
                if self.dry_run and importer.index.blocked:  # the plan is shown, but no setup until a person decides
                    raise Stop("another importer for this source already holds some of these issues: " + ", ".join(
                        f"{name} made {count}" for name, count in importer.index.blocked.items()),
                        "run the import as that principal, or import into another wirkspace (workspace_id=)")
            except (Stop, wirk.WirkError, Failure) as stop:  # a run that stops still says what it did (§2.3)
                self.report(census, importer, importer.outcomes, you, setup=None, stopped=stop)
                tell(stop)
                return 2
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
        if (files := you.get("files")) and not files["ready"]:  # status names it only when unavailable
            raise Stop(f"this WIRK service has no file storage ({files.get('reason')}), and each imported issue keeps "
                       "its raw archive there. Nothing was read from GitHub or written to WIRK, and no setup was made",
                       "ask your WIRK administrator to make file storage available, then run the same command again",
                       "files_unavailable")
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
            fix = (f"{PERSON_STEP}: wirk admin --request {self.setup_file}" if self.setup_file.exists()
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
        if not isinstance(mapped, dict) or set(mapped) - {"users", "names", "field_limit", "status", "cancelled"}:
            raise Stop(f"{path} holds users, names, field_limit, status and cancelled only")
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
            for field in plan.values() if 0 < len(field.options) <= limit and field.key not in importer.vocab]
        operations = fields
        if not registered:
            if not self.token_file.exists():
                new_token(self.token_file)
            token = read_token(self.token_file)
            operations = [{"op": "principal.create", "id": self.principal, "kind": "agent", "name": f"{self.adapter.source} importer",
                           "person_id": self.principal.rsplit(f"-{self.source}-import", 1)[0]},
                          {"op": "member.set", "principal_id": self.principal, "role": "editor"}, *fields,
                          {"op": "token.add", "principal_id": self.principal, "sha256": digest(token), "label": f"{self.source} import"}]
        if not self.map_file.exists():
            users = dict(sorted({user: shown for record in records for user, shown in record.assignees}.items()))
            names = {user: shown for user, shown in users.items() if shown != f"@{user}"}  # IDs say nothing to a person
            cancelled = {"cancelled": sorted(self.adapter.cancelled)} if hasattr(self.adapter, "cancelled") else {}
            self.map_file.write_text(json.dumps({"users": dict.fromkeys(users), **({"names": names} if names else {}),
                                                 "field_limit": 50, "status": DEFAULT_STATUSES, **cancelled}, indent=1) + "\n")
        if not operations:
            return None
        self.setup_file.write_text(json.dumps({"request_id": f"{self.source}-import-setup-{secrets.token_hex(4)}",
                                               "workspace_id": self.workspace, "operations": operations}, indent=1) + "\n")
        return self.setup_file

    # ---------------------------------------------------------------- the report

    def report(self, census, importer, outcomes: list, you: dict, setup: Path | None, stopped=None) -> None:
        counts = Counter(o.outcome for o in outcomes)
        attention = [o for o in outcomes if o.outcome in ATTENTION or o.message]
        limit = self.mapped.get("field_limit", 50)
        large = {plan.key: len(plan.options) for plan in importer.plan.values() if len(plan.options) > limit}
        summary = {"source": self.adapter.source, "principal": self.principal, "dry_run": self.dry_run,
                   "selected": census.selected, "skipped_not_public": census.skipped, "named_not_public": census.named_private,
                   "issues": census.issues, "comments": census.comments, "pull_requests_skipped": census.pulls,
                   "graphql_points": census.points, "outcomes": dict(counts), "text": dict(importer.counts), "header_only": large,
                   "missing_options": {k: sorted(v) for k, v in importer.missing.items()}, "fields_not_set_up": sorted(importer.unset),
                   "owners_not_members": sorted(importer.not_members), "forged_lines_ignored": importer.index.forged,
                   "blocked": dict(importer.index.blocked), "notes": census.notes, "setup": str(setup) if setup else None,
                   "setup_by": PERSON_STEP if setup else None}
        failed = errors_of(outcomes)
        errors = ([problem(stopped)] if stopped else []) + failed
        if self.json:
            print(json.dumps({"ok": not errors, "errors": errors,
                              "data": {"summary": summary, "outcomes": [o.__dict__ for o in outcomes]}}, ensure_ascii=False))
            return
        space = you["wirkspace"]
        lines = [f"{self.adapter.source} {' '.join(self.selection)} → wirkspace {space['name']} ({space['id'].split('_')[-1][:8]}) "
                 f"as {self.principal}" + (" · dry run, nothing written" if self.dry_run else ""),
                 f"selection: {len(census.selected)} {self.adapter.noun[1]} · {census.issues} issues · {census.comments} comments"
                 + (f" · pull requests skipped: {census.pulls}" if census.pulls is not None else "")]
        lines += [f"warning: {name} is {visibility} on {self.adapter.source}: everyone in the wirkspace will read its {count} issues"
                  for name, visibility, count in census.named_private]
        if census.skipped:
            lines += ["skipped, not public: " + ", ".join(name + (f" ({count} issues)" if count is not None else "")
                                                          for name, count in census.skipped)
                      + ". Everyone in the wirkspace would read them. To include them, name them:",
                      "  " + command("wirk", "import", self.source, *(self.selection or census.selected),
                                     *(name for name, _ in census.skipped), "--dry-run")]
        lines.append("items: " + (" · ".join(f"{outcome}: {count}" for outcome, count in sorted(counts.items())) or "none"))
        lines += [f"errors: {error['count']} {error['code']}: {error['message']}" for error in failed]
        fields = [f"header only: {key} ({count} values, over the limit of {limit})" for key, count in large.items()]
        fields += [f"missing options in {key}: {', '.join(sorted(values)[:5])}" for key, values in importer.missing.items()]
        fields += [f"not set up yet: {', '.join(sorted(importer.unset))}"] if importer.unset else []
        if fields:
            lines.append("fields: " + " · ".join(fields))
        if importer.not_members:
            lines.append(f"owners: {', '.join(sorted(importer.not_members))} not in this wirkspace, so their issues have no owner; "
                         f"add them, or change {self.map_file}")
        lines.append(f"links: {len(importer.planned)} planned · {len(importer.notes)} kept as related (the notes say why) · "
                     f"{importer.counts['linked']} written this run")
        lines.append(f"files: attachments stored {importer.counts['attachments']} · left as links {importer.counts['left as links']} · "
                     f"images on other hosts left as links {census.external_images}")
        text = importer.counts
        lines.append(f"text: {text['redacted']} possible credentials redacted · {text['guarded']} [github markers guarded · "
                     f"{text['neutralized']} comment headings neutralized · {text['withheld']} references withheld · "
                     f"{text['addresses']} email addresses typed in text, kept as written")
        lines.append(f"cost: {census.points} {self.adapter.unit} · {self.adapter.source} read and WIRK "
                     f"{'checked' if self.dry_run else 'written'}")
        lines += [f"note: {note}" for note in census.notes]
        if importer.index.forged:
            lines.append(f"ignored: {importer.index.forged} items with a provenance line the importer did not write")
        if setup:
            lines.append(f"setup: review {setup}; {PERSON_STEP}: wirk admin --request {setup}")
        if outcomes:
            first = next((o.key for o in outcomes if o.kind == "issue"), None)
            if first:
                lines.append(f"find one: wirk query text='{self.adapter.source} issue [{first}]'")
        for outcome in attention[:50]:
            lines.append("  " + outcome.line())
        if len(attention) > 50:
            lines.append(f"  {len(attention) - 50} more: add --json for every outcome")
        print("\n".join(lines))

