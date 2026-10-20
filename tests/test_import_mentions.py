"""Historical GitHub issue mentions resolve to native links without losing their source events."""

from dataclasses import replace
import json

import pytest

from import_fakes import FakeWirk
from test_import_links import item_of, links, successful_writes
from test_import_github import node, page, repo
from test_import_wirk import CTX, issue, make
from wirk_cli.importers import github
from wirk_cli.importers.render import Ref


REPOS = ("Rockielab/rockie-runtime", "Rockielab/platform-frontend",
         "Rockielab/platform-context", "Rockielab/rockie-workspace")
CONTEXT = replace(CTX, selected=frozenset(REPOS))


def record(number, repo, **changes):
    return issue(number, key=f"{repo}#{number}", url=f"https://github.com/{repo}/issues/{number}",
                 fields={"Repository": [repo]}, **changes)


def ref(number, repo, *, ident=None, note="issue"):
    return Ref(f"{repo}#{number}", repo, False, ident if ident is not None else str(3000000000 + number), note)


@pytest.fixture
def fake():
    return FakeWirk(fields={"repository": {"rockielab_rockie_runtime": "active",
                                          "rockielab_platform_frontend": "active",
                                          "rockielab_platform_context": "active",
                                          "rockielab_rockie_workspace": "active"}})


def importer(fake, **options):
    return make(fake, ctx=CONTEXT, **options)


def test_selected_four_repo_issue_mentions_make_one_native_link_and_keep_the_source_line(fake):
    source = record(77, REPOS[0], relations=[
        ("mentioned", ref(80, REPOS[3])), ("mentioned", ref(80, REPOS[3])),
        ("mentioned", Ref(f"{REPOS[3]}#80", REPOS[3], False, None, "pull request"))])
    records = [source, record(80, REPOS[3]), record(3, REPOS[1]), record(4, REPOS[2])]
    importer(fake).run(records)
    assert links(fake) == [("related_to", 77, 80)]
    assert f"Mentioned in: [{REPOS[3]}#80]" in fake.items[item_of(fake, 77)]["revisions"][-1]["body"]
    before = len(fake.writes())
    importer(fake).run(records)
    assert len(fake.writes()) == before


@pytest.mark.parametrize("source,target", [(80, 77), (77, 80)])
def test_current_source_links_to_earlier_imported_target_in_either_id_order(fake, source, target):
    earlier = record(target, REPOS[3])
    importer(fake).run([earlier])
    current = record(source, REPOS[0], relations=[("mentioned", ref(target, REPOS[3]))])
    importer(fake).run([current])
    assert links(fake) == [("related_to", 77, 80)]
    before = len(fake.writes())
    importer(fake).run([current])
    assert len(fake.writes()) == before


def test_old_serialized_issue_archive_recovers_native_link_after_target_arrives(fake):
    events = [{"__typename": "CrossReferencedEvent", "createdAt": when,
               "source": {"__typename": "Issue", "number": 80, "fullDatabaseId": "3000000080",
                          "repository": {"nameWithOwner": REPOS[3], "visibility": "PRIVATE"}}}
              for when in ("2026-09-18T01:02:03Z", "2026-09-19T01:02:03Z")]
    source_repo = repo(REPOS[0], "PRIVATE")
    source_node = node(77, url=f"https://github.com/{REPOS[0]}/issues/77",
                       timelineItems=page(events))
    adapter = github.GitHub(run=lambda *args: None)
    old = adapter.record(source_repo, source_node)
    importer(fake).run([old])
    old_item = fake.items[item_of(fake, 77)]["revisions"][-1]
    archive = next(f for f in old_item["files"] if f["filename"].startswith("github-issue-"))
    serialized = fake.objects[archive["sha256"]]
    archived = json.loads(serialized)
    assert archived["issue"]["timelineItems"]["nodes"] == events

    importer(fake).run([record(80, REPOS[3])])
    restored_node = {**archived["issue"], "id": "I_77", "updatedAt": old.version,
                     "comments": page([])}
    restored = adapter.record(source_repo, restored_node)
    importer(fake).run([restored])
    assert links(fake) == [("related_to", 77, 80)]
    assert fake.objects[archive["sha256"]] == serialized
    assert json.loads(fake.objects[archive["sha256"]])["issue"]["timelineItems"]["nodes"] == events


