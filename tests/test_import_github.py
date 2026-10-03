"""The GitHub adapter (docs/plans/importers.md §4.1, §4.2) on synthetic GraphQL pages through a fake `gh`."""

import json

import pytest

from wirk_cli.importers import github, render

PUBLIC, PRIVATE = {"nameWithOwner": "acme/web", "visibility": "PUBLIC"}, {"nameWithOwner": "acme/secret", "visibility": "PRIVATE"}
API = {"nameWithOwner": "acme/api", "visibility": "PRIVATE"}
EMPTY = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def page(nodes, more=False, cursor=None):
    return {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}


def repo(name, visibility="PUBLIC", issues=2, pulls=3, archived=False, enabled=True):
    return {"nameWithOwner": name, "visibility": visibility, "isArchived": archived, "hasIssuesEnabled": enabled,
            "issues": {"totalCount": issues}, "pullRequests": {"totalCount": pulls}}


def node(number, **changes):
    base = {"id": f"I_{number}", "fullDatabaseId": str(3000000000 + number), "number": number, "title": f"Issue {number}",
            "body": f"Body {number}", "url": f"https://github.com/acme/web/issues/{number}", "state": "OPEN", "stateReason": None,
            "createdAt": "2026-09-01T10:00:00Z", "updatedAt": "2026-09-30T12:00:00Z", "closedAt": None,
            "lastEditedAt": None, "locked": False, "activeLockReason": None, "isPinned": False,
            "author": {"__typename": "User", "login": "ada"}, "assignees": {"nodes": []}, "labels": page([]),
            "milestone": None, "issueType": None, "issueFieldValues": {"nodes": []}, "parent": None,
            "subIssues": page([]), "blockedBy": page([]), "blocking": page([]), "relatesTo": page([]), "duplicateOf": None,
            "reactionGroups": [], "comments": page([]), "timelineItems": page([])}
    base.update(changes)
    return base


def ref(number, repository=PUBLIC):
    return {"number": number, "fullDatabaseId": str(3000000000 + number), "repository": repository}


class FakeGh:
    """Answers `gh api` reads: the user's scopes, repositories, issue pages and nested pages."""

    def __init__(self, repos, issues, scopes="repo, read:org", extra=None):
        self.repos, self.issues, self.scopes, self.extra, self.calls = repos, issues, scopes, extra or {}, []
        self.cut_timelines = False

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ["api", "-i"]:
            return 0, f"HTTP/2.0 200 OK\nX-Oauth-Scopes: {self.scopes}\n\n{{\"login\": \"ada\"}}", ""
        request = json.loads(stdin)
        query, variables = request["query"], request["variables"]
        limit = {"cost": 1, "remaining": 4999, "resetAt": "2026-10-02T13:00:00Z"}
        if "repositoryOwner" in query:
            return 0, json.dumps({"data": {"rateLimit": limit, "repositoryOwner": {"repositories": page(self.repos)}}}), ""
        if "issues(first" in query:
            name = f"{variables['owner']}/{variables['name']}"
            found = next(r for r in self.repos if r["nameWithOwner"] == name)
            nodes = self.issues.get(name, [])
            start = int(variables.get("after") or 0)
            window = nodes[start:start + variables["size"]]
            more = start + variables["size"] < len(nodes)
            if self.cut_timelines:  # what GitHub's paged query does to some issues, without saying so
                window = [{**n, "timelineItems": page([])} for n in window]
            return 0, json.dumps({"data": {"rateLimit": limit, "repository": {**found, "page": page(
                window, more, str(start + variables["size"]) if more else None)}}}), ""
        if "repository(owner" in query:
            name = f"{variables['owner']}/{variables['name']}"
            found = next((r for r in self.repos if r["nameWithOwner"] == name), None)
            return 0, json.dumps({"data": {"rateLimit": limit, "repository": found}}), ""
        if "node(id" in query and "timelineItems(" in query and "page: issues" not in query:
            issue = next(n for nodes in self.issues.values() for n in nodes if n["id"] == variables["id"])
            return 0, json.dumps({"data": {"rateLimit": limit, "node": {"timelineItems": issue["timelineItems"]}}}), ""
        if "node(id" in query:
            key = (variables["id"], variables.get("after", "html"))
            found = self.extra[key] if key in self.extra or key[1] != "html" else {"bodyHTML": ""}
            return 0, json.dumps({"data": {"rateLimit": limit, "node": found}}), ""
        raise AssertionError(query[:80])


