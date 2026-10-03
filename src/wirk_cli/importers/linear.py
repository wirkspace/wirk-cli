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
          "duplicate_of": "Duplicate of", "duplicated_by": "Duplicates", "related": "Related", "previously": "Previously"}
SELECTIONS = {"Team": "many", "Workflow": "one", "Priority": "one", "Estimate": "one", "Cycle": "one", "Label": "many"}
STATES = {"triage": "open", "backlog": "open", "unstarted": "open", "started": "in_progress", "completed": "completed",
          "canceled": "cancelled", "duplicate": "cancelled"}
PRIORITIES = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
SIZES = {1: "XS", 2: "S", 3: "M", 5: "L", 8: "XL", 13: "XXL", 21: "XXXL"}  # Linear keeps T-shirt sizes as these numbers
RELATIONS = {"blocks": ("blocking", "blocked_by"), "duplicate": ("duplicate_of", "duplicated_by")}  # else related both ways
UPLOAD = re.compile(r"https://uploads\.linear\.app/[^\s)\]\"'<>]+")
IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
PAGE, LIST, CAP = 25, 250, 100 * 1024 * 1024

# ---------------------------------------------------------------- the queries

REF = "id identifier team { id key private }"
NESTED = {"labels": ("first: 20", "id"), "attachments": ("first: 20", "id title subtitle url sourceType"),
          "history": ("first: 50", "id createdAt actorId fromStateId toStateId fromAssigneeId toAssigneeId fromPriority "
                      "toPriority fromEstimate toEstimate fromDueDate toDueDate fromCycleId toCycleId fromProjectId toProjectId "
                      "fromParentId toParentId fromTeamId toTeamId fromTitle toTitle addedLabelIds removedLabelIds archived "
                      "trashed autoArchived autoClosed updatedDescription")}
ISSUE = ("id identifier number previousIdentifiers url title description priority estimate dueDate createdAt updatedAt "
         "startedAt completedAt canceledAt archivedAt trashed reactionData state { id } team { id } parent { " + REF + " } "
         "project { id } projectMilestone { id } cycle { id } assignee { id } creator { id } "
         + " ".join(f"{name}({args}) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {nodes} }} }}"
                    for name, (args, nodes) in NESTED.items()))
COMMENT = ("id body createdAt editedAt quotedText resolvedAt reactionData parent { id } issue { id } user { id } "
           "resolvingUser { id } botActor { name } externalUser { name }")
