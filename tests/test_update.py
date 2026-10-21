"""wirk keeps itself current: the hourly check after a command, and the detached upgrade it starts."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import httpx
import pytest

from wirk_cli import update

LATEST = {"wirk": "0.4.27", "wirk-mcp": "0.4.26"}


@pytest.fixture
def on(monkeypatch, tmp_path):
    """Updates turned on, with their own configuration directory."""
    monkeypatch.delenv("WIRK_NO_UPDATE", raising=False)
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


def install(prefix, versions, *, uv_tool=False, bin_dir=None, editable=()):
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
            entrypoints = [{{ name = "{name}", install-path = "{bin_dir}/{name}", from = "{name}" }}]
            """))
    return prefix


def recorder(path, log, exit_code=0):
    """A fake executable that records its arguments, working directory and uv's tool folders, then exits."""
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf '%s|%s|%s|%s\\n' "$*" "$PWD" "${{UV_TOOL_DIR:-}}" "${{UV_TOOL_BIN_DIR:-}}" >> {log}
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
        ["tool install wirk==0.4.27", str(on), str(tools), str(bin_dir)],
        ["tool install wirk-mcp==0.4.26", str(on), str(tools), str(bin_dir)]]
    assert state(on).get("notice") is None


def test_the_sibling_mcp_tool_catches_up_when_wirk_is_current(on, tmp_path, fake_uv):
    tools = tmp_path / "tools"
    prefix = install(tools / "wirk", {"wirk": "0.4.27"}, uv_tool=True, bin_dir=tmp_path / "bin")
    install(tools / "wirk-mcp", {"wirk-mcp": "0.4.24", "wirk": "0.4.24"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    assert [call[0] for call in calls(fake_uv)] == ["tool install wirk-mcp==0.4.26"]


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
    assert [call[0] for call in calls(log)] == ["tool install wirk==0.4.27"]


def test_a_uv_tool_install_without_uv_records_the_notice(on, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    prefix = install(tmp_path / "tools" / "wirk", {"wirk": "0.4.21"}, uv_tool=True, bin_dir=tmp_path / "bin")
    update.main(prefix, pypi())
    assert state(on)["notice"] == "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk"


def test_a_venv_with_wirk_mcp_upgrades_wirk_mcp_never_wirk_alone(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21", "wirk-mcp": "0.4.21"})
    log = tmp_path / "pip.log"
    recorder(prefix / "bin" / "python", log)
    update.main(prefix, pypi())
    assert calls(log) == [["-I -m pip install --quiet wirk-mcp==0.4.26", str(on), "", ""]]


def test_a_venv_without_wirk_mcp_upgrades_wirk(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    log = tmp_path / "pip.log"
    recorder(prefix / "bin" / "python", log)
    update.main(prefix, pypi())
    assert [call[0] for call in calls(log)] == ["-I -m pip install --quiet wirk==0.4.27"]


def test_a_failed_upgrade_records_the_command_to_run(on, tmp_path):
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log", exit_code=1)
    update.main(prefix, pypi())
    assert state(on)["notice"] == (f"wirk 0.4.27 is out; this is 0.4.21. To update: "
                                   f"{prefix}/bin/python -I -m pip install --quiet wirk==0.4.27")


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
    assert state(on)["notice"].endswith("To update: uv tool install wirk")


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


def test_nothing_runs_while_another_update_holds_the_lock(on, tmp_path):
    import fcntl
    on.mkdir()
    holder = os.open(on / "update.lock", os.O_WRONLY | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)
    prefix = install(tmp_path / "venv", {"wirk": "0.4.21"})
    recorder(prefix / "bin" / "python", tmp_path / "pip.log")
    transport = pypi()
    update.main(prefix, transport)
    os.close(holder)
    assert not (tmp_path / "pip.log").exists() and transport.asked == []


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
    notice = "wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk"
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
