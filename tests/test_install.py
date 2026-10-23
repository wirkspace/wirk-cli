"""install.sh: one command from nothing to a logged-in machine, in POSIX sh, verified, idempotent and without sudo."""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "install.sh"
LATEST = "0.4.29"


def test_it_is_posix_sh_and_never_uses_sudo():
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            subprocess.run([shell, "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert text.startswith("#!/bin/sh") and "set -eu" in code and "sudo" not in code


def test_help_prints_the_whole_header():
    lines = SCRIPT.read_text().splitlines()[1:]
    header = lines[:next(n for n, line in enumerate(lines) if not line.startswith("#"))]
    result = subprocess.run(["sh", str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == header


def basic_path(tmp_path):
    """A PATH with the system's tools but without uv, claude or codex."""
    bin_dir = tmp_path / "basic-bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("sh", "curl", "mkdir", "mktemp", "rm", "cat", "uname", "grep", "sed", "awk", "sha256sum", "shasum",
                 "cp", "dirname", "tr", "head", "printf", "env", "git", "cut", "touch"):
        found = shutil.which(tool)
        if found and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(found)
    return str(bin_dir)


def test_dry_run_prints_every_step_and_changes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(["sh", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=60,
                            env={"HOME": str(home), "PATH": basic_path(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "https://astral.sh/uv/install.sh" in out
    assert "tool install --force --upgrade wirk-mcp\n" in out and "tool install --force --upgrade wirk\n" in out
    assert "==" not in out and "wirk login" in out  # the latest release, never a pinned one
    assert "Claude Code is not installed; skipped" in out and "Codex is not installed; skipped" in out
    assert out.startswith("Installing WIRK\n(dry run: nothing will be changed)")
    assert list(home.iterdir()) == []


def wheel(folder, name, version, *requires):
    """A small wheel whose command prints its package, version and arguments: enough for uv to install and upgrade."""
    module = name.replace("-", "_")
    info = f"{module}-{version}.dist-info"
    files = {f"{module}.py": f"import sys\ndef main():\n    print({name!r}, {version!r}, *sys.argv[1:])\n",
             f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                 + "".join(f"Requires-Dist: {r}\n" for r in requires),
             f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             f"{info}/entry_points.txt": f"[console_scripts]\n{name} = {module}:main\n"}
    record = "".join(f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b'=').decode()},"
                     f"{len(text.encode())}\n" for path, text in files.items()) + f"{info}/RECORD,,\n"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name_in_wheel, text in {**files, f"{info}/RECORD": record}.items():
            archive.writestr(name_in_wheel, text)
    return path


def publish(tmp_path, version, *, mcp=True):
    """Put wirk VERSION, and wirk-mcp VERSION pinned to it, on the fake package index."""
    wheel(tmp_path / "index", "wirk", version)
    if mcp:
        wheel(tmp_path / "index", "wirk-mcp", version, f"wirk=={version}")


def release(tmp_path, *, corrupt=False):
    """The fake package index with the latest clients, and skill releases (latest and v0.4.26) with SHA256SUMS."""
    publish(tmp_path, "0.4.26")
    publish(tmp_path, LATEST)
    base = tmp_path / "releases"
    for release_path, text in (("latest/download", "latest"), ("download/v0.4.26", "0.4.26")):
        skill = base / "wirk-skill" / "releases" / release_path
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: wirk\n---\n{text}\n")
        digest = "0" * 64 if corrupt else hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest()
        (skill / "SHA256SUMS").write_text(f"{digest}  SKILL.md\n")
    return base


def shims(tmp_path):
    """claude and codex that record their calls and keep a server named wirk the way the real ones do:
    Claude Code's add refuses an existing user entry and its remove fails when there is none; Codex's add replaces."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir(exist_ok=True)
    for name, label, exists in (("claude", "Command", 'echo "MCP server wirk already exists in user config" >&2; exit 1'),
                                ("codex", "command", ":")):
        shim = shim_dir / name
        shim.write_text(f"""#!/bin/sh
echo "{name} $*" >> "{tmp_path}/calls"
state="{tmp_path}/{name}-wirk"
case "$*" in
  "mcp get wirk")  # a project's own entry (marked by .local-wirk) wins over the user entry, as in Claude Code
    if [ -f .local-wirk ]; then echo "  {label}: $(cat .local-wirk)"; exit 0; fi
    [ -f "$state" ] || exit 1
    case "$(cat "$state")" in http*) echo "  URL: $(cat "$state")" ;; *) echo "  {label}: $(cat "$state")" ;; esac ;;
  "mcp add"*" wirk -- "*) if [ -f "$state" ]; then {exists}; fi; all="$*"; printf '%s' "${{all##* -- }}" > "$state" ;;
  "mcp remove"*" wirk") [ -f "$state" ] && rm "$state" && exit 0; exit 1 ;;
