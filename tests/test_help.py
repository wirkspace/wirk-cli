"""Help is short and runnable; the wording, privacy and v2-only rules hold for every text the clients carry."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wirk_cli import __version__, cli

ROOT = Path(__file__).parent.parent
COMMANDS = ["status", "query", "write", "review", "show", "upload", "download", "login", "admin"]


def help_of(capsys, *argv):
    assert cli.main([*argv, "--help"]) == 0
    return capsys.readouterr().out


def test_main_help_is_the_whole_entry_point(capsys):
    text = help_of(capsys)
    lines = text.rstrip("\n").split("\n")
    assert len(lines) <= 25 and "Start with: wirk status" in text
    assert lines[2].startswith("WIRK keeps people and agents aligned")


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_help_is_short(capsys, command):
    text = help_of(capsys, command)
    max_lines = {"write": 41, "admin": 35, "query": 26}.get(command, 25)
    assert len(text.rstrip("\n").split("\n")) <= max_lines
    assert "accept|reject|defer" not in text


def examples(text):
    """The runnable part of each help line: `wirk …`, or the first column of an indented example line."""
    found = re.findall(r"^(?!usage:).*?(wirk [^\n]+?)(?:\s{2,}|$)", text, re.M)
    found += ["wirk " + re.split(r"\s{2,}", line.strip())[0] for line in text.split("\n")
              if line.startswith("  ") and not line.startswith("   ") and not line.strip().startswith(("-", "{", "["))]
    return [line for line in found if "…" not in line and "|" not in line]


@pytest.mark.parametrize("shell", ["/bin/sh", "zsh"])
def test_help_examples_through_sh_and_zsh(capsys, shell):
    for line in examples(help_of(capsys)):
        result = subprocess.run([shell, "-c", "printf '%s\\0' " + line[5:]], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and ["wirk", *result.stdout.split("\0")[:-1]] == shlex.split(line), line


def test_retired_commands_name_their_replacement(capsys):
    assert cli.main(["read", "5c1e7a90"]) == 2
    assert "wirk query" in capsys.readouterr().err


def test_help_is_fast():
    times = []
    for _ in range(5):
        start = time.perf_counter()
        subprocess.run([sys.executable, "-c", "import sys; from wirk_cli import cli; cli.main(['--help']); "
                        "assert 'httpx' not in sys.modules"], check=True, capture_output=True)
        times.append(time.perf_counter() - start)
    assert sorted(times)[2] < 0.5  # a cold interpreter included; the CLI's own share is measured in the record


WORDING = [r"\bworkspace\b(?!_id|-id)", r"\bwork item", r"kind=wirk", r"· wirk ·", r"\bsteward", r"\bnote\b(?= kind)", r"\bdoc\b"]


def agent_text(capsys):
    yield help_of(capsys)
    for command in COMMANDS:
        if command != "admin":
            yield help_of(capsys, command)
    yield (ROOT / "README.md").read_text()


def test_wording(capsys):
    for text in agent_text(capsys):
        for pattern in WORDING:
            assert not re.search(pattern, text), (pattern, text[:200])


# Written in pieces so this file never matches its own patterns.
PRIVATE = ["/Us" "ers/", r"/home/[a-z]", r"\b(?:wsp|item|change|proposal|link|acc)_[0-9a-f]{32}\b", "Co-Auth" "ored-By",
           "Cla" "ude(?! Code)", "Anthr" "opic", r"\bOpus\b", r"\bSonnet\b", r"\bFable\b", r"\bGPT-?\d",
           r"[A-Za-z0-9._%+-]+@(?![A-Za-z0-9.-]*(?:example\.|wirk\.life|users\.noreply\.github\.com))[A-Za-z0-9.-]+\.[a-z]{2,}"]


def history(root: Path) -> str:
    """Each commit's author and message; the files themselves are scanned one by one."""
    return subprocess.run(["git", "-C", str(root), "log", "--all", "--format=%an %ae%n%B"],
                          capture_output=True, text=True, check=True).stdout