def read(fake, selection=("acme",)):
    adapter = github.GitHub(run=fake, sleep=lambda seconds: None)
    adapter.check()
    return adapter, *adapter.read(list(selection))


# ---------------------------------------------------------------- checks and selection

def test_no_login_stops_with_the_fix():
    adapter = github.GitHub(run=lambda args, stdin=None: (1, "", "To get started with GitHub CLI, please run: gh auth login"),
                            sleep=lambda s: None)
    with pytest.raises(github.Stop) as stop:
        adapter.check()
    assert "gh auth login" in stop.value.fix


def test_an_owner_means_its_public_repositories_and_names_the_rest():
    fake = FakeGh([repo("acme/web"), repo("acme/api", "PRIVATE", issues=812), repo("acme/off", enabled=False),
                   repo("acme/empty", "PRIVATE", issues=0)], {})
    adapter, census, records = read(fake)
    assert census.selected == ["acme/web"]
    assert census.skipped == [("acme/api", 812)]
    assert census.pulls == 3
    _, census, _ = read(FakeGh([repo("acme/web"), repo("acme/api", "PRIVATE")], {}), ("acme", "acme/api"))
    assert census.selected == ["acme/api", "acme/web"] and census.skipped == []
    assert census.named_private == [("acme/api", "private", 2)]


def test_projects_are_reported_unread_without_the_scope():
    adapter, census, _ = read(FakeGh([repo("acme/web")], {}))
    assert "gh auth refresh -s read:project" in census.notes[0]


# ---------------------------------------------------------------- mapping

def test_an_issue_maps_to_a_record():
    issue = node(5, state="CLOSED", stateReason="COMPLETED", closedAt="2026-09-03T09:00:00Z", lastEditedAt="2026-09-02T00:00:00Z",
                 assignees={"nodes": [{"login": "ben"}, {"login": "ada"}]},
                 labels=page([{"name": "zeta", "description": ""}, {"name": "alpha", "description": "first"}]),
                 milestone={"title": "v1.0", "dueOn": "2026-12-15T08:00:00Z", "state": "OPEN"}, issueType={"name": "Bug"},
                 issueFieldValues={"nodes": [
                     {"__typename": "IssueFieldSingleSelectValue", "name": "High", "field": {"name": "Priority"}},
                     {"__typename": "IssueFieldDateValue", "value": "2026-11-30", "field": {"name": "Target date"}}]},
                 locked=True, activeLockReason="TOO_HEATED", isPinned=True,
                 reactionGroups=[{"content": "THUMBS_UP", "reactors": {"totalCount": 3}}, {"content": "EYES", "reactors": {"totalCount": 0}}],
                 timelineItems=page([{"__typename": "ClosedEvent", "createdAt": "2026-09-03T09:00:00Z", "actor": {"login": "ben"},
                                      "stateReason": "COMPLETED", "closer": {"__typename": "PullRequest", "number": 9,
                                                                            "merged": True, "repository": PUBLIC}},
                                     {"__typename": "CrossReferencedEvent", "createdAt": "2026-09-02T00:00:00Z", "actor": {"login": "ada"},
                                      "source": {"__typename": "PullRequest", "number": 9, "repository": PUBLIC}},
                                     {"__typename": "CrossReferencedEvent", "createdAt": "2026-09-02T01:00:00Z", "actor": {"login": "ada"},
                                      "source": {"__typename": "Issue", "number": 4, "repository": PRIVATE}}]))
    _, _, records = read(FakeGh([repo("acme/web")], {"acme/web": [issue]}))
    record = records[0]
    assert (record.ident, record.key, record.state) == ("3000000005", "acme/web#5", "completed")
    assert record.closed == "closed as completed by @ben at 2026-09-03T09:00:00Z"
    assert record.opened == ["Opened by @ada 2026-09-01T10:00:00Z · edited · closed by @ben 2026-09-03T09:00:00Z as completed"]
    assert "Type: Bug · Milestone: v1.0 (due 2026-12-15, open)" in record.facts
    assert "Labels: alpha, zeta" in record.facts and "Issue fields: Priority High · Target date 2026-11-30" in record.facts
    assert "Locked on GitHub: too heated" in record.facts and "Pinned on GitHub" in record.facts
    assert "Reactions: 👍 3" in record.facts
    assert record.assignees == [("ben", "@ben"), ("ada", "@ada")]
    assert record.fields == {"Repository": ["acme/web"], "Label": ["alpha", "zeta"], "Milestone": ["v1.0"], "Issue type": ["Bug"],
                             "Priority": ["High"]}
    assert record.due == "2026-11-30"
    kinds = [(kind, ref.key, ref.public, ref.note) for kind, ref in record.relations]
    assert ("closed_by", "acme/web#9", True, "pull request, merged") in kinds
    assert ("mentioned", "acme/web#9", True, "pull request") in kinds
    assert ("mentioned", "acme/secret#4", False, "issue") in kinds


