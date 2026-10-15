"""The Linear adapter as a library: a poll since a time, on lists from the last full read, renders each issue as the full
read does. The workspace is synthetic until L0 records a real one (linear_workspace.py)."""

import json
import re

import httpx
import pytest

from import_fakes import FakeWirk
from test_import_linear import FakeLinear, adapter, folder, issue, page, read, ref  # noqa: F401 (folder is a fixture)
import linear_workspace as ws
from wirk_cli.client import Service
from wirk_cli.importers import DEFAULT_STATUSES, linear, render, wirk
from wirk_cli.importers.render import Stop


class PollingLinear(FakeLinear):
    """Also answers a poll: its since and ids variables, and an issue's children and relations nested."""

    def __init__(self, issues=ws.ISSUES, relations=ws.RELATIONS, comments=ws.COMMENTS):
        super().__init__([dict(node) for node in issues], relations, comments)

    def nested_of(self, ident, teams):
        keys = {team["id"]: team for team in self.lists["teams"]}
        children = [{"id": n["id"], "identifier": n["identifier"], "number": n["number"],
                     "team": {key: keys[n["team"]["id"]][key] for key in ("id", "key", "private")}}
                    for n in reversed(self.lists["issues"]) if (n["parent"] or {}).get("id") == ident and n["team"]["id"] in teams]
        return {"children": children,
                "relations": [r for r in self.lists["issueRelations"] if (r["issue"] or {}).get("id") == ident],
                "inverseRelations": [r for r in self.lists["issueRelations"] if (r["relatedIssue"] or {}).get("id") == ident]}

    def answer(self, query, variables):
        data = self.answered(query, variables)
        self.forbidden(data)
        return data

    def answered(self, query, variables):
        more = re.search(r"issue\(id: \$id\) \{ (\w+)\(", query)
        if "viewer" in query:
            return super().answer(query, variables)
        if more and more[1] in linear.POLLED:
            return {"issue": {more[1]: self.window(self.nested_of(variables["id"], variables["teams"])[more[1]], variables)}}
        name = re.search(r"\{\s*(\w+)\(", query)[1]
        if not {"since", "ids"} & variables.keys():
            return super().answer(query, variables)
        since, ids = variables.get("since"), set(variables.get("ids") or [])
        team = {n["id"]: n["team"]["id"] for n in self.lists["issues"]}
        if name == "comments":
            nodes = [c for c in self.lists["comments"] if c["issue"] and (c["issue"]["id"] in ids or since and c["updatedAt"] > since
                                                                         and team.get(c["issue"]["id"]) in variables["teams"])]
        else:
            first = {key: int(re.search(rf"{key}\(first: (\d+)", query)[1]) for key in linear.POLLED}
            nodes = [{**n, **{key: self.window(found, {"first": first[key]})
                              for key, found in self.nested_of(n["id"], variables["teams"]).items()}}
                     for n in self.lists["issues"] if n["team"]["id"] in variables["teams"] and (n["updatedAt"] > since or n["id"] in ids)]
        return {name: self.window(nodes, variables)}

    def forbidden(self, value, path=()):
        """With each relation end the key cannot read, the FORBIDDEN error Linear sends beside its null."""
        if isinstance(value, dict):
            self.errors += [{"message": "Entity not found", "path": [*path, end], "extensions": {"code": "FORBIDDEN"}}
                            for end in ("issue", "relatedIssue") if "relatedIssue" in value and value[end] is None]
            for key, item in value.items():
                self.forbidden(item, (*path, key))
        elif isinstance(value, list):
            for number, item in enumerate(value):
                self.forbidden(item, (*path, number))


def poll(fake, folder, since, lists, selection=()):
    found = adapter(fake, folder)
    found.check({})
    fake.requests.clear()
    return found, *found.read(list(selection), since=since, lists=lists)


def roots(fake):
    return [re.search(r"\{\s*(\w+)\(", json.loads(r.content)["query"])[1] for r in fake.requests if r.method == "POST"]


