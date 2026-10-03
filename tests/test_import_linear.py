"""The Linear adapter (docs/plans/importers.md §5) on synthetic GraphQL answers through a fake endpoint. The answers
are shaped from the fields the Linear research note read in Linear's published schema; real-workspace evidence waits for
a read key (the plan's L rows marked R)."""

import json
import os
import re
import time

import httpx
import pytest

from wirk_cli.importers import linear, render
from wirk_cli.importers.render import Stop

KEY = "lin_api_" + "k" * 40


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def page(nodes, more=False, cursor=None):
    return {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}


def team(key, name, private=False, tz="UTC", scale="fibonacci"):
    return {"id": f"team-{key}", "key": key, "name": name, "private": private, "timezone": tz, "issueEstimationType": scale}


def state(team_key, name, kind, position=0):
    return {"id": f"st-{team_key}-{name}", "name": name, "type": kind, "position": position, "team": {"id": f"team-{team_key}"}}


def ref(n, team_key="ENG", private=False):
    return {"id": uid(n), "identifier": f"{team_key}-{n}", "team": {"id": f"team-{team_key}", "key": team_key, "private": private}}


def issue(n, team_key="ENG", **changes):
    base = {"id": uid(n), "identifier": f"{team_key}-{n}", "number": n, "previousIdentifiers": [],
            "url": f"https://linear.app/acme/issue/{team_key}-{n}/issue-{n}", "title": f"Issue {n}", "description": f"Body {n}",
            "priority": 0, "estimate": None, "dueDate": None, "createdAt": "2026-09-01T10:00:00.000Z",
            "updatedAt": "2026-09-30T12:00:00.000Z", "startedAt": None, "completedAt": None, "canceledAt": None,
            "archivedAt": None, "trashed": None, "state": {"id": f"st-{team_key}-Todo"}, "team": {"id": f"team-{team_key}"},
            "parent": None, "project": None, "projectMilestone": None, "cycle": None, "assignee": None,
            "creator": {"id": "u-ada"}, "labels": page([]), "attachments": page([]), "history": page([]), "reactionData": []}
    base.update(changes)
    return base


def comment(n, issue_n, body, **changes):
    base = {"id": f"c-{n}", "body": body, "createdAt": f"2026-09-02T11:00:{n:02d}.000Z", "editedAt": None, "quotedText": None,
            "resolvedAt": None, "parent": None, "issue": {"id": uid(issue_n)}, "user": {"id": "u-ben"}, "resolvingUser": None,
            "botActor": None, "externalUser": None, "reactionData": []}
    base.update(changes)
    return base


TEAMS = [team("ENG", "Engineering", tz="America/Los_Angeles"), team("OPS", "Operations", tz="Europe/Berlin", scale="tShirt"),
         team("SEC", "Security", private=True)]
STATES = [state(key, name, kind, position) for key in ("ENG", "OPS", "SEC")
          for position, (name, kind) in enumerate([("Triage", "triage"), ("Todo", "unstarted"), ("In Review", "started"),
                                                   ("Done", "completed"), ("Won't Fix", "canceled"), ("Duplicate", "duplicate")])]
USERS = [{"id": "u-ada", "name": "Ada Lovelace", "displayName": "ada", "active": True},
         {"id": "u-ben", "name": "Ben Sample", "displayName": "ben", "active": False}]
LABELS = [{"id": "l-bug", "name": "Bug", "isGroup": False, "parent": None},
          {"id": "l-area", "name": "Area", "isGroup": True, "parent": None},
          {"id": "l-front", "name": "Frontend", "isGroup": False, "parent": {"id": "l-area"}},
          {"id": "l-back", "name": "Backend", "isGroup": False, "parent": {"id": "l-area"}}]
CYCLES = [{"id": "cy-42", "number": 42, "name": None, "team": {"id": "team-ENG"}}]
PROJECTS = [{"id": "p-1", "name": "Checkout v2"}]
MILESTONES = [{"id": "m-1", "name": "Beta"}]


