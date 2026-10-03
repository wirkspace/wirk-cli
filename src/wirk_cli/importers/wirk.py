"""The importer against WIRK (docs/plans/importers.md §3.4–§3.7): the index of earlier imports and its trust rules, the
decision for each object, one issue per write, refusals, receipts and files. WIRK's index is the only state."""

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import io
import re
import secrets

from ..client import Failure
from . import render

PAGE = {"limit": 100, "max_bytes": 65536}
UNSETTLED = {"outcome_unknown", "storage_failed", "storage_mismatch"}  # failures of one issue's write or file
KEYED = re.compile(r"\[((?:\\.|[^\]\\])*)\]")  # a key in brackets, a ] inside it escaped


class WirkError(Exception):
    def __init__(self, problem: dict):
        super().__init__(problem.get("message", ""))
        self.code, self.problem = problem.get("code", "error"), problem


class NotMember(Exception):
    """WIRK refused an owner (the exception's argument): the issue is planned again without them."""


@dataclass
class Outcome:
    key: str
    kind: str
    outcome: str
    message: str = ""
    item: str | None = None

    def line(self) -> str:
        return " ".join(filter(None, [self.key, "" if self.kind == "issue" else self.kind, self.outcome
                                      + (":" if self.message else ""), self.message, f"({self.item[5:13]})" if self.item else ""]))


@dataclass
class Held:
    item: str
    r: int
    by: str
    archived: bool
    hash: str


class Wirk:
    """The four routes the importer uses, through the CLI's own connection."""

    def __init__(self, service, workspace: str | None = None):
        self.service, self.workspace = service, workspace

    def post(self, route: str, body: dict, uncertain: str | None = None) -> dict:
        body = {**body, "format": "json", **({"workspace_id": self.workspace} if self.workspace else {})}
        return self.service.post(route, body, uncertain=uncertain)

    def ok(self, route: str, body: dict) -> dict:
        """The answer, or WIRK's refusal as a WirkError."""
        answer = self.post(route, body)
        if not answer["ok"]:
            raise WirkError(answer["errors"][0])
        return answer

    def status(self) -> dict:
        return self.ok("/v2/status", {"max_bytes": 65536})["data"]

    def cards(self, fields: dict):
        body = {"fields": fields, **PAGE}
        while True:
            answer = self.ok("/v2/query", body)
            yield from answer["data"].get("cards", [])
            if answer["page"].get("complete", True) or not answer["page"].get("next_cursor"):
                return
            body = {**body, "cursor": answer["page"]["next_cursor"]}

    def item(self, ref, depth: str = "full") -> dict:
        """One item, its body read whole through the continuation."""
        body = {"fetch": [ref], "depth": depth, "max_bytes": 65536}
        answer = self.ok("/v2/query", body)
        view = answer["data"]["results"][0]
        text = view.get("body", "")
        while not view.get("body_complete", True) and answer["page"].get("next_cursor"):
            answer = self.ok("/v2/query", {**body, "cursor": answer["page"]["next_cursor"]})
            view = answer["data"]["results"][0]
            text += view.get("body", "")
        return {**view, "body": text}

    def write(self, body: dict) -> dict:
        """An envelope; a write whose outcome stays unknown is settled by its receipt."""
        try:
            return self.post("/v2/write", body, uncertain=body["request_id"])
        except Failure as failure:
            if failure.code != "outcome_unknown":
                raise
            found = self.post("/v2/query", {"receipt": body["request_id"]})
            if found["ok"] and found["data"].get("write_state") == "applied":
                return {**found, "receipt": True}
            raise

    def upload(self, data: bytes, filename: str, description: str, metadata: dict, request_id: str) -> str:
        declared = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for _ in range(2):
            signed = self.ok("/v2/files", {"upload": declared})["data"]
            if not signed["present"]:
                self.service.put(signed["put"], io.BytesIO(data), len(data), "wirk import (run it again)")
            confirm = {"filename": filename, **declared, "description": description[:500], "metadata": metadata}
            answer = self.post("/v2/files", {"request_id": request_id, "confirm": confirm}, uncertain=request_id)
            if answer["ok"]:
                return answer["data"]["upload"]["id"]
            if answer["errors"][0]["code"] != "upload_incomplete":
                raise WirkError(answer["errors"][0])
        raise WirkError(answer["errors"][0])


