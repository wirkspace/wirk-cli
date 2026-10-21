"""wirk keeps itself current: after a command, at most once an hour, a detached process installs PyPI's latest."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from importlib.metadata import distributions
from pathlib import Path

from .client import config_dir

CHILD = [sys.executable, "-I", "-m", "wirk_cli.update"]
HOUR, DAY = 3600, 86400
RELEASE = re.compile(r"\d+\.\d+\.\d+")


def after(command: str) -> None:
    """In the command's process, once its answer is out: show a notice once a day, start a check once an hour."""
    if command == "login" or os.environ.get("WIRK_NO_UPDATE") or os.name != "posix":
        return
    try:
        directory = config_dir()
        held = lock(directory)
        if held is None:  # an update is running now
            return
        with os.fdopen(held, "w"):
            data, now = load(directory), time.time()
            if data.get("notice") and abs(now - data.get("shown", 0)) >= DAY:
                print(data["notice"], file=sys.stderr)
                data["shown"] = now
            due = abs(now - data.get("checked", 0)) >= HOUR
            if due:
                data["checked"] = now
            save(directory, data)
        if due:
            subprocess.Popen(CHILD, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:  # an update never changes a command's answer
        pass


def main(prefix: Path = Path(sys.prefix), transport=None) -> None:
    """In the detached process: upgrade each install that PyPI has a newer release of; record what could not be."""
    directory = config_dir()
    held = lock(directory)
    if held is None:
        return
    with os.fdopen(held, "w"):
        wanted = [(name, have, command, env, latest(name, transport)) for name, have, command, env in targets(prefix)]
        if any(want is None for *_, want in wanted):  # PyPI did not answer plainly: try again next hour
            return
        notices = []
        for name, have, command, env, want in wanted:
            if not newer(want, have):
                continue
            command = command and [part.format(want) for part in command]
            if not command or not upgraded(command, env, directory):
                notices.append(f"{name} {want} is out; this is {have}. To update: "
                               + (shlex.join(command) if command else f"uv tool install {name}"))
        data = load(directory)
        data["notice"] = "; ".join(notices) or None
        save(directory, data)


def targets(prefix: Path) -> list:
    """(name, installed version, upgrade command with {} for the version or None, environment) per install."""
    found = installed(prefix)
    if any(editable for _, editable in found.values()):  # a developer's checkout is never replaced
        return []
    name = "wirk-mcp" if "wirk-mcp" in found else "wirk"  # wirk-mcp pins its wirk: upgrading it brings the pair
    if (prefix / "uv-receipt.toml").exists():
        sibling = prefix.parent / "wirk-mcp"
        tools = [(name, prefix)] + ([("wirk-mcp", sibling)] if name == "wirk" and (sibling / "uv-receipt.toml").exists()
                                    else [])
        return [uv_tool(tool, place) for tool, place in tools]
    command = [str(prefix / "bin" / "python"), "-I", "-m", "pip", "install", "--quiet", name + "=={}"]
    return [(name, found[name][0], command if (prefix / "pyvenv.cfg").exists() else None, None)]


def installed(prefix: Path) -> dict:
    """{name: (version, editable)} for wirk and wirk-mcp in the environment at PREFIX."""
    found = {}
    for dist in distributions(path=[str(path) for path in prefix.glob("lib/python*/site-packages")]):
        name = dist.metadata["Name"]
        if name in ("wirk", "wirk-mcp"):
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
            found[name] = (dist.version, bool(direct.get("dir_info", {}).get("editable")))
    return found


def uv_tool(name: str, prefix: Path):
    """The uv tool at PREFIX upgrades in place: its own tool folder, and its commands where they are now."""
    import tomllib
    tool = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
    uv = shutil.which("uv") or next((str(path) for path in (Path(os.environ.get("XDG_BIN_HOME") or "/nonexistent") / "uv",
                                                            Path.home() / ".local" / "bin" / "uv") if path.is_file()), None)
    env = {**os.environ, "UV_TOOL_DIR": str(prefix.parent),
           "UV_TOOL_BIN_DIR": str(Path(tool["entrypoints"][0]["install-path"]).parent)}
    return name, installed(prefix)[name][0], uv and [uv, "tool", "install", name + "=={}"], env


def latest(name: str, transport=None):
    """PyPI's latest release of NAME as N.N.N, or None when PyPI does not answer plainly."""
    import httpx
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            version = client.get(f"https://pypi.org/pypi/{name}/json").raise_for_status().json()["info"]["version"]
    except Exception:
        return None
    return version if isinstance(version, str) and RELEASE.fullmatch(version) else None


def newer(want: str, have: str) -> bool:
    return bool(RELEASE.fullmatch(have)) and [*map(int, want.split("."))] > [*map(int, have.split("."))]


def upgraded(command: list, env, directory: Path) -> bool:
    try:
        return subprocess.run(command, env=env, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def lock(directory: Path):
    """A descriptor holding update.lock, or None when another process holds it."""
    import fcntl
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "update.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        os.close(descriptor)
        return None


def load(directory: Path) -> dict:
    try:
        data = json.loads((directory / "update.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(directory: Path, data: dict) -> None:
    temporary = directory / "update.json.new"
    temporary.write_text(json.dumps(data))
    temporary.replace(directory / "update.json")


if __name__ == "__main__":
    main()
