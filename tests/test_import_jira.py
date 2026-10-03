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
BASE = "https://api.atlassian.com/ex/jira/cloud-1"


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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = urlsplit(str(request.url))
        if url.path == "/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": "cloud-1"})
        if self.captcha:
            return httpx.Response(401, headers={"X-Seraph-LoginReason": "AUTHENTICATION_DENIED"})
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "7", "RateLimit-Reason": "jira-burst-based"})
        path, query = url.path.removeprefix("/ex/jira/cloud-1"), parse_qs(url.query)
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json=ADA)
        if path == "/rest/api/3/project/search":
            start = int(query.get("startAt", ["0"])[0])
            return httpx.Response(200, json={"values": PROJECTS[start:start + 2], "startAt": start, "isLast": start + 2 >= len(PROJECTS)})
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
    found.check()
    return found, *found.read(list(selection))


def by_key(records):
    return {record.key: record for record in records}


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
        adapter(fake, folder).check()
    assert "chmod 600" in stop.value.fix
    os.remove(folder / "jira-key")
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check()
    assert "jira-key" in stop.value.fix and "read" in stop.value.fix.lower() and TOKEN not in str(stop.value) + stop.value.fix


def test_a_captcha_lockout_stops_and_a_429_waits_its_retry_after(folder):
    fake = FakeJira([issue(1)])
    fake.captcha = True
    with pytest.raises(Stop) as stop:
        adapter(fake, folder).check()
    assert "CAPTCHA" in str(stop.value)
    fake.captcha, fake.limited, slept = False, 1, []
    found = adapter(fake, folder, slept)
    found.check()
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
                 "Time: estimate 3h, remaining 1h, logged 2h 20m", "Watchers at import: 3 · Votes: 2",
                 "Customer tier: Gold · Owner: Team A · Region: EMEA › Germany · Build: build-77", "Web links: Runbook https://example.com/runbook"]:
        assert line in header
    assert record.body == "\\## Steps\n\n@Ada Example knows.\n\n## Acceptance notes\n\nMust not double charge."
    assert ("previously", "OPS-3") in [(kind, r.key) for kind, r in record.relations]
    assert record.raw["changelog"][0]["items"][0]["toString"] == "SEED-5"
    text = json.dumps(record.raw)
    assert "emailAddress" not in text and "avatarUrls" not in text and "lastViewed" not in text and "isWatching" not in text
    assert "hasVoted" not in text and '"updated"' not in json.dumps(record.raw["issue"]["fields"])


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
