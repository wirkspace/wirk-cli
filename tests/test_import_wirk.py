"""The importer against WIRK (docs/plans/importers.md §3.4–§3.7): the index and its trust rules, the decision for each
object, one issue per write, refusals, receipts and files, on an in-memory WIRK."""

import httpx
import pytest

from import_fakes import FakeWirk, refusal
from wirk_cli.client import Service
from wirk_cli.importers import render, wirk
from wirk_cli.importers.render import Comment, Context, Record, Ref

CTX = Context(source="GitHub", selected=frozenset({"acme/api"}), noun=("repository", "repositories"),
              labels={"parent": "Parent", "sub_issue": "Sub-issues in GitHub order", "blocked_by": "Blocked by",
                      "blocking": "Blocking", "duplicate_of": "Duplicate of", "related": "Related", "closed_by": "Closed by",
                      "mentioned": "Mentioned in", "transferred_from": "Transferred from"})
LABELS = {"open": "open", "in_progress": "in_progress", "completed": "completed", "cancelled": "cancelled"}


def issue(number, **changes):
    base = dict(source="GitHub", kind="issue", ident=str(3000000000 + number), key=f"acme/api#{number}",
                url=f"https://github.com/acme/api/issues/{number}", version="2026-09-30T12:00:00Z",
                title=f"Issue {number}", body=f"Body of {number}.", state="open", closed=None,
                opened=[f"Opened by @ada 2026-09-01T10:00:00Z"], facts=[], assignees=[], fields={"Repository": ["acme/api"]},
                due=None, relations=[], comments=[], attachments=[], raw={"number": number}, raw_comments=[])
    base.update(changes)
    return Record(**base)


def said(text, when="2026-09-02T11:00:00Z"):
    return Comment(author="@ben", created=when, edited=False, body=text, reactions="", hidden=None, version=when)


def make(fake, **options):
    service = Service("https://wirk.test", "wirk_" + "t" * 43, transport=httpx.MockTransport(fake))
    plan = render.plan_fields([], {"Repository": "one"}, "GitHub")
    defaults = dict(users={}, statuses=LABELS, plan=plan, withheld=lambda node: False, download=lambda attachment: b"",
                    dry_run=False, overwrite=False)
    importer = wirk.Importer(wirk.Wirk(service), CTX, **{**defaults, **options})
    importer.start()
    return importer


@pytest.fixture
def fake():
    return FakeWirk(fields={"repository": {"acme_api": "active"}})


def item_of_issue(fake, number):
    return next(i for i, item in fake.mine().items() if f" {3000000000 + number}," in item["revisions"][-1]["body"].split("\n")[0])


def outcomes(result):
    return {(o.key, o.kind): o.outcome for o in result}


# ---------------------------------------------------------------- creating

def test_each_issue_is_one_work_item_with_its_archive_and_one_discussion_doc(fake):
    result = make(fake).run([issue(1, comments=[said("Hi."), said("Again.")]), issue(2)])
    assert outcomes(result) == {("acme/api#1", "issue"): "created", ("acme/api#1", "comments"): "created",
                                ("acme/api#2", "issue"): "created"}
    writes = fake.writes()
    assert len(writes) == 2 and len(writes[0]["operations"]) == 3  # one issue per write: work, doc, their link
    work = [i for i in fake.items.values() if i["revisions"][-1]["work"] is not None]
    assert len(work) == 2
    snap = work[0]["revisions"][-1]
    assert render.provenance(snap["body"].split("\n")[0])[:3] == ("GitHub", "issue", "3000000001")
    assert snap["fields"] == {"status": "open", "repository": "acme_api"}
    assert [f["filename"] for f in snap["files"]] == ["github-issue-3000000001.json"]
    doc = next(i for i in fake.items.values() if i["revisions"][-1]["work"] is None)["revisions"][-1]
    assert [f["filename"] for f in doc["files"]] == ["github-comments-3000000001.json"]
    assert "comment" not in snap["body"].casefold()  # nothing about comments on the work item
    assert [l["type"] for l in fake.links.values()] == ["related_to"]


