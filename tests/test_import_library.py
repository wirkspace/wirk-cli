"""The importer as a library for a service that keeps importing: other trusted makers, kept source versions, and a run
that resumes from a position, on the in-memory WIRK."""

import pytest

from import_fakes import FakeWirk, refusal
from test_import_wirk import issue, make, outcomes, said
from wirk_cli.importers.render import Ref


@pytest.fixture
def fake():
    return FakeWirk(principal="sam-github", person="sam", fields={"repository": {"acme_api": "active"}})


def test_an_item_another_trusted_maker_wrote_is_the_importers_own(fake):
    make(fake).run([issue(1), issue(2)])
    fake.as_whom = "sam-github-import"  # a terminal import's item is never taken over
    make(fake).run([issue(3)])
    fake.as_whom, fake.principal = None, "kim-github"  # the next administrator's connection
    result = make(fake, mine={"sam-github"}).run([issue(1), issue(2, title="Renamed"), issue(3, title="Changed")])
    assert outcomes(result) == {("acme/api#1", "issue"): "current", ("acme/api#2", "issue"): "updated",
                                ("acme/api#3", "issue"): "blocked"}
    assert len(fake.mine()) == 3  # nothing made twice


def test_each_item_keeps_its_source_version_written_and_read_again_from_wirk(fake):
    first = make(fake)
    first.run([issue(1, version="2026-10-14T20:00:00Z"), issue(2, comments=[said("Late.", "2026-10-14T21:00:00Z")])])
    again = make(fake)
    for index in (first.index, again.index):
        assert index.held[("issue", "3000000001")][0].version == "20261014T200000Z"
        assert index.held[("comments", "3000000002")][0].version == "20261014T210000Z"  # a discussion's is its latest comment's


class Pause(Exception):
    """The runner's own stop, raised from inside a write: never WIRK's refusal, so it is not one issue's error."""


def pause(fake, words):
    def fault(route, body, before):
        if route == "write" and before and words in body.get("reason", ""):
            raise Pause(words)
    fake.faults.append(fault)


def blocked_by(number):
    return [("blocked_by", Ref(f"acme/api#{number}", "acme/api", True, str(3000000000 + number)))]


def refuse(fake, words):
    answer = fake.write
    fake.write = lambda body: refusal("invalid_field", "refused") if words in body["reason"] else answer(body)
    return answer


def written(fake, since=0):
    return [body["reason"] for body in fake.writes()[since:]]


def test_a_paused_run_resumes_past_the_last_settled_record_and_plans_links_over_every_record(fake):
    records = [issue(1, relations=blocked_by(4)), issue(2), issue(3), issue(4)]
    pause(fake, "acme/api#3 ")
    position = {}
    with pytest.raises(Pause):
        make(fake).run(records, position=position)
    assert position == {"record": "3000000002"}
    fake.faults.clear()
    before = len(fake.writes())
    result = make(fake).run(records, position=position)
    assert {key for key, kind in outcomes(result) if kind == "issue"} == {"acme/api#3", "acme/api#4"}
    assert written(fake, before) == ["Imported from GitHub acme/api#3 by wirk import github",
                                     "Imported from GitHub acme/api#4 by wirk import github",
                                     "Imported from GitHub: the relations of acme/api#1"]
    assert position == {"record": "3000000004", "link": "3000000001"}


def test_the_link_pass_resumes_from_its_own_position(fake):
    records = [issue(1, relations=blocked_by(3)), issue(2, relations=blocked_by(3)), issue(3)]
    pause(fake, "relations of acme/api#2")
    position = {}
    with pytest.raises(Pause):
        make(fake).run(records, position=position)
    assert position == {"record": "3000000003", "link": "3000000001"}
    fake.faults.clear()
    before = len(fake.writes())
    assert make(fake).run(records, position=position) == []
    assert written(fake, before) == ["Imported from GitHub: the relations of acme/api#2"]
    assert position == {"record": "3000000003", "link": "3000000002"}


def test_an_error_holds_the_position_before_it_while_the_run_goes_on(fake):
    refuse(fake, "acme/api#2 ")
    position = {}
    result = make(fake).run([issue(1), issue(2), issue(3)], position=position)
    assert outcomes(result) == {("acme/api#1", "issue"): "created", ("acme/api#2", "issue"): "error",
                                ("acme/api#3", "issue"): "created"}
    assert position == {"record": "3000000001"}


def test_a_record_older_than_what_is_written_is_skipped_at_its_write_and_keeps_its_links(fake):
    make(fake).run([issue(1, title="Newer", version="2026-10-14T20:00:00Z")])
    result = make(fake).run([issue(1, title="Older", relations=blocked_by(4)), issue(4)])
    assert outcomes(result)[("acme/api#1", "issue")] == "current"
    assert [item["revisions"][-1]["title"] for item in fake.mine().values()] == ["Newer", "Issue 4"]
    assert [link["type"] for link in fake.links.values()] == ["requires"]


@pytest.mark.parametrize("records", [[issue(1, relations=blocked_by(2)), issue(2), issue(3)],
                                     [issue(1), issue(2, relations=blocked_by(1)), issue(3)]], ids=["target", "source"])
def test_a_link_to_a_record_that_errored_is_written_when_the_run_resumes(fake, records):
    answer, position = refuse(fake, "acme/api#2 "), {}
    make(fake).run(records, position=position)
    assert not fake.links and position["record"] == "3000000001"
    fake.write = answer
    make(fake).run(records, position=position)
    assert [link["type"] for link in fake.links.values()] == ["requires"]
    assert position == {"record": "3000000003", "link": "3000000002" if records[1].relations else "3000000001"}
