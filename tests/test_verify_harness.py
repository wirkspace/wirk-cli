"""The independent import check reads every page and stops: a page can be incomplete with nothing to continue."""

from verify.github_rest import Wirk


def test_a_page_without_a_cursor_ends_the_list():
    """Core answers complete: false with no cursor when a view on the last page is not whole (wirk-core #36). Sending
    that null cursor back would read the first page again, forever."""
    sent, wirk = [], Wirk.__new__(Wirk)

    def post(route, body):
        sent.append(dict(body))
        assert len(sent) < 3, "the harness asked again with no cursor"
        return {"data": {"cards": [{"id": "only"}]}, "page": {"complete": False, "next_cursor": None}}

    wirk.post = post
    assert list(wirk.cards({"kind": "work"})) == [{"id": "only"}] and len(sent) == 1
