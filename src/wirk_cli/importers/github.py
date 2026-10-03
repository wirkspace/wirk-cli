"""The GitHub adapter (docs/plans/importers.md §4): issues read through the person's `gh` login, with GraphQL queries and
GETs only. It never reads gh's token and never writes to GitHub; pull requests are counted, never read."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import render
from .render import Stop

SOURCE = "GitHub"
NOUN = ("repository", "repositories")
LABELS = {"parent": "Parent", "sub_issue": "Sub-issues in GitHub order", "blocked_by": "Blocked by", "blocking": "Blocking",
          "duplicate_of": "Duplicate of", "related": "Related", "closed_by": "Closed by", "mentioned": "Mentioned in",
          "transferred_from": "Transferred from"}
SELECTIONS = {"Repository": "one", "Label": "many", "Milestone": "one", "Issue type": "one"}
STATES = {("CLOSED", "NOT_PLANNED"): "cancelled", ("CLOSED", "DUPLICATE"): "cancelled"}
REASONS = {"COMPLETED": "completed", "NOT_PLANNED": "not planned", "DUPLICATE": "duplicate"}
EMOJI = {"THUMBS_UP": "👍", "THUMBS_DOWN": "👎", "LAUGH": "😄", "HOORAY": "🎉", "CONFUSED": "😕", "HEART": "❤️",
         "ROCKET": "🚀", "EYES": "👀"}
LIMITED = re.compile(r"secondary rate limit|abuse detection|HTTP 429", re.I)
SPENT = re.compile(r"API rate limit (already )?exceeded", re.I)  # the hour's budget: wait for its reset
RESET = "query { rateLimit { cost remaining resetAt } }"
TIMEOUT = re.compile(r"timeout|HTTP 50[234]|Something went wrong", re.I)
PAGE = 25
READERS = 4  # timelines read at once (decision 80); writes to WIRK stay one at a time
CAP = 100 * 1024 * 1024  # bytes a download may hold (§3.7)
# Where GitHub serves attachment bytes; a redirect anywhere else is not followed, and no credential is ever sent.
FILE_HOSTS = {"github.com", "objects.githubusercontent.com", "private-user-images.githubusercontent.com",
              "user-images.githubusercontent.com"}
ATTACHMENT = (r"https://github\.com/(?:user-attachments/(?:assets|files)/[\w.-]+(?:/[^\s)\]\"'<>]+)?"
              r"|[\w.-]+/[\w.-]+/(?:assets|files)/\d+/[^\s)\]\"'<>]+)|https://user-images\.githubusercontent\.com/[^\s)\]\"'<>]+")
LINKED = re.compile(rf"(!?)\[([^\]]*)\]\(({ATTACHMENT})\)")
BARE = re.compile(ATTACHMENT)
IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
HTML = "query($id: ID!) { node(id: $id) { ... on Issue { bodyHTML } ... on IssueComment { bodyHTML } } }"
SIGNED = re.compile(r"https://private-user-images\.githubusercontent\.com/[^\"'\s<>]+")

# ---------------------------------------------------------------- the queries

REF = "number fullDatabaseId repository { nameWithOwner visibility }"
SUBJECT = f"__typename ... on Issue {{ {REF} }} ... on PullRequest {{ number repository {{ nameWithOwner visibility }} }}"
ACTOR = "actor { __typename login }"
FIELD_NAME = ("field { ... on IssueFieldSingleSelect { name } ... on IssueFieldMultiSelect { name } ... on IssueFieldDate "
              "{ name } ... on IssueFieldText { name } ... on IssueFieldNumber { name } }")
# One fragment per timeline event, each with its actor and time; comments, and the mentioned and subscribed events that
# comments cause, stay out (condition 3), so a comment never changes the issue's archive.
EVENTS = {
    "AddedToProjectEvent": "", "AddedToProjectV2Event": "wasAutomated", "AssignedEvent": "assignee { ... on Actor { login } }",
    "BlockedByAddedEvent": f"blockingIssue {{ {REF} }}", "BlockedByRemovedEvent": f"blockingIssue {{ {REF} }}",
    "BlockingAddedEvent": f"blockedIssue {{ {REF} }}", "BlockingRemovedEvent": f"blockedIssue {{ {REF} }}",
    "ClosedEvent": "stateReason closer { __typename ... on PullRequest { number merged repository { nameWithOwner visibility } } "
                   "... on Commit { abbreviatedOid repository { nameWithOwner visibility } } }",
    "CommentDeletedEvent": "deletedCommentAuthor { login }", "ConnectedEvent": f"isCrossRepository subject {{ {SUBJECT} }}",
    "ConvertedFromDraftEvent": "wasAutomated", "ConvertedNoteToIssueEvent": "projectColumnName",
    "ConvertedToDiscussionEvent": "discussion { number }",
    "CrossReferencedEvent": f"isCrossRepository willCloseTarget referencedAt source {{ {SUBJECT} }}",
    "DemilestonedEvent": "milestoneTitle", "DisconnectedEvent": f"isCrossRepository subject {{ {SUBJECT} }}",
    "IssueCommentPinnedEvent": "issueComment { fullDatabaseId }", "IssueCommentUnpinnedEvent": "issueComment { fullDatabaseId }",
    "IssueFieldAddedEvent": "value issueField { ... on IssueFieldSingleSelect { name } ... on IssueFieldDate { name } }",
    "IssueFieldChangedEvent": "newValue previousValue issueField { ... on IssueFieldSingleSelect { name } ... on IssueFieldDate { name } }",
    "IssueFieldRemovedEvent": "issueField { ... on IssueFieldSingleSelect { name } ... on IssueFieldDate { name } }",
    "IssueTypeAddedEvent": "issueType { name }", "IssueTypeChangedEvent": "issueType { name } prevIssueType { name }",
    "IssueTypeRemovedEvent": "issueType { name }", "LabeledEvent": "label { name }", "LockedEvent": "lockReason",
    "MarkedAsDuplicateEvent": f"isCrossRepository canonical {{ {SUBJECT} }}", "MilestonedEvent": "milestoneTitle",
    "MovedColumnsInProjectEvent": "", "ParentIssueAddedEvent": f"parent {{ {REF} }}", "ParentIssueRemovedEvent": f"parent {{ {REF} }}",
    "PinnedEvent": "", "ProjectV2ItemStatusChangedEvent": "wasAutomated",
    "ReferencedEvent": "isCrossRepository isDirectReference commit { abbreviatedOid } commitRepository { nameWithOwner visibility }",
    "RemovedFromProjectEvent": "", "RemovedFromProjectV2Event": "wasAutomated", "RenamedTitleEvent": "previousTitle currentTitle",
    "ReopenedEvent": "stateReason", "SubIssueAddedEvent": f"subIssue {{ {REF} }}", "SubIssueRemovedEvent": f"subIssue {{ {REF} }}",
    "TransferredEvent": "fromRepository { nameWithOwner visibility }", "UnassignedEvent": "assignee { ... on Actor { login } }",
    "UnlabeledEvent": "label { name }", "UnlockedEvent": "", "UnmarkedAsDuplicateEvent": f"isCrossRepository canonical {{ {SUBJECT} }}",
    "UnpinnedEvent": "", "UserBlockedEvent": "blockDuration",
}
# Project events need the read:project scope even for their time, so they are asked for only when the login has it.
PROJECT_EVENTS = {"AddedToProjectEvent", "AddedToProjectV2Event", "ConvertedFromDraftEvent", "ConvertedNoteToIssueEvent",
                  "MovedColumnsInProjectEvent", "ProjectV2ItemStatusChangedEvent", "RemovedFromProjectEvent",
                  "RemovedFromProjectV2Event"}
COMMENT = ("nodes { id fullDatabaseId author { __typename login } body createdAt lastEditedAt isMinimized minimizedReason "
           "reactionGroups { content reactors { totalCount } } }")


def connections(projects: bool) -> dict:
    """Each nested connection of an issue: its arguments and what each node holds."""
    events = {name: extra for name, extra in EVENTS.items() if projects or name not in PROJECT_EVENTS}
    types = ", ".join(re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper().replace("PROJECT_V_2", "PROJECT_V2") for name in events)
    timeline = "nodes { __typename " + " ".join(f"... on {name} {{ createdAt {ACTOR} {extra} }}" for name, extra in events.items()) + " }"
    return {"labels": ("first: 100", "nodes { name description }"), "subIssues": ("first: 100", f"nodes {{ {REF} }}"),
            "blockedBy": ("first: 100", f"nodes {{ {REF} }}"), "blocking": ("first: 100", f"nodes {{ {REF} }}"),
            "relatesTo": ("first: 100", f"nodes {{ {REF} }}"), "comments": ("first: 100", COMMENT),
            "timelineItems": (f"first: 100, itemTypes: [{types}]", timeline)}


def issue_fields(projects: bool) -> str:
    """An issue's fields for the paged query. Its timeline is not among them: GitHub's paged query can return a timeline
    short, totalCount included, without saying so, so each issue's timeline is read on its own (§4.1)."""
    paged = " ".join(f"{name}({args}) {{ pageInfo {{ hasNextPage endCursor }} {nodes} }}"
                     for name, (args, nodes) in connections(projects).items() if name != "timelineItems")
    return (f"id fullDatabaseId number title body url state stateReason(enableDuplicate: true) createdAt updatedAt closedAt "
            f"lastEditedAt locked activeLockReason isPinned author {{ __typename login }} assignees(first: 10) {{ nodes {{ login }} }} "
            f"milestone {{ title dueOn state }} issueType {{ name }} parent {{ {REF} }} duplicateOf {{ {REF} }} "
            f"issueFieldValues(first: 20) {{ nodes {{ __typename ... on IssueFieldSingleSelectValue {{ name {FIELD_NAME} }} "
            f"... on IssueFieldMultiSelectValue {{ options {{ name }} {FIELD_NAME} }} ... on IssueFieldDateValue {{ value {FIELD_NAME} }} "
            f"... on IssueFieldTextValue {{ value {FIELD_NAME} }} ... on IssueFieldNumberValue {{ value {FIELD_NAME} }} }} }} "
            f"reactionGroups {{ content reactors {{ totalCount }} }} {paged}")


