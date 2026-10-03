"""An in-memory WIRK for the importer's tests: the v2 status, query, write and files routes as the published docs and
core describe them, small enough to read. The contract test checks the same behaviors against real core."""

import copy
import hashlib
import json
import re
import secrets

import httpx

FIXED = ["kind", "state", "proposal", "owner", "linked", "folder", "changed_days", "text", "archived"]


def envelope(data=None, *, errors=(), status=200, notices=(), page=None):
    return httpx.Response(status, json={"ok": not errors, "data": data or {}, "errors": list(errors), "notices": list(notices),
                                        "page": page or {"complete": True, "next_cursor": None}, "timing_ms": {"total": 1}})


def refusal(code, message, status=422, **extra):
    return envelope({}, errors=[{"code": code, "message": message, **extra}], status=status)


class Refused(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


def first_line(body):
    for line in (body or "").split("\n"):
        line = re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)+", "", line)
        line = " ".join(re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line).split())
        if line:
            return line if len(line) <= 100 else line[:99] + "…"
    return None


class FakeWirk:
    def __init__(self, principal="alice-github-import", person="alice", members=("alice", "bob"), statuses=None, fields=None):
        self.principal, self.person, self.members = principal, person, set(members)
        self.definitions = {"status": statuses or {"open": "active", "in_progress": "active", "completed": "completed",
                                                   "cancelled": "cancelled"}, **(fields or {})}
        self.items, self.links, self.objects, self.uploads, self.receipts = {}, {}, {}, {}, {}
        self.requests, self.duplicates, self.faults = [], {}, []
        self.as_whom = None  # a principal other than the importer, for tests that act as a person
        self.tokens = None  # token -> principal; None takes every token as the importer's

    # ---------------------------------------------------------------- transport

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "store.test":
            self.objects[request.url.path[1:]] = request.content
            return httpx.Response(200)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        route = request.url.path.rsplit("/", 1)[-1]
        if self.tokens is not None:  # who the bearer token belongs to, when the test registers tokens
            caller = self.tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))
            if caller is None:
                return refusal("unauthenticated", "This token is not registered", 401)
            self.as_whom = None if caller == self.principal else caller
        for fault in list(self.faults):
            if fault(route, body, before=True):
                raise httpx.ReadTimeout("lost before it ran")
        answer = getattr(self, route)(body)
        for fault in list(self.faults):
            if fault(route, body, before=False):
                raise httpx.ReadTimeout("lost after it ran")
        return answer

    def lose(self, route, *, after=True, times=1, when=lambda body: True):
        """The next `times` answers on `route` are lost, before or after the request ran."""
        left = [times]

        def fault(seen, body, before):
            if seen == route and before != after and left[0] > 0 and when(body):
                left[0] -= 1
                return True
            return False
        self.faults.append(fault)

    # ---------------------------------------------------------------- status

    def status(self, body):
        vocab = [{"key": key, "name": f"{key}: " + ", ".join(options)} for key, options in self.definitions.items()]
        return envelope({"you": {"principal": self.who(), "kind": "agent", "person": self.person,
                                 "wirkspace": {"id": "wsp_" + "1" * 32, "name": "Scratch"}, "capabilities": ["read", "edit"]},
                         "ask": {"fields": vocab + [{"key": key, "name": key} for key in FIXED]}})

    # ---------------------------------------------------------------- query

    def resolve(self, ref):
        if isinstance(ref, dict):
            return self.resolve(ref["ref"])[0], ref["revision"]
        found = [i for i in list(self.items) + list(self.links) if i == ref or i.split("_", 1)[1].startswith(ref)]
        if len(found) != 1:
            raise Refused("not_available", f"no record {ref}")
        return found[0], None

    def card(self, item_id, revision=None):
        item = self.items[item_id]
        snap = item["revisions"][(revision or len(item["revisions"])) - 1]
        card = {"id": item_id, "kind": "work" if snap["work"] is not None else "doc", "r": snap["r"], "title": snap["title"],
                "fields": snap["fields"], "by": snap["by"], "by_kind": "agent", "changed": "2026-10-02T00:00:00Z"}
        if first_line(snap["body"]):
            card["line"] = first_line(snap["body"])
        if item["archived"]:
            card["archived"] = True
        if snap["work"] and snap["work"].get("owner_id"):
            card["owner"] = snap["work"]["owner_id"]
        return card, snap

    def view(self, item_id, revision, depth):
        card, snap = self.card(item_id, revision)
        if depth == "card":
            return card
        view = {**card, "body": snap["body"], "body_complete": True}
        out = [{"type": l["type"], "link": lid, "r": 1, "id": l["to"] if l["from"] == item_id else l["from"],
                "from": l["from"], "to": l["to"]}
               for lid, l in self.links.items() if not l["removed"] and (l["from"] == item_id or (l["type"] == "related_to" and l["to"] == item_id))
               and not (l["type"] == "related_to" and self.items[l["to"] if l["from"] == item_id else l["from"]]["archived"])]
        if out:
            view["links_out"] = out
        if snap["files"]:
            view["attachments"] = [{k: f[k] for k in ("id", "filename")} for f in snap["files"]]
        if depth == "all":
            view["all"] = {"metadata": {"created_by": self.items[item_id]["revisions"][0]["by"], "updated_by": snap["by"]},
                           "versions": [{"r": s["r"], "by": s["by"]} for s in self.items[item_id]["revisions"]],
                           "files": copy.deepcopy(snap["files"])}
        return view

    def query(self, body):
        try:
            if "receipt" in body:
                stored = self.receipts.get(body["receipt"])
                return envelope(stored[1]) if stored else refusal("not_available", "no receipt", 404)
            if "fetch" in body:
                results = []
                for ref in body["fetch"]:
                    found, revision = self.resolve(ref)
                    if found.startswith("link_"):
                        link = self.links[found]
                        results.append({"id": found, "type": link["type"], "from": {"id": link["from"]}, "to": {"id": link["to"]},
                                        "created_by": link["by"]})
                    else:
                        results.append(self.view(found, revision, body.get("depth", "full")))
                return envelope({"results": results})
        except Refused as error:
            return refusal(error.code, str(error), 404)
        fields = body.get("fields", {})
        text, archived = fields.get("text", "").casefold(), fields.get("archived", False)
        matches = [i for i, item in self.items.items() if item["archived"] == archived
                   and text in (item["revisions"][-1]["title"] + "\n" + item["revisions"][-1]["body"]).casefold()]
        start, limit = int(body.get("cursor") or 0), body.get("limit", 20)
        window = matches[start:start + limit]
        more = start + limit < len(matches)
        return envelope({"cards": [self.card(i)[0] for i in window]},
                        page={"complete": not more, "next_cursor": str(start + limit) if more else None})

    # ---------------------------------------------------------------- write

    def write(self, body):
        rid = body["request_id"]
        canonical = json.dumps({k: v for k, v in body.items() if k != "format"}, sort_keys=True)
        if rid in self.receipts:
            stored, data = self.receipts[rid]
            if stored != canonical:
                return refusal("request_conflict", "a different body under this request ID", 409)
            return envelope(data, notices=[{"code": "replayed", "message": "stored receipt"}])
        saved = copy.deepcopy((self.items, self.links))
        try:
            data = self.apply(body)
        except Refused as error:
            self.items, self.links = saved
            self.receipts[rid] = (canonical, {})
            status = 409 if error.code in ("basis_changed", "likely_duplicate") else 422
            return refusal(error.code, str(error), status, **error.extra)
        self.receipts[rid] = (canonical, data)
        return envelope(data)

    def who(self):
        return self.as_whom or self.principal

    def apply(self, body):
        for ref, revision in (body.get("expect") or {}).items():
            found, _ = self.resolve(ref)
            current = len(self.items[found]["revisions"])
            if current != revision:
                raise Refused("basis_changed", f"{ref} is at r{current}; you read r{revision}")
        named, results = {}, []
        for index, op in enumerate(body["operations"]):
            try:
                results.append(self.operation(op, named, body))
            except Refused as error:
                error.extra["input_index"] = index
                raise
            results[-1]["operation_index"] = index
        return {"request_id": body["request_id"], "write_state": "applied", "results": results,
                "revisions": {r["id"]: r["revision"] for r in results if r.get("resource") == "item"}}

    def ident(self, ref, named):
        return named[ref[1:]] if ref.startswith("$") else self.resolve(ref)[0]

    def check_fields(self, fields):
        for key, value in (fields or {}).items():
            if key not in self.definitions:
                raise Refused("unknown_enum_field", f"no field {key}")
            for option in ([] if value is None else value if isinstance(value, list) else [value]):
                if option not in self.definitions[key]:
                    raise Refused("unknown_enum_option", f"{key} has no {option}")

    def completed(self, snap):
        return snap["work"] is not None and self.definitions["status"].get(snap["fields"].get("status")) == "completed"

    def operation(self, op, named, body):
        if op["op"] == "item.create":
            data = op["data"]
            if len(data["title"]) > 200:
                raise Refused("title_too_long", "titles are at most 200 characters")
            self.check_fields(data.get("fields"))
            work = data.get("work")
            if work is not None and work.get("owner_id") and work["owner_id"] not in self.members:
                raise Refused("unknown_owner", "not a member", choices=[{"key": m} for m in sorted(self.members)])
            existing = self.duplicates.get(data["title"])
            if work is not None and existing and existing not in (op.get("allow_duplicate_of") or []):
                raise Refused("likely_duplicate", "looks like the same wirk", choices=[{"key": existing}])
            item_id = "item_" + secrets.token_hex(16)
            snap = {"r": 1, "title": data["title"], "body": data.get("body", ""), "fields": dict(data.get("fields") or {}),
                    "work": work, "files": [self.uploads[u] for u in data.get("uploads", [])], "by": self.who()}
            if self.completed(snap) and not body.get("reason") and not snap["files"]:
                raise Refused("reason_required", "completing work needs its evidence")
            self.items[item_id] = {"revisions": [snap], "archived": False}
            if op.get("ref"):
                named[op["ref"]] = item_id
            return {"resource": "item", "id": item_id, "revision": 1}
        if op["op"] == "item.edit":
            item_id, patch = self.ident(op["id"], named), op["patch"]
            item = self.items[item_id]
            snap = copy.deepcopy(item["revisions"][-1])
            self.check_fields(patch.get("fields"))
            for key in ("title", "body"):
                if key in patch:
                    snap[key] = patch[key]
            for key, value in (patch.get("fields") or {}).items():
                if value is None:
                    snap["fields"].pop(key, None)
                else:
                    snap["fields"][key] = value
            if "work" in patch:
                snap["work"] = None if patch["work"] is None else {**(snap["work"] or {}), **patch["work"]}
                if snap["work"] and snap["work"].get("owner_id") and snap["work"]["owner_id"] not in self.members:
                    raise Refused("unknown_owner", "not a member")
            snap["files"] += [self.uploads[u] for u in patch.get("attach_uploads", [])]
            for swap in patch.get("replace_files", []):
                snap["files"] = [{**self.uploads[swap["upload_id"]], "id": f["id"]} if f["id"] == swap["file_id"] else f
                                 for f in snap["files"]]
            was = item["revisions"][-1]
            if self.completed(snap) and not self.completed(was):
                if not body.get("reason"):
                    raise Refused("reason_required", "completing work needs its evidence")
                for link in self.links.values():
                    if not link["removed"] and link["type"] == "requires" and link["from"] == item_id and \
                            not self.completed(self.items[link["to"]]["revisions"][-1]):
                        raise Refused("prerequisite_incomplete", "A prerequisite is unavailable or incomplete")
            snap["r"], snap["by"] = len(item["revisions"]) + 1, self.who()
            item["revisions"].append(snap)
            return {"resource": "item", "id": item_id, "revision": snap["r"]}
        if op["op"] in ("item.archive", "item.restore"):
            item_id = self.ident(op["id"], named)
            if op["op"] == "item.archive" and not body.get("reason"):
                raise Refused("reason_required", "Archiving needs a reason")
            item = self.items[item_id]
            item["archived"] = op["op"] == "item.archive"
            snap = {**copy.deepcopy(item["revisions"][-1]), "r": len(item["revisions"]) + 1, "by": self.who()}
            item["revisions"].append(snap)
            return {"resource": "item", "id": item_id, "revision": snap["r"]}
        if op["op"] == "link.create":
            data = op["data"]
            source, target = self.ident(data["from"], named), self.ident(data["to"], named)
            if data["type"] in ("requires", "contributes_to"):
                if self.reaches(target, source, data["type"]):
                    raise Refused("link_cycle", "This relationship would create a work cycle")
            if data["type"] == "requires" and self.completed(self.items[source]["revisions"][-1]):
                raise Refused("invalid_link", "Reopen completed work before adding a prerequisite")
            link_id = "link_" + secrets.token_hex(16)
            self.links[link_id] = {"type": data["type"], "from": source, "to": target, "by": self.who(), "removed": False}
            return {"resource": "link", "id": link_id, "revision": 1}
        if op["op"] == "link.remove":
            self.links[self.resolve(op["id"])[0]]["removed"] = True
            return {"resource": "link", "id": op["id"], "revision": 2}
        raise Refused("invalid_input", f"unknown op {op['op']}")

    def reaches(self, start, goal, kind):
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            if current == goal:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack += [l["to"] for l in self.links.values() if not l["removed"] and l["type"] == kind and l["from"] == current]
        return False

    # ---------------------------------------------------------------- files

    def files(self, body):
        if "upload" in body:
            declared = body["upload"]
            present = declared["sha256"] in self.objects
            return envelope({"present": present, **({} if present else {"put": {"url": f"https://store.test/{declared['sha256']}",
                                                                                 "headers": {}}})})
        if "confirm" in body:
            declared = body["confirm"]
            data = self.objects.get(declared["sha256"])
            if data is None:
                return refusal("upload_incomplete", "send the bytes first")
            assert hashlib.sha256(data).hexdigest() == declared["sha256"]
            upload_id = "upload_" + secrets.token_hex(16)
            self.uploads[upload_id] = {"id": "file_" + secrets.token_hex(16), "filename": declared["filename"],
                                       "sha256": declared["sha256"], "bytes": declared["bytes"],
                                       "description": declared.get("description"), "metadata": declared.get("metadata")}
            return envelope({"upload": {**self.uploads[upload_id], "id": upload_id}})
        return refusal("invalid_input", "files takes upload or confirm here")

    # ---------------------------------------------------------------- reading the result

    def mine(self, kind="work"):
        return {i: item for i, item in self.items.items() if self.card(i)[0]["kind"] == kind}

    def writes(self):
        return [body for route, body in self.requests if route.endswith("/write")]