def test_non_issue_or_unselected_or_self_mention_never_links(fake):
    records = [record(77, REPOS[0], relations=[
        ("mentioned", Ref(f"{REPOS[1]}#80", REPOS[1], False, None, "pull request")),
        ("mentioned", ref(77, REPOS[0])), ("mentioned", ref(81, "Other/account"))]),
        record(80, REPOS[1]), record(81, REPOS[2])]
    importer(fake).run(records)
    assert links(fake) == []


def test_same_issue_number_in_other_owner_cannot_resolve_by_text(fake):
    source = record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3], ident="9999990080"))])
    importer(fake).run([source, record(80, REPOS[3])])
    assert links(fake) == []


def test_earlier_target_link_is_not_removed_when_current_record_loses_mention(fake):
    importer(fake).run([record(80, REPOS[3])])
    current = record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3]))])
    importer(fake).run([current])
    changed = replace(current, relations=[], version="2026-10-01T12:00:00Z")
    importer(fake).run([changed])
    assert links(fake) == [("related_to", 77, 80)]


def test_person_created_native_link_is_reused(fake):
    source, target = record(77, REPOS[0]), record(80, REPOS[3])
    importer(fake).run([source, target])
    a, b = item_of(fake, 77), item_of(fake, 80)
    fake.as_whom = "bob"
    fake.write({"request_id": "person-link", "operations": [
        {"op": "link.create", "data": {"type": "related_to", "from": a, "to": b}}]})
    fake.as_whom = None
    importer(fake).run([replace(source, relations=[("mentioned", ref(80, REPOS[3]))]), target])
    assert links(fake) == [("related_to", 77, 80)]
    assert len([link for link in fake.links.values() if not link["removed"]]) == 1


def test_archived_ambiguous_and_other_importer_targets_are_not_guessed(fake):
    target = record(80, REPOS[3])
    importer(fake).run([target])
    target_item = item_of(fake, 80)
    fake.as_whom = "bob"
    fake.write({"request_id": "archive-target", "reason": "Not needed", "expect": {target_item: 1},
                "operations": [{"op": "item.archive", "id": target_item}]})
    fake.as_whom = None
    importer(fake).run([record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3]))])])
    assert links(fake) == []

    another = FakeWirk(fields=fake.definitions)
    importer(another).run([target])
    original = another.items[item_of(another, 80)]["revisions"][-1]
    another.write({"request_id": "duplicate-target", "operations": [
        {"op": "item.create", "data": {"title": "Ambiguous", "body": original["body"], "work": {},
                                      "fields": original["fields"]}}]})
    importer(another).run([record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3]))])])
    assert links(another) == []

    blocked = FakeWirk(fields=fake.definitions)
    blocked.as_whom = "other-github-import"
    importer(blocked).run([target])
    blocked.as_whom = None
    importer(blocked).run([record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3]))])])
    assert links(blocked) == []


def test_a_different_wirkspace_cannot_supply_the_prior_target(fake):
    importer(fake).run([record(80, REPOS[3])])
    other_space = FakeWirk(fields=fake.definitions)
    importer(other_space).run([record(77, REPOS[0], relations=[("mentioned", ref(80, REPOS[3]))])])
    assert links(other_space) == []

@pytest.mark.parametrize("dependent,blocker", [(9, 1), (1, 9)])
def test_a_mention_does_not_replace_a_distinct_blocker_in_either_id_order(fake, dependent, blocker):
    records = [record(blocker, REPOS[0]), record(dependent, REPOS[0], relations=[
        ("blocked_by", ref(blocker, REPOS[0])), ("mentioned", ref(blocker, REPOS[0]))])]
    importer(fake).run(records)
    assert set(links(fake)) == {("related_to", 1, 9), ("requires", dependent, blocker)}
    before = len(fake.writes())
    importer(fake).run(records)
    assert len(fake.writes()) == before


@pytest.mark.parametrize("dependent,blocker", [(9, 1), (1, 9)])
def test_an_earlier_mention_then_only_a_blocker_still_makes_the_gate(fake, dependent, blocker):
    first = [record(blocker, REPOS[0]), record(dependent, REPOS[0], relations=[
        ("mentioned", ref(blocker, REPOS[0]))])]
    importer(fake).run(first)
    assert links(fake) == [("related_to", 1, 9)]
    later = [record(blocker, REPOS[0]), record(dependent, REPOS[0], relations=[
        ("blocked_by", ref(blocker, REPOS[0]))])]
    importer(fake).run(later)
    assert ("requires", dependent, blocker) in links(fake)


