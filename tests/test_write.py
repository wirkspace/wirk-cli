"""The write shortcuts and review build one fixed v2 body each; what the CLI can know is refused before any call."""

import json

import pytest

from conftest import envelope, refusal


def sent(run, argv, answer=None):
    code, out, err, fake = run(argv, answer)
    assert code == 0, (out, err)
    body = fake.bodies()[-1]
    assert fake.requests[-1].url.path == ("/v2/review" if argv[0] == "review" else "/v2/write")
    body.pop("request_id")
    body.pop("format")
    return body


def test_progress_note_linked_to_two_items(run):
    assert sent(run, ["write", "new", "R1.5 progress", "--body", "Did things.", "--link", "related_to:5c1e7a90",
                      "--link", "related_to:8d24f6b1"]) == {"operations": [
        {"op": "item.create", "ref": "new", "data": {"title": "R1.5 progress", "body": "Did things."}},
        {"op": "link.create", "data": {"type": "related_to", "from": "$new", "to": "5c1e7a90"}},
        {"op": "link.create", "data": {"type": "related_to", "from": "$new", "to": "8d24f6b1"}}]}


def test_task_under_a_parent_taken_with_owner_me(run):
    assert sent(run, ["write", "new", "Rate-limit the API", "owner=me", "status=open", "--criterion", "429 after 100",
                      "--criterion", "Documented", "--link", "contributes_to:2f9b3c4e@7"]) == {
        "expect": {"2f9b3c4e": 7}, "operations": [
            {"op": "item.create", "ref": "new", "data": {"title": "Rate-limit the API", "fields": {"status": "open"},
             "work": {"owner_id": "me", "criteria": [{"text": "429 after 100"}, {"text": "Documented"}]}}},
            {"op": "link.create", "data": {"type": "contributes_to", "from": "$new", "to": "2f9b3c4e"}}]}


@pytest.mark.parametrize("words, data", [
    (["kind=work"], {"work": {}}), (["kind=wirk"], {"work": {}}), (["kind=record"], {}), (["kind=doc"], {}),
    (["kind=context", "level=initiative"], {"context": {"level": "initiative"}}),
    (["kind=context", "level=organization"], {"context": {"level": "organization"}}),
    (["labels=a,b"], {"fields": {"labels": ["a", "b"]}}),
    (["workspace_id=1a2b3c4d"], {}),
])
def test_write_new_kinds_and_fields(run, words, data):
    body = sent(run, ["write", "new", "Title", *words])
    assert body["operations"][0]["data"] == {"title": "Title", **data}
    if "workspace_id=1a2b3c4d" in words:
        assert body["workspace_id"] == "1a2b3c4d"


def test_the_first_argument_after_new_is_always_the_title(run):
    assert sent(run, ["write", "new", "status=done"])["operations"][0]["data"] == {"title": "status=done"}


def test_body_file_from_a_file_and_from_standard_input(run, tmp_path, monkeypatch):
    (tmp_path / "note.md").write_text("From a file.")
    assert sent(run, ["write", "new", "T", "--body-file", "note.md"])["operations"][0]["data"]["body"] == "From a file."
    (tmp_path / "piped.txt").write_text("From stdin.")
    monkeypatch.setattr("sys.stdin", open(tmp_path / "piped.txt"))
    assert sent(run, ["write", "new", "T", "--body-file", "-"])["operations"][0]["data"]["body"] == "From stdin."


def test_upload_and_duplicate_override_on_new(run):
    body = sent(run, ["write", "new", "T", "kind=work", "--upload", "upload_7e8f", "--allow-duplicate-of", "71f0c8ae",
                      "--reason", "It differs: another service"])
    assert body["reason"] == "It differs: another service"
    assert body["operations"][0] == {"op": "item.create", "ref": "new", "allow_duplicate_of": ["71f0c8ae"],
                                     "data": {"title": "T", "work": {}, "uploads": ["upload_7e8f"]}}


def test_edit_completion_with_evidence_and_a_file(run):
    assert sent(run, ["write", "edit", "5c1e7a90@4", "status=completed", "--upload", "upload_7e8f",
                      "--evidence", "Load test log attached"]) == {
        "expect": {"5c1e7a90": 4}, "reason": "Load test log attached", "operations": [
            {"op": "item.edit", "id": "5c1e7a90", "patch": {"fields": {"status": "completed"},
                                                             "attach_uploads": ["upload_7e8f"]}}]}


def test_edit_fields_owner_title_body_and_links(run):
    assert sent(run, ["write", "edit", "5c1e7a90@3", "priority=", "owner=", "--title", "New", "--body", "B",
                      "--link", "requires:6a0d2e83"]) == {
        "expect": {"5c1e7a90": 3}, "operations": [
            {"op": "item.edit", "id": "5c1e7a90", "patch": {"title": "New", "body": "B", "fields": {"priority": None},
                                                             "work": {"owner_id": None}}},
            {"op": "link.create", "data": {"type": "requires", "from": "5c1e7a90", "to": "6a0d2e83"}}]}