class FakeLinear:
    """Answers Linear's GraphQL endpoint: root lists by `first`/`after`, team filters, nested follow-ups, rate limits."""

    def __init__(self, issues, relations=(), comments=(), teams=TEAMS, files=None):
        self.lists = {"teams": list(teams), "workflowStates": STATES, "users": USERS, "issueLabels": LABELS, "cycles": CYCLES,
                      "projects": PROJECTS, "projectMilestones": MILESTONES, "issueRelations": list(relations),
                      "comments": list(comments), "issues": list(issues)}
        self.files, self.requests, self.limited, self.complexity, self.nested = files or {}, [], 0, 50, {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "uploads.linear.app" or request.method == "GET":
            found = self.files.get(str(request.url))
            if isinstance(found, httpx.Response):
                return found
            return httpx.Response(200, content=found) if found is not None else httpx.Response(404)
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables") or {}
        reset = str(int((time.time() + 30) * 1000))  # Linear gives resets in milliseconds since the epoch
        if self.limited:
            self.limited -= 1
            return httpx.Response(400, headers={"X-RateLimit-Requests-Reset": reset}, json={
                "errors": [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]})
        headers = {"X-Complexity": str(self.complexity), "X-RateLimit-Requests-Remaining": "2400",
                   "X-RateLimit-Complexity-Remaining": "2900000", "X-RateLimit-Requests-Reset": reset}
        return httpx.Response(200, headers=headers, json={"data": self.answer(query, variables)})

    def answer(self, query, variables):
        if "viewer" in query:
            return {"viewer": {"id": "u-ada"}, "organization": {"urlKey": "acme"}}
        found = re.search(r"\{\s*(\w+)\(", query)
        name = found[1]
        if name == "issue":  # a nested connection of one issue, past its first page
            connection = re.search(r"issue\(id: \$id\) \{ (\w+)\(", query)[1]
            return {"issue": {connection: self.window(self.nested[(variables["id"], connection)], variables)}}
        nodes = self.lists[name]
        if variables.get("teams") is not None:
            if name == "issues":
                nodes = [n for n in nodes if n["team"]["id"] in variables["teams"]]
            elif name == "comments":
                teams = {n["id"]: n["team"]["id"] for n in self.lists["issues"]}
                nodes = [n for n in nodes if teams.get(n["issue"]["id"]) in variables["teams"]]
        return {name: self.window(nodes, variables)}

    @staticmethod
    def window(nodes, variables):
        start, size = int(variables.get("after") or 0), variables["first"]
        more = start + size < len(nodes)
        return page(nodes[start:start + size], more, str(start + size) if more else None)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "import"
    path.mkdir(mode=0o700)
    (path / "linear-key").write_text(KEY + "\n")
    os.chmod(path / "linear-key", 0o600)
    return path


def adapter(fake, folder, slept=None):
    return linear.Linear(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=(slept or []).append, folder=folder)


def read(fake, folder, selection=()):
    found = adapter(fake, folder)
    found.check()
    return found, *found.read(list(selection))


def by_key(records):
    return {record.key: record for record in records}


# ---------------------------------------------------------------- the key and the selection

def test_the_key_is_an_owner_only_file_and_only_ever_sent_to_linear(folder):
    os.chmod(folder / "linear-key", 0o644)
    with pytest.raises(Stop) as stop:
        adapter(FakeLinear([]), folder).check()
    assert "chmod 600" in stop.value.fix
    os.remove(folder / "linear-key")
    with pytest.raises(Stop) as stop:
        adapter(FakeLinear([]), folder).check()
    assert "Read" in stop.value.fix and "linear-key" in stop.value.fix and KEY not in str(stop.value) + stop.value.fix
    (folder / "linear-key").write_text("not a key")
    os.chmod(folder / "linear-key", 0o600)
    with pytest.raises(Stop):
        adapter(FakeLinear([]), folder).check()


def test_public_teams_by_default_private_ones_only_when_named(folder):
    fake = FakeLinear([issue(1), issue(2, "OPS"), issue(3, "SEC")])
    _, census, records = read(fake, folder)
    assert census.selected == ["ENG", "OPS"] and census.skipped == [("SEC", None)]
    assert sorted(by_key(records)) == ["ENG-1", "OPS-2"]
    _, census, records = read(fake, folder, ["ENG", "SEC"])
    assert census.selected == ["ENG", "SEC"] and census.skipped == [] and census.named_private == [("SEC", "private", 1)]
    with pytest.raises(Stop):
        read(fake, folder, ["NOPE"])


def test_only_queries_reach_linear_with_the_key_and_no_email_is_asked_for(folder):
    fake = FakeLinear([issue(1)], comments=[comment(1, 1, "Hi")])
    read(fake, folder)
    for request in fake.requests:
        assert request.url == httpx.URL(linear.API) and request.headers["authorization"] == KEY
        query = json.loads(request.content)["query"]
        assert query.lstrip().startswith("query") and "mutation" not in query and "email" not in query.lower()


# ---------------------------------------------------------------- one issue

def test_an_issue_maps_to_a_record(folder):
    fake = FakeLinear([issue(7, title="Fix login", description="It fails.", priority=2, estimate=3, dueDate="2026-03-08",
                             state={"id": "st-ENG-In Review"}, assignee={"id": "u-ben"}, cycle={"id": "cy-42"},
                             project={"id": "p-1"}, projectMilestone={"id": "m-1"}, previousIdentifiers=["OPS-45"],
                             labels=page([{"id": "l-bug"}, {"id": "l-front"}]), startedAt="2026-09-03T15:02:10.000Z",
                             attachments=page([{"id": "a-1", "title": "Keep next= through SSO", "subtitle": "#412 open",
                                                "url": "https://github.com/acme/web/pull/412", "sourceType": "github"}]),
                             reactionData=[{"emoji": "+1", "userIds": ["u-ada", "u-ben"]}])])
    record = by_key(read(fake, folder)[2])["ENG-7"]
    assert (record.kind, record.ident, record.url) == ("issue", uid(7), "https://linear.app/acme/issue/ENG-7/issue-7")
    assert record.state == "in_progress" and record.version == "2026-09-30T12:00:00.000Z"
    assert record.assignees == [("ben", "@ben")]
    assert record.fields == {"Team": ["ENG"], "Workflow": ["In Review"], "Priority": ["High"], "Estimate": ["3"],
                             "Cycle": ["ENG cycle 42"], "Label": ["Bug"], "Area": ["Frontend"]}
    assert record.due == "2026-03-09T06:59:59Z"  # the end of the day in Los Angeles, the day its clocks moved
    header = "\n".join(record.opened + record.facts)
    for line in ["Opened by @ada 2026-09-01T10:00:00.000Z · started 2026-09-03T15:02:10.000Z",
                 "Team: Engineering (ENG) · Status: In Review (started) · Priority: High · Estimate: 3 (fibonacci)",
                 "Due: 2026-03-08 (America/Los_Angeles)", "Cycle: ENG cycle 42", "Project: Checkout v2 · Milestone: Beta",
                 "Labels: Bug · Area: Frontend", "Previously: [OPS-45]", "Reactions: +1 2",
                 'Attachment: github "Keep next= through SSO" #412 open https://github.com/acme/web/pull/412']:
        assert line in header
    assert record.raw["issue"]["identifier"] == "ENG-7" and "updatedAt" not in record.raw["issue"]


@pytest.mark.parametrize("name, meaning", [("Triage", "open"), ("Todo", "open"), ("In Review", "in_progress"), ("Done", "completed"),
                                           ("Won't Fix", "cancelled"), ("Duplicate", "cancelled")])
def test_status_follows_the_state_type(name, meaning, folder):
    record = read(FakeLinear([issue(1, state={"id": f"st-ENG-{name}"})]), folder)[2][0]
    assert record.state == meaning and record.fields["Workflow"] == [name]


def test_estimates_are_named_on_each_teams_scale_and_completion_has_its_evidence(folder):
    fake = FakeLinear([issue(1, "OPS", estimate=5, state={"id": "st-OPS-Done"}, completedAt="2026-09-04T09:00:00.000Z")])
    record = read(fake, folder)[2][0]
    assert record.fields["Estimate"] == ["L"] and record.closed == "completed at 2026-09-04T09:00:00.000Z"


def test_archived_and_trashed_issues_carry_why(folder):
    fake = FakeLinear([issue(1, archivedAt="2026-03-01T00:00:00.000Z"), issue(2, archivedAt="2026-09-30T00:00:00.000Z", trashed=True),
                       issue(3)])
    records = by_key(read(fake, folder)[2])
    assert records["ENG-1"].archived == "Archived in Linear on 2026-03-01"
    assert records["ENG-2"].archived == "In Linear's trash since 2026-09-30"
    assert records["ENG-3"].archived is None


# ---------------------------------------------------------------- relations and the reference rule

def test_relations_both_ways_with_references_into_private_teams_withheld(folder):
    issues = [issue(1), issue(2, parent=ref(1)), issue(3), issue(4), issue(5, parent=ref(9, "SEC", private=True))]
    relations = [{"id": "r-1", "type": "blocks", "issue": ref(1), "relatedIssue": ref(3)},
                 {"id": "r-2", "type": "duplicate", "issue": ref(4), "relatedIssue": ref(3)},
                 {"id": "r-3", "type": "similar", "issue": ref(3), "relatedIssue": ref(2)},
                 {"id": "r-4", "type": "blocks", "issue": ref(8, "SEC", private=True), "relatedIssue": ref(1)}]
    found, census, records = read(FakeLinear(issues, relations), folder)
    records = by_key(records)
    kinds = lambda key: [(kind, item.key) for kind, item in records[key].relations]
    assert ("sub_issue", "ENG-2") in kinds("ENG-1") and ("blocking", "ENG-3") in kinds("ENG-1")
    assert ("blocked_by", "SEC-8") in kinds("ENG-1") and ("parent", "ENG-1") in kinds("ENG-2")
    assert ("blocked_by", "ENG-1") in kinds("ENG-3") and ("duplicated_by", "ENG-4") in kinds("ENG-3")
    assert ("duplicate_of", "ENG-3") in kinds("ENG-4") and ("related", "ENG-2") in kinds("ENG-3")
    ctx = found.context(census.selected)
    hidden = [item for kind, item in records["ENG-1"].relations if not ctx.shown(item)]
    assert [item.key for item in hidden] == ["SEC-8"]
    archive = render.archive(records["ENG-5"].raw, found.withheld(ctx), {})
    assert b"SEC" not in archive and b"withheld" in archive


# ---------------------------------------------------------------- comments, files and paging

def test_comments_in_threads_with_replies_resolutions_quotes_and_bots(folder):
    comments = [comment(1, 1, "First", resolvedAt="2026-09-05T00:00:00.000Z", resolvingUser={"id": "u-ada"}),
                comment(2, 1, "Second", user=None, botActor={"name": "Triage bot"}),
                comment(3, 1, "A reply", parent={"id": "c-1"}, user={"id": "u-ada"}, editedAt="2026-09-03T00:00:00.000Z",
                        quotedText="the quoted line", reactionData=[{"emoji": "eyes", "userIds": ["u-ben"]}])]
    _, census, records = read(FakeLinear([issue(1)], comments=comments), folder)
    record = records[0]
    assert census.comments == 3
    assert [c.body for c in record.comments] == ["First", "> the quoted line\n\nA reply", "Second"]
    first, reply, bot = record.comments
    assert first.author == "@ben" and first.marks == ("resolved by @ada at 2026-09-05T00:00:00.000Z",)
    assert reply.marks == ("reply to @ben",) and reply.edited and reply.reactions == "eyes 1"
    assert bot.author == "Triage bot (bot)"
    assert first.version == "2026-09-05T00:00:00.000Z"  # a resolution changes the discussion
    assert [c["id"] for c in record.raw_comments] == ["c-1", "c-3", "c-2"]


def test_uploads_are_attachments_fetched_with_the_key_only_from_linear(folder):
    image = "https://uploads.linear.app/acme/1f2e/screenshot.png"
    elsewhere = "https://uploads.linear.app/acme/9a9a/moved.pdf"
    fake = FakeLinear([issue(1, description=f"See ![login page]({image})")],
                      comments=[comment(1, 1, f"Also {elsewhere}")],
                      files={image: b"PNG", elsewhere: httpx.Response(302, headers={"location": "https://evil.example/x"})})
    found, census, records = read(fake, folder)
    body, comment_file = sorted(records[0].attachments, key=lambda a: a.comment)
    assert body.name.startswith("linear-attachment-") and body.name.endswith("-screenshot.png") and comment_file.comment
    assert found.download(body) == b"PNG" and found.download(comment_file) is None
    gets = [r for r in fake.requests if r.method == "GET"]
    assert all(r.url.host == "uploads.linear.app" and r.headers["authorization"] == KEY for r in gets)


def test_pages_and_long_nested_connections_are_followed(folder):
    history = [{"id": f"h-{n}", "createdAt": f"2026-09-01T00:00:{n % 60:02d}.000Z"} for n in range(70)]
    fake = FakeLinear([issue(n) for n in range(1, 60)] + [issue(60, history=page(history[:50], True, "50"))])
    fake.nested[(uid(60), "history")] = history
    records = read(fake, folder)[2]
    assert len(records) == 60 and len(by_key(records)["ENG-60"].raw["issue"]["history"]["nodes"]) == 70


def test_a_rate_limit_waits_for_its_reset_and_complexity_is_counted(folder):
    fake = FakeLinear([issue(1)])
    fake.limited = 1
    slept = []
    found = adapter(fake, folder, slept)
    found.check()
    census, records = found.read([])
    assert len(records) == 1 and len(slept) == 1 and 25 < slept[0] <= 31
    assert census.points == 50 * (len(fake.requests) - 1)  # every answered query, the check included