LIMIT = "rateLimit { cost remaining resetAt }"
REPO = "nameWithOwner visibility isArchived hasIssuesEnabled issues { totalCount } pullRequests { totalCount }"
OWNER = (f"query($owner: String!, $after: String) {{ {LIMIT} repositoryOwner(login: $owner) {{ repositories(first: 100, "
         f"after: $after) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {REPO} }} }} }} }}")
NAMED = f"query($owner: String!, $name: String!) {{ {LIMIT} repository(owner: $owner, name: $name) {{ {REPO} }} }}"


def issues_query(projects: bool) -> str:
    return (f"query($owner: String!, $name: String!, $after: String, $size: Int!) {{ {LIMIT} repository(owner: $owner, "
            f"name: $name) {{ {REPO} page: issues(first: $size, after: $after, orderBy: {{field: CREATED_AT, direction: ASC}}) "
            f"{{ pageInfo {{ hasNextPage endCursor }} nodes {{ {issue_fields(projects)} }} }} }} }}")


def more_query(name: str, projects: bool) -> str:
    args, nodes = connections(projects)[name]
    args = args.replace("first: 100", "first: 100, after: $after")
    return (f"query($id: ID!, $after: String) {{ {LIMIT} node(id: $id) {{ ... on Issue {{ {name}({args}) "
            f"{{ pageInfo {{ hasNextPage endCursor }} {nodes} }} }} }} }}")