def tracked_text():
    names = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    for name in names:
        path = ROOT / name
        if path.is_file() and name != "LICENSE":
            yield name, path.read_text(errors="replace")
    if os.environ.get("WIRK_PRIVACY_HISTORY") == "1":  # set at publication and on the public repository
        yield "history", history(ROOT)


def test_privacy_scan():
    for name, text in tracked_text():
        for pattern in PRIVATE:
            assert not re.search(pattern, text), (name, pattern, re.search(pattern, text).group(0))


def test_no_v1():
    for path in (ROOT / "src").rglob("*.py"):
        assert "/v1/" not in path.read_text(), path


def test_the_email_guard_lets_github_noreply_authors_through():
    """Published commits are authored with a GitHub noreply address; the history scan must accept it and nothing else."""
    email = PRIVATE[-1]
    assert not re.search(email, "A Person <12345+someone@users.noreply.github.com>")
    assert re.search(email, "A Person <someone@" "corp.test>")  # in pieces, so the scan of this file passes


def test_the_history_scan_reads_authors_and_messages(tmp_path):
    """The files are scanned above; the history adds who wrote each commit and what it says, not diffs full of decorators."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "t.py").write_text("@pytest.fixture\ndef home():\n    pass\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "t.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=A Person", "-c", "user.email=1+someone@users.noreply.github.com",
                    "commit", "-q", "-m", "Add a test"], check=True)
    text = history(tmp_path)
    assert "Add a test" in text and "@pytest.fixture" not in text
    assert not [pattern for pattern in PRIVATE if re.search(pattern, text)]


def test_version_prints_the_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"wirk {__version__}\n"


def test_one_version_everywhere():
    project = __import__("tomllib").loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert project == __version__ == "0.4.23"


def test_main_help_offers_what_is_live_and_says_who_decides(capsys):
    """Decision 85: an agent whose role may review decides with its own token; a background agent only proposes."""
    text = help_of(capsys)
    assert "\n  show status " in text
    assert "\n  review ID@N accept --reason 'Why'  " in text and "only people" not in text.lower()
    assert "a background agent only proposes" in text and "--person" not in text


def test_review_help_says_who_decides_and_what_person_means(capsys):
    text = help_of(capsys, "review")
    assert "Anyone whose role may review decides" in text and "not_authorized: your role (editor) cannot review" in text
    assert "person_required" not in text and "only people" not in text.lower() and "you proposed" not in text
    assert "--person decides as yourself" in text and "wirk login --person" in text
    assert [line for line in text.split("\n") if line.startswith("  review ") and "--person" in line] == []


def test_show_help_describes_hosted_lifetime_and_unconfigured_services(capsys):
    text = help_of(capsys, "show")
    assert "24 hours" in text and "revoked" in text and "views_unavailable" in text
    assert "Not live yet" not in text


def test_show_help_gives_a_complete_handoff_view_file(capsys):
    text = help_of(capsys, "show")
    recipe = json.loads(text.split("Example view.json:\n", 1)[1].split("\nRun:", 1)[0])
    assert recipe["workspace_id"] == "WORKSPACE_ID"
    assert recipe["title"] and len(recipe["blocks"]) == 3
    assert recipe["blocks"][0]["note"]
    assert recipe["blocks"][1]["item"] == "ITEM_ID"
    assert recipe["blocks"][2]["items"] == {"kind": "message", "linked": "ITEM_ID"}
    assert recipe["blocks"][2]["limit"] == 10
    assert "note, facts, items and item" in text
    assert "workspace_id goes in the file" in text
    assert "show status does not take workspace_id=" in text


def test_write_help_says_who_adds_context_and_names_two_refusals(capsys):
    text = help_of(capsys, "write")
    assert "when you may make it" in text and "your person may" not in text and "requires_review" in text and "--propose --reason" in text
    assert "basis_changed" in text and "quotation_mismatch" in text


def changes() -> dict:
    """The README's changelog: each section's heading (a version, or Next release) and its text."""
    text = (ROOT / "README.md").read_text().split("## Changes", 1)[1].split("\n## ", 1)[0]
    return dict(section.split("\n", 1) for section in text.split("\n### ")[1:])


def test_the_readme_says_what_this_release_changed():
    """Messages (decision 90): the inbox query and the two write operations, in 0.4.3."""
    released = changes()["0.4.3"]
    assert "inbox=me" in released and "message.send" in released and "message.acknowledge" in released


def test_the_readme_says_what_0_4_5_changed():
    released = changes()["0.4.5"]
    assert "write message" in released and "write seen" in released and "show status" in released


def test_the_readme_describes_the_importer_fix_in_0_4_4():
    assert "If WIRK becomes unreachable while an import starts" in changes()["0.4.4"]


def test_the_readme_keeps_a_place_for_the_next_release():
    assert changes()["Next release"].strip()


ALL = [*COMMANDS, "import"]


def test_ranking_by_meaning_always_names_pro(capsys):
    """Free and Team rank by words (decision 65); help never promises meaning ranking without the plan."""
    for text in [help_of(capsys), *(help_of(capsys, command) for command in ALL)]:
        for line in text.split("\n"):
            assert "by meaning" not in line or "Pro" in line, line


def test_budgets_state_their_range(capsys):
    assert "max_bytes (1024–65536;" in help_of(capsys, "query") and "limit (1–100" in help_of(capsys, "query")
    assert "wait (0–60 s, with inbox=me)" in help_of(capsys, "query") and "wait for a message" in help_of(capsys, "query")
    assert "1024–65536" in help_of(capsys, "status")


def test_download_says_file_is_a_file_id(capsys):
    assert "file ID" in help_of(capsys, "download") and "file ID" in help_of(capsys)


def test_write_help_retries_and_parent_revisions(capsys):
    text = help_of(capsys, "write")
    assert "contributes_to" in text and "TO@N" in text
    assert "--request-id it printed" not in text  # --request takes no --request-id; the hint prints the retry
    assert not [line for line in text.split("\n") if "kind=context" in line and "--propose" in line]


def test_write_help_teaches_one_link_form(capsys):
    """WIRK 63fefe7c: the example links without @N, as write.md does; one clause says when @N matters."""
    text = help_of(capsys, "write")
    assert re.search(r"--link contributes_to:[0-9a-f]+  under an initiative", text)
    assert not re.search(r"--link \w+:[0-9a-f]+@", text)
    guidance = " ".join(text.split())
    assert "linked-from item's rN" in guidance
    assert "parent work's rN" in guidance
    assert "no destination rN for an initiative" in guidance


def test_write_help_distinguishes_context_levels_and_stdin_forms(capsys):
    text = help_of(capsys, "write")
    assert "kind=context level=initiative --body-file" in text
    assert "kind=context level=organization --body-file" in text
    assert "--body-file PATH reads a regular file; - reads raw body text from stdin" in text
    assert "--request - reads one JSON request" in text


def test_the_admin_example_gives_the_person_the_membership(capsys):
    text = help_of(capsys, "admin")
    assert '"member.set", "principal_id": "alice", "role"' in text and '"principal_id": "alice-agents", "role"' not in text


def test_help_lines_stay_narrow(capsys):
    for text in [help_of(capsys), *(help_of(capsys, command) for command in ALL)]:
        assert all(len(line) <= 120 or line in RESERVATION_LONG_LINES for line in text.split("\n"))


def test_query_help_states_the_budget_and_what_a_read_leaves_out(capsys):
    """wirk-core #36: the defaults and range of max_bytes, the three signals of a partial read and the two link shapes."""
    text = " ".join(help_of(capsys, "query").split())
    assert "max_bytes (1024–65536; default 8192 for cards, 16384 at depth full, 32768 at depth all)" in text
    for signal in ("page.complete", "body_complete", "left_out", "Left out for the budget", "not shown", "more:"):
        assert signal in text, signal
    for shape in ("links_out", "links_in", "by_status", "at most 5 entries", "query linked=ID"):
        assert shape in text, shape


