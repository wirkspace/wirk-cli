"""wirk keeps itself current: after a command, at most once an hour, a detached process installs PyPI's latest
and replaces each installed copy of the WIRK skill with its latest release.

Only after() runs in every command, so what the detached process alone needs is imported inside its functions.
"""

import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .client import config_dir

CHILD = [sys.executable, "-I", "-m", "wirk_cli.update"]
HOUR, DAY = 3600, 86400
INDEX = "https://pypi.org/simple"  # the only index an upgrade asks: uv and pip read no other index or config
RELEASE = re.compile(r"\d+\.\d+\.\d+")
SKILL = "https://github.com/wirkspace/wirk-skill/releases"
WIRK_SKILL = re.compile(r"---\n(?:(?!---).*\n)*?name: wirk\n")  # front matter naming the skill
SKILL_VERSION = re.compile(r"\nWIRK skill (\d+\.\d+\.\d+)\.\n?\Z")  # the skill's last line


def after(command: str) -> None:
    """In the command's process, once its answer is out: show a notice once a day, start a check once an hour."""
    if command == "login" or os.environ.get("WIRK_NO_UPDATE") or os.name != "posix":
        return
    try:
        directory = config_dir()
        with locked(directory) as held:
            if not held:  # an update is running now
                return
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
    with locked(directory, wait=True):  # after any update still running, so two never change one environment at once
        wanted = [(name, have, command, env, latest(name, transport)) for name, have, command, env in targets(prefix)]
        if any(want is None for *_, want in wanted):  # PyPI did not answer plainly: try again next hour
            return
        notices = []
        for name, have, command, env, want in wanted:
            command = command and [part.format(want) for part in command]
            if newer(want, have) and not (command and upgraded(command, env, directory)):
                notices.append(f"{name} {want} is out; this is {have}. To update: uv tool install {name}=={want}")
        notices += refresh_skill(transport)
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
        tools = [uv_tool(name, prefix, found[name][0])]
        sibling = prefix.parent / "wirk-mcp"  # the installer's layout: wirk-mcp in a tool of its own
        mcp = installed(sibling).get("wirk-mcp") if name == "wirk" and (sibling / "uv-receipt.toml").exists() else None
        return tools + ([uv_tool("wirk-mcp", sibling, mcp[0])] if mcp and not mcp[1] else [])
    command = [str(prefix / "bin" / "python"), "-I", "-m", "pip", "install", "--isolated", "--quiet",
               "--disable-pip-version-check", "--index-url", INDEX, name + "=={}"]
    env = {**os.environ, "PIP_CONFIG_FILE": os.devnull}  # devnull: pip reads no config file at all, the venv's included
    return [(name, found[name][0], command if (prefix / "pyvenv.cfg").exists() else None, env)]


def installed(prefix: Path) -> dict:
    """{name: (version, editable)} for wirk and wirk-mcp in the environment at PREFIX."""
    from importlib.metadata import distributions
    found = {}
    for dist in distributions(path=[str(path) for path in prefix.glob("lib/python*/site-packages")]):
        name = dist.metadata["Name"]
        if name in ("wirk", "wirk-mcp"):
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
            found[name] = (dist.version, bool(direct.get("dir_info", {}).get("editable")))
    return found


def uv_tool(name: str, prefix: Path, version: str):
    """The uv tool at PREFIX upgrades in place: its tool folder, its commands' folder and its Python stay; its
    --with packages do not (the installer's wirk-mcp tool has the old wirk there, which the new one's pin replaces).
    Only uv's index settings are dropped: others, such as UV_PYTHON, can make the upgrade fail but not redirect it."""
    import shutil
    import tomllib
    tool = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
    uv = shutil.which("uv", path=f"{os.environ.get('PATH', '')}{os.pathsep}{Path.home() / '.local' / 'bin'}")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("UV_INDEX", "UV_DEFAULT_INDEX",
           "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS", "UV_CONFIG_FILE"))}
    env.update(UV_TOOL_DIR=str(prefix.parent), UV_TOOL_BIN_DIR=str(Path(tool["entrypoints"][0]["install-path"]).parent))
    python = ["--python", tool["python"]] if "python" in tool else []
    return name, version, uv and [uv, "tool", "install", "--no-config", "--default-index", INDEX, *python,
                                  name + "=={}"], env


def refresh_skill(transport=None) -> list:
    """Replace each installed copy of the WIRK skill older than its latest release; a notice per copy that cannot be.
    Only regular files in the installer's folders are replaced, never through a link and never in a new folder.
    The latest tag comes first; the release's files are downloaded only when some copy is older."""
    import hashlib
    import httpx
    home = Path.home()
    folders = [home / ".claude", Path(os.environ.get("CODEX_HOME") or home / ".codex"), home / ".agents"]
    copies = {}
    for path in (folder / "skills" / "wirk" / "SKILL.md" for folder in folders):
        try:
            text = "" if path.parent.is_symlink() or path.is_symlink() else path.read_text(errors="replace")
        except OSError:  # none there, or unreadable
            continue
        if WIRK_SKILL.match(text):
            copies[path] = skill_version(text)
    if not copies:
        return []
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            tag = client.get(f"{SKILL}/latest").headers.get("location", "").rpartition("/tag/v")[2]
            if not RELEASE.fullmatch(tag) or not any(newer(tag, have) for have in copies.values()):
                return []
            skill, sums = (client.get(f"{SKILL}/download/v{tag}/{name}", follow_redirects=True)
                           .raise_for_status().content for name in ("SKILL.md", "SHA256SUMS"))
    except Exception:  # no plain answer: the next hour asks again
        return []
    text = skill.decode(errors="replace")
    if f"{hashlib.sha256(skill).hexdigest()}  SKILL.md" not in sums.decode(errors="replace").splitlines() \
            or skill_version(text) != tag or not WIRK_SKILL.match(text):
        return []
    notices = []
    for path, have in copies.items():
        if newer(tag, have) and not replaced(path, skill):
            notices.append(f"WIRK skill {tag} is out; {path} is older. To update: curl -fsSL https://wirk.life/install | sh")
    return notices


def skill_version(text: str) -> str:
    """The version on a skill's last line, or 0.0.0 for a copy from before skills carried one."""
    found = SKILL_VERSION.search(text)
    return found[1] if found else "0.0.0"


def replaced(path: Path, content: bytes) -> bool:
    """Write CONTENT beside PATH with its mode, flush it to disk, then rename it over PATH."""
    temporary = path.with_name(".SKILL.md.new")
    try:
        with open(temporary, "wb") as file:
            os.chmod(temporary, path.stat().st_mode & 0o777)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
        return False


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


@contextmanager
def locked(directory: Path, wait: bool = False):
    """Holds update.lock and gives True; without wait, gives False at once when another process holds it. Any other
    failure to lock raises: nothing runs without the lock."""
    import fcntl  # POSIX only: the module itself still loads anywhere
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory / "update.lock", "a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


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
