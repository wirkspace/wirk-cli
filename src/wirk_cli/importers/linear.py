"""The Linear adapter (docs/plans/importers.md §5): issues read through Linear's GraphQL API with a personal key restricted
to Read, saved owner-only at <config>/import/linear-key. Queries only; the key goes only to Linear; no email is asked for."""

from collections import Counter, defaultdict
from datetime import date, datetime, time as day_time, timezone
import os
import re
import stat
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..client import config_dir
from . import render
from .render import Stop

SOURCE, NOUN = "Linear", ("team", "teams")
API = "https://api.linear.app/graphql"
FILE_HOSTS = {"uploads.linear.app"}  # where uploads are read, with the key; anything else stays a link (§3.7)
TRANSPORT = None  # how requests leave; tests answer through a fake
KEY = re.compile(r"lin_api_[A-Za-z0-9]{20,}")
LABELS = {"parent": "Parent", "sub_issue": "Sub-issues", "blocked_by": "Blocked by", "blocking": "Blocking",
          "duplicate_of": "Duplicate of", "duplicated_by": "Duplicates", "related": "Related", "project": "Project",
          "milestone": "Milestone", "of": "Belongs to", "initiative": "Initiatives", "includes": "Projects",
          "parent_initiative": "Parent initiative", "sub_initiative": "Sub-initiatives",
          "previously": "Previously"}
KINDS, DOCS = ("issue", "project", "milestone", "initiative", "document", "update"), frozenset({"initiative", "document", "update"})
SELECTIONS = {"Team": "many", "Workflow": "one", "Priority": "one", "Estimate": "one", "Cycle": "one", "Label": "many",
              "Health": "one"}
STATES = {"triage": "open", "backlog": "open", "unstarted": "open", "started": "in_progress", "completed": "completed",
          "canceled": "cancelled", "duplicate": "cancelled", "planned": "open", "paused": "open"}  # issue and project types
HEALTH = {"onTrack": "On track", "atRisk": "At risk", "offTrack": "Off track"}
PRIORITIES = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
SIZES = {1: "XS", 2: "S", 3: "M", 5: "L", 8: "XL", 13: "XXL", 21: "XXXL"}  # Linear keeps T-shirt sizes as these numbers
RELATIONS = {"blocks": ("blocking", "blocked_by"), "duplicate": ("duplicate_of", "duplicated_by")}  # else related both ways
UPLOAD = re.compile(r"https://uploads\.linear\.app/[^\s)\]\"'<>]+")
IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
PAGE, LIST, CAP = 25, 250, 100 * 1024 * 1024

# ---------------------------------------------------------------- the queries

REF = "id identifier team { id key private }"
# An issue page of 25 asks about 3,600 of the 10,000 points one query may cost (by Linear's published rules: 0.1 a
# property, 1 an object, children times their page size); the real cost is read from X-Complexity on a real workspace.
NESTED = {"labels": ("first: 20", "id"), "attachments": ("first: 20", "id title subtitle url sourceType"),
          "history": ("first: 20", "id createdAt actorId fromStateId toStateId fromAssigneeId toAssigneeId fromPriority "
                      "toPriority fromEstimate toEstimate fromDueDate toDueDate fromCycleId toCycleId fromProjectId toProjectId "
                      "fromParentId toParentId fromTeamId toTeamId fromTitle toTitle addedLabelIds removedLabelIds archived "
                      "trashed autoArchived autoClosed updatedDescription")}
ISSUE = ("id identifier number previousIdentifiers url title description priority estimate dueDate createdAt updatedAt "
         "startedAt completedAt canceledAt archivedAt trashed reactionData state { id } team { id } parent { " + REF + " } "
         "project { id } projectMilestone { id } cycle { id } assignee { id } creator { id } "
         + " ".join(f"{name}({args}) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {nodes} }} }}"
                    for name, (args, nodes) in NESTED.items()))
COMMENT = ("id body createdAt editedAt quotedText resolvedAt reactionData parent { id } issue { id } project { id } "
           "initiative { id } projectUpdate { id } initiativeUpdate { id } documentContent { document { id } } user { id } "
           "resolvingUser { id } botActor { name } externalUser { name }")