def test_help_names_messages_and_their_two_operations(capsys):
    """docs/plans/messaging.md §8: the inbox, the board query and the two write operations, as short forms (01289c53)."""
    assert "query inbox=me" in help_of(capsys) and "write message" in help_of(capsys)
    query_help = help_of(capsys, "query")
    assert "query kind=message linked=" in query_help and "inbox=me" in query_help and "participant=" in query_help
    write_help = help_of(capsys, "write")
    assert '{"op": "message.send", "ref": "m", "data": {"body", "title", "to": ["alice-agents"], "reply_to", "uploads"}}' in write_help
    assert '{"op": "message.acknowledge", "messages": [ID]}' in write_help and "received, not agreed" in write_help
    assert 'a reply notifies only its "to"' in write_help


def test_query_help_distinguishes_message_replies_from_board_posts(capsys):
    text = help_of(capsys, "query")
    assert any("query kind=message linked=" in line and "board" in line for line in text.splitlines())
    assert "direct replies (20 at a time), query its ID; kind=message linked=ITEM lists board messages" in text


def test_the_message_short_forms_have_one_line_each_in_the_main_help(capsys):
    lines = help_of(capsys).split("\n")
    assert [line.split()[:2] for line in lines if line.startswith("  write message") or line.startswith("  write seen")] == [
        ["write", "message"], ["write", "seen"]]


