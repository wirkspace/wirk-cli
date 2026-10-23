"""wirk keeps itself current: the hourly check after a command, and the detached upgrade it starts."""

import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest

from wirk_cli import update

LATEST = {"wirk": "0.4.27", "wirk-mcp": "0.4.26"}
UV = "tool install --no-config --default-index https://pypi.org/simple"
PIP = "-I -m pip install --isolated --quiet --disable-pip-version-check --index-url https://pypi.org/simple"
INDEX_ENVIRONMENT = ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
                     "UV_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS")


@pytest.fixture
def on(monkeypatch, tmp_path):
    """Updates turned on, with their own configuration directory and a home of their own for the skill's folders."""
    monkeypatch.delenv("WIRK_NO_UPDATE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    directory = tmp_path / "config"
    monkeypatch.setenv("WIRK_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def started(monkeypatch):
    """What the command's process starts, recorded instead of started."""
    calls, real = [], subprocess.Popen

    def popen(argv, **how):
        if argv != update.CHILD:  # the CLI's own subprocesses (git, for status) run as before
            return real(argv, **how)
        calls.append((argv, how))
    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def pypi(versions=LATEST, *, status=200, body=None):
    asked = []

    def answer(request):
        asked.append(request.url.path)
        name = request.url.path.split("/")[2]
        if body is not None:
            return httpx.Response(status, content=body)
        return httpx.Response(status, json={"info": {"version": versions[name]}})
    transport = httpx.MockTransport(answer)
    transport.asked = asked
    return transport


def offline():
    def answer(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    return httpx.MockTransport(answer)


def install(prefix, versions, *, uv_tool=False, bin_dir=None, editable=(), python=None):
    """A fake environment at PREFIX holding these package versions."""
    site = prefix / "lib" / "python3.12" / "site-packages"
    for name, version in versions.items():
        info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        if name in editable:
            (info / "direct_url.json").write_text(json.dumps({"url": "file:///src", "dir_info": {"editable": True}}))
    (prefix / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (prefix / "bin").mkdir()
    if uv_tool:
        name = next(iter(versions))
        (prefix / "uv-receipt.toml").write_text(textwrap.dedent(f"""\
            [tool]
            requirements = [{{ name = "{name}" }}]
            {f'python = "{python}"' if python else ""}
            entrypoints = [{{ name = "{name}", install-path = "{bin_dir}/{name}", from = "{name}" }}]
            """))
    return prefix


def recorder(path, log, exit_code=0):
    """A fake executable that records its arguments, working directory and uv's tool folders, then exits."""
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf '%s|%s|%s|%s\\n' "$*" "$PWD" "${{UV_TOOL_DIR:-}}" "${{UV_TOOL_BIN_DIR:-}}" >> {log}
        env | grep -E '^(UV|PIP)_' >> {log}.environment
        exit {exit_code}
        """))
    path.chmod(0o755)
    return path


def calls(log):
    return [line.split("|") for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    """A fake uv on PATH; returns its log."""
    folder = tmp_path / "path"
    folder.mkdir()
    log = tmp_path / "uv.log"
    recorder(folder / "uv", log)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return log


def state(directory):
    return json.loads((directory / "update.json").read_text())


# The upgrade, in the detached process


def test_a_uv_tool_install_upgrades_itself_and_its_sibling_mcp_tool(on, tmp_path, fake_uv):
    tools, bin_dir = tmp_path / "tools", tmp_path / "custom-bin"
    prefix = install(tools / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=bin_dir)
    install(tools / "wirk-mcp", {"wirk-mcp": "0.4.21", "wirk": "0.4.21"}, uv_tool=True, bin_dir=bin_dir)
    update.main(prefix, pypi())
    assert calls(fake_uv) == [
        [f"{UV} wirk==0.4.27", str(on), str(tools), str(bin_dir)],
        [f"{UV} wirk-mcp==0.4.26", str(on), str(tools), str(bin_dir)]]
    assert state(on).get("notice") is None


def test_the_sibling_mcp_tool_catches_up_when_wirk_is_current(on, tmp_path, fake_uv):
    tools = tmp_path / "tools"
    prefix = install(tools / "wirk", {"wirk": "0.4.27"}, uv_tool=True, bin_dir=tmp_path / "bin")
    install(tools / "wirk-mcp", {"wirk-mcp": "0.4.24", "wirk": "0.4.24"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    assert [call[0] for call in calls(fake_uv)] == [f"{UV} wirk-mcp==0.4.26"]


def test_an_editable_sibling_mcp_tool_is_never_replaced(on, tmp_path, fake_uv):
    tools = tmp_path / "tools"
    prefix = install(tools / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    install(tools / "wirk-mcp", {"wirk-mcp": "0.4.21", "wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin",
            editable=["wirk-mcp"])
    update.main(prefix, pypi())
    assert [call[0] for call in calls(fake_uv)] == [f"{UV} wirk==0.4.27"]


def test_the_receipts_python_is_kept(on, tmp_path, fake_uv):
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin",
                     python="3.13")
    update.main(prefix, pypi())
    assert [call[0] for call in calls(fake_uv)] == [f"{UV} --python 3.13 wirk==0.4.27"]


def test_no_index_named_by_the_environment_reaches_the_upgrade(on, tmp_path, fake_uv, monkeypatch):
    for name in INDEX_ENVIRONMENT:
        monkeypatch.setenv(name, "http://127.0.0.1:9/elsewhere")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    environment = (tmp_path / "uv.log.environment").read_text().splitlines()
    assert [line for line in environment if line.startswith("UV_") and "elsewhere" in line] == []
    assert f"UV_CACHE_DIR={tmp_path / 'cache'}" in environment  # the rest of uv's settings stay


def test_uv_is_found_where_the_installer_puts_it(on, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    log = tmp_path / "uv.log"
    recorder(home / ".local" / "bin" / "uv", log)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    assert [call[0] for call in calls(log)] == [f"{UV} wirk==0.4.27"]


def test_a_uv_tool_install_without_uv_records_the_notice(on, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    assert state(on)["notice"] == "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk==0.4.27"


def test_a_venv_with_wirk_mcp_upgrades_wirk_mcp_never_wirk_alone(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21", "wirk-mcp": "0.4.21"})
    log = tmp_path / "pip.log"
    recorder(prefix / "bin" / "python", log)
    update.main(prefix, pypi())
    assert calls(log) == [[f"{PIP} wirk-mcp==0.4.26", str(on), "", ""]]


def test_a_venv_without_wirk_mcp_upgrades_wirk(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    log = tmp_path / "pip.log"
    recorder(prefix / "bin" / "python", log)
    update.main(prefix, pypi())
    assert [call[0] for call in calls(log)] == [f"{PIP} wirk==0.4.27"]


def test_a_failed_upgrade_records_a_command_that_works_anywhere(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log", exit_code=1)
    update.main(prefix, pypi())
    assert state(on)["notice"] == "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk==0.4.27"


def test_a_later_success_clears_the_notice(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log", exit_code=1)
    update.main(prefix, pypi())
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    update.main(prefix, pypi())
    assert state(on)["notice"] is None


def test_outside_a_virtual_environment_it_only_records_the_notice(on, tmp_path):
    prefix = install(tmp_path / "system", {"wirk": "0.4.21"})
    (prefix / "pyvenv.cfg").unlink()
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    update.main(prefix, pypi())
    assert not (tmp_path / "pip.log").exists()
    assert state(on)["notice"].endswith("To update: uv tool install wirk==0.4.27")


@pytest.mark.parametrize("transport", [offline(), pypi(status=500), pypi(body=b"<html>"), pypi({"wirk": "0.5.0rc1"}),
                                       pypi({"wirk": "0.4.21"}), pypi({"wirk": "0.4.20"})],
                         ids=["timeout", "500", "not json", "not N.N.N", "current", "older"])
def test_nothing_runs_and_nothing_is_recorded_unless_pypi_has_a_newer_release(on, tmp_path, transport):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    update.main(prefix, transport)
    assert not (tmp_path / "pip.log").exists()
    assert not (on / "update.json").exists() or state(on).get("notice") is None


def test_an_unreachable_pypi_keeps_an_earlier_notice(on, tmp_path):
    prefix = install(tmp_path / "system", {"wirk": "0.4.21"})
    (prefix / "pyvenv.cfg").unlink()
    update.main(prefix, pypi())
    update.main(prefix, offline())
    assert state(on)["notice"]


@pytest.mark.parametrize("editable", ["wirk", "wirk-mcp"])
def test_an_editable_install_is_never_replaced(on, tmp_path, editable):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21", "wirk-mcp": "0.4.21"}, editable=[editable])
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    transport = pypi()
    update.main(prefix, transport)
    assert not (tmp_path / "pip.log").exists() and transport.asked == []


def test_an_update_waits_for_whoever_holds_the_lock_then_runs(on, tmp_path):
    import fcntl
    on.mkdir()
    holder = os.open(on / "update.lock", os.O_WRONLY | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    transport = pypi()
    waiting = threading.Thread(target=update.main, args=(prefix, transport))
    waiting.start()
    time.sleep(0.3)
    assert transport.asked == []  # two upgrades of one environment never run together
    os.close(holder)
    waiting.join(timeout=5)
    assert [call[0] for call in calls(tmp_path / "pip.log")] == [f"{PIP} wirk==0.4.27"]


# The hourly check, in the command's process


def test_a_command_starts_the_detached_check_once_an_hour(on, run, started):
    run(["status"])
    run(["status"])
    assert len(started) == 1
    argv, how = started[0]
    assert argv == [sys.executable, "-I", "-m", "wirk_cli.update"]
    assert how["start_new_session"] and how["cwd"] == on
    assert all(how[stream] == subprocess.DEVNULL for stream in ("stdin", "stdout", "stderr"))


def test_after_an_hour_it_checks_again_and_a_future_stamp_counts_as_stale(on, run, started):
    run(["status"])
    for stamp in (time.time() - 3601, time.time() + 7200):
        data = state(on)
        data["checked"] = stamp
        (on / "update.json").write_text(json.dumps(data))
        run(["status"])
    assert len(started) == 3


@pytest.mark.parametrize("argv", [["login", "--bogus"], ["login", "extra"], ["login", "--help"]])
def test_login_never_checks_or_prints_the_notice(on, run, started, argv):
    on.mkdir(exist_ok=True)
    (on / "update.json").write_text(json.dumps({"notice": "wirk 0.4.27 is out"}))
    _, _, err, _ = run(argv)
    assert started == [] and "0.4.27" not in err


def test_wirk_no_update_turns_it_all_off(on, run, started, monkeypatch):
    monkeypatch.setenv("WIRK_NO_UPDATE", "1")
    on.mkdir(exist_ok=True)
    (on / "update.json").write_text(json.dumps({"notice": "wirk 0.4.27 is out"}))
    _, _, err, _ = run(["status"])
    assert started == [] and "0.4.27" not in err


def test_the_notice_is_printed_once_a_day_on_stderr_only(on, run, started):
    on.mkdir(exist_ok=True)
    notice = "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk==0.4.27"
    (on / "update.json").write_text(json.dumps({"notice": notice}))
    plain, quiet = run(["status", "--json"]), run(["status", "--json"])
    assert plain[0] == quiet[0] == 0 and plain[1] == quiet[1]
    assert plain[2].strip() == notice and notice not in quiet[2]


def test_a_corrupt_state_file_changes_nothing_about_the_answer(on, run, started):
    on.mkdir(exist_ok=True)
    (on / "update.json").write_text("{not json")
    code, out, err, _ = run(["status"])
    assert (code, out.strip(), err) == (0, "done", "")


def test_a_caller_capturing_output_does_not_wait_for_the_detached_process(on, tmp_path):
    pid_file = tmp_path / "pid"
    sleeper = [sys.executable, "-c", f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                                     "time.sleep(30)"]
    script = f"from wirk_cli import update; update.CHILD = {sleeper!r}; update.after('status')"
    began = time.monotonic()
    subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=5, check=True)
    assert time.monotonic() - began < 5
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.05)
    os.kill(int(pid_file.read_text()), signal.SIGTERM)


# The real uv and pip, offline: the release comes from the index the update names, never one the environment names


class Index:
    """A local package index that counts what it is asked and has nothing."""

    def __init__(self):
        hits = self.hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_error(404)

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/simple"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def indexes(monkeypatch, tmp_path):
    """(the update's index, an index every index setting of the environment names, a uv.toml naming it too)"""
    right, wrong = Index(), Index()
    monkeypatch.setattr(update, "INDEX", right.url)
    config = tmp_path / "uv.toml"
    config.write_text(f'[[index]]\nurl = "{wrong.url}"\ndefault = true\n')
    for name in INDEX_ENVIRONMENT:
        monkeypatch.setenv(name, str(config) if name == "UV_CONFIG_FILE" else wrong.url)
    user = tmp_path / "xdg" / "uv" / "uv.toml"  # a user's own uv settings, with an index that comes before the default
    user.parent.mkdir(parents=True)
    user.write_text(f'[[index]]\nurl = "{wrong.url}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("PIP_NO_CACHE_DIR", "1")
    yield right, wrong
    right.close()
    wrong.close()


@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv")
def test_the_real_uv_asks_only_the_updates_index(on, tmp_path, indexes):
    right, wrong = indexes
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    [(_, _, command, env)] = update.targets(prefix)
    shutil.rmtree(prefix)  # the fake environment has no Python for uv to check; a fresh install resolves the same way
    on.mkdir()
    assert not update.upgraded([part.format("9.9.9") for part in command], env, on)  # nothing to install offline
    assert right.hits and wrong.hits == []


@pytest.fixture(scope="module")
def venv(tmp_path_factory):
    """A real virtual environment with pip, holding a wirk to upgrade."""
    prefix = tmp_path_factory.mktemp("real") / "venv"
    if subprocess.run([sys.executable, "-m", "venv", str(prefix)], capture_output=True).returncode:
        pytest.skip("needs venv with pip")
    [site] = prefix.glob("lib/python*/site-packages")
    info = site / "wirk-0.4.21.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: wirk\nVersion: 0.4.21\n")
    return prefix


@pytest.mark.parametrize("where", ["environment", "PIP_CONFIG_FILE", "the environment's pip.conf"])
def test_the_real_pip_asks_only_the_updates_index(on, tmp_path, indexes, venv, monkeypatch, where):
    right, wrong = indexes
    config = tmp_path / "pip.conf" if where == "PIP_CONFIG_FILE" else venv / "pip.conf"
    if where != "environment":
        (tmp_path / "links").mkdir()
        config.write_text(f"[global]\nextra-index-url = {wrong.url}\nfind-links = {wrong.url}/links\n")
    if where == "PIP_CONFIG_FILE":
        monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    [(_, _, command, env)] = update.targets(venv)
    on.mkdir()
    try:
        assert not update.upgraded([part.format("9.9.9") for part in command], env, on)  # nothing to install offline
    finally:
        (venv / "pip.conf").unlink(missing_ok=True)
    assert right.hits and wrong.hits == []


def test_an_update_that_cannot_take_the_lock_runs_nothing(on, tmp_path, monkeypatch):
    import errno
    import fcntl

    def broken(*args):
        raise OSError(errno.ENOLCK, "no locks available")
    monkeypatch.setattr(fcntl, "flock", broken)
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    with pytest.raises(OSError):
        update.main(prefix, pypi())
    assert not (tmp_path / "pip.log").exists()


# The skill, refreshed in the same detached process


def skill(version=None, name="wirk"):
    return f"---\nname: {name}\ndescription: WIRK for agents.\n---\n\n# WIRK\n" + (f"\nWIRK skill {version}.\n" if version else "")


def github(tag="v0.4.29", text=None, sums=None):
    """PyPI as pypi() answers, plus the skill's releases: the latest tag by redirect, then that tag's two files."""
    text = skill(tag[1:]) if text is None else text
    sums = f"{hashlib.sha256(text.encode()).hexdigest()}  SKILL.md\n" if sums is None else sums
    asked = []

    def answer(request):
        path = request.url.path
        asked.append(path)
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"info": {"version": LATEST[path.split("/")[2]]}})
        if path == "/wirkspace/wirk-skill/releases/latest":
            return httpx.Response(302, headers={"Location": f"https://github.com/wirkspace/wirk-skill/releases/tag/{tag}"})
        files = {f"/wirkspace/wirk-skill/releases/download/{tag}/SKILL.md": text,
                 f"/wirkspace/wirk-skill/releases/download/{tag}/SHA256SUMS": sums}
        return httpx.Response(200, text=files[path]) if path in files else httpx.Response(404)
    transport = httpx.MockTransport(answer)
    transport.github = lambda: [path for path in asked if not path.startswith("/pypi/")]
    return transport


FOLDERS = (".claude/skills/wirk", ".agents/skills/wirk", ".codex/skills/wirk")


def copies(home, text, folders=FOLDERS[:2]):
    for folder in folders:
        (home / folder).mkdir(parents=True)
        (home / folder / "SKILL.md").write_text(text)
        (home / folder / "SKILL.md").chmod(0o600)
    return [home / folder / "SKILL.md" for folder in folders]


@pytest.fixture
def current(tmp_path):
    """An install already at PyPI's latest, so only the skill can change."""
    return install(tmp_path / "venv", {"wirk": "0.4.27"})


def test_a_newer_skill_release_replaces_each_installed_copy_and_creates_none(on, tmp_path, current):
    home = tmp_path / "home"
    paths = copies(home, skill("0.4.25"))
    transport = github()
    update.main(current, transport)
    assert [path.read_text() for path in paths] == [skill("0.4.29")] * 2
    assert [oct(path.stat().st_mode & 0o777) for path in paths] == ["0o600"] * 2
    assert not (home / ".codex").exists() and sorted(p.name for p in paths[0].parent.iterdir()) == ["SKILL.md"]
    assert transport.github() == ["/wirkspace/wirk-skill/releases/latest",
                                  "/wirkspace/wirk-skill/releases/download/v0.4.29/SKILL.md",
                                  "/wirkspace/wirk-skill/releases/download/v0.4.29/SHA256SUMS"]
    assert state(on).get("notice") is None


def test_codex_home_names_codexs_folder(on, tmp_path, current, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    [path] = copies(tmp_path, skill("0.4.25"), ["codex/skills/wirk"])
    update.main(current, github())
    assert path.read_text() == skill("0.4.29")


def test_a_copy_without_a_version_line_counts_as_older(on, tmp_path, current):
    [path] = copies(tmp_path / "home", skill(), FOLDERS[:1])
    update.main(current, github())
    assert path.read_text() == skill("0.4.29")


@pytest.mark.parametrize("installed", ["0.4.29", "0.4.30"])
def test_a_current_skill_asks_only_for_the_tag_and_downloads_nothing(on, tmp_path, current, installed):
    paths = copies(tmp_path / "home", skill(installed))
    transport = github()
    update.main(current, transport)
    assert transport.github() == ["/wirkspace/wirk-skill/releases/latest"]
    assert [path.read_text() for path in paths] == [skill(installed)] * 2


def test_without_an_installed_copy_github_is_never_asked(on, tmp_path, current):
    transport = github()
    update.main(current, transport)
    assert transport.github() == []


@pytest.mark.parametrize("release", [dict(sums="0" * 64 + "  SKILL.md\n"), dict(sums=""),
                                     dict(text=skill("0.4.28")), dict(text=skill("0.4.29", name="other"))],
                         ids=["wrong sum", "no sum", "line is not the tag", "not the wirk skill"])
def test_a_release_file_that_does_not_check_out_writes_nothing(on, tmp_path, current, release):
    paths = copies(tmp_path / "home", skill("0.4.25"))
    update.main(current, github(**release))
    assert [path.read_text() for path in paths] == [skill("0.4.25")] * 2
    assert state(on).get("notice") is None


def test_links_and_other_skills_are_left_alone(on, tmp_path, current):
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text(skill("0.4.25"))
    (home / ".claude" / "skills" / "wirk").mkdir(parents=True)
    (home / ".claude" / "skills" / "wirk" / "SKILL.md").symlink_to(checkout / "SKILL.md")
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "wirk").symlink_to(checkout)
    [other] = copies(home, skill("0.4.25", name="other"), FOLDERS[2:])
    transport = github()
    update.main(current, transport)
    assert (checkout / "SKILL.md").read_text() == skill("0.4.25") and other.read_text() == skill("0.4.25", "other")
    assert transport.github() == []


def test_a_copy_that_cannot_be_replaced_is_named_in_the_notice(on, tmp_path, current):
    locked_copy, fine = copies(tmp_path / "home", skill("0.4.25"))
    locked_copy.parent.chmod(0o500)
    try:
        update.main(current, github())
    finally:
        locked_copy.parent.chmod(0o700)
    assert locked_copy.read_text() == skill("0.4.25") and fine.read_text() == skill("0.4.29")
    assert state(on)["notice"] == (f"WIRK skill 0.4.29 is out; {locked_copy} is older. "
                                   "To update: curl -fsSL https://wirk.life/install | sh")


def test_an_unreachable_github_changes_nothing_and_the_next_check_retries(on, tmp_path, current):
    paths = copies(tmp_path / "home", skill("0.4.25"))

    def pypi_only(request):
        if request.url.host != "pypi.org":
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json={"info": {"version": LATEST[request.url.path.split("/")[2]]}})
    update.main(current, httpx.MockTransport(pypi_only))
    assert [path.read_text() for path in paths] == [skill("0.4.25")] * 2 and state(on).get("notice") is None
    update.main(current, github())
    assert [path.read_text() for path in paths] == [skill("0.4.29")] * 2


def test_an_old_wirk_that_could_not_be_upgraded_keeps_its_own_skill(on, tmp_path):
    prefix = install(tmp_path / "system", {"wirk": "0.4.21"})
    (prefix / "pyvenv.cfg").unlink()  # cannot be upgraded: a notice
    [path] = copies(tmp_path / "home", skill("0.4.25"), FOLDERS[:1])
    transport = github()
    update.main(prefix, transport)
    assert path.read_text() == skill("0.4.25") and transport.github() == []
    assert state(on)["notice"] == "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk==0.4.27"


def test_two_checks_under_one_home_each_write_their_own_temporary_file(on, tmp_path, monkeypatch):
    """Two configuration folders have two locks but share the skill: one check's file is never opened by the other."""
    [path] = copies(tmp_path / "home", skill("0.4.25"), FOLDERS[:1])
    real, other = os.fsync, []

    def fsync(descriptor):  # the first check has written its file; the second runs to the end before it renames
        real(descriptor)
        if not other:
            other.append(None)
            other.append(update.refresh_skill(github()))
    monkeypatch.setattr(update.os, "fsync", fsync)
    assert update.refresh_skill(github()) == [] and other[1] == []
    assert path.read_text() == skill("0.4.29") and [p.name for p in path.parent.iterdir()] == ["SKILL.md"]