def test_a_completed_issue_is_created_completed_with_its_evidence(fake):
    make(fake).run([issue(3, state="completed", closed="closed as completed by @ada at 2026-03-04T09:00:00Z")])
    write = fake.writes()[0]
    assert write["reason"].startswith("Imported from GitHub: acme/api#3 closed as completed by @ada")
    assert next(iter(fake.items.values()))["revisions"][-1]["fields"]["status"] == "completed"


def test_fields_without_an_option_stay_text_and_are_reported(fake):
    fake.definitions["label"] = {"bug": "active"}
    importer = make(fake, plan=render.plan_fields([issue(4, fields={"Repository": ["acme/api"], "Label": ["bug", "new"]})],
                                                  {"Repository": "one", "Label": "many"}, "GitHub"))
    importer.run([issue(4, fields={"Repository": ["acme/api"], "Label": ["bug", "new"]})])
    assert next(iter(fake.mine().values()))["revisions"][-1]["fields"]["label"] == ["bug"]
    assert importer.missing == {"label": {"new"}}


# ---------------------------------------------------------------- re-running

def test_a_second_run_writes_and_uploads_nothing(fake):
    records = [issue(1, comments=[said("Hi.")]), issue(2)]
    make(fake).run(records)
    before = len(fake.requests)
    result = make(fake).run(records)
    assert set(outcomes(result).values()) == {"current"}
    assert not [r for r, _ in fake.requests[before:] if r.endswith(("/write", "/files"))]


def test_a_new_comment_revises_only_the_discussion_doc(fake):
    make(fake).run([issue(1, comments=[said("Hi.")])])
    result = make(fake).run([issue(1, comments=[said("Hi."), said("New.", "2026-10-01T00:00:00Z")])])
    assert outcomes(result) == {("acme/api#1", "issue"): "current", ("acme/api#1", "comments"): "updated"}
    work = next(iter(fake.mine().values()))
    assert len(work["revisions"]) == 1


def test_a_change_in_the_source_updates_with_expect(fake):
    make(fake).run([issue(1)])
    result = make(fake).run([issue(1, title="Renamed", state="completed", closed="closed as completed by @ada at T")])
    assert outcomes(result) == {("acme/api#1", "issue"): "updated"}
    write = fake.writes()[-1]
    assert list(write["expect"].values()) == [1] and write["reason"].startswith("Imported from GitHub: acme/api#1 closed")
    assert next(iter(fake.mine().values()))["revisions"][-1]["title"] == "Renamed"


def test_an_edit_in_wirk_wins_until_overwrite(fake):
    make(fake).run([issue(1)])
    item = next(iter(fake.mine()))
    fake.as_whom = "bob"
    fake.write({"request_id": "bob-1", "expect": {item: 1}, "operations": [{"op": "item.edit", "id": item, "patch": {"title": "Mine now"}}]})
    fake.as_whom = None
    result = make(fake).run([issue(1, body="Changed at the source.")])
    assert result[0].outcome == "skipped" and "changed in WIRK since import (r2 by bob)" in result[0].message
    listed = make(fake, dry_run=True, overwrite=True).run([issue(1, body="Changed at the source.")])[0]
    assert listed.outcome == "would update" and "changed in WIRK since import (r2 by bob)" in listed.message
    result = make(fake, overwrite=True).run([issue(1, body="Changed at the source.")])
    assert result[0].outcome == "updated" and fake.writes()[-1]["expect"] == {item: 2}
    assert "changed in WIRK since import (r2 by bob); replaced by --overwrite" in result[0].message


def test_an_unchanged_source_stays_current_whoever_edited_last(fake):
    make(fake).run([issue(1)])
    item = next(iter(fake.mine()))
    fake.as_whom = "bob"
    fake.write({"request_id": "bob-1", "expect": {item: 1}, "operations": [{"op": "item.edit", "id": item, "patch": {"fields": {"status": "in_progress"}}}]})
    fake.as_whom = None
    assert make(fake).run([issue(1)])[0].outcome == "current"


# ---------------------------------------------------------------- trust (A11)

def test_a_forged_line_is_ignored(fake):
    fake.as_whom = "mallory"
    line = render.line1("GitHub", "issue", "3000000001", "2026-09-30T12:00:00Z", "0" * 12)
    fake.write({"request_id": "m-1", "operations": [{"op": "item.create", "data": {"title": "Forged", "body": line, "work": {}}}]})
    fake.as_whom = None
    importer = make(fake)
    assert outcomes(importer.run([issue(1)])) == {("acme/api#1", "issue"): "created"}
    assert importer.index.forged == 1


