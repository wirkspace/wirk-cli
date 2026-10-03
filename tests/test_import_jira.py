"""The Jira adapter (docs/plans/importers.md §6) on synthetic answers through a fake Jira Cloud. The answers are shaped
from the REST v3 OpenAPI the Jira note read (issue, search/jql, changelog bulk fetch, comments, worklogs, remote links,
fields, projects) and Atlassian's document format; real-site evidence waits for the person's site (the plan's J rows)."""

import base64
import json
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from wirk_cli.importers import jira, render
from wirk_cli.importers.render import Stop

TOKEN = "ATATT3x" + "T" * 40
CLOUD = "4a1b2c3d-0000-4000-8000-00000000c10d"
BASE = f"https://api.atlassian.com/ex/jira/{CLOUD}"


def adf(*paragraphs):
    return {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}


def person(account, name, kind="atlassian"):
    return {"accountId": account, "displayName": name, "accountType": kind, "emailAddress": f"{account}@example.com",
            "avatarUrls": {"48x48": "https://avatars.example/a.png"}, "active": True}


ADA, BEN, BOT = person("acc-ada", "Ada Example"), person("acc-ben", "Ben Sample"), person("acc-bot", "Automation", "app")
PROJECTS = [{"id": "10000", "key": "SEED", "name": "Seed project"}, {"id": "10001", "key": "OPS", "name": "Operations"},
            {"id": "10002", "key": "OUT", "name": "Outside"}]
