"""The Wirk-Context header sent with status: what client and repository this is, each value checked by the service's rules
so one odd value never voids the rest. The service shows it as reported, never as authority."""

import hashlib
import os
import re
import socket
import subprocess

RULES = {"harness": r"[a-z0-9.-]{1,40}", "version": r"[A-Za-z0-9.+-]{1,40}", "session": r"[0-9a-f]{16}",
         "branch": r"[!-~]{1,200}"}
REMOTE = re.compile(r"(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:@]+)(?::\d+)?[:/]([^/:]+)/([^/]+?)(?:\.git)?/?")


def git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def remote_name(remote: str) -> str | None:
    """host/owner/name from a remote URL, with credentials, port, scheme and .git dropped; None for anything else."""
    match = REMOTE.fullmatch(remote)
    name = "/".join(match.groups()) if match else ""
    valid = re.fullmatch(r"[!-~]{1,200}", name) and "@" not in name and "://" not in name  # the service's repo rule
    return name if valid else None


def repository() -> tuple[str | None, str | None]:
    top = git("rev-parse", "--show-toplevel")
    if not top:
        return None, None
    name = remote_name(git("remote", "get-url", "origin") or "") or "local:" + hashlib.sha256(top.encode()).hexdigest()[:8]
    branch = git("branch", "--show-current")
    return name, branch if branch and not set(branch) & set("~^:?*[\\") else None


def session() -> str:
    """Commands run from one terminal or one agent process share a POSIX session, so they share this."""
    return hashlib.sha256(f"{socket.gethostname()}:{os.getsid(0)}".encode()).hexdigest()[:16]


def header(harness: str, version: str, session_id: str) -> str:
    repo, branch = repository()
    values = {"harness": re.sub(r"[^a-z0-9.-]", "", harness.lower().replace(" ", "-")), "version": version,
              "session": session_id, "repo": repo, "branch": branch}
    return " ".join(f"{key}={value}" for key, value in values.items()
                    if value and (key == "repo" or re.fullmatch(RULES[key], value)))