LISTS = {  # read whole at the start: (arguments, what each node holds)
    "teams": ("", "id key name private timezone issueEstimationType"),
    "workflowStates": ("includeArchived: true", "id name type position team { id }"),
    "users": ("includeDisabled: true", "id name displayName"),
    "issueLabels": ("includeArchived: true", "id name isGroup parent { id }"),
    "cycles": ("includeArchived: true", "id number name team { id }"),
    "projects": ("includeArchived: true", "id name"),
    "projectMilestones": ("", "id name"),
    "issueRelations": ("", f"id type issue {{ {REF} }} relatedIssue {{ {REF} }}"),
}
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
               f"where only you can read it: (umask 077 && pbpaste > {path})")
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

    def graphql(self, query: str, variables: dict) -> dict:
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
            if "RATELIMITED" in codes:  # waiting for the reset is not a failure
                self.sleep(self.until(answer.headers))
                continue
            if answer is not None and (answer.status_code in (401, 403) or codes & {"AUTHENTICATION_ERROR", "FORBIDDEN"}):
                raise Stop("Linear refused the key", "make a new key restricted to Read, and save it as the last one was")
            if answer is None or answer.status_code >= 500:
                failures += 1
                if failures > 3:
                    raise Stop("Linear did not answer; run the same command again")
                self.sleep(5 * 2 ** failures)
                continue
            if answer.status_code != 200 or errors or "data" not in body:
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
            answer = self.graphql(query, {"first": size, "after": after, **({"teams": teams} if teams is not None else {})})[name]
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
        return render.Context(SOURCE, frozenset(selected), NOUN, LABELS)

    def read(self, selection: list) -> tuple[render.Census, list]:
        lists = {name: self.every(name, args, nodes) for name, (args, nodes) in LISTS.items()}
        self.teams, self.states, self.users, self.labels, self.cycles, self.projects, self.milestones = (
            {node["id"]: node for node in lists[name]} for name in
            ("teams", "workflowStates", "users", "issueLabels", "cycles", "projects", "projectMilestones"))
        self.keys = keys = {team["key"]: team for team in lists["teams"]}
        unknown = [word for word in selection if word not in keys]
        if unknown:
            raise Stop(f"Linear has no team {', '.join(unknown)} that this key can see",
                       f"the teams it can see: {', '.join(sorted(keys))}")
        chosen = [keys[word] for word in selection] or [team for team in lists["teams"] if not team["private"]]
        issues = self.every("issues", f"includeArchived: true, filter: {{ {IN_TEAMS} }}", ISSUE, PAGE, [t["id"] for t in chosen])
        for node in issues:
            for name in NESTED:
                self.rest_of(node, name)
        comments = defaultdict(list)
        for found in self.every("comments", f"includeArchived: true, filter: {{ issue: {{ {IN_TEAMS} }} }}", COMMENT, 100,
                                [t["id"] for t in chosen]):
            comments[found["issue"]["id"]].append(found)
        read, related, children = {node["id"] for node in issues}, defaultdict(list), defaultdict(list)
        for relation in lists["issueRelations"]:
            ends, kinds = (relation["issue"], relation["relatedIssue"]), RELATIONS.get(relation["type"], ("related", "related"))
            for this, other, kind in ((ends[0], ends[1], kinds[0]), (ends[1], ends[0], kinds[1])):
                if this["id"] in read:
                    related[this["id"]].append((kind, other, relation))
        for node in sorted(issues, key=lambda node: node["number"]):
            if node["parent"]:
                children[node["parent"]["id"]].append(node)
        records = [self.record(node, comments[node["id"]], related[node["id"]], children[node["id"]]) for node in issues]
        census = render.Census(selected=sorted(team["key"] for team in chosen), issues=len(records), points=self.points)
        census.skipped = [(team["key"], None) for team in sorted(lists["teams"], key=lambda t: t["key"])
                          if team["private"] and team not in chosen]
        census.named_private = [(team["key"], "private", sum(node["team"]["id"] == team["id"] for node in issues))
                                for team in chosen if team["private"]]
        census.comments = sum(len(record.comments) for record in records)
        census.external_images = sum(1 for node in issues for text in [node["description"] or ""] +
                                     [c["body"] or "" for c in comments[node["id"]]]
                                     for url in IMAGE.findall(text) if not UPLOAD.match(url))
        return census, records

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
        relations = ([("parent", self.reference(node["parent"]))] if node["parent"] else []) + \
            [("sub_issue", self.reference(child)) for child in children] + \
            sorted(((kind, self.reference(other)) for kind, other, _ in related), key=lambda pair: (pair[0], pair[1].key))
        # an identifier from before a move names its team: shown, and kept in the archive, only as any reference is (§3.9)
        previous = [{"identifier": key, "team": {"key": key.rsplit("-", 1)[0], "private": key.rsplit("-", 1)[0] not in self.keys
                                                 or self.keys[key.rsplit("-", 1)[0]]["private"]}} for key in node["previousIdentifiers"]]
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
        project, milestone = (self.projects.get((node["project"] or {}).get("id")),
                              self.milestones.get((node["projectMilestone"] or {}).get("id")))
        facts = [" · ".join([f"Team: {team['name']} ({team['key']})", f"Status: {state['name']} ({state['type']})",
                             *([f"Priority: {priority}"] if priority else []),
                             *([f"Estimate: {estimate} ({team['issueEstimationType']})"] if estimate else [])]),
                 f"Due: {node['dueDate']} ({team['timezone'] or 'UTC'})" if node["dueDate"] else "",
                 f"Cycle: {cycle}" if cycle else "",
                 " · ".join([*([f"Project: {project['name']}"] if project else []),
                             *([f"Milestone: {milestone['name']}"] if milestone else [])]),
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
