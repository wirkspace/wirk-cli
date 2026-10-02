"""Arguments and the service's printed commands become exactly the request bodies they describe."""

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from wirk_cli import grammar

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "service_lines.json").read_text())


@pytest.mark.parametrize("words, body", [
    ([], {}),
    (["6a0d2e83"], {"fetch": ["6a0d2e83"]}),
    (["Weekly sync", "5c1e7a90@2"], {"fetch": ["Weekly sync", {"ref": "5c1e7a90", "revision": 2}]}),
    (["about=hook drain", "status=open"], {"about": "hook drain", "fields": {"status": "open"}}),
    (["status=open,in_progress", "kind=work", "owner=me", "limit=50"],
     {"fields": {"status": ["open", "in_progress"], "kind": "work", "owner": "me"}, "limit": 50}),
    (["kind=context", "state=active"], {"fields": {"kind": "context", "state": "active"}}),
    (["text=a, b"], {"fields": {"text": "a, b"}}),
    (["linked=2f9b3c4e", "depth=card"], {"fields": {"linked": "2f9b3c4e"}, "depth": "card"}),
    (["proposal=proposed,deferred"], {"fields": {"proposal": ["proposed", "deferred"]}}),
    (["receipt=w-3f9a2c41d0"], {"receipt": "w-3f9a2c41d0"}),
    (["status=open", "limit=5", "cursor=AXic"], {"fields": {"status": "open"}, "limit": 5, "cursor": "AXic"}),
])
def test_query_grammar_table(words, body):
    assert grammar.query_body(words) == body


@pytest.mark.parametrize("token, expected", [
    ("5c1e7a90@2", {"ref": "5c1e7a90", "revision": 2}),
    ("item_5c1e7a90ab@3", {"ref": "item_5c1e7a90ab", "revision": 3}),
    ("proposal_c4a1e902@1", {"ref": "proposal_c4a1e902", "revision": 1}),
    ("Meet@3", "Meet@3"), ("x@3pm", "x@3pm"), ("hook@drain", "hook@drain"), ("5c1e7a90", "5c1e7a90"),
])
def test_ref_at_revision_only_for_ids(token, expected):
    assert grammar.ref(token) == expected


def test_unquoted_words_are_refused_before_any_call():
    with pytest.raises(grammar.UsageError, match=r"wirk query 'about=hook drain'.*wirk query 'hook drain'"):
        grammar.query_body(["hook", "drain"])
    assert grammar.query_body(["5c1e7a90", "6a0d2e83"]) == {"fetch": ["5c1e7a90", "6a0d2e83"]}
    assert grammar.query_body(["5c1e7a90", "Roadmap"]) == {"fetch": ["5c1e7a90", "Roadmap"]}
    assert grammar.query_body(["hook drain"]) == {"fetch": ["hook drain"]}


def test_status_bodies():
    assert grammar.status_body([]) == {}
    assert grammar.status_body(["fix", "the", "hook", "drain"]) == {"task": "fix the hook drain"}
    assert grammar.status_body(["task=fix it", "max_bytes=65536", "workspace_id=1a2b3c4d"]) == {
        "task": "fix it", "max_bytes": 65536, "workspace_id": "1a2b3c4d"}
    with pytest.raises(grammar.UsageError, match="status takes no filters"):
        grammar.status_body(["status=open"])


@pytest.mark.parametrize("words", [["limit=five"], ["max_bytes=1k"], ["status=open", "status=done"]])
def test_bad_numbers_and_repeated_keys_are_refused(words):
    with pytest.raises(grammar.UsageError):
        grammar.query_body(words)


def shell_words(shell, line):
    """The arguments a real shell passes for `line` (nothing else runs: printf only prints them)."""
    result = subprocess.run([shell, "-c", "printf '%s\\0' " + line], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\0")[:-1]


@pytest.mark.parametrize("shell", ["/bin/sh", "zsh"])
@pytest.mark.parametrize("case", FIXTURE, ids=lambda case: case["line"][:60])
def test_service_lines_through_sh_and_zsh(shell, case):
    label, _, command = case["line"].partition(": ")
    words = shell_words(shell, command)
    assert words == shlex.split(command)
    operation, rest = words[0], words[1:]
    if "body" in case:
        parse = grammar.query_body if operation == "query" else grammar.status_body
        assert parse(rest) == case["body"]


SAFE = re.compile(r"[A-Za-z0-9_.,:/@%+=-]+")


def quote(value):
    """The service's quoting rule (S6), restated from its wire documentation."""
    if SAFE.fullmatch(value) and not value.startswith("="):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


AWKWARD = ["plain", "two words", "it's", 'say "hi"', "back\\slash", "$HOME", "`date`", "*", "?", "[x]", "!", "#", "~",
           "&;|", "=sum", "naïve — ünïcode", "a,b", "100%"]


@pytest.mark.parametrize("shell", ["/bin/sh", "zsh"])
def test_generated_bodies_round_trip(shell):
    for value in AWKWARD:
        body = {"fetch": [value], "about": value} if "," not in value else {"about": value}
        argv = [quote(part) for part in ([*body.get("fetch", []), f"about={value}"])]
        words = shell_words(shell, " ".join(argv))
        assert words == [*body.get("fetch", []), f"about={value}"]
        assert grammar.query_body(words) == body