def test_write_help_gives_the_message_forms_beside_the_json_shapes(capsys):
    text = help_of(capsys, "write")
    assert "wirk write message [--to ID,…] [--on ITEM,…]" in text and "wirk write seen ID…" in text
    assert '{"op": "message.send"' in text and '{"op": "message.acknowledge"' in text


def test_query_help_says_a_list_leaves_messages_out_unless_it_asks(capsys):
    assert "A list leaves messages out unless it asks" in " ".join(help_of(capsys, "query").split())


def test_query_help_names_the_answer_shape_the_cap_and_the_one_cut_hint(capsys):
    """11be4836 (joint record §1.5): where a JSON answer keeps what, how far max_bytes goes, and that a cut body has one
    hint, the line that continues it, with depth=all left to its last page."""
    text = " ".join(help_of(capsys, "query").split())
    for shape in ("data.results[]", "data.cards[]", "r (its revision", "fields and owner", "page.next_cursor",
                  "notifications", "body_complete"):
        assert shape in text, shape
    assert "32768 at depth all); larger is refused" in text
    assert "a cut body ends with … and one more: line that continues it" in text and "depth=all starts over" in text


def test_write_help_spells_the_owner_three_ways_in_one_sentence(capsys):
    """11be4836: owner=me in the CLI, work.owner_id on the wire, owner at the top of a read; none of them changes."""
    sentences = [s for s in " ".join(help_of(capsys, "write").split()).split(". ") if "owner_id" in s]
    assert len(sentences) == 1 and all(word in sentences[0] for word in ("owner=me", "work.owner_id", "owner at the top"))


def test_write_edit_help_is_the_edit_section_alone(capsys):
    """11be4836: `wirk write edit --help` printed the whole write help; it gives the edit form, its examples, the owner
    spelling and the kind change a claim makes, within the line limit."""
    text = help_of(capsys, "write", "edit")
    lines = text.rstrip("\n").split("\n")
    assert text != help_of(capsys, "write") and lines[0].startswith("usage: wirk write edit ID@N")
    assert len(lines) <= 23
    assert all(len(line) <= 120 or line in RESERVATION_LONG_LINES for line in lines)
    assert "write new" not in text and '{"op"' not in text
    for needed in ("status=in_progress owner=me", "reservation=occupied", "--evidence", "basis_changed",
                   "record → work", "owner=me", "work.owner_id", "write message --on"):
        assert needed in text, needed
    assert help_of(capsys, "write", "new") == help_of(capsys, "write")  # new uses general write help