@dataclass
class Census:
    selected: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (repository, issues) not public and not named
    named_private: list = field(default_factory=list)  # (repository, visibility, issues) not public, named (§3.9)
    pulls: int = 0
    issues: int = 0
    comments: int = 0
    external_images: int = 0
    points: int = 0  # GraphQL points the read cost
    notes: list = field(default_factory=list)


def gh(args: list, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    except FileNotFoundError:
        raise Stop("the GitHub CLI is not installed", "install it from https://cli.github.com, then: gh auth login") from None
    return done.returncode, done.stdout, done.stderr


def person(actor: dict | None) -> str:
    if not actor:
        return "@ghost (deleted account)"
    if actor.get("__typename") == "Bot":
        return f"@{actor['login']}[bot] (bot)"
    return f"@{actor['login']}" + (" (mannequin)" if actor.get("__typename") == "Mannequin" else "")


def seconds_until(moment: str) -> float:
    return max(1.0, (datetime.fromisoformat(moment.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())


def day(moment: str | None) -> str:
    return (moment or "")[:10]


def reactions(groups: list) -> str:
    return " · ".join(f"{EMOJI.get(g['content'], g['content'].lower())} {g['reactors']['totalCount']}"
                      for g in groups or [] if g["reactors"]["totalCount"])


class Gate:
    """One rate-limit budget for every reader: a pause that any reader sets holds them all."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock, self.until, self.lock = sleep, clock, 0.0, threading.Lock()

    def hold(self, seconds: float) -> None:
        with self.lock:
            self.until = max(self.until, self.clock() + seconds)

    def wait(self) -> None:
        """Sleep out the pause, and again if another reader extends it meanwhile."""
        slept = None
        while True:
            with self.lock:
                until, left = self.until, self.until - self.clock()
            if left <= 0 or until == slept:
                return
            self.sleep(left)
            slept = until


class GitHub:
    source, noun, labels = SOURCE, NOUN, LABELS

    def __init__(self, run=None, sleep=None, http=None):
        self.run, self.scopes, self.projects, self.http = run or gh, set(), False, http
        self.gate, self.points, self.counting = Gate(sleep or time.sleep), 0, threading.Lock()

    # ---------------------------------------------------------------- talking to GitHub

    def check(self) -> None:
        code, out, err = self.run(["api", "-i", "user"])
        if code != 0:
            raise Stop("gh is not logged in to github.com", "gh auth login")
        found = re.search(r"^x-oauth-scopes:\s*(.*)$", out, re.I | re.M)
        self.scopes = {scope.strip() for scope in (found[1] if found else "").split(",") if scope.strip()}
        self.projects = bool({"read:project", "project"} & self.scopes)

    def graphql(self, query: str, variables: dict) -> dict:
        assert not query.lstrip().startswith("mutation")  # the importer only reads
        refusals = 0
        while True:
            self.gate.wait()
            code, out, err = self.run(["api", "graphql", "--input", "-"], json.dumps({"query": query, "variables": variables}))
            try:
                answer = json.loads(out) if out.strip() else {}
            except ValueError:
                answer = {}
            if code == 0 and answer.get("data") and not answer.get("errors"):
                self.pace(answer["data"].get("rateLimit"))
                return answer["data"]
            text = out + err
            if SPENT.search(text):  # waiting for the reset is not a failed attempt
                self.gate.hold(self.until_reset() + 1)
                continue
            if LIMITED.search(text) and refusals < 4:  # a minute, doubling: four pauses, each followed by a retry
                self.gate.hold(60 * 2 ** refusals)
                refusals += 1
                continue
            if LIMITED.search(text):
                raise Stop("GitHub kept refusing for its secondary rate limit; wait an hour and run the same command again")
            if TIMEOUT.search(text):
                raise TimeoutError(text[:200])
            raise Stop(f"GitHub refused a read: {(answer.get('errors') or [{}])[0].get('message') or err.strip()[:200]}")

    def until_reset(self) -> float:
        """Seconds until the hour's GraphQL budget resets; GitHub answers this query even when the budget is spent."""
        code, out, _ = self.run(["api", "graphql", "--input", "-"], json.dumps({"query": RESET, "variables": {}}))
        try:
            return seconds_until(json.loads(out)["data"]["rateLimit"]["resetAt"])
        except (ValueError, KeyError, TypeError):
            return 60.0

    def pace(self, limit: dict | None) -> None:
        """Wait for the reset when the hour's points run low, so a long read never fails halfway."""
        with self.counting:
            self.points += (limit or {}).get("cost", 0)
        if limit and limit["remaining"] < max(50, 2 * limit["cost"] * READERS):
            self.gate.hold(seconds_until(limit["resetAt"]) + 1)

    # ---------------------------------------------------------------- what to read

    def context(self, selected: list) -> render.Context:
        return render.Context(SOURCE, frozenset(selected), NOUN, LABELS)

    def repositories(self, selection: list, census: Census) -> list:
        named = [word for word in selection if "/" in word]
        owners = [word for word in selection if "/" not in word]
        found = {}
        for owner in owners:
            after = None
            while True:
                answer = self.graphql(OWNER, {"owner": owner, "after": after})["repositoryOwner"]
                if answer is None:
                    raise Stop(f"GitHub has no user or organization {owner} that this login can see")
                for repo in answer["repositories"]["nodes"]:
                    if repo["hasIssuesEnabled"] and (repo["visibility"] == "PUBLIC" or repo["issues"]["totalCount"]):
                        if repo["visibility"] == "PUBLIC":
                            found[repo["nameWithOwner"]] = repo
                        else:
                            census.skipped.append((repo["nameWithOwner"], repo["issues"]["totalCount"]))
                if not answer["repositories"]["pageInfo"]["hasNextPage"]:
                    break
                after = answer["repositories"]["pageInfo"]["endCursor"]
        for word in named:
            owner, name = word.split("/", 1)
            repo = self.graphql(NAMED, {"owner": owner, "name": name})["repository"]
            if repo is None:
                raise Stop(f"GitHub has no repository {word} that this login can see")
            found[repo["nameWithOwner"]] = repo
            if repo["visibility"] != "PUBLIC":
                census.named_private.append((repo["nameWithOwner"], repo["visibility"].lower(), repo["issues"]["totalCount"]))
        census.skipped = [(name, count) for name, count in census.skipped if name not in found]
        census.selected = sorted(found)
        census.pulls = sum(repo["pullRequests"]["totalCount"] for repo in found.values())
        return [found[name] for name in census.selected]

    def read(self, selection: list) -> tuple[Census, list]:
        census = Census()
        if not self.projects:
            census.notes.append("projects not read: this gh login lacks read:project; to include them: "
                                "gh auth refresh -s read:project")
        records = []
        for repo in self.repositories(selection, census):
            for node in self.issues(repo):
                records.append(self.record(repo, node))
                census.comments += len(records[-1].comments)
                census.external_images += sum(1 for text in [node["body"] or ""] + [c["body"] or "" for c in node["comments"]["nodes"]]
                                              for url in IMAGE.findall(text) if not BARE.fullmatch(url))
        census.issues, census.points = len(records), self.points
        return census, records

    def issues(self, repo: dict):
        owner, name = repo["nameWithOwner"].split("/")
        after, size = None, PAGE
        while True:
            try:
                answer = self.graphql(issues_query(self.projects), {"owner": owner, "name": name, "after": after, "size": size})
            except TimeoutError:
                if size == 1:
                    raise Stop(f"GitHub timed out reading one issue of {repo['nameWithOwner']}; run the same command again")
                size = max(1, size // 2)
                continue
            issues = answer["repository"]["page"]
            for node in issues["nodes"]:
                for connection in connections(self.projects):
                    if connection != "timelineItems":
                        self.rest_of(node, connection)
                node["timelineItems"] = {"pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}  # read alone
            with ThreadPoolExecutor(max_workers=READERS) as readers:
                list(readers.map(lambda node: self.rest_of(node, "timelineItems"), issues["nodes"]))
            yield from issues["nodes"]
            if not issues["pageInfo"]["hasNextPage"]:
                return
            after = issues["pageInfo"]["endCursor"]

    def rest_of(self, node: dict, connection: str) -> None:
        """A nested connection longer than its first page, read to the end."""
        found = node[connection]
        while found["pageInfo"]["hasNextPage"]:
            more = self.read_alone(more_query(connection, self.projects), {"id": node["id"], "after": found["pageInfo"]["endCursor"]})["node"][connection]
            found = {"pageInfo": more["pageInfo"], "nodes": found["nodes"] + more["nodes"]}
        node[connection] = found

    def read_alone(self, query: str, variables: dict) -> dict:
        """One issue's query, tried again after a timeout, since nothing smaller can be asked for."""
        for attempt in range(3):
            try:
                return self.graphql(query, variables)
            except TimeoutError:
                self.gate.hold(5 * 2 ** attempt)
        raise Stop("GitHub timed out reading one issue three times; run the same command again")

    # ---------------------------------------------------------------- one issue as a record

    def reference(self, found: dict, note: str = "") -> render.Ref:
        repo = found["repository"]
        return render.Ref(f"{repo['nameWithOwner']}#{found['number']}", repo["nameWithOwner"], repo["visibility"] == "PUBLIC",
                          found.get("fullDatabaseId"), note)

    def record(self, repo: dict, node: dict) -> render.Record:
        scope = repo["nameWithOwner"]
        closing = next((e for e in reversed(node["timelineItems"]["nodes"]) if e["__typename"] == "ClosedEvent"), None)
        state = "open" if node["state"] == "OPEN" else STATES.get((node["state"], node["stateReason"]), "completed")
        closer = person((closing or {}).get("actor")) if closing else None
        reason = REASONS.get(node["stateReason"] or "COMPLETED", "completed")
        closed = f"closed as {reason} by {closer} at {node['closedAt']}" if node["state"] == "CLOSED" else None
        opened = " · ".join([f"Opened by {person(node['author'])} {node['createdAt']}", *(["edited"] if node["lastEditedAt"] else []),
                             *([f"closed by {closer} {node['closedAt']} as {reason}"] if closed else [])])
        fields, facts, due = self.fields_and_facts(repo, node)
        comments = [render.Comment(person(c["author"]), c["createdAt"], bool(c["lastEditedAt"]), c["body"] or "",
                                   reactions(c["reactionGroups"]), (c["minimizedReason"] or "hidden").lower() if c["isMinimized"] else None,
                                   c["lastEditedAt"] or c["createdAt"]) for c in node["comments"]["nodes"]]
        raw = {key: value for key, value in node.items() if key not in ("comments", "updatedAt", "id")}
        return render.Record(SOURCE, "issue", node["fullDatabaseId"], f"{scope}#{node['number']}", node["url"],
                             node["updatedAt"], node["title"], node["body"] or "", state, closed, [opened], facts,
                             [(a["login"], f"@{a['login']}") for a in node["assignees"]["nodes"]], fields, due,
                             self.relations(node, closing), comments, self.attachments(node), {"repository": scope, "issue": raw},
                             node["comments"]["nodes"])

    def relations(self, node: dict, closing: dict | None) -> list:
        """Every reference, in the order the header shows them; the reference rule is the renderer's."""
        found = []
        closer = (closing or {}).get("closer") or {}
        if closer.get("__typename") == "PullRequest":
            found.append(("closed_by", self.reference(closer, "pull request, merged" if closer.get("merged") else "pull request")))
        elif closer.get("__typename") == "Commit":
            repo = closer["repository"]
            found.append(("closed_by", render.Ref(f"{repo['nameWithOwner']}@{closer['abbreviatedOid']}", repo["nameWithOwner"],
                                                  repo["visibility"] == "PUBLIC", None, "commit")))
        found += [("parent", self.reference(node["parent"]))] if node["parent"] else []
        found += [(kind, self.reference(n)) for kind, connection in (("sub_issue", "subIssues"), ("blocked_by", "blockedBy"),
                                                                       ("blocking", "blocking")) for n in node[connection]["nodes"]]
        found += [("duplicate_of", self.reference(node["duplicateOf"]))] if node["duplicateOf"] else []
        found += [("related", self.reference(n)) for n in node["relatesTo"]["nodes"]]
        seen = set()
        for event in node["timelineItems"]["nodes"]:
            source = event.get("source") or {}
            if event["__typename"] == "CrossReferencedEvent" and source.get("repository"):
                mention = self.reference(source, "pull request" if source["__typename"] == "PullRequest" else "issue")
                if mention.key not in seen:
                    seen.add(mention.key)
                    found.append(("mentioned", mention))
            if event["__typename"] == "TransferredEvent" and event.get("fromRepository"):
                origin = event["fromRepository"]
                found.append(("transferred_from", render.Ref(origin["nameWithOwner"], origin["nameWithOwner"],
                                                             origin["visibility"] == "PUBLIC")))
        return found

    def fields_and_facts(self, repo: dict, node: dict) -> tuple[dict, list, str | None]:
        """The field values by name, the header's facts, and the due day from the Target date issue field."""
        fields, values, due = {"Repository": [repo["nameWithOwner"]]}, [], None
        labels = sorted((label["name"] for label in node["labels"]["nodes"]), key=str.casefold)
        milestone, kind = node["milestone"], (node["issueType"] or {}).get("name")
        for name, values_of in (("Label", labels), ("Milestone", [milestone["title"]] if milestone else []),
                                ("Issue type", [kind] if kind else [])):
            if values_of:
                fields[name] = values_of
        for value in sorted(node["issueFieldValues"]["nodes"], key=lambda v: (v.get("field") or {}).get("name", "")):
            name = (value.get("field") or {}).get("name")
            if not name:
                continue
            if value["__typename"] == "IssueFieldSingleSelectValue":
                fields[name] = [value["name"]]
            elif value["__typename"] == "IssueFieldMultiSelectValue":
                fields[name] = [option["name"] for option in value["options"]]
            elif name == "Target date":
                due = day(value["value"])
            values.append(f"{name} {', '.join(fields[name]) if name in fields else value['value']}")
        when = ", ".join([*([f"due {day(milestone['dueOn'])}"] if milestone and milestone["dueOn"] else []),
                          *([milestone["state"].lower()] if milestone else [])])
        lock = (node["activeLockReason"] or "locked").lower().replace("_", " ").replace("off topic", "off-topic")
        facts = [" · ".join([*([f"Type: {kind}"] if kind else []), *([f"Milestone: {milestone['title']} ({when})"] if milestone else [])]),
                 *([f"Labels: {', '.join(labels)}"] if labels else []),
                 *([f"Issue fields: {' · '.join(values)}"] if values else []),
                 *([f"Locked on GitHub: {lock}"] if node["locked"] else []),
                 *(["Pinned on GitHub"] if node["isPinned"] else []),
                 *([f"Reactions: {reactions(node['reactionGroups'])}"] if reactions(node["reactionGroups"]) else []),
                 *(["Repository archived on GitHub"] if repo.get("isArchived") else [])]
        return fields, [fact for fact in facts if fact], due

    # ---------------------------------------------------------------- what the importer needs besides records

    def selections(self, records: list) -> dict:
        found = dict(SELECTIONS)
        for record in records:
            for name, values in record.fields.items():
                found.setdefault(name, "many" if len(values) > 1 else "one")
        return found

    def withheld(self, ctx: render.Context):
        """Whether a node of the raw JSON points into a repository neither selected nor public."""
        def check(node: dict) -> bool:
            for key in ("repository", "fromRepository", "commitRepository"):
                repo = node.get(key)
                if isinstance(repo, dict) and "nameWithOwner" in repo and not ctx.shown(
                        render.Ref("", repo["nameWithOwner"], repo.get("visibility") == "PUBLIC")):
                    return True
            return False
        return check

    def attachments(self, node: dict) -> list:
        """Files uploaded to GitHub that the body or a comment references, each once, named by a hash of its URL."""
        found = {}
        for holder, text, in_comment in [(node["id"], node["body"] or "", False)] + [
                (c.get("id", ""), c["body"] or "", True) for c in node["comments"]["nodes"]]:
            named = {match[3]: match[2] for match in LINKED.finditer(text)}
            for url in BARE.findall(text):
                if url not in found:
                    original = named.get(url) or urlsplit(url).path.rsplit("/", 1)[-1]
                    found[url] = render.Attachment(render.attachment_name(SOURCE, url, original), url, in_comment, holder)
        return list(found.values())

    def download(self, attachment) -> bytes | None:
        """The bytes, without any credential; for a private repository through the signed link GitHub renders."""
        data = self.fetch(attachment.url)
        if data is None and attachment.holder:
            html = (self.graphql(HTML, {"id": attachment.holder}).get("node") or {}).get("bodyHTML") or ""
            asset = urlsplit(attachment.url).path.rsplit("/", 1)[-1]
            signed = next((url for url in SIGNED.findall(html) if asset in url), None)
            data = self.fetch(signed.replace("&amp;", "&")) if signed else None
        return data

    def fetch(self, url: str) -> bytes | None:
        import httpx
        self.http = self.http or httpx.Client(follow_redirects=False, timeout=httpx.Timeout(60, connect=10))
        for _ in range(5):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname not in FILE_HOSTS:
                return None
            with self.http.stream("GET", url) as answer:
                if answer.status_code in (301, 302, 303, 307, 308) and answer.headers.get("location"):
                    url = answer.headers["location"]
                    continue
                if answer.status_code != 200 or parts.hostname == "github.com":  # github.com itself serves pages, not files
                    return None
                data = bytearray()
                for block in answer.iter_bytes():
                    data += block
                    if len(data) > CAP:
                        return None
                return bytes(data)
        return None
