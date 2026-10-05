"""install.sh: one command from nothing to a logged-in machine, in POSIX sh, verified, idempotent and without sudo."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "install.sh"
VERSION = "0.4.0"


def test_it_is_posix_sh_and_never_uses_sudo():
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            subprocess.run([shell, "-n", str(SCRIPT)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert text.startswith("#!/bin/sh") and "set -eu" in code and "sudo" not in code


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
    assert f"wirk-{VERSION}-py3-none-any.whl" in out and f"wirk_mcp-{VERSION}-py3-none-any.whl" in out
    assert "SHA256SUMS" in out and "wirk login" in out
    assert "Claude Code is not installed; skipped" in out and "Codex is not installed; skipped" in out
    assert out.startswith(f"Installing WIRK {VERSION}\n(dry run: nothing will be changed)")
    assert list(home.iterdir()) == []


def release(tmp_path, *, corrupt=False):
    """Built wheels laid out as GitHub release downloads, with their SHA256SUMS."""
    base = tmp_path / "releases"
    dist = tmp_path / "dist"
    subprocess.run(["uv", "build", "-q", "--wheel", "--out-dir", str(dist), str(ROOT)], check=True)
    mcp = tmp_path / "mcp-project"
    (mcp / "src" / "wirk_mcp").mkdir(parents=True)
    (mcp / "src" / "wirk_mcp" / "__init__.py").write_text("def main():\n    print('wirk-mcp')\n")
    (mcp / "pyproject.toml").write_text(f"""[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"
[project]
name = "wirk-mcp"
version = "{VERSION}"
requires-python = ">=3.12"
dependencies = ["wirk=={VERSION}"]
[project.scripts]
wirk-mcp = "wirk_mcp:main"
[tool.hatch.build.targets.wheel]
packages = ["src/wirk_mcp"]
""")
    subprocess.run(["uv", "build", "-q", "--wheel", "--out-dir", str(dist), str(mcp)], check=True)
    for repo, wheel in (("wirk-cli", f"wirk-{VERSION}-py3-none-any.whl"), ("wirk-mcp", f"wirk_mcp-{VERSION}-py3-none-any.whl")):
        target = base / repo / "releases" / "download" / f"v{VERSION}"
        target.mkdir(parents=True)
        shutil.copy(dist / wheel, target / wheel)
        digest = hashlib.sha256((target / wheel).read_bytes()).hexdigest()
        (target / "SHA256SUMS").write_text(f"{'0' * 64 if corrupt else digest}  {wheel}\n")
    skill = base / "wirk-skill" / "releases" / "download" / f"v{VERSION}"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: wirk\n---\n")
    (skill / "SHA256SUMS").write_text(f"{hashlib.sha256((skill / 'SKILL.md').read_bytes()).hexdigest()}  SKILL.md\n")
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


def service():
    """A WIRK service without web sign-in: login falls back to showing the digest."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            code = "not_available" if self.path == "/v2/login" else "unauthenticated"
            payload = json.dumps({"ok": False, "errors": [{"code": code, "message": code}], "notices": [],
                                  "text": f"Error {code}", "page": {"complete": True, "next_cursor": None}}).encode()
            self.send_response(404 if code == "not_available" else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def install(tmp_path, base, *extra, cwd=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    uv = shutil.which("uv")
    env = {"HOME": str(home), "PATH": f"{shims(tmp_path)}:{os.path.dirname(uv)}:{basic_path(tmp_path)}",
           "WIRK_RELEASE_BASE": f"file://{base}", "UV_TOOL_DIR": str(tmp_path / "tools"),
           "UV_TOOL_BIN_DIR": str(tmp_path / "bin"), "UV_PYTHON_PREFERENCE": "only-system", "UV_PYTHON": sys.executable}
    return subprocess.run(["sh", str(SCRIPT), "--yes", *extra], capture_output=True, text=True, timeout=300, env=env, cwd=cwd)


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed to build and install the wheels")


@needs_uv
def test_a_real_install_from_verified_release_wheels_is_idempotent(tmp_path):
    url, server = service()
    base = release(tmp_path)
    first = install(tmp_path, base, "--url", url)
    assert first.returncode == 0, first.stdout + first.stderr
    bin_dir = tmp_path / "bin"
    assert (bin_dir / "wirk").exists() and (bin_dir / "wirk-mcp").exists()
    calls = (tmp_path / "calls").read_text()
    assert f"claude mcp add --scope user wirk -- {bin_dir}/wirk-mcp" in calls
    assert f"codex mcp add wirk -- {bin_dir}/wirk-mcp" in calls
    assert "This machine's token is not registered yet" in first.stdout
    before_login = first.stdout.split("Connecting this machine")[0]
    assert len(before_login.strip().splitlines()) <= 8 and "would run" not in before_login  # a few calm lines
    assert ".whl" not in before_login and ".sums" not in before_login  # no download paths or commands
    assert "Their SHA-256 sums match" in before_login or "SHA-256 sums match the release" in before_login
    home = tmp_path / "home"
    assert (home / ".claude" / "skills" / "wirk" / "SKILL.md").exists() and (home / ".agents" / "skills" / "wirk" / "SKILL.md").exists()
    assert not (home / ".codex" / "skills").exists()  # Codex reads ~/.agents/skills too; one copy, not two
    second = install(tmp_path, base, "--url", url)
    server.shutdown()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already installed" in second.stdout and "replacing" not in second.stdout
    for tool, source in receipt_sources(tmp_path):  # uv upgrades from where it installed; that must outlive the installer
        assert source.exists(), (tool, source)
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
    url, server = service()
    base = release(tmp_path)
    old, home = earlier_install(tmp_path, codex_copy)
    result = install(tmp_path, base, "--url", url)
    server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert registered(tmp_path, "claude") == registered(tmp_path, "codex") == f"{tmp_path}/bin/wirk-mcp"
    assert result.stdout.count(f"replacing the wirk MCP server that ran {old}/.venv/bin/wirk-mcp") == 2
    release_skill = (base / "wirk-skill" / "releases" / "download" / f"v{VERSION}" / "SKILL.md").read_text()
    for folder in (home / ".claude" / "skills" / "wirk", home / codex_copy):
        assert not folder.is_symlink() and not (folder / "SKILL.md").is_symlink(), folder
        assert (folder / "SKILL.md").read_text() == release_skill, folder
    other = ".codex/skills/wirk" if codex_copy == ".agents/skills/wirk" else ".agents/skills/wirk"
    assert not (home / other).exists()  # Codex lists each copy, so no second one is made
    assert (old / "skill" / "SKILL.md").read_text() == "old skill\n"  # never written through a link


@needs_uv
def test_the_user_entry_is_read_outside_any_project_and_a_url_entry_is_replaced(tmp_path):
    """Run from a project with its own wirk entry, the installer still judges the user entry; a URL entry is named."""
    url, server = service()
    base = release(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".local-wirk").write_text("/somewhere/else/wirk-mcp")
    (tmp_path / "claude-wirk").write_text(f"{tmp_path}/bin/wirk-mcp")  # already current
    (tmp_path / "codex-wirk").write_text("https://old.example/mcp")
    result = install(tmp_path, base, "--url", url, cwd=project)
    server.shutdown()
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


def receipt_sources(tmp_path):
    """Where uv would look for each installed tool when upgrading it."""
    for tool in ("wirk", "wirk-mcp"):
        receipt = (tmp_path / "tools" / tool / "uv-receipt.toml").read_text()
        for source in re.findall(r'(?:path|url|directory) = "([^"]+)"', receipt):
            yield tool, Path(source.removeprefix("file://"))


@needs_uv
def test_a_rerun_repairs_an_install_whose_wheels_are_gone(tmp_path):
    """An install made by an earlier installer points uv at wheels that no longer exist; running again repairs it."""
    url, server = service()
    base = release(tmp_path)
    assert install(tmp_path, base, "--url", url).returncode == 0
    shutil.rmtree(tmp_path / "home" / ".local" / "share" / "wirk" / "wheels")
    again = install(tmp_path, base, "--url", url)
    server.shutdown()
    assert again.returncode == 0, again.stdout + again.stderr
    assert all(source.exists() for _, source in receipt_sources(tmp_path))


@needs_uv
def test_a_bad_checksum_stops_before_anything_is_installed(tmp_path):
    base = release(tmp_path, corrupt=True)
    result = install(tmp_path, base, "--url", "http://127.0.0.1:9")
    assert result.returncode != 0 and "does not match its SHA-256 sum" in result.stderr
    assert not (tmp_path / "bin" / "wirk").exists()


def test_the_documented_command_is_the_sites_and_the_version_is_this_release():
    command = "curl -fsSL https://wirk.life/install | sh"
    for name in ("README.md", "install.sh", ".github/workflows/release.yml"):
        assert command in (ROOT / name).read_text(), name
    project = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{VERSION}"' in project and f'VERSION="${{WIRK_VERSION:-{VERSION}}}"' in SCRIPT.read_text()