FIELDS = [{"id": "customfield_10020", "name": "Sprint", "custom": True, "schema": {"type": "array", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
          {"id": "customfield_10016", "name": "Story Points", "custom": True, "schema": {"type": "number"}},
          {"id": "customfield_10030", "name": "Customer tier", "custom": True,
           "schema": {"type": "option", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
          {"id": "customfield_10031", "name": "Owner", "custom": True,
           "schema": {"type": "option", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
          {"id": "customfield_10032", "name": "Region", "custom": True,
           "schema": {"type": "option-with-child", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:cascadingselect"}},
          {"id": "customfield_10033", "name": "Acceptance notes", "custom": True,
           "schema": {"type": "string", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea"}},
          {"id": "customfield_10034", "name": "Build", "custom": True,
           "schema": {"type": "string", "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"}},
          {"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string", "system": "summary"}}]
# what a project read with expand=description,lead returns, with the per-viewer and volatile fields an archive must not keep
LISTED = [{**p, "description": f"What {p['name']} is for.\n# Not a heading", "lead": BEN, "favourite": False, "isPrivate": False,
           "insight": {"totalIssueCount": 3}, "permissions": {"canEdit": False}, "self": f"{BASE}/rest/api/3/project/{p['id']}"}
          for p in PROJECTS]
COMPONENTS = {"10000": [{"id": "10101", "name": "Web", "description": "The site", "issueCount": 4},
                        {"id": "10100", "name": "API", "description": "The service", "lead": ADA, "issueCount": 7}]}
VERSIONS = {"10000": [{"id": "10201", "name": "2.0", "description": "Next", "released": False, "archived": False, "startDate": "2026-09-01",
                       "releaseDate": "2026-11-01", "overdue": False, "userStartDate": "01/Sep/26", "userReleaseDate": "01/Nov/26"},
                      {"id": "10200", "name": "1.0", "released": True, "archived": False, "releaseDate": "2026-06-01", "overdue": False}]}


def sprint(n, state="active", goal="Ship sign-in"):
    return {"id": n, "name": f"Sprint {n}", "state": state, "boardId": 3, "goal": goal, "startDate": f"2026-09-{n:02d}T00:00:00.000Z",
            "endDate": f"2026-09-{n + 13:02d}T00:00:00.000Z", **({"completeDate": f"2026-09-{n + 13:02d}T09:00:00.000Z"} if state == "closed" else {})}


def ref(key, ident, summary="Elsewhere"):
    return {"id": ident, "key": key, "self": f"{BASE}/rest/api/3/issue/{ident}",
            "fields": {"summary": summary, "status": {"name": "To Do"}, "issuetype": {"name": "Task"}}}


def issue(n, project="SEED", **fields):
    ident = str(20000 + n)
    base = {"summary": f"Issue {n}", "description": adf(f"Body {n}."), "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "resolution": None, "resolutiondate": None, "issuetype": {"name": "Task", "subtask": False, "hierarchyLevel": 0},
            "priority": {"name": "Medium"}, "project": next(p for p in PROJECTS if p["key"] == project),
            "assignee": None, "reporter": ADA, "creator": ADA, "created": "2026-09-01T10:00:00.000+0000",
            "updated": "2026-09-30T12:00:00.000+0000", "duedate": None, "labels": [], "components": [], "fixVersions": [],
            "versions": [], "security": None, "parent": None, "subtasks": [], "issuelinks": [], "attachment": [],
            "comment": {"comments": [], "total": 0, "startAt": 0, "maxResults": 100},
            "worklog": {"worklogs": [], "total": 0, "startAt": 0, "maxResults": 20}, "watches": {"watchCount": 0, "isWatching": False},
            "votes": {"votes": 0, "hasVoted": False}, "lastViewed": "2026-10-01T00:00:00.000+0000", "timeoriginalestimate": None,
            "timeestimate": None, "timespent": None}
    base.update(fields)
    return {"id": ident, "key": f"{project}-{n}", "self": f"{BASE}/rest/api/3/issue/{ident}", "fields": base}


def comment(n, body, author=BEN, **more):
    return {"id": str(30000 + n), "author": author, "updateAuthor": author, "body": adf(body), "created": f"2026-09-02T11:00:{n:02d}.000+0000",
            "updated": f"2026-09-02T11:00:{n:02d}.000+0000", "jsdPublic": True, **more}


class FakeJira:
    """Answers Jira Cloud's REST v3 reads under one cloudId, and the site's tenant_info."""

    def __init__(self, issues, changelogs=None, remote=None, comments=None, files=None):
        self.issues, self.changelogs, self.remote = list(issues), changelogs or {}, remote or {}
        self.more_comments, self.files, self.requests, self.limited, self.captcha = comments or {}, files or {}, [], 0, False
        self.tenant = None  # tenant_info's answer, when a test needs another
        self.projects, self.components, self.versions = json.loads(json.dumps(LISTED)), json.loads(json.dumps(COMPONENTS)), \
            json.loads(json.dumps(VERSIONS))
        self.answers = {}  # path -> the answer a test wants there instead

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlsplit(str(request.url))
        if url.path == "/_edge/tenant_info":
            return self.tenant if self.tenant is not None else httpx.Response(200, json={"cloudId": CLOUD})
        if self.captcha:
            return httpx.Response(401, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"})
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "7", "RateLimit-Reason": "jira-burst-based"})
        path, query = url.path.removeprefix(f"/ex/jira/{CLOUD}"), parse_qs(url.query)
        if path in self.answers:
            return self.answers[path]
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json=ADA)
        if path == "/rest/api/3/project/search":
            start = int(query.get("startAt", ["0"])[0])
            return httpx.Response(200, json={"values": self.projects[start:start + 2], "startAt": start,
                                             "isLast": start + 2 >= len(self.projects)})
        if path.startswith("/rest/api/3/project/") and path.endswith("/components"):
            return httpx.Response(200, json=self.components.get(path.split("/")[-2], []))
        if path.startswith("/rest/api/3/project/") and path.endswith("/version"):
            start, found = int(query.get("startAt", ["0"])[0]), self.versions.get(path.split("/")[-2], [])
            return httpx.Response(200, json={"values": found[start:start + 50], "startAt": start, "isLast": start + 50 >= len(found)})
        if path == "/rest/api/3/field":
            return httpx.Response(200, json=FIELDS)
        if path == "/rest/api/3/search/jql":
            body = json.loads(request.content)
            chosen = [i for i in self.issues if self.matches(i, body["jql"])]
            start = int(body.get("nextPageToken") or 0)
            window = chosen[start:start + body["maxResults"]]
            more = start + body["maxResults"] < len(chosen)
            return httpx.Response(200, json={"issues": window, **({"nextPageToken": str(start + body["maxResults"])} if more else {})})
        if path == "/rest/api/3/changelog/bulkfetch":
            body = json.loads(request.content)
            return httpx.Response(200, json={"issueChangeLogs": [{"issueId": i, "changeHistories": self.changelogs.get(i, [])}
                                                                  for i in body["issueIdsOrKeys"]]})
        if path.endswith("/remotelink"):
            return httpx.Response(200, json=self.remote.get(path.split("/")[-2], []))
        if path.endswith("/comment"):
            start = int(query["startAt"][0])
            found = self.more_comments[path.split("/")[-2]]
            return httpx.Response(200, json={"comments": found[start:start + 100], "startAt": start, "total": len(found)})
        if path.startswith("/rest/api/3/attachment/content/"):
            assert query.get("redirect") == ["false"]
            data = self.files.get(path.split("/")[-1])
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        raise AssertionError(f"unexpected {request.method} {path}")

    @staticmethod
    def matches(issue, jql):
        projects = jql.split("project in (")[1].split(")")[0] if "project in (" in jql else ""
        keys = jql.split("key in (")[1].split(")")[0] if "key in (" in jql else ""
        return f'"{issue["fields"]["project"]["key"]}"' in projects or f'"{issue["key"]}"' in keys


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "import"
    path.mkdir(mode=0o700)
    (path / "jira-key").write_text(json.dumps({"site": "acme.atlassian.net", "email": "admin@example.com", "token": TOKEN}))
    os.chmod(path / "jira-key", 0o600)
    return path


def adapter(fake, folder, slept=None):
    return jira.Jira(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=(slept if slept is not None else []).append,
                     folder=folder)


def read(fake, folder, selection=()):
    found = adapter(fake, folder)
    found.check({})
    return found, *found.read(list(selection))


def by_key(records):
    """The issues read, by key; project docs are tested on their own."""
    return {record.key: record for record in records if record.kind == "issue"}


# ---------------------------------------------------------------- the key, the site and the selection

def test_the_key_file_holds_site_login_and_token_owner_only_and_only_atlassian_hears_them(folder):
    fake = FakeJira([issue(1)])
    read(fake, folder)
    expected = "Basic " + base64.b64encode(f"admin@example.com:{TOKEN}".encode()).decode()
    for request in fake.requests:
        if request.url.path == "/_edge/tenant_info":
            assert request.url.host == "acme.atlassian.net" and "authorization" not in request.headers
        else:
            assert str(request.url).startswith(BASE) and request.headers["authorization"] == expected
            assert request.method == "GET" or request.url.path.endswith(("/search/jql", "/changelog/bulkfetch"))
    os.chmod(folder / "jira-key", 0o644)
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check({})
    assert "chmod 600" in stop.value.fix
    os.remove(folder / "jira-key")
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check({})
    assert "jira-key" in stop.value.fix and "read" in stop.value.fix.lower() and TOKEN not in str(stop.value) + stop.value.fix


def test_a_captcha_lockout_stops_and_a_429_waits_its_retry_after(folder):
    fake = FakeJira([issue(1)])
    fake.captcha = True
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check({})
    assert "CAPTCHA" in str(stop.value)
    fake.captcha, fake.limited, slept = False, 1, []
    found = adapter(fake, folder, slept)
    found.check({})
    assert slept == [7]


def test_every_project_the_token_browses_by_default_or_the_projects_and_issues_named(folder):
    fake = FakeJira([issue(1), issue(2, "OPS"), issue(3, "OUT")])
    _, census, records = read(fake, folder)
    assert census.selected == ["OPS", "OUT", "SEED"] and sorted(by_key(records)) == ["OPS-2", "OUT-3", "SEED-1"]
    _, census, records = read(fake, folder, ["SEED", "OPS-2"])
    assert census.selected == ["OPS", "SEED"] and sorted(by_key(records)) == ["OPS-2", "SEED-1"]
    with pytest.raises(Stop):
        read(fake, folder, ["NOPE"])


def test_restricted_issues_comments_and_worklogs_are_skipped_unless_the_issue_is_named(folder):
    secret = issue(9, security={"name": "Staff only"}, summary="Secret plan")
    open_one = issue(1, comment={"comments": [comment(1, "Public"), comment(2, "Managers only", visibility={"type": "role", "value": "Managers"})],
                                 "total": 2, "startAt": 0, "maxResults": 100},
                     worklog={"worklogs": [{"id": "w1", "author": BEN, "timeSpentSeconds": 3600, "started": "2026-09-02T09:00:00.000+0000"},
                                           {"id": "w2", "author": BEN, "timeSpentSeconds": 60, "started": "2026-09-02T09:00:00.000+0000",
                                            "visibility": {"type": "group", "value": "hr"}}], "total": 2, "startAt": 0, "maxResults": 20})
    found, census, records = read(FakeJira([open_one, secret]), folder)
    assert sorted(by_key(records)) == ["SEED-1"] and census.skipped == [("SEED-9", None)]
    record = by_key(records)["SEED-1"]
    assert [c.body for c in record.comments] == ["Public"] and "Not imported: 1 restricted comment, 1 restricted worklog" in record.facts
    assert [w["id"] for w in record.raw["worklogs"]] == ["w1"] and "Managers only" not in json.dumps(record.raw_comments)
    _, census, records = read(FakeJira([open_one, secret]), folder, ["SEED", "SEED-9", "SEED-1"])
    assert sorted(by_key(records)) == ["SEED-1", "SEED-9"] and census.named_private == [("SEED-9", "restricted", 1)]
    assert len(by_key(records)["SEED-1"].comments) == 2


# ---------------------------------------------------------------- one issue

def test_an_issue_maps_to_a_record_with_fields_only_for_small_current_sets(folder):
    node = issue(5, summary="Checkout fails", status={"name": "In Review", "statusCategory": {"key": "indeterminate"}},
                 priority={"name": "High"}, assignee=BEN, duedate="2026-10-15", labels=["alpha", "ünïcødé"],
                 components=[{"name": "API"}, {"name": "Web"}],
                 fixVersions=[{"name": "1.2", "released": False, "archived": False}, {"name": "1.0", "released": True, "archived": False}],
                 versions=[{"name": "1.1", "released": True, "archived": True}],
                 customfield_10020=[{"id": 41, "name": "Sprint 41", "state": "closed"}, {"id": 42, "name": "Sprint 42", "state": "active"}],
                 customfield_10016=5.0, customfield_10030={"value": "Gold"}, customfield_10031={"value": "Team A"},
                 customfield_10032={"value": "EMEA", "child": {"value": "Germany"}}, customfield_10033=adf("Must not double charge."),
                 customfield_10034="build-77", watches={"watchCount": 3, "isWatching": True}, votes={"votes": 2, "hasVoted": True},
                 timeoriginalestimate=10800, timeestimate=3600, timespent=8400,
                 description={"version": 1, "type": "doc", "content": [
                     {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Steps"}]},
                     {"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": "acc-ada", "text": "@Ada Example"}},
                                                       {"type": "text", "text": " knows."}]}]})
    changelogs = {"20005": [{"id": "1", "author": ADA, "created": "2026-09-03T00:00:00.000+0000",
                             "items": [{"field": "Key", "fieldtype": "jira", "fromString": "OPS-3", "toString": "SEED-5"}]}]}
    remote = {"20005": [{"id": 1, "object": {"url": "https://example.com/runbook", "title": "Runbook"}, "application": {}}]}
    record = by_key(read(FakeJira([node], changelogs, remote), folder, ["SEED", "OPS"])[2])["SEED-5"]
    assert (record.kind, record.ident, record.url) == ("issue", "20005", "https://acme.atlassian.net/browse/SEED-5")
    assert record.state == "in_progress" and record.version == "2026-09-30T12:00:00.000+0000"
    assert record.assignees == [("acc-ben", "Ben Sample")] and record.due == "2026-10-15T23:59:59Z"
    assert record.fields == {"Workflow": ["In Review"], "Issue type": ["Task"], "Priority": ["High"], "Project": ["SEED"],
                             "Label": ["alpha", "ünïcødé"], "Component": ["API", "Web"], "Release": ["1.2"], "Sprint": ["Sprint 42"],
                             "Story points": ["5"], "Customer tier": ["Gold"], "Owner": ["Team A"], "Region": ["EMEA › Germany"]}
    header = "\n".join(record.opened + record.facts)
    for line in ["Reported by Ada Example 2026-09-01T10:00:00.000+0000",
                 "Type: Task · Status: In Review (indeterminate) · Resolution: none · Priority: High · Project: SEED (Seed project)",
                 "Labels: alpha, ünïcødé · Components: API, Web · Fix versions: 1.2, 1.0 · Affects versions: 1.1",
                 "Sprints: Sprint 41 (closed), Sprint 42 (active) · Story points: 5 · Due: 2026-10-15",
                 "Customer tier: Gold · Owner: Team A · Region: EMEA › Germany · Build: build-77", "Web links: Runbook https://example.com/runbook"]:
        assert line in header
    assert record.body == "\\## Steps\n\n@Ada Example knows.\n\n## Acceptance notes\n\nMust not double charge."
    assert ("previously", "OPS-3") in [(kind, r.key) for kind, r in record.relations]
    assert record.raw["changelog"][0]["items"][0]["toString"] == "SEED-5"
    text = json.dumps(record.raw)
    assert "emailAddress" not in text and "avatarUrls" not in text and "lastViewed" not in text and "isWatching" not in text
    assert "hasVoted" not in text and '"updated"' not in json.dumps(record.raw["issue"]["fields"])
    assert "Watchers" not in header and "Time:" not in header  # counts that churn without the issue changing: kept nowhere
    assert not {"watches", "votes", "timespent", "timeestimate", "timeoriginalestimate"} & set(record.raw["issue"]["fields"])


@pytest.mark.parametrize("category, resolution, meaning", [("new", None, "open"), ("indeterminate", None, "in_progress"),
                                                           ("done", "Fixed", "completed"), ("done", "Won't Do", "cancelled"),
                                                           ("done", "Duplicate", "cancelled")])
def test_status_follows_the_category_and_the_resolution(category, resolution, meaning, folder):
    node = issue(1, status={"name": "Done" if category == "done" else "Open", "statusCategory": {"key": category}},
                 resolution={"name": resolution} if resolution else None, resolutiondate="2026-09-05T00:00:00.000+0000" if resolution else None)
    record = read(FakeJira([node]), folder)[2][0]
    assert record.state == meaning
    if category == "done":
        assert record.closed == f"is Done there (resolution {resolution}, resolved 2026-09-05T00:00:00.000+0000)"
        assert record.fields["Resolution"] == [resolution]


def test_story_points_are_header_only_past_twenty_values(folder):
    records = read(FakeJira([issue(n, customfield_10016=float(n)) for n in range(1, 23)]), folder)[2]
    assert all("Story points" not in r.fields for r in records) and "Story points: 7" in "\n".join(by_key(records)["SEED-7"].facts)


# ---------------------------------------------------------------- links, comments and files

def test_links_parents_and_the_reference_rule(folder):
    nodes = [issue(1, issuetype={"name": "Epic", "subtask": False, "hierarchyLevel": 1}),
             issue(2, parent=ref("SEED-1", "20001"), subtasks=[ref("SEED-3", "20003")],
                   issuelinks=[{"id": "500", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                "outwardIssue": ref("SEED-4", "20004")},
                               {"id": "501", "type": {"name": "Cloners", "inward": "is cloned by", "outward": "clones"},
                                "inwardIssue": ref("SEED-4", "20004")},
                               {"id": "502", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                "inwardIssue": ref("OUT-7", "20107", "Secret outside")}]),
             issue(3, parent=ref("SEED-2", "20002")),
             issue(4, issuelinks=[{"id": "500", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                   "inwardIssue": ref("SEED-2", "20002")},
                                  {"id": "503", "type": {"name": "Duplicate", "inward": "is duplicated by", "outward": "duplicates"},
                                   "outwardIssue": ref("SEED-1", "20001")}])]
    found, census, records = read(FakeJira(nodes), folder, ["SEED"])
    records = by_key(records)
    pairs = lambda key: [(kind, r.key, r.note) for kind, r in records[key].relations]
    assert ("sub_issue", "SEED-2", "") in pairs("SEED-1") and ("parent", "SEED-1", "") in pairs("SEED-2")
    assert ("sub_issue", "SEED-3", "") in pairs("SEED-2") and ("blocking", "SEED-4", "") in pairs("SEED-2")
    assert ("related", "SEED-4", "is cloned by") in pairs("SEED-2") and ("blocked_by", "OUT-7", "") in pairs("SEED-2")
    assert ("blocked_by", "SEED-2", "") in pairs("SEED-4") and ("duplicate_of", "SEED-1", "") in pairs("SEED-4")
    ctx = found.context(census.selected)
    assert [r.key for _, r in records["SEED-2"].relations if not ctx.shown(r)] == ["OUT-7"]
    archive = render.archive(records["SEED-2"].raw, found.withheld(ctx), {})
    assert b"OUT-7" not in archive and b"Secret outside" not in archive and b"SEED-4" in archive


def test_comments_go_to_the_discussion_with_internal_notes_marked_and_long_threads_paged(folder):
    many = [comment(n, f"Comment {n}") for n in range(1, 131)]
    node = issue(1, comment={"comments": many[:100], "total": 130, "startAt": 0, "maxResults": 100})
    many[3] = {**many[3], "jsdPublic": False, "updated": "2026-09-03T00:00:00.000+0000"}
    record = read(FakeJira([node], comments={"20001": many}), folder)[2][0]
    assert len(record.comments) == 130 and record.comments[0].author == "Ben Sample"
    assert record.comments[3].marks == ("internal",) and record.comments[3].edited
    assert len(record.raw_comments) == 130


def test_attachments_are_files_fetched_without_redirects_and_images_name_them(folder):
    node = issue(1, attachment=[{"id": "10010", "filename": "shot.png", "mimeType": "image/png", "size": 3, "author": ADA,
                                 "created": "2026-09-02T00:00:00.000+0000", "content": f"{BASE}/rest/api/3/attachment/content/10010"}],
                 description={"version": 1, "type": "doc", "content": [
                     {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "media-uuid", "alt": "shot.png"}}]}]})
    found, census, records = read(FakeJira([node], files={"10010": b"PNG"}), folder)
    record = records[0]
    assert record.body == "[attached: shot.png]" and record.attachments[0].name.endswith("-shot.png")
    assert found.download(record.attachments[0]) == b"PNG"



# ---------------------------------------------------------------- the review's findings

def test_a_skipped_restricted_issue_is_never_named_nor_titled_by_the_issues_around_it(folder):
    breach = issue(2, security={"name": "Staff only"}, summary="Breach of the payment vault")
    visible = issue(1, parent=ref("SEED-2", "20002", "Breach of the payment vault"), subtasks=[ref("SEED-2", "20002", "Breach of the payment vault")],
                    issuelinks=[{"id": "600", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                 "outwardIssue": ref("SEED-2", "20002", "Breach of the payment vault")},
                                {"id": "601", "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                                 "outwardIssue": ref("SEED-3", "20003", "A visible title")}])
    history = {"20001": [{"id": "9", "author": ADA, "created": "2026-09-03T00:00:00.000+0000",
                          "items": [{"field": "Link", "fieldtype": "jira", "to": "SEED-2", "toString": "This issue blocks SEED-2"}]}]}
    found, census, records = read(FakeJira([visible, breach, issue(3)], history), folder)
    record = by_key(records)["SEED-1"]
    ctx = found.context(census.selected)
    made = render.work_item(ctx, record, {}, {})
    assert "SEED-2" not in made.body and "Breach" not in made.body
    assert not [r for _, r in record.relations if r.key == "SEED-2" and ctx.shown(r)]
    archive = render.archive(record.raw, found.withheld(ctx), {})
    assert b"SEED-2" not in archive and b"Breach" not in archive and b"A visible title" not in archive and b"SEED-3" in archive


def test_attachments_of_comments_left_out_are_neither_fetched_nor_named(folder):
    media = lambda name: {"version": 1, "type": "doc", "content": [{"type": "mediaSingle", "content": [
        {"type": "media", "attrs": {"id": f"m-{name}", "alt": name}}]}]}
    files = [{"id": f"1001{n}", "filename": name, "mimeType": "image/png", "size": 3, "author": ADA, "created": "2026-09-02T00:00:00.000+0000",
              "content": f"{BASE}/rest/api/3/attachment/content/1001{n}"} for n, name in enumerate(["seen.png", "secret.png", "loose.png"])]
    shown, hidden = comment(1, ""), comment(2, "", visibility={"type": "role", "value": "Managers"})
    shown["body"], hidden["body"] = media("seen.png"), media("secret.png")
    node = issue(1, attachment=files, comment={"comments": [shown, hidden], "total": 2, "startAt": 0, "maxResults": 100})
    found, census, records = read(FakeJira([node], files={"10010": b"A", "10011": b"B", "10012": b"C"}), folder)
    record = records[0]
    assert [a.name.rsplit("-", 1)[-1] for a in record.attachments] == ["seen.png"]
    assert "Not imported: 1 restricted comment, 2 attachments" in record.facts
    text = json.dumps(record.raw) + json.dumps(record.raw_comments)
    assert "secret.png" not in text and "loose.png" not in text


def test_a_move_from_a_project_not_selected_keeps_its_name_out_of_the_history(folder):
    moved = {"20005": [{"id": "1", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": [
        {"field": "Key", "fieldtype": "jira", "fromString": "OPS-3", "toString": "SEED-5"},
        {"field": "project", "fieldtype": "jira", "from": "10001", "fromString": "Operations", "to": "10000", "toString": "Seed project"},
        {"field": "Workflow", "fieldtype": "jira", "from": "30001", "fromString": "OPS ops workflow", "to": "30000", "toString": "SEED workflow"},
        {"field": "status", "fieldtype": "jira", "from": "4", "fromString": "OPS triage", "to": "1", "toString": "To Do"},
        {"field": "Component", "fieldtype": "jira", "from": "7", "fromString": "OPS backend", "to": None, "toString": None},
        {"field": "Fix Version", "fieldtype": "jira", "from": "8", "fromString": "OPS 1.0", "to": None, "toString": None},
        {"field": "Sprint", "fieldtype": "custom", "from": "9", "fromString": "OPS sprint 3", "to": None, "toString": None}]},
        {"id": "2", "author": ADA, "created": "2026-09-04T00:00:00.000+0000", "items": [
            {"field": "summary", "fieldtype": "jira", "fromString": "Old words", "toString": "Issue 5"}]}]}
    found, census, records = read(FakeJira([issue(5)], moved), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    for name in (b"Operations", b"OPS ops workflow", b"OPS-3", b"OPS triage", b"OPS backend", b"OPS 1.0", b"OPS sprint 3"):
        assert name not in archive, name
    assert b"Old words" in archive  # a later change of the issue's own keeps its history


def test_cancelled_resolutions_come_from_the_map_with_broad_defaults(folder):
    done = lambda n, why: issue(n, status={"name": "Done", "statusCategory": {"key": "done"}}, resolution={"name": why})
    nodes = [done(1, "Declined"), done(2, "Cannot Reproduce"), done(3, "Fixed")]
    states = {key: r.state for key, r in by_key(read(FakeJira(nodes), folder)[2]).items()}
    assert states == {"SEED-1": "cancelled", "SEED-2": "cancelled", "SEED-3": "completed"}
    found = adapter(FakeJira(nodes), folder)
    found.cancelled = {"Fixed"}
    found.check({})
    assert {r.key: r.state for r in found.read([])[1]}["SEED-3"] == "cancelled"


@pytest.mark.parametrize("answer", [httpx.Response(200, text="<html>not json</html>"), httpx.Response(200, json={"cloudId": "../evil"})])
def test_a_site_whose_cloud_id_cannot_be_read_stops_cleanly(answer, folder):
    fake = FakeJira([issue(1)])
    fake.tenant = answer
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check({})
    assert "cloud" in str(stop.value).lower() and "cloud_id" in stop.value.fix and TOKEN not in str(stop.value) + stop.value.fix



def test_a_watch_or_vote_alone_leaves_the_work_item_current(folder):
    from import_fakes import FakeWirk
    from test_import_wirk import make, outcomes
    store = FakeWirk(fields={})
    node = issue(1, watches={"watchCount": 1, "isWatching": False}, votes={"votes": 0, "hasVoted": False}, timespent=60,
                 aggregatetimespent=60, timetracking={"timeSpent": "1m", "timeSpentSeconds": 60})
    found, census, records = read(FakeJira([node]), folder)
    assert set(outcomes(make(store, ctx=found.context(census.selected)).run(records)).values()) == {"created"}
    node["fields"].update(watches={"watchCount": 5, "isWatching": True}, votes={"votes": 3, "hasVoted": True}, timespent=120,
                          aggregatetimespent=120, timetracking={"timeSpent": "2m", "timeSpentSeconds": 120})
    found, census, records = read(FakeJira([node]), folder)
    before = len(store.writes())
    assert set(outcomes(make(store, ctx=found.context(census.selected)).run(records)).values()) == {"current"}
    assert len(store.writes()) == before



def test_the_history_never_names_a_file_that_was_not_kept(folder):
    files = [{"id": f"1002{n}", "filename": name, "mimeType": "text/plain", "size": 3, "author": ADA, "created": "2026-09-02T00:00:00.000+0000",
              "content": f"{BASE}/rest/api/3/attachment/content/1002{n}"} for n, name in enumerate(["seen.png", "payroll-dump.csv"])]
    shown, hidden = comment(1, ""), comment(2, "Managers only", visibility={"type": "role", "value": "Managers"})
    shown["body"] = {"version": 1, "type": "doc", "content": [{"type": "mediaSingle", "content": [
        {"type": "media", "attrs": {"id": "m-seen", "alt": "seen.png"}}]}]}
    history = {"20001": [{"id": "5", "author": ADA, "created": "2026-09-02T00:00:00.000+0000", "items": [
        {"field": "Attachment", "fieldtype": "jira", "from": None, "fromString": None, "to": "10020", "toString": "seen.png"},
        {"field": "Attachment", "fieldtype": "jira", "from": None, "fromString": None, "to": "10021", "toString": "payroll-dump.csv"}]}]}
    node = issue(1, attachment=files, comment={"comments": [shown, hidden], "total": 2, "startAt": 0, "maxResults": 100})
    found, census, records = read(FakeJira([node], history), folder)
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"payroll-dump.csv" not in archive and b"seen.png" in archive


def history_item(field, before, after, **ids):
    return {"field": field, "fieldtype": "jira", "fromString": before, "toString": after, **ids}


def test_history_recorded_while_in_a_project_not_selected_is_withheld(folder):
    entries = {"20005": [
        {"id": "13", "author": ADA, "created": "2026-09-04T00:00:00.000+0000", "items": [history_item("summary", "Old words", "Issue 5")]},
        {"id": "11", "author": ADA, "created": "2026-09-01T00:00:00.000+0000", "items": [
            history_item("status", "To Do", "Outside legal hold"), history_item("Sprint", None, "Outside sprint 9"),
            history_item("Component", None, "Outside payroll component")]},
        {"id": "12", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": [
            history_item("Key", "OUT-7", "SEED-5"), history_item("project", "Outside", "Seed project", **{"from": "10002", "to": "10000"})]}]}
    found, census, records = read(FakeJira([issue(5)], entries), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"Outside" not in archive and b"OUT-7" not in archive and b"Old words" in archive


def test_a_round_trip_through_a_project_not_selected_withholds_the_whole_stay(folder):
    move = lambda ident, when, before, after, old, new: {"id": ident, "author": ADA, "created": when, "items": [
        history_item("Key", old, new), history_item("project", *(p["name"] for p in (before, after)), **{"from": before["id"], "to": after["id"]})]}
    seed, out = PROJECTS[0], PROJECTS[2]
    entries = {"20005": [
        {"id": "25", "author": ADA, "created": "2026-09-05T00:00:00.000+0000", "items": [history_item("summary", "Later words", "Issue 5")]},
        move("22", "2026-09-02T00:00:00.000+0000", seed, out, "SEED-5", "OUT-9"),
        {"id": "21", "author": ADA, "created": "2026-09-01T00:00:00.000+0000", "items": [history_item("summary", "First words", "Next")]},
        move("24", "2026-09-04T00:00:00.000+0000", out, seed, "OUT-9", "SEED-5"),
        {"id": "23", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": [history_item("status", "To Do", "Outside legal hold")]}]}
    found, census, records = read(FakeJira([issue(5)], entries), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"Outside" not in archive and b"OUT-9" not in archive and b"legal hold" not in archive
    assert b"First words" in archive and b"Later words" in archive


def test_a_move_whose_from_is_missing_fails_closed(folder):
    entries = {"20005": [
        {"id": "41", "author": ADA, "created": "2026-09-01T00:00:00.000+0000", "items": [history_item("status", "To Do", "Outside legal hold")]},
        {"id": "42", "author": ADA, "created": "2026-09-02T00:00:00.000+0000", "items": [
            history_item("project", "Outside", "Seed project", to="10000")]},
        {"id": "43", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": [history_item("summary", "Later words", "Issue 5")]}]}
    found, census, records = read(FakeJira([issue(5)], entries), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"Outside" not in archive and b"Later words" in archive


def test_history_is_ordered_by_instant_then_id_across_offsets_and_ties(folder):
    move = lambda ident, when, before, after: {"id": ident, "author": ADA, "created": when, "items": [
        history_item("project", None, None, **{"from": before, "to": after})]}
    fall_back = {"20005": [  # 01:30-07:00 is 08:30Z, before the 01:15-08:00 move at 09:15Z, though it sorts after it as text
        move("52", "2026-11-01T01:15:00.000-0800", "10002", "10000"),
        {"id": "51", "author": ADA, "created": "2026-11-01T01:30:00.000-0700", "items": [history_item("status", "To Do", "Outside legal hold")]}]}
    found, census, records = read(FakeJira([issue(5)], fall_back), folder, ["SEED"])
    assert b"Outside legal hold" not in render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    same_time = {"20005": [  # two moves in one second, newest first: the id orders them
        move("62", "2026-09-02T00:00:00.000+0000", "10002", "10000"), move("61", "2026-09-02T00:00:00.000+0000", "10000", "10002"),
        {"id": "60", "author": ADA, "created": "2026-09-01T00:00:00.000+0000", "items": [history_item("summary", "Early words", "Issue 5")]}]}
    found, census, records = read(FakeJira([issue(5)], same_time), folder, ["SEED"])
    assert b"Early words" in render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})


def test_a_history_entry_without_a_readable_time_or_id_is_withheld_and_the_read_goes_on(folder):
    status = lambda words: [history_item("status", "To Do", words)]
    entries = {"20005": [
        {"id": "71", "author": ADA, "items": status("No time")},
        {"id": "72", "author": ADA, "created": "yesterday", "items": status("Bad time")},
        {"id": "73", "author": ADA, "created": None, "items": status("Null time")},
        {"id": "x74", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": status("Bad id")},
        {"author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": status("No id")},
        {"id": "75", "author": ADA, "created": "2026-09-04T00:00:00.000+0000", "items": [history_item("summary", "Good words", "Issue 5")]}]}
    found, census, records = read(FakeJira([issue(5)], entries), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"Good words" in archive
    assert not [words for words in (b"No time", b"Bad time", b"Null time", b"Bad id", b"No id") if words in archive]


def test_a_move_at_a_time_that_cannot_be_read_withholds_the_whole_history(folder):
    entries = {"20005": [
        {"id": "81", "author": ADA, "created": "2026-09-01T00:00:00.000+0000", "items": [history_item("status", "To Do", "Outside legal hold")]},
        {"id": "82", "author": ADA, "created": "not a time", "items": [
            history_item("project", "Seed project", "Outside", **{"from": "10000", "to": "10002"})]},
        {"id": "83", "author": ADA, "created": "2026-09-03T00:00:00.000+0000", "items": [history_item("summary", "Later words", "Issue 5")]}]}
    found, census, records = read(FakeJira([issue(5)], entries), folder, ["SEED"])
    archive = render.archive(records[0].raw, found.withheld(found.context(census.selected)), {})
    assert b"Outside" not in archive and b"legal hold" not in archive and b"Later words" not in archive


# ---------------------------------------------------------------- project docs (docs/plans/importers-jira-projects.md)

def docs(records):
    return {record.key: record for record in records if record.kind == "project"}


def sprinted(n, *sprints, project="SEED", **fields):
    return issue(n, project, customfield_10020=list(sprints), **fields)


def test_a_project_selected_as_a_project_becomes_one_doc_of_what_a_team_needs(folder):
    fake = FakeJira([sprinted(1, sprint(7)), sprinted(2, sprint(7), sprint(6, "closed", "Close the beta")), issue(3, "OPS")])
    found, census, records = read(fake, folder, ["SEED"])
    assert list(docs(records)) == ["SEED"] and census.issues == 2
    doc = docs(records)["SEED"]
    assert (doc.ident, doc.title, doc.url) == ("p10000", "Seed project", "https://acme.atlassian.net/browse/SEED")
    assert doc.opened == ["Lead: Ben Sample"] and doc.fields == {} and not doc.comments and not doc.relations
    assert doc.body == "\n\n".join([
        "What Seed project is for.\n\\# Not a heading",
        "## Components\n\n- API · lead Ada Example\n- Web · no lead",
        "## Versions\n\n- 1.0 · released · start none · release 2026-06-01\n- 2.0 · unreleased · start 2026-09-01 · release 2026-11-01",
        "## Sprints\n\n- Sprint 6 · closed · 2026-09-06T00:00:00.000Z to 2026-09-19T00:00:00.000Z · completed 2026-09-19T09:00:00.000Z"
        " · goal: Close the beta\n- Sprint 7 · active · 2026-09-07T00:00:00.000Z to 2026-09-20T00:00:00.000Z · goal: Ship sign-in"])
    projects = [request for request in fake.requests if request.url.path.endswith("/project/search")]
    assert all(parse_qs(urlsplit(str(r.url)).query)["expand"] == ["description,lead"] for r in projects)
    assert "project docs: 1 read" in census.notes


def test_the_project_archive_holds_only_what_the_doc_shows_and_no_email(folder):
    found, census, records = read(FakeJira([sprinted(1, sprint(7))]), folder, ["SEED"])
    doc = docs(records)["SEED"]
    archive = json.loads(render.archive(doc.raw, found.withheld(found.context(census.selected)), {}))
    person = lambda user: {"accountId": user["accountId"], "displayName": user["displayName"]}
    assert archive == {
        "project": {"id": "10000", "key": "SEED", "name": "Seed project", "description": "What Seed project is for.\n# Not a heading",
                    "lead": person(BEN)},
        "components": [{"id": "10100", "name": "API", "lead": person(ADA)}, {"id": "10101", "name": "Web", "lead": None}],
        "versions": [{"id": "10200", "name": "1.0", "released": True, "archived": False, "startDate": None, "releaseDate": "2026-06-01"},
                     {"id": "10201", "name": "2.0", "released": False, "archived": False, "startDate": "2026-09-01",
                      "releaseDate": "2026-11-01"}],
        "sprints": [{"id": 7, "name": "Sprint 7", "state": "active", "startDate": "2026-09-07T00:00:00.000Z",
                     "endDate": "2026-09-20T00:00:00.000Z", "completeDate": None, "goal": "Ship sign-in"}]}
    assert "@example.com" not in doc.body + json.dumps(archive)


def test_a_named_issue_brings_no_doc_for_its_project(folder):
    found, census, records = read(FakeJira([issue(1), issue(5, "OPS")]), folder, ["SEED", "OPS-5"])
    assert list(docs(records)) == ["SEED"] and {r.key for r in records if r.kind == "issue"} == {"SEED-1", "OPS-5"}
    found, census, records = read(FakeJira([issue(5, "OPS")]), folder, ["OPS-5"])
    assert not docs(records) and not [note for note in census.notes if note.startswith("project docs")]


def test_a_sprint_holding_only_a_skipped_restricted_issue_appears_on_no_doc(folder):
    fake = FakeJira([sprinted(1, sprint(7)), sprinted(9, sprint(8, goal="Secret"), security={"name": "Staff only"})])
    found, census, records = read(fake, folder, ["SEED"])
    assert "Sprint 8" not in docs(records)["SEED"].body and "Secret" not in json.dumps(docs(records)["SEED"].raw)


def test_a_project_and_an_issue_with_the_same_id_stay_apart(folder):
    from import_fakes import FakeWirk
    from test_import_wirk import make, outcomes
    same = issue(1)
    same["id"] = "10000"  # Jira's project and issue IDs are separate sequences that meet
    blocked = issue(2, issuelinks=[{"id": "500", "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                                    "inwardIssue": ref("SEED-1", "10000")}])
    found, census, records = read(FakeJira([same, blocked]), folder, ["SEED"])
    assert sorted(r.ident for r in records) == ["10000", "20002", "p10000"]
    store = FakeWirk(fields={})
    result = outcomes(make(store, ctx=found.context(census.selected)).run(records))
    assert result == {("SEED-1", "issue"): "created", ("SEED-2", "issue"): "created", ("SEED", "project"): "created"}
    work = {i: item for i, item in store.items.items() if item["revisions"][-1]["work"] is not None}
    assert [(l["type"], l["from"] in work, l["to"] in work) for l in store.links.values()] == [("requires", True, True)]


def run_into(store, fake, folder, selection, clock="2026-10-03T12:00:00Z"):
    from test_import_wirk import make
    found = jira.Jira(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=[].append, folder=folder, clock=lambda: clock)
    found.check({})
    census, records = found.read(list(selection))
    return {(o.key, o.kind): o.outcome for o in make(store, ctx=found.context(census.selected)).run(records)}


def test_a_rerun_writes_nothing_when_only_what_the_doc_does_not_show_changes(folder):
    from import_fakes import FakeWirk
    store, fake = FakeWirk(fields={}), FakeJira([sprinted(1, sprint(7)), sprinted(2, sprint(6, "closed"))])
    assert run_into(store, fake, folder, ["SEED"])[("SEED", "project")] == "created"
    fake.projects[0].update(favourite=True, insight={"totalIssueCount": 9}, permissions={"canEdit": True}, isPrivate=True)
    fake.components["10000"][0]["issueCount"] = 40
    fake.versions["10000"][0].update(overdue=True, userStartDate="1 Sep", userReleaseDate="1 Nov")
    fake.issues[0], fake.issues[1] = sprinted(1, sprint(6, "closed")), sprinted(2, sprint(7))  # the same two sprints, swapped
    before = len(store.writes())
    result = run_into(store, fake, folder, ["SEED"], clock="2026-10-04T08:00:00Z")
    assert result[("SEED", "project")] == "current"
    assert [w for w in store.writes()[before:] if "Jira project" in json.dumps(w)] == []


def test_a_moved_release_date_or_a_started_sprint_revises_the_project_doc(folder):
    from import_fakes import FakeWirk
    store, fake = FakeWirk(fields={}), FakeJira([sprinted(1, sprint(7, "future")), issue(2)])
    run_into(store, fake, folder, ["SEED"])
    fake.versions["10000"][0]["releaseDate"] = "2026-12-01"
    result = run_into(store, fake, folder, ["SEED"])
    assert result[("SEED", "project")] == "updated" and result[("SEED-2", "issue")] == "current"
    fake.issues[0] = sprinted(1, sprint(7, "active"))
    assert run_into(store, fake, folder, ["SEED"])[("SEED", "project")] == "updated"


@pytest.mark.parametrize("where", ["components", "version"])
@pytest.mark.parametrize("answer", [httpx.Response(403), httpx.Response(404), httpx.Response(400), httpx.Response(500)])
def test_a_project_whose_components_or_versions_cannot_be_read_gets_no_doc_and_the_run_goes_on(where, answer, folder):
    from import_fakes import FakeWirk
    store, fake = FakeWirk(fields={}), FakeJira([issue(1), issue(2, "OPS")])
    assert run_into(store, fake, folder, ["SEED", "OPS"])[("SEED", "project")] == "created"
    fake.answers[f"/rest/api/3/project/10000/{where}"] = answer
    fake.issues[0] = issue(1, summary="Changed")
    found = jira.Jira(http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=[].append, folder=folder)
    found.check({})
    census, records = found.read(["SEED", "OPS"])
    assert list(docs(records)) == ["OPS"] and len([r for r in records if r.kind == "issue"]) == 2
    assert "project docs: 1 read · 1 skipped (components or versions could not be read): SEED" in census.notes
    result = run_into(store, fake, folder, ["SEED", "OPS"])
    assert result[("SEED-1", "issue")] == "updated" and ("SEED", "project") not in result  # the earlier doc stays as it was


@pytest.mark.parametrize("answer", [httpx.Response(401, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"}), httpx.Response(401)])
def test_a_captcha_or_a_dead_token_during_a_components_read_still_stops(answer, folder):
    fake = FakeJira([issue(1)])
    fake.answers["/rest/api/3/project/10000/components"] = answer
    with pytest.raises(Stop) as stop:
        read(fake, folder, ["SEED"])
    assert type(stop.value) is Stop and ("CAPTCHA" in str(stop.value) or "refused the token" in str(stop.value))


def test_a_narrower_selection_reports_no_project_missing_but_one_no_longer_browsed_is(folder):
    from import_fakes import FakeWirk
    store, fake = FakeWirk(fields={}), FakeJira([issue(1), issue(2, "OPS")])
    run_into(store, fake, folder, ["SEED", "OPS"])
    assert "missing" not in run_into(store, fake, folder, ["SEED"]).values()
    fake.projects = [p for p in fake.projects if p["key"] != "OPS"]
    result = run_into(store, fake, folder, ["SEED"])
    assert result[("OPS", "project")] == "missing"
    assert not any(item["archived"] for item in store.items.values())