@pytest.mark.parametrize("form, first_line, needed", [
    ("message", "usage: wirk write message", ("--on", "--to", "--reply-to", "--body-file",
                                               "workspace_id=ID", "--request-id", "--json",
                                               "active work", "inbox", "reachable ancestor boards",
                                               "--propose is refused")),
    ("seen", "usage: wirk write seen", ("workspace_id=ID", "--request-id", "--json",
                                           "addressed to you", "boards of work you own", "ancestor boards", "across sessions",
                                           "receipt, not agreement", "own handoff")),
])
def test_message_short_form_help_is_targeted_and_read_only(capsys, monkeypatch, form, first_line, needed):
    def no_transport(*args, **kwargs):
        pytest.fail("help attempted a transport call")

    monkeypatch.setattr(cli, "connect", no_transport)
    text = help_of(capsys, "write", form)
    lines = text.rstrip("\n").split("\n")
    assert lines[0].startswith(first_line)
    assert len(lines) <= 25 and max(map(len, lines)) <= 120
    assert text != help_of(capsys, "write")
    assert "write new" not in text and "write edit" not in text and '{"op"' not in text
    for phrase in needed:
        assert phrase in text


def test_write_help_limits_common_options_to_supported_short_forms(capsys):
    text = help_of(capsys, "write")
    assert len(text.rstrip("\n").split("\n")) <= 41
    assert ("--link TYPE is related_to, contributes_to, requires or works_at (a person record to a company record)."
            in " ".join(text.split()))
    assert "every form also takes --reason TEXT, --propose" not in text
    assert "--propose needs --reason and cannot send or acknowledge messages" in text


def test_query_help_shows_how_to_read_a_files_text(capsys):
    """2c355356's blind trial: an agent could download originals but not find the derived text; the help shows the
    existing object fetch with a whole-file select, and points at the cites link for an exact quotation."""
    text = " ".join(help_of(capsys, "query").split())
    assert '"select":[{"type":"whole","part":{"type":"file","file_id":"F"}}]' in text
    assert "[partial: REASON] marks known incomplete extraction" in text and "(cite: write --help)" in text


def test_write_help_says_what_item_create_makes_by_default(capsys):
    """friction entry 14: kind=record in item.create data was refused unknown_field; a record needs no key."""
    text = " ".join(help_of(capsys, "write").split())
    assert "--request - reads one JSON request. A record is the default; data.work makes work." in text
    assert text.count("--propose needs --reason") == 1


def test_query_help_names_where_a_fetched_body_is(capsys):
    """friction entry 22: agents measuring a body against a limit dug it out of the whole answer."""
    assert "body_complete covers the body alone (in a fetch, data.results[0].body)." in help_of(capsys, "query")


def test_import_help_names_the_destination_and_the_persons_command(capsys):
    """friction entry 25: the destination wirkspace was hard to find, and the person's step was buried in a sentence."""
    text = help_of(capsys, "import")
    assert "workspace_id=ID names the destination; the ID comes from wirk status." in " ".join(text.split())
    assert "\n     wirk login --person && wirk admin --request ~/.config/wirk/import/github-setup.json\n" in text


LOGIN_HELP = """usage: wirk login [--person] [--url URL] [--new]

Connect this machine. It makes this machine's token, keeps it in ~/.config/wirk (or $WIRK_CONFIG_DIR),
readable only by you, and opens your browser on WIRK's approval: one click approves it.
With no browser here (SSH, a container) it prints a link to open in a browser on any computer;
--person then asks for the code it shows and signs in only you. To connect your agents for status,
query and write, run wirk login separately. A login lasts until you revoke the machine on your
account page or it goes 90 days unused; then run login --new for a new machine token.
In an agent chat (Codex or Claude Code), the chat runs wirk login once and signs in as itself under
this machine's approval, with no browser. --new is only ever for the machine.
Where a service has no web sign-in, it prints the token's digest instead (safe to share; the token stays
here) for your WIRK administrator to register. The token is only ever sent to its address.

  login                                connect your agents on this machine
  login --person                       your own sign-in, at your own terminal, for review --person
  login --url https://wirk.example.org --new        another address needs a new token
"""  # wirk-core docs/plans/one-click-login.md §6, byte for byte