def test_a_preexisting_person_related_link_cannot_stand_in_for_a_blocker(fake):
    importer(fake).run([record(1, REPOS[0]), record(9, REPOS[0])])
    one, nine = item_of(fake, 1), item_of(fake, 9)
    fake.as_whom = "bob"
    fake.write({"request_id": "person-related", "operations": [
        {"op": "link.create", "data": {"type": "related_to", "from": one, "to": nine}}]})
    fake.as_whom = None
    records = [record(1, REPOS[0]), record(9, REPOS[0], relations=[("blocked_by", ref(1, REPOS[0]))])]
    importer(fake).run(records)
    assert set(links(fake)) == {("related_to", 1, 9), ("requires", 9, 1)}


def test_a_refused_gate_reuses_the_mention_and_keeps_another_gate_in_the_batch(fake):
    importer(fake).run([record(n, REPOS[0]) for n in (1, 2, 9)])
    one, nine = item_of(fake, 1), item_of(fake, 9)
    fake.as_whom = "bob"
    fake.write({"request_id": "person-reverse-gate", "expect": {one: 1}, "operations": [
        {"op": "link.create", "data": {"type": "requires", "from": one, "to": nine}}]})
    fake.as_whom = None
    records = [record(1, REPOS[0]), record(2, REPOS[0]), record(9, REPOS[0], relations=[
        ("blocked_by", ref(1, REPOS[0])), ("blocked_by", ref(2, REPOS[0])),
        ("mentioned", ref(1, REPOS[0]))])]
    importer(fake).run(records)
    assert set(links(fake)) == {("related_to", 1, 9), ("requires", 1, 9), ("requires", 9, 2)}
    before = len(successful_writes(fake))
    importer(fake).run(records)
    assert len(successful_writes(fake)) == before
    assert len([link for link in fake.links.values() if not link["removed"] and link["type"] == "related_to"]) == 1


def test_a_refused_gate_reuses_mention_after_a_person_completed_work(fake):
    records = [record(1, REPOS[0]), record(9, REPOS[0], relations=[
        ("blocked_by", ref(1, REPOS[0])), ("mentioned", ref(1, REPOS[0]))])]
    importer(fake).run(records)
    nine = item_of(fake, 9)
    gate = next(key for key, link in fake.links.items() if link["type"] == "requires")
    fake.as_whom = "bob"
    fake.write({"request_id": "person-removed-gate", "operations": [{"op": "link.remove", "id": gate}]})
    fake.write({"request_id": "person-completed", "reason": "Done", "expect": {nine: 1}, "operations": [
        {"op": "item.edit", "id": nine, "patch": {"fields": {"status": "completed"}}}]})
    fake.as_whom = None
    importer(fake).run(records)
    assert links(fake) == [("related_to", 1, 9)]
    before = len(successful_writes(fake))
    importer(fake).run(records)
    assert len(successful_writes(fake)) == before


def test_refused_gate_reuses_related_edge_written_in_an_earlier_chunk(fake):
    numbers = [1, 9, 40, *range(10, 40)]
    importer(fake).run([record(n, REPOS[0]) for n in numbers])
    one, nine = item_of(fake, 1), item_of(fake, 9)
    fake.as_whom = "bob"
    fake.write({"request_id": "person-cycle-boundary", "expect": {nine: 1}, "operations": [
        {"op": "link.create", "data": {"type": "requires", "from": nine, "to": one}}]})
    fake.as_whom = None
    related = [("related", ref(n, REPOS[0])) for n in range(10, 40)]
    source = record(1, REPOS[0], relations=[("mentioned", ref(9, REPOS[0])),
            ("blocked_by", ref(9, REPOS[0])), ("blocked_by", ref(40, REPOS[0])), *related])
    records = [source, *[record(n, REPOS[0]) for n in numbers if n != 1]]
    importer(fake).run(records)
    found = links(fake)
    assert ("requires", 1, 40) in found and ("requires", 1, 9) not in found
    assert sum(link[0] == "related_to" for link in found) == 31
    before = len(successful_writes(fake))
    importer(fake).run(records)
    assert len(successful_writes(fake)) == before