def test_a_poll_asks_only_for_what_changed_and_takes_the_lists_from_the_last_full_read(folder):
    fake = PollingLinear()
    full, _, _ = read(fake, folder)
    _, census, records = poll(fake, folder, ws.SINCE, full.lists)
    assert sorted(record.key for record in records) == ["ENG-1", "ENG-6", "ENG-7"]  # ENG-6 through its comment
    assert {record.kind for record in records} == {"issue"} and census.issues == 3
    assert set(roots(fake)) == {"issues", "comments"}  # no team, label, project or relation list read again
    sent = [json.loads(r.content) for r in fake.requests if r.method == "POST"]
    asked, variables = next((body["query"], body["variables"]) for body in sent if "issues(" in body["query"])
    assert "$since: DateTimeOrDuration!" in asked and "$ids: [ID!]" in asked and "updatedAt: { gt: $since }" in asked
    assert variables["since"] == ws.SINCE and variables["ids"] == [ref(6)["id"]] and "includeArchived: true" in asked
    assert not [body for body in sent if ws.SINCE in body["query"] or ref(6)["id"] in body["query"]]  # values travel as variables
    assert "id type issue { id identifier team { id key private } } relatedIssue { id identifier team { id key private } }" in asked


def test_a_polled_issue_renders_byte_for_byte_as_the_full_read_does(folder):
    fake = PollingLinear()
    full, census, records = read(fake, folder)
    found, _, polled = poll(fake, folder, "2000-01-01T00:00:00.000Z", full.lists)
    whole = {record.ident: record for record in records if record.kind == "issue"}
    assert sorted(record.key for record in polled) == sorted(record.key for record in whole.values())
    for record in polled:
        assert record == whole[record.ident], record.key
        assert render.archive(record.raw, found.withheld(found.context(census.selected)), {}) == \
            render.archive(whole[record.ident].raw, full.withheld(full.context(census.selected)), {})
    parent = whole[ref(1)["id"]]
    assert [r["id"] for r in parent.raw["relations"]] == ["r-1", "r-2", "r-3", "r-5", "r-9"]  # by relation ID, both ways
    assert [(kind, other.key) for kind, other in parent.relations if kind == "sub_issue"] == [
        ("sub_issue", "ENG-2"), ("sub_issue", "ENG-3"), ("sub_issue", "OPS-4")]  # chosen teams, archived too, by number
    assert not {"children", "relations", "inverseRelations"} & set(parent.raw["issue"])


def test_a_poll_reads_a_list_again_only_when_an_issue_names_an_id_it_lacks(folder):
    fake = PollingLinear(issues=ws.ISSUES[:1] + [issue(7, updatedAt=ws.NEW, labels=page([{"id": "l-bug"}]))])
    full, _, _ = read(fake, folder)
    lists = {**full.lists, "issueLabels": [label for label in full.lists["issueLabels"] if label["id"] != "l-bug"]}
    found, _, records = poll(fake, folder, ws.SINCE, lists)
    assert roots(fake).count("issueLabels") == 1 and "teams" not in roots(fake)
    assert next(r for r in records if r.key == "ENG-7").fields["Label"] == ["Bug"]
    assert found.lists["issueLabels"] == full.lists["issueLabels"] and len(lists["issueLabels"]) == len(full.lists["issueLabels"]) - 1


@pytest.mark.parametrize("count", [30, 130])
def test_children_past_the_first_page_are_paged_with_their_filter(folder, count):
    children = [issue(n, parent=ref(1)) for n in range(2, count + 2)] + [issue(999, "SEC", parent=ref(1))]
    fake = PollingLinear(issues=[issue(1, updatedAt=ws.NEW)] + children, relations=[], comments=[])
    full, _, records = read(fake, folder)
    _, _, polled = poll(fake, folder, ws.SINCE, full.lists)
    assert polled == [next(r for r in records if r.key == "ENG-1")]
    assert len([kind for kind, _ in polled[0].relations if kind == "sub_issue"]) == count
    assert "issue" in roots(fake)  # the rest read issue by issue, past the poll's first page


@pytest.mark.parametrize("selection", [(), ("ENG", "OPS", "SEC")], ids=["public", "private team chosen"])
def test_a_poll_over_a_real_window_renders_as_the_full_read(folder, selection):
    fake = PollingLinear()
    full, census, records = read(fake, folder, selection)
    found, _, polled = poll(fake, folder, ws.SINCE, full.lists, selection)
    whole = {record.ident: record for record in records}
    assert sorted(record.key for record in polled) == ["ENG-1", "ENG-6", "ENG-7"]
    for record in polled:
        assert record == whole[record.ident], record.key
        assert render.archive(record.raw, found.withheld(found.context(census.selected)), {}) == \
            render.archive(whole[record.ident].raw, full.withheld(full.context(census.selected)), {})
    children = [other.key for kind, other in whole[ref(1)["id"]].relations if kind == "sub_issue"]
    assert children == ["ENG-2", "ENG-3", "OPS-4"] + (["SEC-10"] if selection else [])