def test_a_retargeted_item_keeps_the_key_the_importer_wrote(fake):
    make(fake).run([issue(1), issue(2)])
    first = next(i for i, item in fake.mine().items() if "3000000001" in item["revisions"][-1]["body"].split("\n")[0])
    body = fake.items[first]["revisions"][-1]["body"]
    fake.as_whom = "bob"  # point issue 1's item at issue 3
    fake.write({"request_id": "bob-1", "expect": {first: 1}, "operations": [{"op": "item.edit", "id": first,
                "patch": {"body": body.replace("3000000001", "3000000003", 1)}}]})
    fake.as_whom = None
    result = outcomes(make(fake).run([issue(1, body="Changed."), issue(2), issue(3)]))
    assert result[("acme/api#1", "issue")] == "skipped"  # its own item, person-revised
    assert result[("acme/api#3", "issue")] == "created"  # untouched by the retargeted line
    assert result[("acme/api#2", "issue")] == "current"


def edit_as(fake, who, item, body):
    fake.as_whom = who
    fake.write({"request_id": f"{who}-{len(fake.receipts)}", "expect": {item: len(fake.items[item]["revisions"])},
                "operations": [{"op": "item.edit", "id": item, "patch": {"body": body}}]})
    fake.as_whom = None


def test_a_line_a_person_writes_above_the_first_keeps_the_item_the_importers(fake):
    make(fake).run([issue(1)])
    item = next(iter(fake.mine()))
    edit_as(fake, "bob", item, "Triage note from Bob.\n" + fake.items[item]["revisions"][-1]["body"])
    assert outcomes(make(fake).run([issue(1)])) == {("acme/api#1", "issue"): "current"}
    assert outcomes(make(fake).run([issue(1, body="Changed.")])) == {("acme/api#1", "issue"): "skipped"}
    assert len(fake.mine()) == 1


def test_an_issue_gone_from_the_source_is_named_under_a_persons_line_too(fake):
    make(fake).run([issue(1), issue(2)])
    item = item_of_issue(fake, 1)
    edit_as(fake, "bob", item, "Triage note from Bob.\n" + fake.items[item]["revisions"][-1]["body"])
    assert outcomes(make(fake).run([issue(2)])) == {("acme/api#1", "issue"): "missing", ("acme/api#2", "issue"): "current"}

def test_a_duplicate_choice_the_importer_made_is_never_overridden(fake):
    make(fake).run([issue(1, title="Same words")])
    item = next(iter(fake.mine()))
    edit_as(fake, "bob", item, "Bob rewrote all of it.")  # no line of the importer's left to find it by
    fake.duplicates["Same words"] = item
    result = make(fake).run([issue(1, title="Same words")])
    assert result[0].outcome == "error" and "likely_duplicate" in result[0].message
    assert len(fake.mine()) == 1 and "allow_duplicate_of" not in str(fake.writes())


def test_another_importers_items_block_their_keys(fake):
    fake.as_whom = "bob-github-import"
    make(fake).run([issue(1)])
    fake.as_whom = None
    before = len(fake.writes())
    importer = make(fake)
    result = importer.run([issue(1), issue(2)])
    assert outcomes(result)[("acme/api#1", "issue")] == "blocked"
    assert "bob-github-import" in result[0].message
    assert importer.index.blocked == {"bob-github-import": 1}
    assert len(fake.writes()) == before + 1  # only issue 2


# ---------------------------------------------------------------- the rest of the decision table

