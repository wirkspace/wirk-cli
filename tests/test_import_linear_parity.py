"""What a Linear issue keeps when it reaches WIRK, end to end: Linear's GraphQL answers (the recorded shapes of
test_import_linear) read by the real adapter, written into an in-memory WIRK. No call reaches Linear or a real WIRK."""

import httpx
import pytest

from import_fakes import FakeWirk
from test_import_linear import FakeLinear, comment, folder, issue, read, ref, state  # noqa: F401 - folder is a fixture
from wirk_cli.client import Service
from wirk_cli.importers import DEFAULT_STATUSES, render, wirk

WORKFLOW = {"triage": "active", "todo": "active", "in_review": "active", "done": "active"}


@pytest.fixture
def hub():
    return FakeWirk(fields={"team": {"eng": "active"}, "workflow": WORKFLOW})


def importer(linear, folder, hub, **options):
    source, census, records = read(linear, folder)
    ctx = source.context(census.selected)
    service = Service("https://wirk.test", "wirk_" + "t" * 43, transport=httpx.MockTransport(hub))
    run = wirk.Importer(wirk.Wirk(service), ctx, users={}, statuses=DEFAULT_STATUSES,
                        plan=render.plan_fields(records, source.selections(records), source.source), withheld=source.withheld(ctx),
                        download=source.download, dry_run=False, overwrite=False, rewrite=source.rewrite, **options)
    run.start()
    return run, records


def go(linear, folder, hub, **options):
    """The outcomes for the issues: the projects, milestones and the rest the fixture holds are not what is tested."""
    run, records = importer(linear, folder, hub, **options)
    return [o for o in run.run(records) if o.kind == "issue"]


def item(hub, n):
    """The item made for issue n, found by its first line the first time and remembered after a person rewrites it."""
    seen = hub.__dict__.setdefault("seen", {})
    for i, found in hub.mine().items():
        if f"Linear issue {uid_of(n)}, " in found["revisions"][0]["body"]:
            seen[n] = i
    return seen[n]


def latest(hub, n):
    return hub.items[item(hub, n)]["revisions"][-1]


def person(hub, who, n, patch):
    target = item(hub, n)
    hub.as_whom = who
    hub.write({"request_id": f"{who}-{len(hub.receipts)}", "expect": {target: len(hub.items[target]["revisions"])},
               "operations": [{"op": "item.edit", "id": target, "patch": patch}]})
    hub.as_whom = None


def moved(n, name, **changes):
    return issue(n, state={"id": f"st-ENG-{name}"}, **changes)


# ---------------------------------------------------------------- statuses

def test_a_state_keeps_its_name_whatever_its_type(folder, hub):
    linear = FakeLinear([moved(1, "Triage"), moved(2, "Todo"), moved(3, "In Review")])
    go(linear, folder, hub)
    assert [(latest(hub, n)["fields"]["status"], latest(hub, n)["fields"]["workflow"]) for n in (1, 2, 3)] == \
        [("open", "triage"), ("open", "todo"), ("in_progress", "in_review")]
    assert "Status: Triage (triage)" in latest(hub, 1)["body"]


def test_a_state_wirk_has_no_option_for_is_said_and_kept_in_the_header(folder):
    hub = FakeWirk(fields={"team": {"eng": "active"}, "workflow": {"todo": "active"}})
    result = go(FakeLinear([moved(1, "In Review")]), folder, hub)
    assert "not options in WIRK, kept in the header: Workflow: In Review" in result[0].message
    assert "Status: In Review (started)" in latest(hub, 1)["body"] and latest(hub, 1)["fields"]["status"] == "in_progress"


def test_one_option_wirk_lacks_does_not_cost_the_other_fields(folder):
    hub = FakeWirk(fields={"team": {"eng": "active"}, "workflow": {"todo": "active"}})
    hub.faults = []
    go(FakeLinear([moved(1, "Todo")]), folder, hub)
    del hub.definitions["workflow"]["todo"]  # retired in WIRK after the plan was made
    result = go(FakeLinear([moved(1, "Todo", description="Changed")]), folder, hub)
    assert latest(hub, 1)["fields"].get("team") == ["eng"] and "workflow" not in latest(hub, 1)["fields"], result