UPDATE = "id body health url createdAt editedAt user { id } reactionData"
LISTS = {  # read whole at the start: (arguments, what each node holds)
    "teams": ("", "id key name private timezone issueEstimationType"),
    "workflowStates": ("includeArchived: true", "id name type position team { id }"),
    "users": ("includeDisabled: true", "id name displayName"),
    "issueLabels": ("includeArchived: true", "id name isGroup parent { id }"),
    "cycles": ("includeArchived: true", "id number name team { id }"),
    "projects": ("includeArchived: true", "id name description content url status { id name type } priority lead { id } "
                 "teams(first: 20) { nodes { id } } targetDate targetDateResolution health createdAt updatedAt completedAt "
                 "canceledAt archivedAt trashed"),
    "projectMilestones": ("", "id name description targetDate status project { id }"),
    "issueRelations": ("includeArchived: true", f"id type issue {{ {REF} }} relatedIssue {{ {REF} }}"),
    "projectRelations": ("", "id type project { id } relatedProject { id } projectMilestone { id } relatedProjectMilestone { id }"),
    "initiatives": ("includeArchived: true", "id name description content url status owner { id } targetDate health "
                    "parentInitiative { id } createdAt updatedAt archivedAt trashed"),
    "initiativeToProjects": ("", "id initiative { id } project { id }"),
    "documents": ("includeArchived: true", "id title content url creator { id } createdAt updatedAt project { id } "
                  "initiative { id } issue { id } archivedAt trashed"),
    "projectUpdates": ("", UPDATE + " project { id }"),
    "initiativeUpdates": ("", UPDATE + " initiative { id }"),
}
PARTIAL = {"issueRelations"}  # lists whose answer may leave out what the key cannot read, with an error saying so
IN_TEAMS = "team: { id: { in: $teams } }"