@pytest.mark.parametrize("state,reason,expected", [("OPEN", None, "open"), ("OPEN", "REOPENED", "open"),
                                                   ("CLOSED", "NOT_PLANNED", "cancelled"), ("CLOSED", "DUPLICATE", "cancelled"),
                                                   ("CLOSED", None, "completed")])
def test_states(state, reason, expected):
    _, _, records = read(FakeGh([repo("acme/web")], {"acme/web": [node(1, state=state, stateReason=reason)]}))
    assert records[0].state == expected


def test_relations_keep_their_ids_and_order():
    issue = node(8, parent=ref(1), subIssues=page([ref(12), ref(10), ref(3, PRIVATE)]), blockedBy=page([ref(20)]),
                 blocking=page([ref(21)]), duplicateOf=ref(2), relatesTo=page([ref(30)]))
    _, _, records = read(FakeGh([repo("acme/web")], {"acme/web": [issue]}))
    relations = [(kind, ref.key, ref.ident) for kind, ref in records[0].relations]
    assert relations[:3] == [("parent", "acme/web#1", "3000000001"), ("sub_issue", "acme/web#12", "3000000012"),
                             ("sub_issue", "acme/web#10", "3000000010")]
    assert ("blocked_by", "acme/web#20", "3000000020") in relations and ("duplicate_of", "acme/web#2", "3000000002") in relations
    assert ("related", "acme/web#30", "3000000030") in relations and ("blocking", "acme/web#21", "3000000021") in relations


def test_people_bots_and_deleted_accounts():
    comments = page([{"fullDatabaseId": "1", "author": None, "body": "gone", "createdAt": "2026-09-02T00:00:00Z",
                      "lastEditedAt": None, "isMinimized": False, "minimizedReason": None, "reactionGroups": []},
                     {"fullDatabaseId": "2", "author": {"__typename": "Bot", "login": "github-actions"}, "body": "beep",
                      "createdAt": "2026-09-03T00:00:00Z", "lastEditedAt": "2026-09-04T00:00:00Z", "isMinimized": True,
                      "minimizedReason": "outdated", "reactionGroups": [{"content": "HEART", "reactors": {"totalCount": 2}}]}])
    _, _, records = read(FakeGh([repo("acme/web")], {"acme/web": [node(1, author=None, comments=comments)]}))
    record = records[0]
    assert record.opened[0].startswith("Opened by @ghost (deleted account)")
    first, second = record.comments
    assert first.author == "@ghost (deleted account)" and not first.edited
    assert (second.author, second.edited, second.hidden, second.reactions, second.version) == (
        "@github-actions[bot] (bot)", True, "outdated", "❤️ 2", "2026-09-04T00:00:00Z")


def test_the_archive_holds_no_comments_and_nothing_that_moves_with_them():
    issue = node(1, comments=page([{"fullDatabaseId": "1", "author": {"login": "ben"}, "body": "hi", "createdAt": "2026-09-02T00:00:00Z",
                                    "lastEditedAt": None, "isMinimized": False, "minimizedReason": None, "reactionGroups": []}]))
    _, _, records = read(FakeGh([repo("acme/web")], {"acme/web": [issue]}))
    raw = records[0].raw
    assert "comments" not in raw["issue"] and "updatedAt" not in raw["issue"]
    assert records[0].raw_comments == [issue["comments"]["nodes"][0]]