def test_a_polls_context_reports_nothing_missing(folder):
    fake = PollingLinear()
    full, census, _ = read(fake, folder)
    found, census, _ = poll(fake, folder, ws.SINCE, full.lists)
    assert full.context(census.selected).complete and not found.context(census.selected).complete
    assert found.context(census.selected).seen == full.context(census.selected).seen


def importer(fake_wirk, found, census):
    ctx = found.context(census.selected)
    service = Service("https://wirk.test", "wirk_" + "t" * 43, transport=httpx.MockTransport(fake_wirk))
    made = wirk.Importer(wirk.Wirk(service), ctx, users={}, statuses={state: state for state in DEFAULT_STATUSES.values()},
                         plan={}, withheld=found.withheld(ctx), download=lambda attachment: None, dry_run=False, overwrite=False)
    made.start()
    return made


def test_a_poll_then_the_nights_read_write_a_changed_issue_once_and_a_poll_reports_nothing_missing(folder):
    fake, store = PollingLinear(), FakeWirk(principal="sam-linear-import", person="sam")
    full, census, records = read(fake, folder)
    importer(store, full, census).run(records)
    fake.lists["issues"][0].update(title="Parent, renamed", updatedAt="2026-10-14T21:00:00.000Z")
    found, census, polled = poll(fake, folder, ws.NEW, full.lists)
    before, links = len(store.requests), {ident: link["removed"] for ident, link in store.links.items()}
    outcomes = importer(store, found, census).run(polled)
    assert links and {ident: link["removed"] for ident, link in store.links.items()} == links  # none outside the batch touched
    assert [(o.key, o.outcome) for o in outcomes if o.kind == "issue"] == [("ENG-1", "updated")]
    assert not [o for o in outcomes if o.outcome == "missing"]
    assert not [body for route, body in store.requests[before:] if route == "/v2/query" and body.get("depth") == "full"
                and body["fetch"][0] not in {o.item for o in outcomes}]  # no item outside the poll is read
    full, census, records = read(fake, folder)
    assert {o.outcome for o in importer(store, full, census).run(records) if o.key == "ENG-1"} == {"current"}


def test_a_poll_refuses_an_error_that_is_not_an_unreadable_relation_end(folder):
    fake = PollingLinear(issues=[issue(1, updatedAt=ws.NEW, assignee={"id": "u-ada"})], relations=[], comments=[])
    full, _, _ = read(fake, folder)
    answer = fake.answer

    def broken(query, variables):
        data = answer(query, variables)
        if "issues(" in query:
            fake.errors = [{"message": "Internal error", "path": ["issues", "nodes", 0, "assignee"],
                            "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]
            data["issues"]["nodes"][0]["assignee"] = None
        return data
    fake.answer = broken
    with pytest.raises(Stop, match="Internal error"):
        read(fake, folder)
    with pytest.raises(Stop, match="Internal error"):
        poll(fake, folder, ws.SINCE, full.lists)


def test_a_list_read_again_is_kept_on_the_adapter_for_the_next_poll_and_no_lists_reads_them_all(folder):
    fake = PollingLinear(issues=ws.ISSUES[:1] + [issue(7, updatedAt=ws.NEW, labels=page([{"id": "l-bug"}]))])
    full, _, _ = read(fake, folder)
    found, _, _ = poll(fake, folder, ws.SINCE, {**full.lists, "issueLabels": []})
    assert roots(fake).count("issueLabels") == 1
    again, _, _ = poll(fake, folder, ws.SINCE, found.lists)
    assert "issueLabels" not in roots(fake) and again.lists == found.lists
    poll(fake, folder, ws.SINCE, {})  # an empty dict is no lists: every list is read, as at a first import
    assert set(linear.LISTS) <= set(roots(fake))


def test_lists_serve_only_a_read_of_changes(folder):
    fake = PollingLinear()
    full, _, _ = read(fake, folder)
    with pytest.raises(ValueError, match="since"):
        full.read([], lists=full.lists)  # a full read reads every list itself, so nothing it names is ever missing