def test_archived_skipped_ambiguous_and_missing(fake):
    make(fake).run([issue(1), issue(2), issue(3)])
    items = {item["revisions"][-1]["body"].split("\n")[0].split(" ")[2][:-1]: i for i, item in fake.mine().items()}
    fake.as_whom = "bob"  # a person archives it
    fake.write({"request_id": "a-1", "reason": "Not needed", "expect": {items["3000000001"]: 1},
                "operations": [{"op": "item.archive", "id": items["3000000001"]}]})
    fake.as_whom = None
    twin = fake.items[items["3000000002"]]["revisions"][-1]
    fake.write({"request_id": "t-1", "operations": [{"op": "item.create", "data": {"title": "Twin", "body": twin["body"], "work": {}}}]})
    before = len(fake.writes())
    result = outcomes(make(fake).run([issue(1, title="Changed"), issue(2, title="Changed")]))
    assert result == {("acme/api#1", "issue"): "skipped", ("acme/api#2", "issue"): "ambiguous",
                      ("acme/api#3", "issue"): "missing"}
    assert len(fake.writes()) == before
    assert not fake.items[items["3000000003"]]["archived"]


def test_a_dry_run_writes_and_uploads_nothing(fake):
    result = make(fake, dry_run=True).run([issue(1, comments=[said("Hi.")])])
    assert set(outcomes(result).values()) == {"would create"}
    assert not [r for r, _ in fake.requests if r.endswith(("/write", "/files"))]


# ---------------------------------------------------------------- refusals and receipts

def test_a_likely_duplicate_is_resent_and_reported(fake):
    make(fake).run([issue(1, title="Same words")])
    fake.duplicates["Same words"] = next(iter(fake.mine()))
    result = make(fake).run([issue(1, title="Same words"), issue(2, title="Same words")])
    created = [o for o in result if o.key == "acme/api#2"][0]
    assert created.outcome == "created" and "possible duplicate kept separate" in created.message
    resent = fake.writes()[-1]
    assert resent["operations"][0]["allow_duplicate_of"] == [fake.duplicates["Same words"]]
    assert "Separate GitHub issues acme/api#2 and acme/api#1; imported as they are" in resent["reason"]
    assert "possible duplicate kept separate: acme/api#1" in created.message


def test_an_owner_who_is_not_a_member_is_neither_claimed_nor_sealed_until_they_join(fake):
    records = [issue(n, assignees=[("ben", "@ben")]) for n in (1, 2)]
    importer = make(fake, users={"ben": "carol"})
    assert set(outcomes(importer.run(records)).values()) == {"created"} and importer.not_members == {"carol"}
    for item in fake.mine().values():
        snap = item["revisions"][-1]
        assert snap["work"] == {} and "owner carol" not in snap["body"] and "@ben (not in WIRK)" in snap["body"]
    before = len(fake.writes())
    assert set(outcomes(make(fake, users={"ben": "carol"}).run(records)).values()) == {"current"}
    assert len(fake.writes()) == before + 1  # one refused try a run, to learn carol is still not a member
    fake.members.add("carol")
    assert set(outcomes(make(fake, users={"ben": "carol"}).run(records)).values()) == {"updated"}
    for item in fake.mine().values():
        snap = item["revisions"][-1]
        assert snap["work"]["owner_id"] == "carol" and "@ben (owner carol)" in snap["body"]


def test_a_lost_answer_is_settled_by_its_receipt(fake):
    fake.lose("write", after=True, times=2)  # the write ran; both the answer and the identical resend's answer are lost
    result = make(fake).run([issue(1)])
    assert result[0].outcome == "created" and "receipt" in result[0].message
    assert len(fake.mine()) == 1


def test_a_refusal_or_an_unsettled_write_is_an_error_for_that_issue_and_the_run_goes_on(fake):
    make(fake).run([issue(1), issue(2)])
    first = item_of_issue(fake, 1)
    query = fake.query
    fake.query = lambda body: refusal("not_available", "No record") if first in str(body.get("fetch")) else query(body)
    result = outcomes(make(fake).run([issue(1, body="Changed."), issue(2, body="Changed.")]))
    assert result == {("acme/api#1", "issue"): "error", ("acme/api#2", "issue"): "updated"}
    fake.query = query
    fake.lose("write", after=False, times=2, when=lambda body: "3000000003" in body["request_id"])  # never ran, never settled
    result = make(fake).run([issue(1), issue(2), issue(3), issue(4)])
    assert outcomes(result)[("acme/api#3", "issue")] == "error" and "outcome_unknown" in result[2].message
    assert outcomes(result)[("acme/api#4", "issue")] == "created"
    assert outcomes(make(fake).run([issue(3)]))[("acme/api#3", "issue")] == "created"