def test_timeline_leaves_out_comments_and_the_events_they_cause():
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1)]})
    read(fake)
    query = next(json.loads(stdin)["query"] for args, stdin in fake.calls if stdin and "timelineItems(" in stdin)
    types = query.split("itemTypes: [", 1)[1].split("]", 1)[0]
    for left_out in ("ISSUE_COMMENT", "MENTIONED_EVENT", "SUBSCRIBED_EVENT", "UNSUBSCRIBED_EVENT"):
        assert left_out not in types.split(", ")
    assert "CROSS_REFERENCED_EVENT" in types and "CLOSED_EVENT" in types


def test_withheld_references_are_found_wherever_they_sit():
    adapter = github.GitHub(run=FakeGh([], {}), sleep=lambda s: None)
    ctx = adapter.context(["acme/api"])
    check = adapter.withheld(ctx)
    assert check({"number": 1, "repository": PRIVATE}) and check({"fromRepository": PRIVATE})
    assert check({"commitRepository": PRIVATE}) and not check({"repository": PUBLIC}) and not check({"repository": API})


# ---------------------------------------------------------------- paging, pacing, read-only

def test_issue_pages_and_long_nested_connections_are_followed():
    comment = lambda n: {"fullDatabaseId": str(n), "author": {"login": "ben"}, "body": f"c{n}", "createdAt": f"2026-09-02T00:00:{n % 60:02d}Z",
                         "lastEditedAt": None, "isMinimized": False, "minimizedReason": None, "reactionGroups": []}
    issues = [node(n) for n in range(1, 60)]
    issues[0] = node(1, comments=page([comment(n) for n in range(100)], True, "c100"))
    extra = {("I_1", "c100"): {"comments": page([comment(n) for n in range(100, 105)])}}
    _, census, records = read(FakeGh([repo("acme/web", issues=59)], {"acme/web": issues}, extra=extra))
    assert len(records) == 59 and len(records[0].comments) == 105 and census.issues == 59
    assert census.points == 64  # the repositories, three issue pages, the comments' second page and 59 timelines, a point each


def test_a_page_that_times_out_is_retried_smaller():
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(n) for n in range(1, 4)]})
    sizes = []

    def flaky(args, stdin=None):
        if stdin and "issues(first" in stdin:
            sizes.append(json.loads(stdin)["variables"]["size"])
            if len(sizes) == 1:
                return 1, json.dumps({"errors": [{"message": "Something went wrong while executing your query. This may be the result of a timeout"}]}), "gh: HTTP 502"
        return fake(args, stdin)

    _, _, records = read(flaky)
    assert sizes[:2] == [25, 12] and len(records) == 3


def test_secondary_limits_back_off_and_a_low_budget_waits_for_the_reset():
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1)]})
    slept, tries = [], []

    def limited(args, stdin=None):
        if stdin and "issues(first" in stdin and len(tries) < 1:
            tries.append(1)
            return 1, json.dumps({"message": "You have exceeded a secondary rate limit"}), "gh: HTTP 403"
        return fake(args, stdin)

    adapter = github.GitHub(run=limited, sleep=slept.append)
    adapter.check()
    adapter.read(["acme"])
    assert slept and slept[0] >= 59  # the shared pause, less the moment that passed since it was set


def test_only_reads_reach_github():
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1)]})
    read(fake)
    for args, stdin in fake.calls:
        assert args[0] == "api" and "-X" not in args and "--method" not in args
        if stdin:
            assert not json.loads(stdin)["query"].lstrip().startswith("mutation")


def test_timelines_are_read_issue_by_issue():
    """GitHub's paged query can return an issue's timeline short, totalCount included, so each is read on its own."""
    events = page([{"__typename": "LabeledEvent", "createdAt": "2026-09-02T00:00:00Z", "actor": {"login": "ada"},
                    "label": {"name": "bug"}}])
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1, timelineItems=events)]})
    fake.cut_timelines = True
    _, _, records = read(fake)
    assert len(records[0].raw["issue"]["timelineItems"]["nodes"]) == 1
    query = next(json.loads(stdin)["query"] for args, stdin in fake.calls if stdin and "page: issues" in stdin)
    assert "timelineItems(" not in query  # pages no longer ask for timelines at all