def test_propose_and_link(run):
    assert sent(run, ["write", "edit", "6a0d2e83@2", "status=cancelled", "--propose", "--reason", "Idle 30 days"]) == {
        "expect": {"6a0d2e83": 2}, "mode": "propose", "reason": "Idle 30 days",
        "operations": [{"op": "item.edit", "id": "6a0d2e83", "patch": {"fields": {"status": "cancelled"}}}]}
    assert sent(run, ["write", "link", "b38e6f05@1", "contributes_to", "2f9b3c4e@7"]) == {
        "expect": {"b38e6f05": 1, "2f9b3c4e": 7},
        "operations": [{"op": "link.create", "data": {"type": "contributes_to", "from": "b38e6f05", "to": "2f9b3c4e"}}]}


def test_a_completion_without_evidence_reaches_the_service_and_its_words_are_printed(run):
    words = ("Completing work needs its evidence: a note naming the tests that pass, a file path or a link, "
             "or a file attached or cited in this write")
    code, out, err, fake = run(["write", "edit", "5c1e7a90@4", "status=completed"],
                               lambda request: refusal("reason_required", words, text=f"Not applied w-1 · reason_required: {words}"))
    assert code == 1 and len(fake.requests) == 1
    assert out == f"Not applied w-1 · reason_required: {words}\n"


@pytest.mark.parametrize("argv, words", [
    (["write", "edit", "5c1e7a90", "status=done"], "5c1e7a90@N"),
    (["write", "link", "5c1e7a90", "related_to", "6a0d2e83"], "5c1e7a90@N"),
    (["write", "new", "T", "--propose"], "--reason"),
    (["write", "new", "T", "--link", "cites:6a0d2e83"], "related_to, contributes_to or requires"),
    (["write", "edit", "5c1e7a90@1", "kind=work"], "--request"),
    (["write", "edit", "5c1e7a90@1", "--criterion", "x"], "--request"),
    (["write", "new", "T", "--body", "a", "--body-file", "b"], "--body"),
    (["write", "new", "T", "--evidence", "x", "--reason", "y"], "--evidence"),
    (["write", "new", "T", "--reason", "REASON"], "your own words"),
    (["write", "new", "T", "--reason", "…"], "your own words"),
    (["write", "edit", "5c1e7a90@1", "--evidence", "..."], "your own words"),
    (["write", "new", "T", "kind=folder"], "--request"),
    (["write", "new", "T", "kind=memo"], "kind is work, record or context here"),
    (["write", "new", "T", "level=initiative"], "kind=context"),
    (["write", "archive", "5c1e7a90@1"], "--request"),
    (["write", "restore", "5c1e7a90@1"], "--request"),
])
def test_write_usage_errors_send_nothing(run, argv, words):
    code, out, err, fake = run(argv)
    assert code == 2 and fake.requests == []
    assert words in err


def test_write_request_sends_the_file_exactly(run, tmp_path):
    body = {"request_id": "mine-1", "operations": [{"op": "item.archive", "id": "5c1e7a90"}], "expect": {"5c1e7a90": 2},
            "reason": "Superseded"}
    (tmp_path / "batch.json").write_text(json.dumps(body))
    code, out, err, fake = run(["write", "--request", "batch.json"])
    assert code == 0 and fake.bodies() == [body]


def test_review_bodies(run):
    body = sent(run, ["review", "c4a1e902@1", "e7b35d16@2", "defer", "--reason", "Wait for the bench"])
    assert body == {"decisions": [
        {"id": "c4a1e902", "revision": 1, "action": "defer", "reason": "Wait for the bench"},
        {"id": "e7b35d16", "revision": 2, "action": "defer", "reason": "Wait for the bench"}]}


@pytest.mark.parametrize("argv", [
    ["review", "c4a1e902@1", "ACTION", "--reason", "REASON"],
    ["review", "c4a1e902@1", "accept", "--reason", "REASON"],
    ["review", "c4a1e902@1", "accept", "--reason", ""],
    ["review", "c4a1e902@1", "accept"],
    ["review", "c4a1e902", "accept", "--reason", "Fine"],
    ["review", "c4a1e902@1", "approve", "--reason", "Fine"],
])
def test_a_pasted_or_incomplete_decision_decides_nothing(run, argv):
    code, out, err, fake = run(argv)
    assert code == 2 and fake.requests == []


def test_request_ids_are_generated_or_given(run):
    code, out, err, fake = run(["write", "new", "T"])
    assert __import__("re").fullmatch(r"w-[0-9a-f]{10}", fake.bodies()[0]["request_id"])
    code, out, err, fake = run(["write", "new", "T", "--request-id", "mine-7"])
    assert fake.bodies()[0]["request_id"] == "mine-7"
    code, out, err, fake = run(["review", "c4a1e902@1", "accept", "--reason", "Fine"])
    assert __import__("re").fullmatch(r"r-[0-9a-f]{10}", fake.bodies()[0]["request_id"])
