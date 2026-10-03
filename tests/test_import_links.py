"""Links between imported issues (docs/plans/importers.md §3.6): planned once per pair, the fallbacks with their notes,
no link to an item archived in WIRK, the importer's own links kept in step, never a person's."""

from import_fakes import FakeWirk
from test_import_wirk import issue, make, outcomes
from wirk_cli.importers.render import Ref

import pytest


def to(number, scope="acme/api", public=False):
    return Ref(key=f"{scope}#{number}", scope=scope, public=public, ident=str(3000000000 + number))


@pytest.fixture
def fake():
    return FakeWirk(fields={"repository": {"acme_api": "active"}})


def links(fake):
    """(type, from issue number, to issue number) for every live link between work items."""
    number = {i: int(item["revisions"][-1]["body"].split("\n")[0].split(" ")[2][:-1]) - 3000000000
              for i, item in fake.mine().items()}
    found = [(l["type"], number[l["from"]], number[l["to"]]) for l in fake.links.values()
             if not l["removed"] and l["from"] in number and l["to"] in number]
    return sorted((kind, *sorted(ends)) if kind == "related_to" else (kind, *ends) for kind, *ends in found)


def item_of(fake, n):
    return next(i for i, item in fake.mine().items() if f" {3000000000 + n}," in item["revisions"][-1]["body"].split("\n")[0])


def body_of(fake, n):
    return fake.items[item_of(fake, n)]["revisions"][-1]["body"]


def test_parents_blockers_duplicates_and_relations_become_links_once(fake):
    records = [issue(1, relations=[("sub_issue", to(2)), ("blocking", to(3))]),
               issue(2, relations=[("parent", to(1))]),
               issue(3, relations=[("blocked_by", to(1)), ("duplicate_of", to(4)), ("related", to(4))]),
               issue(4, relations=[("related", to(3))])]
    make(fake).run(records)
    assert links(fake) == [("contributes_to", 2, 1), ("related_to", 3, 4), ("requires", 3, 1)]
    write = next(w for w in fake.writes() if any(o["op"] == "link.create" and o["data"]["type"] == "contributes_to" for o in w["operations"]))
    assert set(write["expect"]) == {item_of(fake, 1), item_of(fake, 2)}  # both ends of contributes_to


def test_the_fallbacks_and_their_notes(fake):
    records = [issue(10, state="completed", closed="closed as completed by @ada at T", relations=[("blocked_by", to(11))]),
               issue(11),
               issue(12, relations=[("blocked_by", to(13))]), issue(13, state="cancelled", closed="closed as not planned by @ada at T"),
               issue(14, relations=[("blocked_by", to(15))]), issue(15, relations=[("blocked_by", to(14))])]
    make(fake).run(records)
    assert links(fake) == [("related_to", 10, 11), ("related_to", 12, 13), ("related_to", 14, 15), ("requires", 14, 15)] or \
        links(fake) == [("related_to", 10, 11), ("related_to", 12, 13), ("related_to", 15, 14), ("requires", 14, 15)]
    assert "Blocked by: [acme/api#11] (kept as related: completed here)" in body_of(fake, 10)
    assert "Blocked by: [acme/api#13] (kept as related: the blocker is cancelled)" in body_of(fake, 12)
    assert "Blocked by: [acme/api#14] (kept as related: it would close a cycle)" in body_of(fake, 15)
    assert "Blocked by: [acme/api#15]\n" in body_of(fake, 14) or body_of(fake, 14).count("kept as related") == 0


def test_no_link_to_a_withheld_or_unimported_issue(fake):
    make(fake).run([issue(1, relations=[("blocked_by", to(7, scope="acme/secret")), ("parent", to(99))])])
    assert links(fake) == []


def test_no_link_to_an_item_archived_in_wirk_and_no_write_for_it_on_the_next_run(fake):
    make(fake).run([issue(1), issue(2)])
    archived = item_of(fake, 2)
    fake.write({"request_id": "a-1", "reason": "Not needed", "expect": {archived: 1}, "operations": [{"op": "item.archive", "id": archived}]})
    records = [issue(1, relations=[("related", to(2))]), issue(2, relations=[("related", to(1))])]
    make(fake).run(records)
    assert not [l for l in fake.links.values() if archived in (l["from"], l["to"]) and l["type"] != "related_to" or
                (l["type"] == "related_to" and archived in (l["from"], l["to"]) and l["by"] == "alice-github-import"
                 and fake.items[l["from"]]["revisions"][-1]["work"] is not None and fake.items[l["to"]]["revisions"][-1]["work"] is not None)]
    before = len(fake.writes())
    make(fake).run(records)
    assert len(fake.writes()) == before


