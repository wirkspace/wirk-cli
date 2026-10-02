"""The importer against WIRK (docs/plans/importers.md §3.4–§3.7): the index of earlier imports and its trust rules, the
decision for each object, one issue per write, refusals, receipts and files. WIRK's index is the only state."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import io
import secrets

from ..client import Failure
from . import render

PAGE = {"limit": 100, "max_bytes": 65536}


class WirkError(Exception):
    def __init__(self, problem: dict):
        super().__init__(problem.get("message", ""))
        self.code, self.problem = problem.get("code", "error"), problem


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

    def data(self, route: str, body: dict) -> dict:
        answer = self.post(route, body)
        if not answer["ok"]:
            raise WirkError(answer["errors"][0])
        return answer["data"]

    def status(self) -> dict:
        return self.data("/v2/status", {"max_bytes": 65536})

    def cards(self, fields: dict):
        body = {"fields": fields, **PAGE}
        while True:
            answer = self.post("/v2/query", body)
            if not answer["ok"]:
                raise WirkError(answer["errors"][0])
            yield from answer["data"].get("cards", [])
            if answer["page"].get("complete", True) or not answer["page"].get("next_cursor"):
                return
            body = {**body, "cursor": answer["page"]["next_cursor"]}

    def item(self, ref, depth: str = "full") -> dict:
        """One item, its body read whole through the continuation."""
        body = {"fetch": [ref], "depth": depth, "max_bytes": 65536}
        answer = self.post("/v2/query", body)
        if not answer["ok"]:
            raise WirkError(answer["errors"][0])
        view = answer["data"]["results"][0]
        text = view.get("body", "")
        while not view.get("body_complete", True) and answer["page"].get("next_cursor"):
            answer = self.post("/v2/query", {**body, "cursor": answer["page"]["next_cursor"]})
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
            signed = self.data("/v2/files", {"upload": declared})
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
                    if card["id"] in seen or not parsed or parsed[0] != self.source:
                        continue
                    seen.add(card["id"])
                    self.trust(card, parsed, archived)
        return self

    def trust(self, card: dict, parsed: tuple, archived: bool):
        if card["by"] != self.me:
            whole = self.wirk.item(card["id"], "all")["all"]
            creator = whole["metadata"]["created_by"]
            if creator != self.me:
                if creator.endswith(f"-{self.source.lower()}-import"):
                    self.blocked_keys[parsed[1:3]] = creator
                    self.blocked[creator] += 1
                else:
                    self.forged += 1
                return
            own = max(version["r"] for version in whole["versions"] if version["by"] == self.me)
            parsed = render.provenance(self.wirk.item({"ref": card["id"], "revision": own}, "card").get("line") or "")
            if not parsed:
                return
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
        self.outcomes, self.notes, self.index, self.progress = [], {}, None, progress or (lambda done, total: None)

    # ---------------------------------------------------------------- the run

    def start(self, me: str | None = None) -> dict:
        """Read status and the index. `me` is the importer's principal when another token reads for its dry run."""
        you = self.wirk.status()
        self.me = me or you["you"]["principal"]
        self.vocab = {entry["key"]: entry["name"].split(": ", 1)[1].split(", ") for entry in you["ask"]["fields"]
                      if ": " in entry["name"] and entry["key"] not in render.RESERVED - {"status"}}
        self.index = Index(self.wirk, self.ctx.source, self.me).build([f"{self.ctx.source} issue ", f"{self.ctx.source} comments"])
        return you["you"]

    def run(self, records: list, complete: bool = True) -> list:
        if self.index is None:
            self.start()
        self.keys = {record.ident: record.key for record in records}
        self.plan_links(records)
        for done, record in enumerate(records, 1):
            self.outcomes += self.one(record)
            self.progress(done, len(records))
        if not self.dry_run:
            self.outcomes += self.link_pass()
        if complete:
            self.outcomes += self.gone({record.ident for record in records})
        return self.outcomes

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
                if relation in ("parent", "sub_issue"):
                    child, parent = (record.ident, ref.ident) if relation == "parent" else (ref.ident, record.ident)
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
                elif relation in ("duplicate_of", "related"):
                    self.planned.add(normal("related_to", record.ident, ref.ident))

    def single(self, ident: str) -> Held | None:
        held = self.index.held.get(("issue", ident), [])
        return held[0] if len(held) == 1 else None

    def ident_of(self, item: str) -> str | None:
        kind, ident = self.index.keys.get(item, (None, None))
        return ident if kind == "issue" else None

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
            answer = self.wirk.data("/v2/query", {"fetch": stale[start:start + 32], "depth": "card"})
            makers.update({found["id"]: found.get("created_by") for found in answer["results"]})
        return [{"op": "link.remove", "id": link} for link in stale if makers.get(link) == self.me]

    def link_pass(self) -> list:
        """The missing links, one write per issue (more when over 32), read from each issue's own fetch."""
        outcomes, by_source = [], defaultdict(list)
        for link in sorted(self.planned):
            by_source[link[1]].append(link)
        for ident, wanted in by_source.items():
            source = self.single(ident)
            targets = [(link, self.single(link[2])) for link in wanted]
            targets = [(link, target) for link, target in targets if target and not target.archived]
            if not source or source.archived or not targets:
                continue
            view = self.wirk.item(source.item, "full")
            have = self.existing(view)
            missing = [(link, target) for link, target in targets if link not in have]
            for start in range(0, len(missing), 31):
                outcomes += self.write_links(ident, source.item, view["r"], missing[start:start + 31])
        return outcomes

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
        made = render.work_item(self.ctx, record, self.users, self.notes)
        self.counts.update(made.counts)
        archive = (f"{self.prefix}-issue-{record.ident}.json", render.archive(record.raw, self.withheld, self.counts))
        attachments = [a for a in record.attachments if not a.comment]
        fields = self.fields_of(record)
        plans = [self.seal(made, record, fields, made.work, [archive], attachments)]
        for number, part in enumerate(render.discussion(self.ctx, record), 1):
            self.counts.update(part.counts)
            files = [(f"{self.prefix}-comments-{record.ident}.json", render.archive(record.raw_comments, self.withheld, self.counts))] \
                if number == 1 else []
            plans.append(self.seal(part, record, None, None, files, [a for a in record.attachments if a.comment] if number == 1 else []))
        return plans

    def seal(self, made, record, fields, work, files, attachments) -> Plan:
        names = [f"{name} {hashlib.sha256(data).hexdigest()}" for name, data in files] + [a.name for a in attachments]
        digest = render.digest(made.title, made.body, fields or {}, work or {}, names)
        version = record.version if made.kind == "issue" else max(c.version for c in record.comments)
        return Plan(made.kind, made.title, render.sealed(made, self.ctx.source, record.ident, version, digest), fields, work,
                    files, attachments, digest)

    def decide(self, held: list, digest: str) -> tuple[str, Held | None, str]:
        active = [h for h in held if not h.archived]
        if len(active) > 1:
            return "ambiguous", None, ", ".join(h.item[5:13] for h in active) + " all claim it"
        if not active:
            return ("skipped", held[0], "archived in WIRK") if held else ("create", None, "")
        found = active[0]
        if found.hash == digest:
            return "current", found, ""
        if found.by == self.me or self.overwrite:
            return "update", found, ""
        return "skipped", found, f"changed in WIRK since import (r{found.r} by {found.by}); to replace it: --overwrite"

    # ---------------------------------------------------------------- one issue

    def one(self, record: render.Record) -> list:
        blocked = self.index.blocked_keys.get(("issue", record.ident))
        if blocked:
            return [Outcome(record.key, "issue", "blocked", f"imported by {blocked}")]
        plans = self.plans(record)
        decided = [(plan, *self.decide(self.index.held[(plan.kind, record.ident)], plan.digest)) for plan in plans]
        _, action, held, message = decided[0]
        if action == "ambiguous" or (action == "skipped" and held.archived):  # the discussion follows its work item
            return [Outcome(record.key, plan.kind, action, message, found.item if found else None)
                    for plan, _, found, _ in decided]
        outcomes = [Outcome(record.key, plan.kind, action, message, held.item if held else None)
                    for plan, action, held, message in decided if action not in ("create", "update")]
        acting = [(plan, action, held) for plan, action, held, _ in decided if action in ("create", "update")]
        if self.dry_run:
            return outcomes + [Outcome(record.key, plan.kind, f"would {action}", item=held.item if held else None)
                               for plan, action, held in acting] + self.stale(record, len(plans) - 1)
        # the work item and the discussion's first part go in one write; later parts one to a write (§3.5)
        together = [entry for entry in acting if entry[0].kind in ("issue", "comments")]
        work = held.item if held else None
        if together:
            outcomes += self.send(record, together, work)
            work = work or next((o.item for o in outcomes if o.kind == "issue" and o.item), None)
        for entry in acting:
            if entry not in together:
                outcomes += self.send(record, [entry], work)
        return outcomes + self.stale(record, len(plans) - 1)

    def operations(self, record, acting, work):
        operations, expect = [], {}
        for plan, action, held in acting:
            view = self.wirk.item(held.item, "all") if held else None
            uploads, patch_files = self.files_for(record, plan, view)
            if view and plan.kind == "issue":
                operations += self.stale_links(view)  # before the edit, so a completion is never gated by a stale link
            if action == "create":
                data = {"title": plan.title, "body": plan.body, **({"uploads": uploads} if uploads else {})}
                if plan.kind == "issue":
                    data.update(work=plan.work, fields=plan.fields)
                operations.append({"op": "item.create", "ref": "w" if plan.kind == "issue" else "d", "data": data})
                if plan.kind != "issue":
                    operations.append({"op": "link.create", "data": {"type": "related_to", "from": "$d", "to": work or "$w"}})
            else:
                patch = {"title": plan.title, "body": plan.body, **patch_files}
                if plan.kind == "issue":
                    patch["fields"] = {key: plan.fields.get(key) for key in self.managed()}
                    patch["work"] = {"owner_id": plan.work.get("owner_id"), "due_at": plan.work.get("due_at")}
                operations.append({"op": "item.edit", "id": held.item, "patch": patch})
                expect[held.item] = held.r
        return operations, expect

    def files_for(self, record, plan, view) -> tuple[list, dict]:
        """Uploads for a new item, or what an edit attaches and swaps; attachments are downloaded only when missing."""
        having = {} if view is None else {f["filename"]: f for f in view["all"]["files"]}
        attach, swap = [], []
        for name, data in plan.files:
            if name in having and having[name]["sha256"] == hashlib.sha256(data).hexdigest():
                continue
            upload = self.wirk.upload(data, name, f"{self.ctx.source} {record.key} as its API returned it, emails removed",
                                      {"role": "original", "origin": {"uri": record.url, "observed_at": record.version}},
                                      self.rid(record))
            (swap.append({"file_id": having[name]["id"], "upload_id": upload}) if name in having else attach.append(upload))
        for attachment in plan.attachments:
            if attachment.name not in having:
                data = self.download(attachment)
                if data is not None:
                    attach.append(self.wirk.upload(data, attachment.name, f"{self.ctx.source} attachment in {record.key}",
                                                   {"role": "original", "origin": {"uri": attachment.url,
                                                                                   "observed_at": record.version}},
                                                   self.rid(record)))
        if view is None:
            return attach, {}
        return [], {**({"attach_uploads": attach} if attach else {}), **({"replace_files": swap} if swap else {})}

    def rid(self, record) -> str:
        return f"{self.prefix}-{record.ident}-{secrets.token_hex(8)}"

    def reason(self, record) -> str:
        if record.state == "completed":
            return render.evidence(self.ctx, record)
        return f"Imported from {self.ctx.source} {record.key} by wirk import {self.prefix}"

    def send(self, record, acting, work) -> list:
        operations, expect = self.operations(record, acting, work)
        body = {"request_id": self.rid(record), "operations": operations, "reason": self.reason(record),
                **({"expect": expect} if expect else {})}
        notes = []
        for _ in range(4):
            answer = self.wirk.write(body)
            if answer["ok"]:
                break
            problem = answer["errors"][0]
            if problem["code"] == "prerequisite_incomplete":
                return [Outcome(record.key, plan.kind, "skipped", "completing it needs its prerequisites completed first "
                                "(a link made in WIRK)", held.item if held else None) for plan, _, held in acting]
            fixed = self.fix(record, body, problem)
            if fixed is None:
                return [Outcome(record.key, plan.kind, "error", f"{problem['code']}: {problem.get('message', '')}")
                        for plan, _, _ in acting]
            notes.append(fixed)
            body = {**body, "request_id": self.rid(record)}
        else:
            return [Outcome(record.key, plan.kind, "error", "refused repeatedly: " + "; ".join(notes)) for plan, _, _ in acting]
        results = answer["data"]["results"]
        if answer.get("receipt"):
            notes.append(f"confirmed by its receipt {body['request_id']}")
        outcomes = []
        for (plan, action, held), result in zip(acting, [r for r in results if r.get("resource") == "item"]):
            self.index.add(plan.kind, record.ident, Held(result["id"], result["revision"], self.me, False, plan.digest))
            outcomes.append(Outcome(record.key, plan.kind, "created" if action == "create" else "updated", "; ".join(notes),
                                    result["id"]))
        return outcomes

    def fix(self, record, body, problem) -> str | None:
        """Change the refused write so it can go through, and say what changed; None when it cannot."""
        index = problem.get("input_index", 0)
        operation = body["operations"][index]
        if problem["code"] == "unknown_owner" and operation["op"] in ("item.create", "item.edit"):
            work = operation.get("data", operation.get("patch", {})).get("work") or {}
            owner = work.pop("owner_id", None)
            return f"owner {owner} is not a member, so the issue has no owner"
        if problem["code"] == "likely_duplicate" and operation["op"] == "item.create":
            choices = [choice.get("key") or choice.get("id") for choice in problem.get("choices") or []][:8]
            mine = {h.item for h in self.index.held[("issue", record.ident)]}
            if not choices or set(choices) & mine:
                return None
            operation["allow_duplicate_of"] = choices
            others = ", ".join(self.key_of(choice) for choice in choices)
            body["reason"] = (body["reason"] + f"\nSeparate {self.ctx.source} issues {record.key} and {others}; "
                              "imported as they are")[:2048]
            return f"possible duplicate kept separate: {others}"
        if problem["code"] in ("unknown_enum_field", "unknown_enum_option") and operation["op"] in ("item.create", "item.edit"):
            fields = operation.get("data", operation.get("patch", {})).get("fields") or {}
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
        outcomes = []
        for (kind, ident), held in list(self.index.held.items()):
            number = 1 if kind == "comments" else int(kind.split("-")[1]) if kind.startswith("comments-") else 0
            if ident != record.ident or number <= parts:
                continue
            for found in held:
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
        """The importer's active items whose issue a complete read of the same selection no longer has."""
        outcomes = []
        for (kind, ident), held in self.index.held.items():
            if kind != "issue" or ident in present:
                continue
            for found in held:
                if found.archived:
                    continue
                key_line = self.wirk.item(found.item, "full")["body"].split("\n")[1]
                key = key_line.split("[", 1)[1].split("]", 1)[0] if "[" in key_line else ident
                if key.split("#")[0] in self.ctx.selected:
                    outcomes.append(Outcome(key, "issue", "missing", f"deleted, transferred out or no longer visible in "
                                                                       f"{self.ctx.source}; nothing was archived", found.item))
        return outcomes


def normal(kind: str, source: str, target: str) -> tuple:
    """A link as the plan holds it: related_to is the same either way round."""
    return (kind, *sorted((source, target), key=lambda ident: (len(ident), ident))) if kind == "related_to" else (kind, source, target)