class Index:
    """Every item whose first line is a provenance line of this source, trusted as §3.4 says."""

    def __init__(self, wirk: Wirk, source: str, me: str):
        self.wirk, self.source, self.me = wirk, source, me
        self.held, self.blocked_keys, self.blocked, self.forged = defaultdict(list), {}, Counter(), 0
        self.keys = {}  # item -> (kind, source ID), for the importer's own items

    def build(self, texts: list):
        seen = set()
        for text in texts:
            for archived in (False, True):
                for card in self.wirk.cards({"text": text, **({"archived": True} if archived else {})}):
                    parsed = render.provenance(card.get("line") or "")
                    if card["id"] not in seen and (not parsed or parsed[0] == self.source):
                        seen.add(card["id"])
                        self.trust(card, parsed, archived)
        return self

    def trust(self, card: dict, parsed: tuple | None, archived: bool):
        """An importer's item is keyed by the line of its own latest revision, so a person's edit, even a line written
        above it, never hides or moves it; another importer's keys are blocked; anyone else's provenance line is forged."""
        creator = card["by"]
        if creator != self.me or not parsed:
            whole = self.wirk.item(card["id"], "all")["all"]
            creator = whole["metadata"]["created_by"]
            if creator != self.me and not creator.endswith(f"-{self.source.lower()}-import"):
                self.forged += parsed is not None
                return
            if card["by"] != creator or not parsed:
                own = max(version["r"] for version in whole["versions"] if version["by"] == creator)
                parsed = render.provenance(self.wirk.item({"ref": card["id"], "revision": own}, "card").get("line") or "")
                if not parsed or parsed[0] != self.source:
                    return
        if creator != self.me:
            self.blocked_keys[parsed[1:3]] = creator
            self.blocked[creator] += 1
        else:
            self.add(*parsed[1:3], Held(card["id"], card["r"], card["by"], archived, parsed[4]))

    def add(self, kind: str, ident: str, held: Held):
        self.held[(kind, ident)] = [h for h in self.held[(kind, ident)] if h.item != held.item] + [held]
        self.keys[held.item] = (kind, ident)


@dataclass
class Plan:
    """One item the importer would write: what it holds and how it compares with WIRK."""
    kind: str
    title: str
    body: str
    fields: dict | None
    work: dict | None
    files: list  # (name, bytes) the importer makes
    attachments: list
    digest: str