def test_a_second_run_with_links_in_place_writes_nothing(fake):
    records = [issue(1, relations=[("sub_issue", to(2))]), issue(2, relations=[("parent", to(1)), ("blocked_by", to(1))])]
    make(fake).run(records)
    before = len(fake.writes())
    make(fake).run(records)
    assert len(fake.writes()) == before


def test_a_relation_gone_from_the_source_is_unlinked_but_never_a_persons_link(fake):
    make(fake).run([issue(1), issue(2, relations=[("blocked_by", to(1)), ("related", to(3))]), issue(3)])
    fake.as_whom = "bob"
    two, one = item_of(fake, 2), item_of(fake, 1)
    fake.write({"request_id": "bob-1", "expect": {two: fake.items[two]["revisions"][-1]["r"]},
                "operations": [{"op": "link.create", "data": {"type": "contributes_to", "from": two, "to": one}}]})
    fake.as_whom = None
    result = make(fake).run([issue(1), issue(2), issue(3)])
    assert links(fake) == [("contributes_to", 2, 1)]  # the importer's requires and related_to are gone; bob's link stays
    assert outcomes(result)[("acme/api#2", "issue")] == "updated"


def test_a_link_cycle_refused_by_wirk_falls_back(fake):
    make(fake).run([issue(1), issue(2)])
    fake.as_whom = "bob"  # a person's link the importer cannot see coming: 1 requires 2
    one, two = item_of(fake, 1), item_of(fake, 2)
    fake.write({"request_id": "bob-1", "expect": {one: 1}, "operations": [{"op": "link.create", "data": {"type": "requires", "from": one, "to": two}}]})
    fake.as_whom = None
    make(fake).run([issue(1), issue(2, relations=[("blocked_by", to(1))])])
    assert ("related_to", 1, 2) in links(fake) and ("requires", 2, 1) not in links(fake)


def test_completing_work_that_its_own_link_gates_swaps_the_gate_first(fake):
    make(fake).run([issue(1), issue(2, relations=[("blocked_by", to(1))])])
    assert ("requires", 2, 1) in links(fake)
    result = make(fake).run([issue(1), issue(2, state="completed", closed="closed as completed by @ada at T",
                                             relations=[("blocked_by", to(1))])])
    assert outcomes(result)[("acme/api#2", "issue")] == "updated"
    assert links(fake) == [("related_to", 1, 2)]  # related_to reads the same either way round
    assert "(kept as related: completed here)" in body_of(fake, 2)


def test_a_persons_gate_keeps_the_work_open_and_says_why(fake):
    make(fake).run([issue(1), issue(2)])
    fake.as_whom = "bob"
    one, two = item_of(fake, 1), item_of(fake, 2)
    fake.write({"request_id": "bob-1", "expect": {two: 1}, "operations": [{"op": "link.create", "data": {"type": "requires", "from": two, "to": one}}]})
    fake.as_whom = None
    result = make(fake).run([issue(1), issue(2, state="completed", closed="closed as completed by @ada at T")])
    found = [o for o in result if o.key == "acme/api#2" and o.kind == "issue"][0]
    assert found.outcome == "skipped" and "a link made in WIRK" in found.message


def test_no_write_holds_more_than_32_operations(fake):
    blockers = [issue(n) for n in range(100, 140)]
    make(fake).run(blockers + [issue(1, relations=[("blocked_by", to(n)) for n in range(100, 140)])])
    assert all(len(w["operations"]) <= 32 and len(w.get("expect", {})) <= 64 for w in fake.writes())
    assert sum(1 for l in links(fake) if l[0] == "requires") == 40


def test_a_requires_a_person_replaced_by_completing_the_work_is_kept_as_related_once(fake):
    records = [issue(1), issue(2, relations=[("blocked_by", to(1))])]
    make(fake).run(records)
    two, gate = item_of(fake, 2), next(i for i, link in fake.links.items() if link["type"] == "requires")
    fake.as_whom = "bob"  # Bob removes the gate and completes 2 in WIRK; the source still has 2 open and blocked
    fake.write({"request_id": "bob-1", "operations": [{"op": "link.remove", "id": gate}]})
    fake.write({"request_id": "bob-2", "reason": "Done", "expect": {two: 1},
                "operations": [{"op": "item.edit", "id": two, "patch": {"fields": {"status": "completed"}}}]})
    fake.as_whom = None
    make(fake).run(records)
    assert links(fake) == [("related_to", 1, 2)]
    before = len(fake.writes())
    make(fake).run(records)
    make(fake).run(records)
    assert len(fake.writes()) == before and links(fake) == [("related_to", 1, 2)]
