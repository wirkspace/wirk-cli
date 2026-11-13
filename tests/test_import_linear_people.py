"""The people a Linear import meets: a placeholder for each assignee, an exact verified email matched at once, a
suggestion (never a decision) for the rest, and no address in anything written. No call reaches Linear or a real WIRK."""

import json

import httpx

from import_fakes import FakeWirk
from test_import_linear import USERS, FakeLinear, folder, issue, read  # noqa: F401 - folder is a fixture
from wirk_cli.client import Service
from wirk_cli.importers import DEFAULT_STATUSES, render, wirk

ADA = {"id": "u-ada", "name": "Ada Lovelace", "displayName": "ada", "active": True, "email": "ada@acme.test",
       "avatarUrl": "https://avatars.test/ada.png"}
BEN = {"id": "u-ben", "name": "Ben Sample", "displayName": "ben", "active": True, "email": "ben@elsewhere.test", "avatarUrl": None}
CARA = {"id": "u-cara", "name": "Zed Unknown", "displayName": "zed", "active": True, "email": None, "avatarUrl": None}


def run_import(folder, hub, users=(ADA, BEN, CARA), **options):
    linear = FakeLinear([issue(1, assignee={"id": "u-ada"}), issue(2, assignee={"id": "u-ben"}), issue(3, assignee={"id": "u-cara"})])
    linear.lists["users"] = list(users)
    source, census, records = read(linear, folder)
    ctx = source.context(census.selected)
    service = Service("https://wirk.test", "wirk_" + "t" * 43, transport=httpx.MockTransport(hub))
    run = wirk.Importer(wirk.Wirk(service), ctx, users={}, statuses=DEFAULT_STATUSES,
                        plan=render.plan_fields(records, source.selections(records), source.source), withheld=source.withheld(ctx),
                        download=source.download, dry_run=False, overwrite=False, rewrite=source.rewrite,
                        emails=source.emails, **options)
    run.start()
    run.run(records)
    return run


def hub():
    return FakeWirk(members=("alice", "bob", "ben-sample"), fields={"team": {"eng": "active"}, "workflow": {
        "triage": "active", "todo": "active", "in_review": "active", "done": "active"}})


def test_every_assignee_becomes_a_placeholder_and_only_an_exact_verified_email_matches(folder):
    wirk_hub = hub()
    wirk_hub.verified = {"ada@acme.test": "alice"}
    run_import(folder, wirk_hub)
    known = {user: found["principal_id"] for (_, user), found in wirk_hub.identities.items()}
    assert known == {"u-ada": "alice", "u-ben": None, "u-cara": None}
    assert wirk_hub.identities[("linear", "u-ada")]["display_name"] == "Ada Lovelace"
    assert wirk_hub.identities[("linear", "u-ada")]["avatar_url"] == "https://avatars.test/ada.png"


def test_a_clear_name_resemblance_is_a_proposal_for_a_reviewer_and_nothing_is_applied(folder):
    wirk_hub = hub()
    run = run_import(folder, wirk_hub)
    assert [p["operations"][0]["patch"]["owner"] for p in wirk_hub.proposals] == ["ben-sample"]
    assert wirk_hub.proposals[0]["mode"] == "propose" and "check before you accept" in wirk_hub.proposals[0]["reason"]
    assert run.people["suggested"] == 1 and run.people["unmatched"] == 2  # ada has no member; zed resembles nobody


def test_a_second_run_matches_and_suggests_the_same_again_without_duplicating_anything(folder):
    wirk_hub = hub()
    run_import(folder, wirk_hub)
    before = (len(wirk_hub.items), len(wirk_hub.proposals), set(wirk_hub.identities))
    run_import(folder, wirk_hub)
    assert (len(wirk_hub.items), len(wirk_hub.proposals), set(wirk_hub.identities)) == before


def test_no_email_is_written_into_a_record_a_request_body_other_than_the_match_or_an_archive(folder):
    wirk_hub = hub()
    run_import(folder, wirk_hub)
    for _, body in wirk_hub.requests:
        for op in body.get("operations", []):
            if op["op"] != "identity.set":
                assert "@acme.test" not in json.dumps(op) and "@elsewhere.test" not in json.dumps(op)
    assert not any(b"@acme.test" in data or b"@elsewhere.test" in data for data in wirk_hub.objects.values())