def test_login_help_is_the_plans_text(capsys):
    assert help_of(capsys, "login") == LOGIN_HELP


def test_help_that_sends_people_to_login_says_one_click(capsys):
    main = help_of(capsys)
    assert ("\n  login                                connect this machine: one click in your browser, "
            "or a link when there is none\n") in main
    assert "wirk login --person (one click in your browser); it asks you to type the decision back." in help_of(capsys, "review")
    assert "(the token made with\nwirk login --person, one click in your browser)." in help_of(capsys, "admin")


RESERVATION_EXAMPLES = """  write edit 5c1e7a90@7 reservation=occupied --request-id RUN-start
  write edit 5c1e7a90@8 reservation=vacant:8 --request-id RUN-release     release it without completing (completing releases it)
  write edit 5c1e7a90@8 reservation=replace:8 --reason 'why, and the run that takes over' --request-id RUN-replace"""

RESERVATION_GUIDANCE = """reservation=occupied starts; occupied:G guards any edit; vacant:G releases; replace:G takes over (reviewer or administrator, with --reason, when a person asks).
RUN is an ID you make once per run; reuse the identical request_id and body after an uncertain result. Never adopt G from a reread; completing the work releases it."""

RESERVATION_STALE = 'A stale N is refused as basis_changed: query again and use the new rN; for reservation edits retain your acquired G and never retry a start while occupied.'
RESERVATION_LONG_LINES = {line for line in [*RESERVATION_EXAMPLES.splitlines(), *RESERVATION_GUIDANCE.splitlines(),
                                           "--propose needs --reason. " + RESERVATION_STALE] if len(line) > 120}


@pytest.mark.parametrize("argv", [("write",), ("write", "edit")])
@pytest.mark.parametrize("index, reservation, revision, reason, request_id", [
    (0, "occupied", 7, None, "RUN-start"),
    (1, "vacant:8", 8, None, "RUN-release"),
    (2, "replace:8", 8, "why, and the run that takes over", "RUN-replace"),
])
def test_reservation_help_examples_parse(capsys, run, argv, index, reservation, revision, reason, request_id):
    text = help_of(capsys, *argv)
    printed = [line for line in examples(text) if line.startswith("wirk write edit ") and "reservation=" in line]
    assert printed == examples(RESERVATION_EXAMPLES)
    code, out, err, fake = run(shlex.split(printed[index])[1:])
    assert code == 0, (out, err)
    assert fake.requests[0].url.path == "/v2/write"
    assert fake.bodies() == [{
        "request_id": request_id, "format": "text", "expect": {"5c1e7a90": revision},
        "operations": [{"op": "item.edit", "id": "5c1e7a90", "patch": {"reservation": reservation}}],
        **({"reason": reason} if reason is not None else {}),
    }]


@pytest.mark.parametrize("argv", [("write",), ("write", "edit")])
def test_reservation_help_keeps_status_examples_and_a_compact_block(capsys, argv):
    text = help_of(capsys, *argv)
    if argv == ("write", "edit"):
        status = "  write edit 5c1e7a90@3 status=in_progress owner=me     claim it"
        assert RESERVATION_STALE in text
        assert "query the item again and use its new rN" not in text
    else:
        status = "  write edit 5c1e7a90@3 status=in_progress                                  N is the rN you read"
        assert '{"op": "item.edit", "id": ID, "patch": {"title", "body", "work", "fields", "reservation", "attach_uploads"}}' in text
    completed = "  write edit 5c1e7a90@4 status=completed --evidence 'tests/test_retry.py passes'"
    assert status + "\n" + completed + "\n" + RESERVATION_EXAMPLES + "\n" + RESERVATION_GUIDANCE + "\n" in text