def test_a_state_moved_in_linear_reaches_an_item_a_person_edited_and_only_that(folder, hub):
    go(FakeLinear([moved(1, "Todo")]), folder, hub)
    person(hub, "bob", 1, {"title": "Mine now"})
    result = go(FakeLinear([moved(1, "In Review", description="Edited in Linear too")]), folder, hub)
    assert [o.outcome for o in result] == ["updated"] and "only what changed in Linear was updated (status, workflow)" in result[0].message
    found = hub.items[item(hub, 1)]["revisions"][-1]
    assert (found["title"], found["fields"]["status"], found["fields"]["workflow"]) == ("Mine now", "in_progress", "in_review")
    assert "Edited in Linear too" not in found["body"]  # a person's item keeps its text; --overwrite replaces it
    before = len(hub.writes())
    assert [o.outcome for o in go(FakeLinear([moved(1, "In Review", description="Edited in Linear too")]), folder, hub)] == ["skipped"]
    assert len(hub.writes()) == before


def test_a_persons_own_status_stands_until_linear_changes_it(folder, hub):
    go(FakeLinear([moved(1, "Todo")]), folder, hub)
    person(hub, "bob", 1, {"fields": {"status": "in_progress"}})
    go(FakeLinear([moved(1, "Todo", description="Only the text changed")]), folder, hub)
    assert latest(hub, 1)["fields"]["status"] == "in_progress"
    go(FakeLinear([moved(1, "Done", completedAt="2026-09-30T00:00:00.000Z", description="Only the text changed")]), folder, hub)
    assert (latest(hub, 1)["fields"]["status"], latest(hub, 1)["fields"]["workflow"]) == ("completed", "done")


def test_an_edit_the_importer_made_after_a_persons_does_not_free_the_text_for_overwriting(folder, hub):
    go(FakeLinear([moved(1, "Todo")]), folder, hub)
    person(hub, "bob", 1, {"body": "Bob's note\n" + latest(hub, 1)["body"]})
    go(FakeLinear([moved(1, "In Review")]), folder, hub)  # the importer's revision is now the newest
    result = go(FakeLinear([moved(1, "In Review", description="Changed again")]), folder, hub)
    assert [o.outcome for o in result] == ["skipped"] and latest(hub, 1)["body"].startswith("Bob's note\n")


# ---------------------------------------------------------------- issue references

def mentions(n, text):
    return issue(n, description=text)


def test_references_in_descriptions_and_comments_become_links_to_imported_items(folder, hub):
    text = ("See ENG-2 and https://linear.app/acme/issue/ENG-2/issue-2 and [ENG-2](https://linear.app/acme/issue/ENG-2/issue-2 \"x\"), "
            "not ENG-9 or `ENG-2` or https://linear.app/acme/issue/ENG-9/other or XENG-2.")
    linear = FakeLinear([mentions(1, text), issue(2)], comments=[comment(1, 1, "Same as ENG-2, and ENG-1.")])
    go(linear, folder, hub)
    path = f"/work?workspace=wsp_{'1' * 32}&item={item(hub, 2)}".replace("&", "&")
    body = latest(hub, 1)["body"].split("\n\n", 1)[1]
    assert body == (f"See [ENG-2]({path.replace('&', '&')}) and [ENG-2]({path}) and [ENG-2]({path} \"x\"), "
                    "not ENG-9 or `ENG-2` or https://linear.app/acme/issue/ENG-9/other or XENG-2.")
    discussion = next(i for i in hub.mine("record").values() if "comments on [ENG-1]" in i["revisions"][-1]["body"])
    assert f"Same as [ENG-2]({path}), and [ENG-1](/work?workspace=wsp_{'1' * 32}&item={item(hub, 1)})." in discussion["revisions"][-1]["body"]


def test_a_reference_is_settled_in_the_run_that_creates_both_and_a_second_run_writes_nothing(folder, hub):
    linear = FakeLinear([mentions(1, "Needs ENG-2"), mentions(2, "Needs ENG-1")])
    result = go(linear, folder, hub)
    assert sorted(o.outcome for o in result) == ["created", "created"]  # no "updated" for the second look
    assert all("](/work?" in latest(hub, n)["body"] for n in (1, 2))
    before = len(hub.writes())
    assert {o.outcome for o in go(linear, folder, hub)} == {"current"} and len(hub.writes()) == before


