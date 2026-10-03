"""The Jira adapter (docs/plans/importers.md §6): a Jira Cloud site's issues read through REST v3 with a scoped read-only
API token. Site, login and token sit in one owner-only file, <config>/import/jira-key; requests go only to Atlassian's
API gateway for the site, reads only, and no email is kept. Restricted issues, comments and worklogs are skipped unless
the issue is named."""

from collections import Counter, defaultdict
import base64
import json
import os
import re
import stat
import time

import httpx

from ..client import config_dir
from . import adf, render
from .render import Stop

SOURCE, NOUN = "Jira", ("project", "projects")
GATEWAY = "https://api.atlassian.com/ex/jira/"
TRANSPORT = None  # how requests leave; tests answer through a fake
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+")
CLOUD = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SITE = re.compile(r"[a-z0-9][a-z0-9-]*\.atlassian\.net")
LABELS = {"parent": "Parent", "sub_issue": "Children", "blocked_by": "Is blocked by", "blocking": "Blocks",
          "duplicate_of": "Duplicates", "duplicated_by": "Is duplicated by", "related": "Links", "previously": "Previous keys"}
SELECTIONS = {"Workflow": "one", "Issue type": "one", "Priority": "one", "Project": "one", "Resolution": "one", "Label": "many",
              "Component": "many", "Release": "many", "Sprint": "many", "Story points": "one"}
STATES = {"new": "open", "indeterminate": "in_progress", "done": "completed"}
SELECT = {"select": "one", "radiobuttons": "one", "multiselect": "many", "multicheckboxes": "many", "cascadingselect": "one"}
STORY_POINTS, POINT_VALUES = {"Story Points", "Story point estimate"}, 20  # a field only with at most 20 values
NOT_KEPT = {"avatarUrls", "lastViewed", "isWatching", "hasVoted", "emailAddress"}  # per viewer, expiring or personal
# counts that churn without the issue changing; the worklogs in the archive carry the time spent
CHURN = {"comment", "worklog", "updated", "watches", "votes", "timeoriginalestimate", "timeestimate", "timespent",
         "aggregatetimeoriginalestimate", "aggregatetimeestimate", "aggregatetimespent", "timetracking", "progress",
         "aggregateprogress"}
CAP = 100 * 1024 * 1024


