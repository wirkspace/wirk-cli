"""The Wirk-Context header: what the client reports with status, each value passing the service's published rules."""

import re
import subprocess

import pytest

from wirk_cli import context

RULES = {"harness": r"[a-z0-9.-]{1,40}", "version": r"[A-Za-z0-9.+-]{1,40}", "session": r"[0-9a-f]{16}"}


def valid(header):
    """The service's rules for the header (its wire documentation)."""
    assert len(header.encode()) < 2048
    seen = {}
    for token in header.split():
        key, _, value = token.partition("=")
        assert key in {"harness", "version", "session", "repo", "branch"} and key not in seen
        seen[key] = value
        if key in RULES:
            assert re.fullmatch(RULES[key], value)
        elif key == "repo":
            assert re.fullmatch(r"local:[0-9a-f]{8}", value) or (
                re.fullmatch(r"[!-~]{1,200}", value) and "@" not in value and "://" not in value
                and re.fullmatch(r"[^/]+/[^/]+/[^/]+", value))
        else:
            assert re.fullmatch(r"[!-~]{1,200}", value) and not set(value) & set("~^:?*[\\")
    return seen


@pytest.mark.parametrize("remote, name", [
    ("https://user:secret@git.example.org/acme/api.git", "git.example.org/acme/api"),
    ("git@git.example.org:acme/api.git", "git.example.org/acme/api"),
    ("ssh://git@gitlab.example.org:2222/acme/api", "gitlab.example.org/acme/api"),
    ("https://github.com/acme/api", "github.com/acme/api"),
    ("https://gitlab.com/group/sub/api.git", None),
    ("/srv/git/api.git", None),
    ("file:///srv/git/api.git", None),
])
def test_remote_names(remote, name):
    assert context.remote_name(remote) == name


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_header_from_a_repository(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "feature/x")
    git(tmp_path, "remote", "add", "origin", "https://token:secret@git.example.org/acme/api.git")
    monkeypatch.chdir(tmp_path)
    seen = valid(context.header("wirk-cli", "0.3.0", "0123456789abcdef"))
    assert seen == {"harness": "wirk-cli", "version": "0.3.0", "session": "0123456789abcdef",
                    "repo": "git.example.org/acme/api", "branch": "feature/x"}


def test_awkward_remote_and_branch(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "fé")  # git allows it; the service's rule (printable ASCII) does not
    git(tmp_path, "remote", "add", "origin", "https://gitlab.com/group/sub/api.git")
    monkeypatch.chdir(tmp_path)
    seen = valid(context.header("wirk-cli", "0.3.0", context.session()))
    assert re.fullmatch(r"local:[0-9a-f]{8}", seen["repo"]) and "branch" not in seen


def test_outside_a_repository_and_with_odd_host_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert set(valid(context.header("Claude Code!", "1.2.0-beta", "zz"))) == {"harness", "version"}
    assert valid(context.header("Claude Code!", "1.2 beta", "zz"))["harness"] == "claude-code"