def due_at(day: str, zone: str | None) -> str:
    """The last second of a due day in the team's time zone, in UTC (§5)."""
    try:
        place = ZoneInfo(zone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        place = timezone.utc
    end = datetime.combine(date.fromisoformat(day), day_time(23, 59, 59), place)
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reactions(data) -> str:
    counts = Counter()
    for entry in data or []:
        many = entry.get("reactions") or entry.get("userIds") or entry.get("users") or []  # the shapes Linear's JSON may take
        counts[entry.get("emoji") or "?"] += entry.get("count") or len(many) or 1
    return " · ".join(f"{emoji} {count}" for emoji, count in counts.items())


class Linear:
    source, noun, unit, needs_selection = SOURCE, NOUN, "complexity points", False

    def __init__(self, http=None, sleep=None, folder=None):
        self.http = http or httpx.Client(transport=TRANSPORT, follow_redirects=False, timeout=httpx.Timeout(60, connect=10))
        self.sleep, self.folder, self.points, self.key = sleep or time.sleep, folder or config_dir() / "import", 0, None

    # ---------------------------------------------------------------- talking to Linear

    def check(self) -> None:
        path = self.folder / "linear-key"
        fix = ("make a personal API key restricted to Read in Linear (Settings, Account, Security & access), then save it "
               f"where only you can read it: run (umask 077 && cat > {path}), paste the key, press Enter and then Ctrl-D")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise Stop(f"no Linear key at {path}", fix) from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Stop(f"{path} must be a regular file of yours that only you can read", f"chmod 600 {path}")
        key = path.read_text(errors="replace").strip()
        if not KEY.fullmatch(key):
            raise Stop(f"{path} does not hold a Linear API key", fix)
        self.key = key
        self.graphql("query { viewer { id } organization { urlKey } }", {})

    def graphql(self, query: str, variables: dict, partial: bool = False) -> dict:
        """The answer's data. With `partial`, an answer that left out what the key cannot read is taken as it is."""
        assert query.lstrip().startswith("query")  # the importer only reads
        failures = 0
        while True:
            try:
                answer = self.http.post(API, json={"query": query, "variables": variables}, headers={"Authorization": self.key})
                body = answer.json()
            except (httpx.TransportError, ValueError):
                answer, body = None, {}
            errors = body.get("errors") or []
            codes = {(error.get("extensions") or {}).get("code") for error in errors}
            whole = not (partial and isinstance(body.get("data"), dict))
            if "RATELIMITED" in codes:  # waiting for the reset is not a failure
                self.sleep(self.until(answer.headers))
                continue
            if answer is not None and (answer.status_code in (401, 403) or whole and codes & {"AUTHENTICATION_ERROR", "FORBIDDEN"}):
                raise Stop("Linear refused the key", "make a new key restricted to Read, and save it as the last one was")
            if answer is None or answer.status_code >= 500:
                failures += 1
                if failures > 3:
                    raise Stop("Linear did not answer; run the same command again")
                self.sleep(5 * 2 ** failures)
                continue
            if answer.status_code != 200 or errors and whole or "data" not in body:
                raise Stop(f"Linear refused a read: {(errors or [{}])[0].get('message') or f'HTTP {answer.status_code}'}")
            self.points += int(answer.headers.get("X-Complexity") or 0)
            if (int(answer.headers.get("X-RateLimit-Requests-Remaining") or 100) < 5
                    or int(answer.headers.get("X-RateLimit-Complexity-Remaining") or 10 ** 6) < 10_000):
                self.sleep(self.until(answer.headers))  # pace below the hour's budget, so a long read never fails halfway
            return body["data"]

    @staticmethod
    def until(headers) -> float:
        """Seconds until the budget resets; Linear gives the reset in milliseconds since the epoch."""
        resets = [int(headers.get(name) or 0) for name in ("X-RateLimit-Requests-Reset", "X-RateLimit-Complexity-Reset")]
        return max(1.0, max(resets) / 1000 - time.time()) + 1 if any(resets) else 60.0

    def every(self, name: str, args: str, nodes: str, size: int = LIST, teams: list | None = None) -> list:
        """A root list read to its end, a page at a time."""
        query = (f"query($first: Int!, $after: String{', $teams: [ID!]' if teams is not None else ''}) {{ {name}(first: $first, "
                 f"after: $after{', ' + args if args else ''}) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {nodes} }} }} }}")
        found, after = [], None
        while True:
            answer = self.graphql(query, {"first": size, "after": after, **({"teams": teams} if teams is not None else {})},
                                  name in PARTIAL)[name]
            found += answer["nodes"]
            if not answer["pageInfo"]["hasNextPage"]:
                return found
            after = answer["pageInfo"]["endCursor"]

    def rest_of(self, node: dict, name: str) -> None:
        """An issue's nested connection longer than its first page, read to the end."""
        query = (f"query($id: String!, $first: Int!, $after: String) {{ issue(id: $id) {{ {name}(first: $first, after: $after) "
                 f"{{ pageInfo {{ hasNextPage endCursor }} nodes {{ {NESTED[name][1]} }} }} }} }}")
        found = node[name]
        while found["pageInfo"]["hasNextPage"]:
            more = self.graphql(query, {"id": node["id"], "first": 100, "after": found["pageInfo"]["endCursor"]})["issue"][name]
            found = {"pageInfo": more["pageInfo"], "nodes": found["nodes"] + more["nodes"]}
        node[name] = found

    # ---------------------------------------------------------------- what to read

    def context(self, selected: list) -> render.Context:
        return render.Context(SOURCE, frozenset(selected), NOUN, LABELS, KINDS, DOCS)

    def read(self, selection: list) -> tuple[render.Census, list]:
        lists = {name: self.every(name, args, nodes) for name, (args, nodes) in LISTS.items()}
        self.teams, self.states, self.users, self.labels, self.cycles, self.projects, self.milestones, self.initiatives = (
            {node["id"]: node for node in lists[name]} for name in
            ("teams", "workflowStates", "users", "issueLabels", "cycles", "projects", "projectMilestones", "initiatives"))
        self.keys = keys = {team["key"]: team for team in lists["teams"]}
        unknown = [word for word in selection if word not in keys]
        if unknown:
            raise Stop(f"Linear has no team {', '.join(unknown)} that this key can see",
                       f"the teams it can see: {', '.join(sorted(keys))}")
        chosen = [keys[word] for word in selection] or [team for team in lists["teams"] if not team["private"]]
        self.chosen = {team["id"] for team in chosen}
        issues = self.every("issues", f"includeArchived: true, filter: {{ {IN_TEAMS} }}", ISSUE, PAGE, sorted(self.chosen))
        for node in issues:
            for name in NESTED:
                self.rest_of(node, name)
        comments = defaultdict(list)
        for found in self.every("comments", f"includeArchived: true, filter: {{ issue: {{ {IN_TEAMS} }} }}", COMMENT, 100,
                                sorted(self.chosen)) + self.every("comments", "includeArchived: true, filter: { issue: { null: true } }",
                                                                  COMMENT, 100):
            parent = found["issue"] or found["projectUpdate"] or found["initiativeUpdate"] or \
                (found["documentContent"] or {}).get("document") or found["project"] or found["initiative"]
            comments[(parent or {}).get("id")].append(found)
        read, related, children = {node["id"] for node in issues}, defaultdict(list), defaultdict(list)
        readable = [r for r in lists["issueRelations"] if r and r["issue"] and r["relatedIssue"]]
        for relation in readable:
            ends, kinds = (relation["issue"], relation["relatedIssue"]), RELATIONS.get(relation["type"], ("related", "related"))
            for this, other, kind in ((ends[0], ends[1], kinds[0]), (ends[1], ends[0], kinds[1])):
                if this["id"] in read:
                    related[this["id"]].append((kind, self.reference(other), relation))
        for node in sorted(issues, key=lambda node: node["number"]):
            if node["parent"]:
                children[node["parent"]["id"]].append(node)
        records = [self.record(node, comments[node["id"]], related[node["id"]], children[node["id"]]) for node in issues]
        others = self.others(lists, issues, comments)
        census = render.Census(selected=sorted(team["key"] for team in chosen), issues=len(records), points=self.points)
        census.skipped = [(team["key"], None) for team in sorted(lists["teams"], key=lambda t: t["key"])
                          if team["private"] and team not in chosen]
        census.named_private = [(team["key"], "private", sum(node["team"]["id"] == team["id"] for node in issues))
                                for team in chosen if team["private"]]
        records += others
        census.comments = sum(len(record.comments) for record in records)
        if len(readable) < len(lists["issueRelations"]):
            census.notes.append(f"relations this key cannot read, left out: {len(lists['issueRelations']) - len(readable)}")
        census.external_images = sum(1 for record in records for text in [record.body] + [c.body for c in record.comments]
                                     for url in IMAGE.findall(text) if not UPLOAD.match(url))
        counts = Counter(record.kind for record in others)
        census.notes.append("also read: " + " · ".join(f"{counts[kind]} {kind}s" for kind in KINDS[1:]))
        return census, records

    # ---------------------------------------------------------------- projects, milestones, initiatives, documents and updates

    def others(self, lists: dict, issues: list, comments: dict) -> list:
        """The objects around the issues: a selected team's projects with their milestones and relations, the initiatives
        that hold them, and the documents and updates of what is imported (§5)."""
        projects = [p for p in lists["projects"] if {t["id"] for t in p["teams"]["nodes"]} & self.chosen]
        ids = {p["id"] for p in projects}
        milestones = [m for m in lists["projectMilestones"] if m["project"]["id"] in ids]
        holds = defaultdict(list)
        for link in lists["initiativeToProjects"]:
            if link["project"]["id"] in self.projects:
                holds[link["initiative"]["id"]].append(self.projects[link["project"]["id"]])
        initiatives = [i for i in lists["initiatives"] if not holds[i["id"]] or {p["id"] for p in holds[i["id"]]} & ids]
        ids |= {node["id"] for node in milestones + initiatives}
        related = defaultdict(list)
        for found in lists["projectRelations"]:  # anchored to a milestone when it names one
            blocker, blocked = found["projectMilestone"] or found["project"], found["relatedProjectMilestone"] or found["relatedProject"]
            kinds = ("blocking", "blocked_by") if found["type"] == "blocks" else ("related", "related")
            related[blocker["id"]].append((kinds[0], self.part(blocked["id"])))
            related[blocked["id"]].append((kinds[1], self.part(blocker["id"])))
        for initiative in initiatives:
            for project in sorted(holds[initiative["id"]], key=lambda p: p["name"]):
                related[project["id"]].append(("initiative", self.part(initiative["id"])))
                related[initiative["id"]].append(("includes", self.part(project["id"])))
            parent = (initiative["parentInitiative"] or {}).get("id")
            if parent in ids:
                related[initiative["id"]].append(("parent_initiative", self.part(parent)))
                related[parent].append(("sub_initiative", self.part(initiative["id"])))
        records = [self.project(node, comments, related) for node in projects]
        for node in milestones:
            project = self.projects[node["project"]["id"]]
            records.append(self.build("milestone", node, self.part(node["id"]).key, node["description"] or "", project["url"],
                                      node.get("updatedAt") or project["updatedAt"], state="completed" if node["status"] == "done" else "open",
                                      facts=[f"Target: {node['targetDate']}"] if node["targetDate"] else [],
                                      due=due_at(node["targetDate"], "UTC") if node["targetDate"] else None,
                                      relations=[("project", self.part(project["id"]))] + related[node["id"]]))
        records += [self.build("initiative", node, node["name"], self.text(node), node["url"], node["updatedAt"],
                               facts=[" · ".join([f"Status: {node['status']}",
                                                  *([f"Health: {HEALTH[node['health']]}"] if node["health"] in HEALTH else []),
                                                  *([f"Target: {node['targetDate']}"] if node["targetDate"] else []),
                                                  *([f"Owner: {self.person(node['owner'])}"] if node["owner"] else [])])],
                               relations=related[node["id"]], comments=comments[node["id"]]) for node in initiatives]
        parents = {**{node["id"]: self.part(node["id"]) for node in projects + initiatives},
                   **{node["id"]: self.reference(node) for node in issues}}
        records += [self.build("document", node, node["title"], node["content"] or "", node["url"], node["updatedAt"],
                               opened=[f"Written by {self.person(node['creator'])} {node['createdAt']}"],
                               relations=[("of", parents[parent])], comments=comments[node["id"]])
                    for node in lists["documents"]
                    for parent in [((node["project"] or node["initiative"] or node["issue"]) or {}).get("id")] if parent in parents]
        for name, kind in (("projectUpdates", "project"), ("initiativeUpdates", "initiative")):
            records += [self.build("update", node, f"{parents[node[kind]['id']].key} update {node['createdAt'][:10]}", node["body"] or "",
                                   node["url"], node["editedAt"] or node["createdAt"],
                                   opened=[" · ".join([f"Posted by {self.person(node['user'])} {node['createdAt']}",
                                                       *(["edited"] if node["editedAt"] else [])])],
                                   facts=[f"Health: {HEALTH[node['health']]}" if node["health"] in HEALTH else "",
                                          f"Reactions: {reactions(node['reactionData'])}" if reactions(node["reactionData"]) else ""],
                                   relations=[("of", parents[node[kind]["id"]])], comments=comments[node["id"]])
                        for node in lists[name] if node[kind]["id"] in parents]
        return records

    def part(self, ident: str) -> render.Ref:
        """A project, milestone or initiative as a reference: shown when one of its teams is selected or public."""
        if ident in self.milestones:
            milestone = self.milestones[ident]
            project = self.part(milestone["project"]["id"])
            return render.Ref(f"{project.key} · {milestone['name']}", project.scope, project.public, ident)
        if ident in self.initiatives:  # initiatives belong to the whole workspace
            return render.Ref(self.initiatives[ident]["name"], "", True, ident)
        teams = [self.teams[t["id"]] for t in self.projects[ident]["teams"]["nodes"] if t["id"] in self.teams]
        scope = next((t["key"] for t in teams if t["id"] in self.chosen), teams[0]["key"] if teams else "")
        return render.Ref(self.projects[ident]["name"], scope, any(not t["private"] for t in teams), ident)

    def project(self, node: dict, comments: dict, related: dict) -> render.Record:
        teams = [self.teams[t["id"]] for t in node["teams"]["nodes"] if t["id"] in self.teams]
        shown = [t["key"] for t in teams if t["id"] in self.chosen or not t["private"]]  # a private team not named stays unnamed
        status, priority, health = node["status"] or {}, PRIORITIES.get(node["priority"]), HEALTH.get(node["health"])
        target, day = self.target(node["targetDate"], node["targetDateResolution"])
        return self.build(
            "project", node, node["name"], self.text(node), node["url"], node["updatedAt"], state=STATES.get(status.get("type"), "open"),
            opened=[" · ".join([f"Created {node['createdAt']}", *(f"{word} {node[word + 'At']}" for word in ("completed", "canceled")
                                                                if node.get(word + "At"))])],
            facts=[" · ".join([f"Teams: {', '.join(shown)}", f"Status: {status.get('name')} ({status.get('type')})",
                               *([f"Priority: {priority}"] if priority else []), *([f"Health: {health}"] if health else [])]),
                   *([f"Target: {target}"] if target else [])],
            lead=node["lead"], due=due_at(day, "UTC") if day else None, relations=related[node["id"]], comments=comments[node["id"]],
            fields={"Team": shown, "Workflow": [status.get("name")], "Priority": [priority], "Health": [health]},
            closed=next((f"{word} at {node[word + 'At']}" for word in ("completed", "canceled") if node.get(word + "At")), None))

    @staticmethod
    def target(day: str | None, resolution: str | None) -> tuple[str | None, str | None]:
        """A target as Linear shows it, and the last day it covers: a month, quarter, half or year ends with its period."""
        if not day:
            return None, None
        when = date.fromisoformat(day)
        months = {"month": 1, "quarter": 3, "halfYear": 6, "year": 12}.get(resolution)
        if not months:
            return day, day
        first = (when.month - 1) // months * months  # months before the period starts
        end = date(when.year + (first + months) // 12, (first + months) % 12 + 1, 1).toordinal() - 1
        shown = {"month": f"{when:%Y-%m}", "quarter": f"{when.year} Q{first // 3 + 1}", "halfYear": f"{when.year} H{first // 6 + 1}",
                 "year": f"{when.year}"}[resolution]
        return shown, date.fromordinal(end).isoformat()

    @staticmethod
    def text(node: dict) -> str:
        return "\n\n".join(part for part in (node["description"], node["content"]) if part)

    def build(self, kind: str, node: dict, key: str, body: str, url: str, version: str, *, state="open", closed=None, opened=(),
              facts=(), lead=None, fields=None, due=None, relations=(), comments=()) -> render.Record:
        """Any object but an issue, as a record."""
        ordered, lead = self.thread(list(comments)), self.users.get((lead or {}).get("id"))
        archived = (f"In Linear's trash since {node['archivedAt'][:10]}" if node.get("trashed") else
                    f"Archived in Linear on {node['archivedAt'][:10]}" if node.get("archivedAt") else None)
        return render.Record(SOURCE, kind, node["id"], key, url, version, key, body, state, closed, list(opened),
                             [fact for fact in facts if fact] + ([archived] if archived else []),
                             [(lead["displayName"], f"@{lead['displayName']}")] if lead else [],
                             {name: values for name, values in (fields or {}).items() if values and None not in values}, due,
                             list(relations), [self.comment(found, {c["id"]: c for c in ordered}) for found in ordered],
                             self.uploads({"description": body}, ordered),
                             {kind: {key: value for key, value in node.items() if key != "updatedAt"}}, ordered, archived)

    # ---------------------------------------------------------------- one issue as a record

    def reference(self, found: dict) -> render.Ref:
        team = {**self.teams.get(found["team"]["id"], {}), **found["team"]}
        return render.Ref(found["identifier"], team["key"], not team["private"], found["id"])

    def person(self, found: dict | None) -> str:
        user = self.users.get((found or {}).get("id"))
        return f"@{user['displayName']}" if user else "@unknown (not visible to this key)"

    def author(self, comment: dict) -> str:
        if comment["user"]:
            return self.person(comment["user"])
        bot, outsider = comment["botActor"], comment["externalUser"]
        return f"{bot['name']} (bot)" if bot else f"{outsider['name']} (external)" if outsider else "@unknown"

    def record(self, node: dict, comments: list, related: list, children: list) -> render.Record:
        team = self.teams[node["team"]["id"]]
        state = self.states.get((node["state"] or {}).get("id"), {"name": "Unknown", "type": "unstarted"})
        fields, facts = self.fields_and_facts(node, team, state)
        archived = (f"In Linear's trash since {node['archivedAt'][:10]}" if node.get("trashed") else
                    f"Archived in Linear on {node['archivedAt'][:10]}" if node.get("archivedAt") else None)
        times = [f"{word} {node[word + 'At']}" for word in ("started", "completed", "canceled") if node.get(word + "At")]
        ordered, assignee = self.thread(comments), self.users.get((node["assignee"] or {}).get("id"))
        milestone, project = (node["projectMilestone"] or {}).get("id"), (node["project"] or {}).get("id")
        relations = ([("parent", self.reference(node["parent"]))] if node["parent"] else []) + \
            [("sub_issue", self.reference(child)) for child in children] + \
            ([("milestone", self.part(milestone))] if milestone in self.milestones else
             [("project", self.part(project))] if project in self.projects else []) + \
            sorted(((kind, other) for kind, other, _ in related), key=lambda pair: (pair[0], pair[1].key))
        # an identifier from before a move names its team: shown, and kept in the archive, only as any reference is (§3.9)
        previous = [{"identifier": key, "team": {"key": prefix, "private": self.keys.get(prefix, {"private": True})["private"]}}
                    for key in node["previousIdentifiers"] for prefix in [key.rsplit("-", 1)[0]]]
        relations += [("previously", render.Ref(p["identifier"], p["team"]["key"], not p["team"]["private"])) for p in previous]
        raw = {"team": team["key"], "relations": [relation for _, _, relation in related],
               "issue": {**{key: value for key, value in node.items() if key != "updatedAt"}, "previousIdentifiers": previous}}
        return render.Record(
            SOURCE, "issue", node["id"], node["identifier"], node["url"], node["updatedAt"], node["title"], node["description"] or "",
            STATES.get(state["type"], "open"),
            next((f"{word} at {node[word + 'At']}" for word in ("completed", "canceled") if node.get(word + "At")), None),
            [" · ".join([f"Opened by {self.person(node['creator'])} {node['createdAt']}", *times])],
            facts + ([archived] if archived else []),
            [(assignee["displayName"], f"@{assignee['displayName']}")] if assignee else [], fields,
            due_at(node["dueDate"], team["timezone"]) if node["dueDate"] else None, relations,
            [self.comment(found, {c["id"]: c for c in comments}) for found in ordered], self.uploads(node, ordered), raw, ordered,
            archived)

    def fields_and_facts(self, node: dict, team: dict, state: dict) -> tuple[dict, list]:
        """The field values by name, and the header's facts: every value is also a header line (§3.8)."""
        groups = defaultdict(list)
        for label in sorted((self.labels[l["id"]] for l in node["labels"]["nodes"] if l["id"] in self.labels),
                            key=lambda label: label["name"].casefold()):
            parent = self.labels.get((label["parent"] or {}).get("id"))
            group = parent["name"] if parent else "Label"
            groups[f"{group} (label group)" if parent and group in SELECTIONS else group].append(label["name"])
        priority, estimate, cycle = PRIORITIES.get(node["priority"]), self.estimate(node["estimate"], team), self.cycle(node["cycle"])
        fields = {"Team": [team["key"]], "Workflow": [state["name"]], "Priority": [priority], "Estimate": [estimate],
                  "Cycle": [cycle], **groups}
        facts = [" · ".join([f"Team: {team['name']} ({team['key']})", f"Status: {state['name']} ({state['type']})",
                             *([f"Priority: {priority}"] if priority else []),
                             *([f"Estimate: {estimate} ({team['issueEstimationType']})"] if estimate else [])]),
                 f"Due: {node['dueDate']} ({team['timezone'] or 'UTC'})" if node["dueDate"] else "",
                 f"Cycle: {cycle}" if cycle else "",
                 " · ".join(f"{'Labels' if name == 'Label' else name}: {', '.join(values)}" for name, values in groups.items()),
                 f"Reactions: {reactions(node['reactionData'])}" if reactions(node["reactionData"]) else "",
                 *(" ".join(filter(None, ["Attachment:", card["sourceType"] or "link", f'"{card["title"]}"', card["subtitle"],
                                          card["url"]])) for card in node["attachments"]["nodes"])]
        return {name: values for name, values in fields.items() if values != [None]}, [fact for fact in facts if fact]

    def estimate(self, value, team: dict) -> str | None:
        if value is None or team["issueEstimationType"] in (None, "notUsed"):
            return None
        if team["issueEstimationType"] == "tShirt":
            return SIZES.get(int(value), str(value))
        return str(int(value)) if float(value).is_integer() else str(value)

    def cycle(self, found: dict | None) -> str | None:
        cycle = self.cycles.get((found or {}).get("id"))
        if not cycle:
            return None
        return cycle["name"] or f"{self.teams.get(cycle['team']['id'], {}).get('key', '')} cycle {cycle['number']}".strip()

    @staticmethod
    def thread(comments: list) -> list:
        """Each thread's first comment, then its replies, each in time order."""
        replies, roots, ordered = defaultdict(list), [], []
        for found in sorted(comments, key=lambda c: (c["createdAt"], c["id"])):
            (replies[found["parent"]["id"]] if found["parent"] else roots).append(found)

        def walk(found):
            ordered.append(found)
            for reply in replies[found["id"]]:
                walk(reply)
        for found in roots:
            walk(found)
        return ordered + [c for c in sorted(comments, key=lambda c: (c["createdAt"], c["id"])) if c not in ordered]

    def comment(self, found: dict, by_id: dict) -> render.Comment:
        parent = by_id.get((found["parent"] or {}).get("id"))
        quote = "\n".join("> " + line for line in found["quotedText"].split("\n")) + "\n\n" if found["quotedText"] else ""
        marks = ([f"reply to {self.author(parent)}"] if parent else []) + \
            ([f"resolved by {self.person(found['resolvingUser'])} at {found['resolvedAt']}"] if found["resolvedAt"] else [])
        return render.Comment(self.author(found), found["createdAt"], bool(found["editedAt"]), quote + (found["body"] or ""),
                              reactions(found["reactionData"]), None,
                              max(filter(None, [found["createdAt"], found["editedAt"], found["resolvedAt"]])), tuple(marks))

    def uploads(self, node: dict, comments: list) -> list:
        """Files uploaded to Linear that the description or a comment references, each once, named by a hash of its path."""
        found = {}
        for text, in_comment in [(node["description"] or "", False)] + [(c["body"] or "", True) for c in comments]:
            for url in UPLOAD.findall(text):
                found.setdefault(url, render.Attachment(render.attachment_name(SOURCE, urlsplit(url).path, url.rsplit("/", 1)[-1]),
                                                        url, in_comment))
        return list(found.values())

    # ---------------------------------------------------------------- what the importer needs besides records

    def selections(self, records: list) -> dict:
        found = dict(SELECTIONS)
        for record in records:
            for name, values in record.fields.items():
                if name not in SELECTIONS:
                    found[name] = "many" if len(values) > 1 or found.get(name) == "many" else "one"
        return found

    def withheld(self, ctx: render.Context):
        """Whether a node of the raw JSON is an issue in a team neither selected nor public."""
        def check(node: dict) -> bool:
            team = node.get("team")
            return isinstance(team, dict) and {"key", "private"} <= team.keys() and not ctx.shown(
                render.Ref("", team["key"], not team["private"]))
        return check

    def download(self, attachment) -> bytes | None:
        """The bytes, read with the key from Linear's upload host only; a redirect elsewhere leaves the file a link."""
        url = attachment.url
        for _ in range(3):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname not in FILE_HOSTS:
                return None
            with self.http.stream("GET", url, headers={"Authorization": self.key}) as answer:
                if answer.status_code in (301, 302, 303, 307, 308) and answer.headers.get("location"):
                    url = answer.headers["location"]
                    continue
                if answer.status_code != 200:
                    return None
                data = bytearray()
                for block in answer.iter_bytes():
                    data += block
                    if len(data) > CAP:
                        return None
                return bytes(data)
        return None