def clean(value):
    """The source's JSON without what moves per viewer or names an email."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if key not in NOT_KEPT}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def number(value) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def bare(found: dict | None) -> dict | None:
    """An issue as another issue embeds it, without its title: a title is never kept for an issue not imported here."""
    return found and {**found, "fields": {key: value for key, value in (found.get("fields") or {}).items() if key != "summary"}}


def placed(entry: dict) -> tuple:
    """A history entry's place: its instant, then Jira's order; one without both goes last and is withheld."""
    try:
        return 0, render.compact(entry["created"]), int(entry["id"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return 1, "", 0


class Jira:
    source, noun, unit, needs_selection = SOURCE, NOUN, "Jira requests", False
    # resolutions of done work that mean it was not done (§6); the map file's "cancelled" replaces them
    cancelled = {"Won't Do", "Duplicate", "Won't Fix", "Cannot Reproduce", "Declined"}

    def __init__(self, http=None, sleep=None, folder=None):
        self.http = http or httpx.Client(transport=TRANSPORT, follow_redirects=False, timeout=httpx.Timeout(60, connect=10))
        self.sleep, self.folder, self.points = sleep or time.sleep, folder or config_dir() / "import", 0

    # ---------------------------------------------------------------- talking to Jira

    def check(self, mapped: dict) -> None:
        self.cancelled = mapped.get("cancelled", self.cancelled)
        path = self.folder / "jira-key"
        if not isinstance(self.cancelled, (list, set)) or not all(isinstance(name, str) for name in self.cancelled):
            raise Stop("the map file's cancelled must be a list of resolution names", '"cancelled": ["Won\'t Do", "Duplicate"]')
        fix = (f"make a scoped API token with the read scopes (read:jira-work, read:jira-user) at id.atlassian.com, then save "
               f'{{"site": "<name>.atlassian.net", "email": "<your Atlassian login>", "token": "<the token>"}} where only you '
               f"can read it: run (umask 077 && cat > {path}), paste it, press Enter and then Ctrl-D")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise Stop(f"no Jira key at {path}", fix) from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Stop(f"{path} must be a regular file of yours that only you can read", f"chmod 600 {path}")
        try:
            key = json.loads(path.read_text())
            self.site, login, token = key["site"], key["email"], key["token"]
        except (ValueError, KeyError, TypeError):
            raise Stop(f"{path} must hold the site, the login and the token as JSON", fix) from None
        if not SITE.fullmatch(self.site or ""):
            raise Stop(f"{path} names {self.site!r}, not a Jira Cloud site such as acme.atlassian.net", fix)
        self.auth = "Basic " + base64.b64encode(f"{login}:{token}".encode()).decode()
        cloud = key.get("cloud_id")
        if not cloud:
            try:
                cloud = self.http.get(f"https://{self.site}/_edge/tenant_info").json().get("cloudId")
            except (httpx.HTTPError, ValueError, AttributeError):
                cloud = None
        if not isinstance(cloud, str) or not CLOUD.fullmatch(cloud):
            raise Stop(f"could not read the cloud ID of {self.site} from its tenant_info",
                       f'check the site in {path}, or add the site\'s "cloud_id" to it')
        self.base = GATEWAY + cloud
        self.call("GET", "/rest/api/3/myself")

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None, raw: bool = False):
        """One read through the site's gateway: waits out a 429, stops on a CAPTCHA lockout or a refused token."""
        assert method == "GET" or path in ("/rest/api/3/search/jql", "/rest/api/3/changelog/bulkfetch")  # reads only
        failures = 0
        while True:
            try:
                answer = self.http.request(method, self.base + path, params=params, json=body, headers={"Authorization": self.auth})
            except httpx.TransportError:
                answer = None
            if answer is not None and answer.status_code == 429:  # waiting is not a failure
                self.sleep(float(answer.headers.get("Retry-After") or 60))
                continue
            if answer is not None and "AUTHENTICATION_DENIED" in answer.headers.get("X-Seraph-LoginReason", ""):
                raise Stop("Jira asks for a CAPTCHA before it takes this login again", f"sign in at https://{self.site} in a "
                           "browser and answer it, then run the same command again")
            if answer is not None and answer.status_code in (401, 403):
                raise Stop("Jira refused the token", "make a new scoped read-only token and save it as the last one was")
            if answer is None or answer.status_code >= 500:
                failures += 1
                if failures > 3:
                    raise Stop("Jira did not answer; run the same command again")
                self.sleep(5 * 2 ** failures)
                continue
            if answer.status_code != 200:
                raise Stop(f"Jira refused a read: HTTP {answer.status_code} for {path}")
            self.points += 1
            return answer.content if raw else answer.json()

    def paged(self, path: str, key: str, size: int) -> list:
        found = []
        while True:
            answer = self.call("GET", path, {"startAt": len(found), "maxResults": size})
            found += answer[key]
            if not answer[key] or answer.get("isLast") or len(found) >= answer.get("total", float("inf")):
                return found

    # ---------------------------------------------------------------- what to read

    def context(self, selected: list) -> render.Context:
        return render.Context(SOURCE, frozenset(selected), NOUN, LABELS)

    def read(self, selection: list) -> tuple[render.Census, list]:
        projects = {p["key"]: p for p in self.paged("/rest/api/3/project/search", "values", 50)}
        self.fields = {f["id"]: f for f in self.call("GET", "/rest/api/3/field")}
        named = {word for word in selection if ISSUE_KEY.fullmatch(word)}
        wanted = [word for word in selection if word not in named] or ([] if named else sorted(projects))
        unknown = [word for word in wanted if word not in projects]
        if unknown:
            raise Stop(f"Jira has no project {', '.join(unknown)} that this token can browse",
                       f"the projects it can browse: {', '.join(sorted(projects))}")
        clauses = [f"project in ({', '.join(json.dumps(k) for k in wanted)})"] * bool(wanted) + \
                  [f"key in ({', '.join(json.dumps(k) for k in sorted(named))})"] * bool(named)
        issues, token = [], None
        while True:
            answer = self.call("POST", "/rest/api/3/search/jql", body={"jql": " OR ".join(clauses) + " ORDER BY id ASC",
                                                                      "fields": ["*all"], "maxResults": 50,
                                                                      **({"nextPageToken": token} if token else {})})
            issues += answer["issues"]
            token = answer.get("nextPageToken")
            if not token:
                break
        restricted = [node for node in issues if node["fields"].get("security") and node["key"] not in named]
        issues = [node for node in issues if node not in restricted]
        self.skipped = {node["key"] for node in restricted} | {node["id"] for node in restricted}  # never named around them
        self.selected = set(wanted) | {key.rsplit("-", 1)[0] for key in named}
        self.shown_projects, self.outside, self.kept_files = {projects[k]["id"] for k in self.selected if k in projects}, set(), set()
        histories = self.changelogs([node["id"] for node in issues])
        children = defaultdict(list)
        for node in issues:
            if node["fields"].get("parent"):
                children[node["fields"]["parent"]["id"]].append(node)
        self.counts = Counter()
        records = [self.record(node, histories.get(node["id"], []), children[node["id"]], node["key"] in named) for node in issues]
        points = {value for record in records for value in record.fields.get("Story points", [])}
        if len(points) > POINT_VALUES:  # too many values for a field: header lines only
            for record in records:
                record.fields.pop("Story points", None)
        census = render.Census(selected=sorted(self.selected), issues=len(records), points=self.points,
                               skipped=[(node["key"], None) for node in restricted],
                               named_private=[(node["key"], "restricted", 1) for node in issues
                                              if node["fields"].get("security") and node["key"] in named])
        census.comments = sum(len(record.comments) for record in records)
        if self.counts:
            census.notes.append("ADF: " + " · ".join(f"{name} {count}" for name, count in sorted(self.counts.items())))
        return census, records

    def changelogs(self, ids: list) -> dict:
        """Every issue's history, oldest first, from the bulk fetch (up to 1,000 issues a request)."""
        found = defaultdict(list)
        for start in range(0, len(ids), 1000):
            token = None
            while True:
                answer = self.call("POST", "/rest/api/3/changelog/bulkfetch",
                                   body={"issueIdsOrKeys": ids[start:start + 1000], "maxResults": 1000,
                                         **({"nextPageToken": token} if token else {})})
                for entry in answer.get("issueChangeLogs", []):
                    found[entry["issueId"]] += entry.get("changeHistories", [])
                token = answer.get("nextPageToken")
                if not token:
                    break
        return found

    # ---------------------------------------------------------------- one issue as a record

    def reference(self, found: dict, note: str = "") -> render.Ref:
        """Jira has no public projects: shown when its project is selected, never when it is a skipped restricted issue."""
        scope = "" if {found["id"], found["key"]} & self.skipped else found["key"].rsplit("-", 1)[0]
        return render.Ref(found["key"], scope, False, found["id"], note)

    @staticmethod
    def name(user: dict | None) -> str:
        if not user:
            return "nobody"
        kind = user.get("accountType")
        return user.get("displayName", "someone") + (f" ({kind})" if kind in ("app", "customer") else "")

    def record(self, node: dict, history: list, children: list, named: bool) -> render.Record:
        f, key = node["fields"], node["key"]
        status, resolution = f["status"] or {}, (f.get("resolution") or {}).get("name")
        category = (status.get("statusCategory") or {}).get("key", "new")
        state = "cancelled" if category == "done" and resolution in self.cancelled else STATES.get(category, "open")
        comments, worklogs = self.every(node, "comment", "comments", 100), self.every(node, "worklog", "worklogs", 5000)
        hidden = [sum(1 for c in found if c.get("visibility")) * (not named) for found in (comments, worklogs)]
        comments, worklogs = ([c for c in found if named or not c.get("visibility")] for found in (comments, worklogs))
        attachments = f.get("attachment") or []
        if any(hidden):  # what a comment or worklog left out may hold: only files the text still shown references stay
            shown = {name for text in [*f.values(), *(c.get("body") for c in comments), *(w.get("comment") for w in worklogs)]
                     for name in adf.names(text)}
            attachments = [a for a in attachments if a["filename"] in shown]
        files, self.kept_files = {a["filename"] for a in attachments}, self.kept_files | {a["id"] for a in attachments}
        fields, facts, sections = self.fields_and_facts(f)
        links = self.call("GET", f"/rest/api/3/issue/{node['id']}/remotelink")
        web = [" ".join(((link.get("object") or {}).get(part) or "") for part in ("title", "url")) for link in links]
        gone = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in zip(
            hidden + [len(f.get("attachment") or []) - len(attachments)], ("restricted comment", "restricted worklog", "attachment")) if n]
        facts += [f"Web links: {', '.join(web)}"] * bool(web) + [f"Not imported: {', '.join(gone)}"] * bool(gone)
        ordered = sorted(history, key=placed)
        moves = [next((i for i in e.get("items", []) if i.get("field") == "project"), None) for e in ordered]
        where = next((m.get("from") for m in moves if m is not None), f["project"]["id"])  # where it began; unknown fails closed
        lost = any(placed(e)[0] and m is not None for e, m in zip(ordered, moves))  # a move at an unknown time: none is shown
        for entry, move in zip(ordered, moves):  # a change made in, or moving into or out of, a project not selected names it (§3.9)
            before, where = (move.get("from"), move.get("to")) if move is not None else (where, where)
            self.outside |= {entry.get("id")} if lost or placed(entry)[0] or {before, where} - self.shown_projects else set()
        raw = {"issue": clean({**node, "fields": self.archived(f, attachments)}), "worklogs": clean(worklogs),
               "changelog": clean(history), "remotelinks": clean(links)}
        body = "\n\n".join(part for part in [adf.markdown(f.get("description"), self.counts, files), *sections] if part)
        assignee = f.get("assignee")
        return render.Record(
            SOURCE, "issue", node["id"], key, f"https://{self.site}/browse/{key}", f["updated"], f.get("summary") or "", body, state,
            f"is {status.get('name')} there (resolution {resolution or 'none'}, resolved {f.get('resolutiondate')})" if category == "done" else None,
            [f"Reported by {self.name(f.get('reporter'))} {f['created']}"], facts,
            [(assignee["accountId"], self.name(assignee))] if assignee else [], fields,
            f"{f['duedate']}T23:59:59Z" if f.get("duedate") else None, self.relations(f, history, children),
            [render.Comment(self.name(c.get("author")), c["created"], c.get("updated", c["created"]) != c["created"],
                            adf.markdown(c.get("body"), self.counts, files), "", None, c.get("updated", c["created"]),
                            ("internal",) if c.get("jsdPublic") is False else ()) for c in comments],
            [render.Attachment(render.attachment_name(SOURCE, a["id"], a["filename"]), f"{self.base}/rest/api/3/attachment/content/{a['id']}",
                               False) for a in attachments],
            raw, clean(comments))

    @staticmethod
    def archived(f: dict, attachments: list) -> dict:
        """The issue's fields for its archive: without what churns, the files left out, and the titles of the issues it
        embeds."""
        kept = {k: v for k, v in f.items() if k not in CHURN}
        return {**kept, "attachment": attachments, "parent": bare(f.get("parent")), "subtasks": [bare(s) for s in f.get("subtasks") or []],
                "issuelinks": [{**link, **{side: bare(link[side]) for side in ("inwardIssue", "outwardIssue") if side in link}}
                               for link in f.get("issuelinks") or []]}

    def relations(self, f: dict, history: list, children: list) -> list:
        """The parent, the children, each link as seen from this issue, and the keys it had before a move."""
        found = [("parent", self.reference(f["parent"]))] if f.get("parent") else []
        found += [("sub_issue", self.reference(child)) for child in {c["id"]: c for c in (f.get("subtasks") or []) + children}.values()]
        for link in f.get("issuelinks") or []:  # on this issue, outwardIssue reads "this <outward> other"
            outward, kind = "outwardIssue" in link, link["type"]
            relation = {("Blocks", True): "blocking", ("Blocks", False): "blocked_by", ("Duplicate", True): "duplicate_of",
                        ("Duplicate", False): "duplicated_by"}.get((kind["name"], outward), "related")
            note = kind["outward" if outward else "inward"] if relation == "related" else ""
            found.append((relation, self.reference(link["outwardIssue" if outward else "inwardIssue"], note)))
        return found + [("previously", render.Ref(item["fromString"], item["fromString"].rsplit("-", 1)[0], False))
                        for entry in history for item in entry.get("items", []) if item.get("field") == "Key" and item.get("fromString")]

    def every(self, node: dict, field: str, key: str, size: int) -> list:
        """An issue's comments or worklogs: those that came with it, or all of them when there are more."""
        embedded = node["fields"].get(field) or {}
        if embedded.get("total", 0) <= len(embedded.get(key) or []):
            return embedded.get(key) or []
        return self.paged(f"/rest/api/3/issue/{node['id']}/{field}", key, size)

    def fields_and_facts(self, f: dict) -> tuple[dict, list, list]:
        """Field values by name (small current sets only become fields, §6), header facts, and paragraph-field sections."""
        sprints, points, custom, sections, fields = [], None, [], [], defaultdict(list)
        for ident, definition in self.fields.items():
            value, schema = f.get(ident), definition.get("schema") or {}
            if value in (None, [], "") or not definition.get("custom"):
                continue
            kind, name = (schema.get("custom") or "").rsplit(":", 1)[-1], definition["name"]
            if kind == "gh-sprint":
                sprints = value
            elif name in STORY_POINTS:
                points = number(value)
            elif kind == "textarea":
                sections.append(f"## {name}\n\n{adf.markdown(value, self.counts) if isinstance(value, dict) else value}")
            else:
                values = self.values(kind, value)
                custom.append(f"{name}: {', '.join(values)}")
                if kind in SELECT:
                    fields[name] = values
        project, resolution = f["project"], (f.get("resolution") or {}).get("name")
        fields.update({"Workflow": [f["status"]["name"]], "Issue type": [f["issuetype"]["name"]], "Project": [project["key"]],
                       "Priority": [(f.get("priority") or {}).get("name")], "Resolution": [resolution], "Label": f.get("labels") or [],
                       "Component": [c["name"] for c in f.get("components") or []],
                       "Release": [v["name"] for v in f.get("fixVersions") or [] if not v.get("released") and not v.get("archived")],
                       "Sprint": [s["name"] for s in sprints if s.get("state") in ("active", "future")], "Story points": [points]})
        names = lambda values: ", ".join(v["name"] for v in values or [])
        facts = [" · ".join([f"Type: {f['issuetype']['name']}", f"Status: {f['status']['name']} ({f['status'].get('statusCategory', {}).get('key')})",
                             f"Resolution: {resolution or 'none'}", f"Priority: {(f.get('priority') or {}).get('name') or 'none'}",
                             f"Project: {project['key']} ({project.get('name')})"]),
                 " · ".join(part for part in [f"Labels: {', '.join(f['labels'])}" if f.get("labels") else "",
                                              f"Components: {names(f.get('components'))}" if f.get("components") else "",
                                              f"Fix versions: {names(f.get('fixVersions'))}" if f.get("fixVersions") else "",
                                              f"Affects versions: {names(f.get('versions'))}" if f.get("versions") else ""] if part),
                 " · ".join(part for part in ["Sprints: " + ", ".join(f"{s['name']} ({s.get('state')})" for s in sprints) if sprints else "",
                                              f"Story points: {points}" if points else "", f"Due: {f['duedate']}" if f.get("duedate") else ""] if part),
                 " · ".join(custom)]
        return ({name: values for name, values in fields.items() if values and None not in values}, [fact for fact in facts if fact],
                sections)

    @staticmethod
    def values(kind: str, value) -> list:
        """A custom field's value as text: options, a cascade as Parent › Child, people by name, the rest as given."""
        def one(item):
            if isinstance(item, dict):
                if "child" in item:
                    return f"{item.get('value')} › {item['child'].get('value')}"
                return str(item.get("value") or item.get("displayName") or item.get("name") or json.dumps(item, sort_keys=True))
            return number(item) if isinstance(item, float) else str(item)
        return [one(item) for item in (value if isinstance(value, list) else [value])]

    # ---------------------------------------------------------------- what the importer needs besides records

    def selections(self, records: list) -> dict:
        found = dict(SELECTIONS)
        for definition in self.fields.values():
            kind = ((definition.get("schema") or {}).get("custom") or "").rsplit(":", 1)[-1]
            if kind in SELECT:
                found[definition["name"]] = SELECT[kind]
        return found

    def withheld(self, ctx: render.Context):
        """Whether a node of the raw JSON is, or names, an issue in a project not selected: Jira has no public projects."""
        def hidden(key: str) -> bool:
            return key in self.skipped or not ctx.shown(render.Ref("", key.rsplit("-", 1)[0], False))

        def check(node: dict) -> bool:
            if isinstance(node.get("key"), str) and ISSUE_KEY.fullmatch(node["key"]):
                return hidden(node["key"])
            if "items" in node and node.get("id") in self.outside:  # the whole history entry, as recorded there
                return True
            if node.get("field") == "Attachment":  # a file not kept is not named in its history either
                return (node.get("to") or node.get("from")) not in self.kept_files
            return "field" in node and any(hidden(key) for value in node.values() if isinstance(value, str)
                                           for key in ISSUE_KEY.findall(value))
        return check

    def download(self, attachment) -> bytes | None:
        """The bytes, streamed by Jira itself (redirect=false), so the token never follows a redirect (§3.7)."""
        if not attachment.url.startswith(self.base + "/rest/api/3/attachment/content/"):
            return None
        try:
            data = self.call("GET", attachment.url.removeprefix(self.base), {"redirect": "false"}, raw=True)
        except Stop:
            return None
        return data if len(data) <= CAP else None