esac
exit 0
""")
        shim.chmod(0o755)
    return str(shim_dir)


def registered(tmp_path, host):
    """The command a host would run for its server named wirk, or None."""
    state = tmp_path / f"{host}-wirk"
    return state.read_text() if state.exists() else None


URL = "http://127.0.0.1:9"


def install(tmp_path, base, *extra, cwd=None, **env):
    """Run the installer against the fake index and releases only: uv never reaches the network."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    uv = shutil.which("uv")
    env |= {"HOME": str(home), "PATH": f"{shims(tmp_path)}:{os.path.dirname(uv)}:{basic_path(tmp_path)}",
            "WIRK_RELEASE_BASE": f"file://{base}", "UV_TOOL_DIR": str(tmp_path / "tools"),
            "UV_TOOL_BIN_DIR": str(tmp_path / "bin"), "UV_FIND_LINKS": str(tmp_path / "index"), "UV_NO_INDEX": "1",
            "UV_OFFLINE": "1", "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_PYTHON_PREFERENCE": "only-system",
            "UV_PYTHON": sys.executable}
    return subprocess.run(["sh", str(SCRIPT), "--yes", *extra], capture_output=True, text=True, timeout=300, env=env, cwd=cwd)


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed to install the wheels")


def versions(tmp_path):
    """What the installed wirk and wirk-mcp commands say they are."""
    return [subprocess.run([tmp_path / "bin" / tool], capture_output=True, text=True).stdout.split()[1]
            for tool in ("wirk", "wirk-mcp")]


@needs_uv
def test_a_real_install_takes_the_latest_clients_and_is_idempotent(tmp_path):
    base = release(tmp_path)
    first = install(tmp_path, base, "--url", URL)
    assert first.returncode == 0, first.stdout + first.stderr
    bin_dir = tmp_path / "bin"
    calls = (tmp_path / "calls").read_text()
    assert f"claude mcp add --scope user wirk -- {bin_dir}/wirk-mcp" in calls
    assert f"codex mcp add wirk -- {bin_dir}/wirk-mcp" in calls
    assert versions(tmp_path) == [LATEST, LATEST]
    assert f"wirk {LATEST} login --url {URL}" in first.stdout
    before_login = first.stdout.split("Connecting this machine")[0]
    assert len(before_login.strip().splitlines()) <= 8 and "would run" not in before_login  # a few calm lines
    assert ".whl" not in before_login and ".sums" not in before_login  # no download paths or commands
    assert f"wirk and wirk-mcp {LATEST}" in before_login
    home = tmp_path / "home"
    assert (home / ".claude" / "skills" / "wirk" / "SKILL.md").exists() and (home / ".agents" / "skills" / "wirk" / "SKILL.md").exists()
    assert not (home / ".codex" / "skills").exists()  # Codex reads ~/.agents/skills too; one copy, not two
    second = install(tmp_path, base, "--url", URL)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "replacing" not in second.stdout and versions(tmp_path) == [LATEST, LATEST]
    assert registered(tmp_path, "claude") == registered(tmp_path, "codex") == f"{bin_dir}/wirk-mcp"
    assert (tmp_path / "calls").read_text().count("mcp add") == 2  # a current registration is left as it is