def test_timelines_are_read_four_at_a_time_and_kept_in_order():
    """Decision 80: at most four timeline reads at once; the records keep GitHub's order."""
    import threading
    import time as clock
    events = lambda n: page([{"__typename": "LabeledEvent", "createdAt": "2026-09-02T00:00:00Z", "actor": {"login": "ada"},
                              "label": {"name": f"l{n}"}}])
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(n, timelineItems=events(n)) for n in range(1, 13)]})
    lock, state = threading.Lock(), {"now": 0, "most": 0}

    def tracked(args, stdin=None):
        alone = stdin and "timelineItems(" in stdin and "page: issues" not in stdin
        if alone:
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            clock.sleep(0.05)
        try:
            return fake(args, stdin)
        finally:
            if alone:
                with lock:
                    state["now"] -= 1

    _, census, records = read(tracked)
    assert state["most"] == 4
    assert [r.key for r in records] == [f"acme/web#{n}" for n in range(1, 13)]
    assert [r.raw["issue"]["timelineItems"]["nodes"][0]["label"]["name"] for r in records] == [f"l{n}" for n in range(1, 13)]
    assert census.points == 2 + 12  # the repositories, one issue page and a timeline each


def test_a_secondary_limit_pauses_every_reader():
    """One reader's secondary-limit refusal holds the others too: one budget, shared."""
    gate = github.Gate(sleep=lambda seconds: slept.append(seconds))
    slept = []
    gate.hold(60)
    import threading
    threads = [threading.Thread(target=gate.wait) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(slept) == 4 and all(55 <= seconds <= 60 for seconds in slept)


def test_a_spent_hourly_budget_waits_for_its_reset():
    """GitHub's primary limit ("API rate limit already exceeded") is a wait until the hour's reset, never a failure."""
    from datetime import datetime, timedelta, timezone
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1)]})
    reset = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    refused, slept = [], []

    def spent(args, stdin=None):
        query = json.loads(stdin)["query"] if stdin else ""
        if "page: issues" in query and not refused:
            refused.append(1)
            return 1, json.dumps({"errors": [{"type": "RATE_LIMIT", "code": "graphql_rate_limit",
                                              "message": "API rate limit already exceeded for user ID 1."}]}), "gh: API rate limit already exceeded"
        if query.strip() == github.RESET:
            return 0, json.dumps({"data": {"rateLimit": {"cost": 1, "remaining": 0, "resetAt": reset}}}), ""
        return fake(args, stdin)

    adapter = github.GitHub(run=spent, sleep=slept.append)
    adapter.check()
    census, records = adapter.read(["acme"])
    assert len(records) == 1 and refused and slept and 1700 < max(slept) <= 1810


def test_a_pause_extended_while_a_reader_sleeps_holds_it_again():
    now, slept = [0.0], []
    gate = github.Gate(clock=lambda: now[0])

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
        if len(slept) == 1:
            gate.hold(30)  # another reader meets a limit meanwhile
    gate.sleep = sleep
    gate.hold(10)
    gate.wait()
    assert slept == [10, 30]


@pytest.mark.parametrize("refusals, read_through", [(4, True), (5, False)])
def test_a_fourth_secondary_limit_still_retries_after_its_pause(refusals, read_through):
    fake = FakeGh([repo("acme/web")], {"acme/web": [node(1)]})
    slept, tries = [], []

    def limited(args, stdin=None):
        if stdin and "page: issues" in stdin and len(tries) < refusals:
            tries.append(1)
            return 1, json.dumps({"message": "You have exceeded a secondary rate limit"}), "gh: HTTP 403"
        return fake(args, stdin)

    adapter = github.GitHub(run=limited, sleep=slept.append)
    adapter.check()
    if read_through:
        assert len(adapter.read(["acme"])[1]) == 1 and [round(s) for s in slept[:4]] == [60, 120, 240, 480]
    else:
        with pytest.raises(github.Stop):
            adapter.read(["acme"])