# ---------------------------------------------------------------- parts

def test_parts_go_one_to_a_write_and_stale_parts_are_archived(fake):
    big = [said("漢" * 60000, f"2026-09-0{n}T00:00:00Z") for n in range(1, 8)]
    make(fake).run([issue(1, comments=big)])
    docs = fake.mine("doc")
    assert len(docs) == 2 and len(fake.writes()) == 2
    assert all(len(w["operations"]) <= 32 for w in fake.writes())
    result = outcomes(make(fake).run([issue(1, comments=big[:2])]))
    assert result[("acme/api#1", "comments-2")] == "archived"
    assert sum(1 for item in fake.mine("doc").values() if item["archived"]) == 1


def test_an_issue_whose_work_item_is_archived_or_ambiguous_gets_no_new_discussion(fake):
    make(fake).run([issue(1), issue(2)])
    items = {item["revisions"][-1]["body"].split("\n")[0].split(" ")[2][:-1]: i for i, item in fake.mine().items()}
    fake.as_whom = "bob"  # a person archives it
    fake.write({"request_id": "a-1", "reason": "Not needed", "expect": {items["3000000001"]: 1},
                "operations": [{"op": "item.archive", "id": items["3000000001"]}]})
    fake.as_whom = None
    twin = fake.items[items["3000000002"]]["revisions"][-1]
    fake.write({"request_id": "t-1", "operations": [{"op": "item.create", "data": {"title": "Twin", "body": twin["body"], "work": {}}}]})
    before = len(fake.writes())
    result = outcomes(make(fake).run([issue(1, comments=[said("Hi.")]), issue(2, comments=[said("Hi.")])]))
    assert result == {("acme/api#1", "issue"): "skipped", ("acme/api#1", "comments"): "skipped",
                      ("acme/api#2", "issue"): "ambiguous", ("acme/api#2", "comments"): "ambiguous"}
    assert len(fake.writes()) == before


def test_what_the_source_archived_is_archived_after_its_write_and_restored_with_it(fake):
    gone = "Archived in Linear on 2026-03-01"
    first = make(fake).run([issue(1, archived=gone, comments=[said("Hi.")]), issue(2)])
    assert [(o.kind, o.outcome) for o in first if o.key == "acme/api#1"] == [
        ("issue", "created"), ("comments", "created"), ("issue", "archived"), ("comments", "archived")]
    one = item_of_issue(fake, 1)
    assert fake.items[one]["archived"] and fake.writes()[1]["reason"] == gone
    before = len(fake.writes())
    assert set(outcomes(make(fake).run([issue(1, archived=gone, comments=[said("Hi.")]), issue(2)])).values()) == {"current"}
    assert len(fake.writes()) == before
    result = make(fake).run([issue(1, comments=[said("Hi.")]), issue(2)])
    assert [(o.kind, o.outcome) for o in result if o.key == "acme/api#1"] == [
        ("issue", "current"), ("comments", "current"), ("issue", "restored"), ("comments", "restored")]
    assert not fake.items[one]["archived"]
    two = item_of_issue(fake, 2)
    fake.as_whom = "bob"  # an archive made by a person is theirs
    fake.write({"request_id": "bob-a", "reason": "Not ours", "expect": {two: 1}, "operations": [{"op": "item.archive", "id": two}]})
    fake.as_whom = None
    assert outcomes(make(fake).run([issue(1, comments=[said("Hi.")]), issue(2)]))[("acme/api#2", "issue")] == "skipped"
    dry = make(fake, dry_run=True).run([issue(1, archived=gone, comments=[said("Hi.")]), issue(2)])
    assert ("issue", "would archive") in [(o.kind, o.outcome) for o in dry if o.key == "acme/api#1"]


def test_an_item_updated_while_archived_is_still_restored_when_the_source_restores_it(fake):
    make(fake).run([issue(1, archived="Archived in Linear on 2026-03-01")])
    result = make(fake).run([issue(1, body="Changed when it came back.")])
    assert [(o.kind, o.outcome) for o in result] == [("issue", "updated"), ("issue", "restored")]
    assert not fake.items[item_of_issue(fake, 1)]["archived"]