def test_a_reference_to_an_issue_not_imported_stays_as_it_was(folder, hub):
    go(FakeLinear([mentions(1, "Blocked on ENG-7")]), folder, hub)
    assert latest(hub, 1)["body"].endswith("Blocked on ENG-7")


# ---------------------------------------------------------------- relations

def blocks(blocker, blocked):
    return {"id": f"r-{blocker}-{blocked}", "type": "blocks", "issue": ref(blocker), "relatedIssue": ref(blocked)}


def link_types(hub):
    numbered = {i: int(f["revisions"][-1]["title"].split()[1]) for i, f in hub.mine().items() if f["revisions"][-1]["title"].startswith("Issue ")}
    return sorted((l["type"], numbered[l["from"]], numbered[l["to"]]) for l in hub.links.values()
                  if not l["removed"] and l["from"] in numbered and l["to"] in numbered)


def test_a_cancelled_blocker_is_still_a_prerequisite_and_a_completed_dependent_says_why(folder, hub):
    done = {"completedAt": "2026-09-30T00:00:00.000Z"}
    linear = FakeLinear([moved(1, "Won't Fix", canceledAt="2026-09-30T00:00:00.000Z"), issue(2), moved(3, "Done", **done), issue(4)],
                        relations=[blocks(1, 2), blocks(4, 3)])
    go(linear, folder, hub)
    assert link_types(hub) == [("related_to", 3, 4), ("requires", 2, 1)]
    assert "Blocked by: [ENG-4] (kept as related: WIRK cannot add a prerequisite to completed work)" in latest(hub, 3)["body"]


def test_a_duplicate_stays_related_and_its_direction_is_in_the_header(folder, hub):
    linear = FakeLinear([issue(1), issue(2)], relations=[{"id": "r", "type": "duplicate", "issue": ref(2), "relatedIssue": ref(1)}])
    go(linear, folder, hub)
    assert link_types(hub) == [("related_to", 1, 2)]
    assert "Duplicate of: [ENG-1] (kept as related" in latest(hub, 2)["body"] and "Duplicates: [ENG-2] (kept as related" in latest(hub, 1)["body"]


def test_links_to_issues_archived_in_linear_are_kept_and_written_once(folder, hub):
    old = moved(2, "Done", completedAt="2026-09-30T00:00:00.000Z", archivedAt="2026-10-01T00:00:00.000Z")
    linear = FakeLinear([issue(1), old, issue(3, parent={"id": uid_of(1), **ref(1)})],
                        relations=[blocks(2, 1), {"id": "r2", "type": "related", "issue": ref(1), "relatedIssue": ref(2)}])
    go(linear, folder, hub)
    assert hub.items[item(hub, 2)]["archived"] is True
    assert link_types(hub) == [("contributes_to", 3, 1), ("related_to", 1, 2), ("requires", 1, 2)]
    before = len(hub.writes())
    go(linear, folder, hub)
    assert len(hub.writes()) == before


def uid_of(n):
    return f"00000000-0000-4000-8000-{n:012d}"


# ---------------------------------------------------------------- uploaded images

URL = "https://uploads.linear.app/acme/abc/flow.png"


def test_an_image_in_linear_points_at_the_attached_file(folder, hub):
    linear = FakeLinear([mentions(1, f"Flow: ![the flow]({URL}) and a [spec]({URL}) and ![out](https://uploads.linear.app/acme/x/big.png)")],
                        files={URL: b"png bytes"})
    go(linear, folder, hub)
    found = latest(hub, 1)
    name = found["files"][-1]["filename"] if found["files"][-1]["filename"].endswith("flow.png") else found["files"][0]["filename"]
    assert f"![the flow]({name}) and a [spec]({name})" in found["body"]
    assert "![out](https://uploads.linear.app/acme/x/big.png)" in found["body"]  # not attached: stays Linear's link
    before = len(hub.writes())
    assert {o.outcome for o in go(linear, folder, hub)} == {"current"} and len(hub.writes()) == before