class Importer:
    def __init__(self, wirk: Wirk, ctx: render.Context, *, users: dict, statuses: dict, plan: dict, withheld, download,
                 dry_run: bool, overwrite: bool, progress=None):
        self.wirk, self.ctx, self.users, self.statuses, self.plan = wirk, ctx, users, statuses, plan
        self.withheld, self.download, self.dry_run, self.overwrite = withheld, download, dry_run, overwrite
        self.prefix, self.counts, self.missing, self.unset = ctx.source.lower(), Counter(), defaultdict(set), set()
        self.not_members = set()  # owners WIRK refused this run: their issues are planned without an owner
        self.outcomes, self.notes, self.progress = [], {}, progress or (lambda done, total: None)

    # ---------------------------------------------------------------- the run

    def start(self, me: str | None = None) -> dict:
        """Read status and the index. `me` is the importer's principal when another token reads for its dry run."""
        you = self.wirk.status()
        self.me = me or you["you"]["principal"]
        self.vocab = {entry["key"]: entry["name"].split(": ", 1)[1].split(", ") for entry in you["ask"]["fields"]
                      if ": " in entry["name"] and entry["key"] not in render.RESERVED - {"status"}}
        self.index = Index(self.wirk, self.ctx.source, self.me).build([f"{self.ctx.source} {kind} " for kind in self.ctx.kinds]
                                                                      + [f"{self.ctx.source} comments"])
        return you["you"]

    def run(self, records: list) -> list:
        self.keys = {record.ident: record.key for record in records}
        self.kinds = {record.ident: record.kind for record in records}
        self.plan_links(records)
        for done, record in enumerate(records, 1):
            self.outcomes += self.guarded(record.key, record.kind, self.attempt, record)
            self.progress(done, len(records))
        if not self.dry_run:
            self.outcomes += self.link_pass()
        self.outcomes += self.gone({record.ident for record in records})
        return self.outcomes

    def attempt(self, record: render.Record) -> list:
        counts = self.counts.copy()
        try:
            return self.one(record)
        except NotMember as refused:  # nothing of this issue was written: plan it again, without that owner
            self.counts = counts
            self.not_members.add(refused.args[0])
            return self.attempt(record)

    def guarded(self, key: str, kind: str, work, *args) -> list:
        """One issue's outcomes; WIRK's refusal, or a write whose outcome stays unknown, is an error for that issue only."""
        try:
            return work(*args)
        except (WirkError, Failure) as error:
            if isinstance(error, Failure) and error.code not in UNSETTLED or error.code == "files_unavailable":
                raise  # the client's own failure, or no file storage: every issue would fail the same way
            return [Outcome(key, kind, "error", f"{error.code}: {error}")]

    # ---------------------------------------------------------------- links (§3.6)

    def plan_links(self, records: list) -> None:
        """Every link the source asks for between this run's issues, once per pair, with each fallback's note."""
        states = {record.ident: record.state for record in records}
        self.planned, requires, seen = set(), defaultdict(set), set()

        def reaches(start, goal):
            stack, visited = [start], set()
            while stack:
                current = stack.pop()
                if current == goal:
                    return True
                if current not in visited:
                    visited.add(current)
                    stack += requires[current]
            return False

        for record in sorted(records, key=lambda r: (len(r.ident), r.ident)):
            for relation, ref in record.relations:
                if ref.ident not in states or not self.ctx.shown(ref):
                    continue
                if relation in ("parent", "project", "milestone", "sub_issue"):  # what it is part of, or a part of it
                    child, parent = (ref.ident, record.ident) if relation == "sub_issue" else (record.ident, ref.ident)
                    self.planned.add(("contributes_to", child, parent))
                elif relation in ("blocked_by", "blocking"):
                    dependent, blocker = (record.ident, ref.ident) if relation == "blocked_by" else (ref.ident, record.ident)
                    if (dependent, blocker) in seen:
                        continue
                    seen.add((dependent, blocker))
                    note = ("completed here" if states[dependent] == "completed" else "the blocker is cancelled"
                            if states[blocker] == "cancelled" else "it would close a cycle" if reaches(blocker, dependent) else None)
                    if note:
                        self.notes[(dependent, blocker)] = f"kept as related: {note}"
                        self.planned.add(normal("related_to", dependent, blocker))
                    else:
                        requires[dependent].add(blocker)
                        self.planned.add(("requires", dependent, blocker))
                elif relation in ("duplicate_of", "duplicated_by", "related", "initiative", "includes", "of"):
                    self.planned.add(normal("related_to", record.ident, ref.ident))

    def single(self, ident: str) -> Held | None:
        held = self.index.held.get((self.kinds.get(ident, "issue"), ident), [])
        return held[0] if len(held) == 1 else None

    def ident_of(self, item: str) -> str | None:
        kind, ident = self.index.keys.get(item, (None, None))
        return ident if kind and not kind.startswith("comments") else None

    def existing(self, view: dict) -> dict:
        """The view's links between imported issues of this run, by their planned form."""
        found = {}
        for entry in view.get("links_out", []):
            ends = self.ident_of(entry["from"]), self.ident_of(entry["to"])
            if None not in ends and ends[0] in self.keys and ends[1] in self.keys:
                found[normal(entry["type"], *ends)] = entry["link"]
        return found

    def stale_links(self, view: dict) -> list:
        """Removals for the importer's own links that the source no longer asks for; a person's links are never touched."""
        stale = [link for key, link in self.existing(view).items() if key not in self.planned]
        makers = {}
        for start in range(0, len(stale), 32):
            answer = self.wirk.ok("/v2/query", {"fetch": stale[start:start + 32], "depth": "card"})
            makers.update({found["id"]: found.get("created_by") for found in answer["data"]["results"]})
        return [{"op": "link.remove", "id": link} for link in stale if makers.get(link) == self.me]

    def link_pass(self) -> list:
        """The missing links, one write per issue (more when over 32), read from each issue's own fetch."""
        outcomes, by_source = [], defaultdict(list)
        for link in sorted(self.planned):
            by_source[link[1]].append(link)
        for ident, wanted in by_source.items():
            outcomes += self.guarded(self.keys[ident], "links", self.links_of, ident, wanted)
        return outcomes

    def links_of(self, ident: str, wanted: list) -> list:
        source = self.single(ident)
        targets = [(link, self.single(link[2])) for link in wanted]
        targets = [(link, target) for link, target in targets if target and not target.archived]
        if not source or source.archived or not targets:
            return []
        view = self.wirk.item(source.item, "full")
        have = self.existing(view)  # a related_to meets a requires: it is what WIRK kept when it refused the gate
        missing = [(link, target) for link, target in targets
                   if link not in have and not (link[0] == "requires" and normal("related_to", *link[1:]) in have)]
        return [outcome for start in range(0, len(missing), 31)
                for outcome in self.write_links(ident, source.item, view["r"], missing[start:start + 31])]

    def write_links(self, ident, item, revision, chunk) -> list:
        operations = [{"op": "link.create", "data": {"type": link[0], "from": item, "to": target.item}} for link, target in chunk]
        expect = {item: revision, **{target.item: target.r for link, target in chunk if link[0] == "contributes_to"}}
        body = {"request_id": f"{self.prefix}-{ident}-{secrets.token_hex(8)}", "operations": operations, "expect": expect,
                "reason": f"Imported from {self.ctx.source}: the relations of {self.keys[ident]}"}
        notes = []
        for _ in range(len(operations) + 1):
            answer = self.wirk.write(body)
            if answer["ok"]:
                self.counts["linked"] += len(operations)
                return [Outcome(self.keys[ident], "links", "linked", "; ".join(notes))] if notes else []
            problem = answer["errors"][0]
            index = problem.get("input_index")
            if problem["code"] not in ("link_cycle", "invalid_link") or index is None:
                return [Outcome(self.keys[ident], "links", "error", f"{problem['code']}: {problem.get('message', '')}")]
            operations[index]["data"]["type"] = "related_to"  # WIRK sees a cycle or a completed end the plan could not
            body = {**body, "request_id": f"{self.prefix}-{ident}-{secrets.token_hex(8)}"}
            notes.append(f"kept as related: WIRK refused it ({problem['code']})")
        return [Outcome(self.keys[ident], "links", "error", "refused repeatedly")]

    # ---------------------------------------------------------------- planning one issue

    def fields_of(self, record: render.Record) -> dict:
        found = {"status": self.statuses[record.state]}
        for name, values in record.fields.items():
            plan = self.plan.get(name)
            if plan is None:
                continue
            if plan.key not in self.vocab:
                self.unset.add(plan.key)
                continue
            keys = [plan.options.get(value) or render.slug(value) for value in values]
            present = [key for key in keys if key in self.vocab[plan.key]]
            absent = {value for value, key in zip(values, keys) if key not in self.vocab[plan.key]}
            if absent:
                self.missing[plan.key].update(absent)
            if present:
                found[plan.key] = present if plan.selection == "many" else present[0]
        return found

    def managed(self) -> set:
        return {"status"} | {plan.key for plan in self.plan.values() if plan.key in self.vocab}

    def plans(self, record: render.Record) -> list:
        users = {user: wirk_id for user, wirk_id in self.users.items() if wirk_id not in self.not_members}
        made = render.work_item(self.ctx, record, users, self.notes)
        self.counts.update(made.counts)
        archive = (f"{self.prefix}-{record.kind}-{record.ident}.json", render.archive(record.raw, self.withheld, self.counts))
        attachments = [a for a in record.attachments if not a.comment]
        doc = record.kind in self.ctx.docs
        plans = [self.seal(made, record, None if doc else self.fields_of(record), None if doc else made.work, [archive], attachments)]
        for number, part in enumerate(render.discussion(self.ctx, record, made), 1):
            self.counts.update(part.counts)
            files = [(f"{self.prefix}-comments-{record.ident}.json", render.archive(record.raw_comments, self.withheld, self.counts))] \
                if number == 1 else []
            plans.append(self.seal(part, record, None, None, files, [a for a in record.attachments if a.comment] if number == 1 else []))
        return plans

    def seal(self, made, record, fields, work, files, attachments) -> Plan:
        names = [f"{name} {hashlib.sha256(data).hexdigest()}" for name, data in files] + [a.name for a in attachments]
        digest = render.digest(made.title, made.body, fields or {}, work or {}, names)
        version = record.version if made.kind == record.kind else max(c.version for c in record.comments)
        return Plan(made.kind, made.title, render.sealed(made, self.ctx.source, record.ident, version, digest), fields, work,
                    files, attachments, digest)

    def decide(self, held: list, digest: str) -> tuple[str, Held | None, str]:
        active = [h for h in held if not h.archived or h.by == self.me]  # an archive the importer made is its own to undo
        if len(active) > 1:
            return "ambiguous", None, ", ".join(h.item[5:13] for h in active) + " all claim it"
        if not active:
            return ("skipped", held[0], "archived in WIRK") if held else ("create", None, "")
        found = active[0]
        if found.hash == digest:
            return "current", found, ""
        if found.by == self.me:
            return "update", found, ""
        changed = f"changed in WIRK since import (r{found.r} by {found.by})"
        return ("update", found, changed + "; replaced by --overwrite") if self.overwrite else \
            ("skipped", found, changed + "; to replace it: --overwrite")

    # ---------------------------------------------------------------- one issue

    def one(self, record: render.Record) -> list:
        blocked = self.index.blocked_keys.get((record.kind, record.ident))
        if blocked:
            return [Outcome(record.key, record.kind, "blocked", f"imported by {blocked}")]
        plans = self.plans(record)
        decided = [(plan, *self.decide(self.index.held[(plan.kind, record.ident)], plan.digest)) for plan in plans]
        _, action, held, message = decided[0]
        if action == "ambiguous" or (action == "skipped" and held.archived):  # the discussion follows its work item
            return [Outcome(record.key, plan.kind, action, message, found.item if found else None)
                    for plan, _, found, _ in decided]
        outcomes = [Outcome(record.key, plan.kind, action, message, held.item if held else None)
                    for plan, action, held, message in decided if action not in ("create", "update")]
        acting = [(plan, action, held) for plan, action, held, _ in decided if action in ("create", "update")]
        said = {plan.kind: message for plan, _, _, message in decided}
        if self.dry_run:  # what --overwrite would replace says who changed it
            return outcomes + [Outcome(record.key, plan.kind, f"would {action}", message, held.item if held else None)
                               for plan, action, held, message in decided if action in ("create", "update")] + \
                self.stale(record, len(plans) - 1) + self.mirror(record, plans)
        # the work item and the discussion's first part go in one write; later parts one to a write (§3.5)
        together = [entry for entry in acting if entry[0].kind in (record.kind, "comments")]
        work = held.item if held else None
        if together:
            outcomes += self.send(record, together, work, said)
            work = work or next((o.item for o in outcomes if o.kind == record.kind and o.item), None)
        for entry in acting:
            if entry not in together:
                outcomes += self.send(record, [entry], work, said)
        return outcomes + self.stale(record, len(plans) - 1) + self.mirror(record, plans)

    def mirror(self, record, plans) -> list:
        """Archive what the source archived, and restore what the importer archived once the source restores it; an archive
        or an edit someone else made is theirs."""
        want = record.archived is not None
        found = [(plan.kind, h) for plan in plans for h in self.index.held.get((plan.kind, record.ident), [])
                 if h.archived != want and h.by == self.me]
        word = "archive" if want else "restore"
        if not found or self.dry_run:
            return [Outcome(record.key, kind, f"would {word}", item=h.item) for kind, h in found]
        answer = self.wirk.write({"request_id": self.rid(record), "reason": record.archived or f"Active again in {self.ctx.source}",
                                  "expect": {h.item: h.r for _, h in found},
                                  "operations": [{"op": f"item.{word}", "id": h.item} for _, h in found]})
        if not answer["ok"]:
            return [Outcome(record.key, record.kind, "error", f"{answer['errors'][0]['code']}: {answer['errors'][0].get('message', '')}")]
        for (kind, h), result in zip(found, answer["data"]["results"]):
            self.index.add(kind, record.ident, replace(h, r=result["revision"], archived=want))
        return [Outcome(record.key, kind, f"{word}d", item=h.item) for kind, h in found]

    def operations(self, record, acting, work):
        operations, expect = [], {}
        for plan, action, held in acting:
            view = self.wirk.item(held.item, "all") if held else None
            uploads, patch_files = self.files_for(record, plan, view)
            if view and plan.kind == record.kind:
                operations += self.stale_links(view)  # before the edit, so a completion is never gated by a stale link
            if action == "create":
                data = {"title": plan.title, "body": plan.body, **({"uploads": uploads} if uploads else {})}
                if plan.work is not None:
                    data.update(work=plan.work, fields=plan.fields)
                operations.append({"op": "item.create", "ref": "w" if plan.kind == record.kind else "d", "data": data})
                if plan.kind != record.kind:
                    operations.append({"op": "link.create", "data": {"type": "related_to", "from": "$d", "to": work or "$w"}})
            else:
                patch = {"title": plan.title, "body": plan.body, **patch_files}
                if plan.work is not None:
                    patch["fields"] = {key: plan.fields.get(key) for key in self.managed()}
                    patch["work"] = {"owner_id": plan.work.get("owner_id"), "due_at": plan.work.get("due_at")}
                operations.append({"op": "item.edit", "id": held.item, "patch": patch})
                expect[held.item] = held.r
        return operations, expect

    def files_for(self, record, plan, view) -> tuple[list, dict]:
        """Uploads for a new item, or what an edit attaches and swaps; attachments are downloaded only when missing."""
        having = {} if view is None else {f["filename"]: f for f in view["all"]["files"]}
        attach, swap = [], []

        def store(data: bytes, name: str, description: str, uri: str) -> str:
            return self.wirk.upload(data, name, description, {"role": "original", "origin": {"uri": uri, "observed_at": record.version}},
                                    self.rid(record))
        for name, data in plan.files:
            if name in having and having[name]["sha256"] == hashlib.sha256(data).hexdigest():
                continue
            upload = store(data, name, f"{self.ctx.source} {record.key} as its API returned it, emails removed", record.url)
            (swap.append({"file_id": having[name]["id"], "upload_id": upload}) if name in having else attach.append(upload))
        for attachment in plan.attachments:
            if attachment.name not in having:
                data = self.download(attachment)
                self.counts["attachments"] += data is not None
                self.counts["left as links"] += data is None
                if data is not None:
                    attach.append(store(data, attachment.name, f"{self.ctx.source} attachment in {record.key}", attachment.url))
        if view is None:
            return attach, {}
        return [], {**({"attach_uploads": attach} if attach else {}), **({"replace_files": swap} if swap else {})}

    def rid(self, record) -> str:
        return f"{self.prefix}-{record.ident}-{secrets.token_hex(8)}"

    def reason(self, record) -> str:
        if record.state == "completed":
            return render.evidence(self.ctx, record)
        return f"Imported from {self.ctx.source} {record.key} by wirk import {self.prefix}"

    def send(self, record, acting, work, said: dict | None = None) -> list:
        operations, expect = self.operations(record, acting, work)
        body = {"request_id": self.rid(record), "operations": operations, "reason": self.reason(record),
                **({"expect": expect} if expect else {})}
        notes = []

        def each(outcome: str, message: str) -> list:
            return [Outcome(record.key, plan.kind, outcome, message, held.item if held else None) for plan, _, held in acting]

        for _ in range(4):
            answer = self.wirk.write(body)
            if answer["ok"]:
                break
            problem = answer["errors"][0]
            if problem["code"] == "prerequisite_incomplete":
                return each("skipped", "completing it needs its prerequisites completed first (a link made in WIRK)")
            fixed = self.fix(record, body, problem)
            if fixed is None:
                return each("error", f"{problem['code']}: {problem.get('message', '')}")
            notes.append(fixed)
            body = {**body, "request_id": self.rid(record)}
        else:
            return each("error", "refused repeatedly: " + "; ".join(notes))
        results = answer["data"]["results"]
        if answer.get("receipt"):
            notes.append(f"confirmed by its receipt {body['request_id']}")
        outcomes = []
        for (plan, action, held), result in zip(acting, [r for r in results if r.get("resource") == "item"]):
            self.index.add(plan.kind, record.ident, Held(result["id"], result["revision"], self.me, bool(held and held.archived),
                                                         plan.digest))
            message = "; ".join(filter(None, [(said or {}).get(plan.kind, ""), *notes]))
            outcomes.append(Outcome(record.key, plan.kind, "created" if action == "create" else "updated", message, result["id"]))
        return outcomes

    def fix(self, record, body, problem) -> str | None:
        """Change the refused write so it can go through, and say what changed; None when it cannot."""
        operation = body["operations"][problem.get("input_index", 0)]
        part = operation.get("data") or operation.get("patch") or {}
        owner = (part.get("work") or {}).get("owner_id")
        if problem["code"] == "unknown_owner" and owner and owner not in self.not_members:
            raise NotMember(owner)
        if problem["code"] == "likely_duplicate" and operation["op"] == "item.create":
            choices = [choice.get("key") or choice.get("id") for choice in problem.get("choices") or []][:8]
            # an item the importer made is kept separate only when the index knows it as another issue's work
            if not choices or any(self.ident_of(choice) == record.ident or self.ident_of(choice) is None and
                                  self.wirk.item(choice, "all")["all"]["metadata"]["created_by"] == self.me
                                  for choice in choices):
                return None
            operation["allow_duplicate_of"] = choices
            others = ", ".join(self.key_of(choice) for choice in choices)
            body["reason"] = (body["reason"] + f"\nSeparate {self.ctx.source} issues {record.key} and {others}; "
                              "imported as they are")[:2048]
            return f"possible duplicate kept separate: {others}"
        if problem["code"] in ("unknown_enum_field", "unknown_enum_option") and operation["op"] in ("item.create", "item.edit"):
            fields = part.get("fields") or {}
            dropped = [key for key in list(fields) if key != "status"]
            for key in dropped:
                fields.pop(key)
            return f"fields {', '.join(dropped)} changed in WIRK during the run; left as header text"
        return None

    def key_of(self, item: str) -> str:
        """An item's source key when it is one of this run's issues, else its short ID."""
        ident = self.ident_of(item)
        return self.keys[ident] if ident in self.keys else item[5:13] if item.startswith("item_") else item

    # ---------------------------------------------------------------- parts and items no longer needed

    def stale(self, record, parts: int) -> list:
        """Discussion parts beyond what the discussion needs now are archived, when the importer wrote them last."""
        outcomes, number = [], parts
        while (render.part_kind(number + 1), record.ident) in self.index.held:
            number += 1
            kind = render.part_kind(number)
            for found in self.index.held[(kind, record.ident)]:
                if found.archived:
                    continue
                if found.by != self.me:
                    outcomes.append(Outcome(record.key, kind, "skipped", f"no longer needed, but changed in WIRK (r{found.r} by {found.by})",
                                            found.item))
                elif self.dry_run:
                    outcomes.append(Outcome(record.key, kind, "would archive", item=found.item))
                else:
                    answer = self.wirk.write({"request_id": self.rid(record), "expect": {found.item: found.r},
                                              "reason": f"No longer needed: the discussion of [{record.key}] now fits in "
                                                        f"{parts} part{'s' if parts != 1 else ''}",
                                              "operations": [{"op": "item.archive", "id": found.item}]})
                    outcomes.append(Outcome(record.key, kind, "archived" if answer["ok"] else "error",
                                            "" if answer["ok"] else answer["errors"][0]["code"], found.item))
        return outcomes

    def gone(self, present: set) -> list:
        """The importer's active items that the source no longer has: an issue missing from a complete read of its scope, or
        another object missing from everything the source showed, selected or not."""
        outcomes = []
        for (kind, ident), held in self.index.held.items():
            if kind.startswith("comments") or ident in present or kind != "issue" and ident in self.ctx.seen:
                continue
            for found in held:
                if not found.archived:
                    outcomes += self.guarded(ident, kind, self.missing_one, kind, ident, found)
        return outcomes

    def missing_one(self, kind: str, ident: str, found: Held) -> list:
        lines = self.wirk.item(found.item, "full")["body"].split("\n")
        key = next((m[1] for line in lines if line.startswith(f"{self.ctx.source} {kind} [") for m in [KEYED.search(line)] if m), ident)
        if kind == "issue" and re.sub(r"[#-][0-9]+$", "", key) not in self.ctx.selected:  # acme/api#12 is in acme/api, ENG-12 in ENG
            return []
        return [Outcome(key, kind, "missing", f"deleted, transferred out or no longer visible in {self.ctx.source}; nothing was archived",
                        found.item)]


def normal(kind: str, source: str, target: str) -> tuple:
    """A link as the plan holds it: related_to is the same either way round."""
    return (kind, *sorted((source, target), key=lambda ident: (len(ident), ident))) if kind == "related_to" else (kind, source, target)
