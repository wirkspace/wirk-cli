"""Help is short and runnable; the wording, privacy and v2-only rules hold for every text the clients carry."""

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
    assert len(text.rstrip("\n").split("\n")) <= (35 if command in ("write", "admin") else 25)
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
    assert project == __version__ == "0.4.2"


def test_main_help_offers_what_is_live_and_says_who_decides(capsys):
    """Decision 85: an agent whose role may review decides with its own token; a background agent only proposes."""
    text = help_of(capsys)
    assert "\n  show " not in text  # not live on api.wirk.life yet; wirk show --help says so
    assert "\n  review ID@N accept --reason 'Why'  " in text and "only people" not in text.lower()
    assert "a background agent only proposes" in text and "--person" not in text


def test_review_help_says_who_decides_and_what_person_means(capsys):
    text = help_of(capsys, "review")
    assert "Anyone whose role may review decides" in text and "not_authorized: your role (editor) cannot review" in text
    assert "person_required" not in text and "only people" not in text.lower() and "you proposed" not in text
    assert "--person decides as yourself" in text and "wirk login --person" in text
    assert [line for line in text.split("\n") if line.startswith("  review ") and "--person" in line] == []


def test_show_help_says_it_is_not_live_yet(capsys):
    text = help_of(capsys, "show")
    assert "Not live yet" in text and "views_unavailable" in text


def test_write_help_says_who_adds_context_and_names_two_refusals(capsys):
    text = help_of(capsys, "write")
    assert "when you may make it" in text and "your person may" not in text and "requires_review" in text and "--propose --reason" in text
    assert "basis_changed" in text and "quotation_mismatch" in text


def changes() -> dict:
    """The README's changelog: each section's heading (a version, or Next release) and its text."""
    text = (ROOT / "README.md").read_text().split("## Changes", 1)[1].split("\n## ", 1)[0]
    return dict(section.split("\n", 1) for section in text.split("\n### ")[1:])


def test_the_readme_says_what_this_release_changed():
    """Decision 89 (record), decision 85's wording (#16) and the recovery hints (#13, #15), all in 0.4.2."""
    released = changes()[__version__]
    assert "record" in released and "Anyone whose role may review decides" in released and "--person" in released
    assert "run the same command again" in released and "--json" in released


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
    assert ("parent work (not an initiative) of a contributes_to link (TO@N, or contributes_to:ID@N with --link)"
            in " ".join(text.split()))


def test_the_admin_example_gives_the_person_the_membership(capsys):
    text = help_of(capsys, "admin")
    assert '"member.set", "principal_id": "alice", "role"' in text and '"principal_id": "alice-agents", "role"' not in text


def test_help_lines_stay_narrow(capsys):
    for text in [help_of(capsys), *(help_of(capsys, command) for command in ALL)]:
        assert max(len(line) for line in text.split("\n")) <= 120


def test_query_help_states_the_budget_and_what_a_read_leaves_out(capsys):
    """wirk-core #36: the defaults and range of max_bytes, the three signals of a partial read and the two link shapes."""
    text = " ".join(help_of(capsys, "query").split())
    assert "max_bytes (1024–65536; default 8192 for cards, 16384 at depth full, 32768 at depth all)" in text
    for signal in ("page.complete", "body_complete", "left_out", "Left out for the budget", "not shown", "more:"):
        assert signal in text, signal
    for shape in ("links_out", "links_in", "by_status", "at most 5 entries", "query linked=ID"):
        assert shape in text, shape


def test_help_names_messages_and_their_two_operations(capsys):
    """docs/plans/messaging.md §8: the inbox, the board query and the two write operations, with nothing new to type."""
    assert "query inbox=me" in help_of(capsys) and "message.send" in help_of(capsys)
    query_help = help_of(capsys, "query")
    assert "query kind=message linked=" in query_help and "inbox=me" in query_help and "participant=" in query_help
    write_help = help_of(capsys, "write")
    assert '{"op": "message.send", "ref": "m", "data": {"body", "title", "to": ["alice-agents"], "reply_to", "uploads"}}' in write_help
    assert '{"op": "message.acknowledge", "messages": [ID]}' in write_help and "received, not agreed" in write_help
    assert 'a reply notifies only its "to"' in write_help