def earlier_install(tmp_path, codex_copy):
    """What an earlier install left: registrations and skill links into an old deployment, which must stay as it was."""
    old = tmp_path / "old-deployment"
    (old / "skill").mkdir(parents=True)
    (old / "skill" / "SKILL.md").write_text("old skill\n")
    for host in ("claude", "codex"):
        (tmp_path / f"{host}-wirk").write_text(f"{old}/.venv/bin/wirk-mcp")
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".claude" / "skills" / "wirk").symlink_to(old / "skill")
    codex = home / codex_copy
    codex.parent.mkdir(parents=True)
    if codex_copy == ".agents/skills/wirk":  # linked to the old folder
        codex.symlink_to(old / "skill")
    else:  # a real folder whose SKILL.md links to the old file
        codex.mkdir()
        (codex / "SKILL.md").symlink_to(old / "skill" / "SKILL.md")
    return old, home


@needs_uv
@pytest.mark.parametrize("codex_copy", [".agents/skills/wirk", ".codex/skills/wirk"])
def test_a_reinstall_replaces_what_an_earlier_install_left(tmp_path, codex_copy):
    """An old registration and skill links into an old deployment are replaced; the old files stay as they were."""
    base = release(tmp_path)
    old, home = earlier_install(tmp_path, codex_copy)
    result = install(tmp_path, base, "--url", URL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert registered(tmp_path, "claude") == registered(tmp_path, "codex") == f"{tmp_path}/bin/wirk-mcp"
    assert result.stdout.count(f"replacing the wirk MCP server that ran {old}/.venv/bin/wirk-mcp") == 2
    release_skill = (base / "wirk-skill" / "releases" / "latest" / "download" / "SKILL.md").read_text()
    for folder in (home / ".claude" / "skills" / "wirk", home / codex_copy):
        assert not folder.is_symlink() and not (folder / "SKILL.md").is_symlink(), folder
        assert (folder / "SKILL.md").read_text() == release_skill, folder
    other = ".codex/skills/wirk" if codex_copy == ".agents/skills/wirk" else ".agents/skills/wirk"
    assert not (home / other).exists()  # Codex lists each copy, so no second one is made
    assert (old / "skill" / "SKILL.md").read_text() == "old skill\n"  # never written through a link


@needs_uv
def test_the_user_entry_is_read_outside_any_project_and_a_url_entry_is_replaced(tmp_path):
    """Run from a project with its own wirk entry, the installer still judges the user entry; a URL entry is named."""
    base = release(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".local-wirk").write_text("/somewhere/else/wirk-mcp")
    (tmp_path / "claude-wirk").write_text(f"{tmp_path}/bin/wirk-mcp")  # already current
    (tmp_path / "codex-wirk").write_text("https://old.example/mcp")
    result = install(tmp_path, base, "--url", URL, cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Claude Code: replacing" not in result.stdout
    assert "Codex: replacing the wirk MCP server that ran https://old.example/mcp" in result.stdout
    assert registered(tmp_path, "claude") == registered(tmp_path, "codex") == f"{tmp_path}/bin/wirk-mcp"


def test_a_dry_run_names_the_replacement_and_changes_nothing(tmp_path):
    old, home = earlier_install(tmp_path, ".agents/skills/wirk")
    result = subprocess.run(["sh", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=60,
                            env={"HOME": str(home), "PATH": f"{shims(tmp_path)}:{basic_path(tmp_path)}"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"replacing the wirk MCP server that ran {old}/.venv/bin/wirk-mcp" in result.stdout
    assert "would run: claude mcp remove --scope user wirk" in result.stdout
    calls = (tmp_path / "calls").read_text()
    assert "mcp add" not in calls and "mcp remove" not in calls
    assert registered(tmp_path, "claude") == registered(tmp_path, "codex") == f"{old}/.venv/bin/wirk-mcp"
    assert (home / ".claude" / "skills" / "wirk").is_symlink() and (home / ".agents" / "skills" / "wirk").is_symlink()


@needs_uv
def test_a_rerun_upgrades_in_place_to_the_latest_release(tmp_path):
    """Run again after a release, the installer moves both clients to it, even from the wheels an earlier one kept."""
    base = release(tmp_path)
    kept = wheel(tmp_path / "home" / ".local" / "share" / "wirk" / "wheels", "wirk", "0.4.15")  # an earlier installer's
    subprocess.run(["uv", "tool", "install", str(kept)], check=True, capture_output=True,
                   env=os.environ | {"UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
                                         "UV_OFFLINE": "1", "UV_PYTHON": sys.executable})
    assert install(tmp_path, base, "--url", URL).returncode == 0
    assert not kept.parent.exists()  # nothing uses them any more
    assert versions(tmp_path) == [LATEST, LATEST]
    publish(tmp_path, "0.4.29")
    again = install(tmp_path, base, "--url", URL)
    assert again.returncode == 0, again.stdout + again.stderr
    assert versions(tmp_path) == ["0.4.29", "0.4.29"]
    assert "wirk/wheels" not in (tmp_path / "tools" / "wirk" / "uv-receipt.toml").read_text()


@needs_uv
@pytest.mark.parametrize("answer, status", [("", 0), ("wirk-mcp v0.4.28", 1)])
def test_no_version_from_uv_stops_before_wirk_is_installed(tmp_path, answer, status):
    """wirk is installed at wirk-mcp's version or not at all: never unpinned, never past a uv failure."""
    base = release(tmp_path)
    shim = Path(shims(tmp_path)) / "uv"
    shim.write_text(f'#!/bin/sh\nif [ "$1 $2" = "tool list" ]; then echo "{answer}"; exit {status}; fi\n'
                    f'exec {shutil.which("uv")} "$@"\n')
    shim.chmod(0o755)
    result = install(tmp_path, base, "--url", URL)
    assert result.returncode != 0 and not (tmp_path / "bin" / "wirk").exists(), result.stdout + result.stderr


@needs_uv
def test_wirk_follows_the_latest_wirk_mcp_so_the_two_always_match(tmp_path):
    """Between the CLI's release and the MCP server's, the installer keeps the pair that was released together."""
    base = release(tmp_path)
    publish(tmp_path, "0.4.29", mcp=False)
    result = install(tmp_path, base, "--url", URL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert versions(tmp_path) == [LATEST, LATEST]


@needs_uv
@pytest.mark.parametrize("version", ["0.4.26", LATEST])  # a wirk-skill v0.4.26 release exists; a v0.4.29 does not yet
def test_wirk_version_pins_the_two_clients_and_the_skill_stays_the_latest(tmp_path, version):
    """The three packages do not share versions, so the skill comes from its own latest release either way."""
    base = release(tmp_path)
    result = install(tmp_path, base, "--url", URL, WIRK_VERSION=version)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"Installing WIRK {version}\n") and versions(tmp_path) == [version, version]
    assert f"wirk {version} login --url {URL}" in result.stdout
    assert (tmp_path / "home" / ".claude" / "skills" / "wirk" / "SKILL.md").read_text().endswith("latest\n")


@needs_uv
def test_a_bad_skill_checksum_stops_before_the_skill_is_installed(tmp_path):
    base = release(tmp_path, corrupt=True)
    result = install(tmp_path, base, "--url", URL)
    assert result.returncode != 0 and "does not match its SHA-256 sum" in result.stderr
    assert not (tmp_path / "home" / ".claude" / "skills" / "wirk").exists()


def test_the_documented_command_is_the_sites_and_no_version_is_pinned():
    command = "curl -fsSL https://wirk.life/install | sh"
    for name in ("README.md", "install.sh", "scripts/release_assets.py"):
        assert command in (ROOT / name).read_text(), name
    assert f'version = "{LATEST}"' in (ROOT / "pyproject.toml").read_text()
    assert not re.search(r"\d+\.\d+\.\d+", SCRIPT.read_text())  # the latest release, whichever it is
