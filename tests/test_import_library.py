"""The importer as a library for a service that keeps importing: other trusted makers, kept source versions, and a run
that resumes from a position, on the in-memory WIRK."""

import pytest

from import_fakes import FakeWirk
from test_import_wirk import issue, make, outcomes


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
