# Plan: wirk keeps itself current

Status: plan, revision 1. Branch `self-update` in wirk-cli only.

## Outcome

Direction, 21 October 2026: the wirk command "should just update every time", with no consent, prompt or effort;
nobody needs a stable version. Today an install falls behind each release until someone reruns the installer. After
this change, any `wirk` installed with `uv tool`, `pipx` or `pip` in a virtual environment reaches the latest PyPI
release within about an hour of its next use, and the answer of the command that triggered it is never delayed,
changed or interrupted.

## Behaviour

- After every command except `wirk login` (its answer printed, its exit code decided), `wirk` reads
  `update.json` in its configuration directory (`~/.config/wirk` or `$WIRK_CONFIG_DIR`). When the last check is an
  hour old or more (or there is none), it writes the new check time and starts a detached process
  (`python -I -m wirk_cli.update`, new session, no inherited stdin, stdout or stderr) and exits at once. The
  command itself never touches the network for this, so its wall time grows by one small file read and write and one
  process start.
- The detached process asks `https://pypi.org/pypi/NAME/json` for `info.version`, compares it with the installed
  version as integer tuples, and when PyPI's is newer runs the matching upgrade to exactly that version:

  | How this wirk was installed (detected from `sys.prefix`) | Upgrade it runs |
  | --- | --- |
  | `uv tool` (an `uv-receipt.toml` beside the interpreter) | `uv tool install NAME==X`, `NAME` from the receipt, with `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` set from where this tool lives |
  | that, and a sibling `wirk-mcp` tool in the same tool directory (the installer's layout) | also `uv tool install wirk-mcp==M`, `M` PyPI's latest wirk-mcp |
  | any other writable virtual environment, `pipx`'s included | `python -m pip install NAME==X` with this interpreter; without pip, `uv pip install --python THIS NAME==X` |
  | anything else: no virtual environment, an unwritable one, no pip and no uv | nothing; see the notice |

  `NAME` is `wirk-mcp` whenever wirk-mcp is installed in the same environment, never `wirk` alone there: wirk-mcp
  pins `wirk==` its own version, so upgrading wirk-mcp to PyPI's latest brings its matching wirk and never breaks the
  pair. Its version is compared with PyPI's latest wirk-mcp, which can lag wirk's.
- The upgrade's output goes to `update.log` in the configuration directory, replaced each time. When it exits
  non-zero, or no upgrade can run here, the process records one notice line in `update.json`; a later success, or
  finding the install current, clears it.
- A recorded notice is printed on standard error at most once in 24 hours, after a command's answer: `wirk 0.4.27 is
  out; this is 0.4.21. To update: uv tool install wirk==0.4.27` (the exact command that failed, or
  `uv tool install wirk` when none can run here). Standard output, and so `--json`, is never touched.
- Silent, with nothing recorded: PyPI unreachable, slow (10 s timeout), or answering something unexpected; a version
  that is not plain `N.N.N`; an installed version equal to or newer than PyPI's; an editable install of wirk or
  wirk-mcp (a developer's checkout is never replaced); any error reading or writing `update.json`.
- Never: during `wirk login` (no check, no notice); when `WIRK_NO_UPDATE` is set to anything non-empty, for CI and
  tests. `wirk --help` has no environment section, so it is documented nowhere new.
- Auth: a release that changes how agents sign in (as 0.4.25 did) can, after a silent upgrade, answer the next call
  with a one-time `actor_setup_required` and its `wirk login` hint, exactly as a manual upgrade would. Nothing new
  is needed for that.
- Several agents on one machine share the configuration directory, so the stamp written before the process starts
  makes the others skip for the hour. Two commands that finish in the same few milliseconds can both start one;
  `uv tool install` and `pip install` to the same exact version are then both no-ops for the second. Accepted.

## Design choices, against the brief

1. The PyPI request is in the detached process, not after the command: agents wait for the process to exit, so a
   check after the answer would still delay them by a network round trip, up to the timeout when offline.
2. pipx needs no branch of its own: its environments run pip through its shared libraries, and an exact-version
   pip install keeps injected packages. `pipx list` keeps showing the version pipx last installed until pipx
   touches it again; cosmetic.
3. The installer puts wirk and wirk-mcp in separate uv tool environments, so "wirk-mcp in the same environment"
   never matches the standard install, and without the sibling rule the MCP server would never update. The sibling
   rule upgrades wirk-mcp's own environment, which holds its own pinned wirk; it never touches wirk's. A running MCP
   server keeps the modules it already imported; new chats start the new one.
4. No retry bookkeeping: the detached process waits for the upgrade's exit status itself and records the notice.
5. Provenance: the installer checks release wheels against the GitHub release's SHA-256 sums; self-updates come
   from PyPI over HTTPS through uv or pip, which check PyPI's published hashes, and PyPI is fed by trusted
   publishing from the same release workflow. After the first update the uv receipt names `wirk==X` from PyPI
   instead of the kept wheel.
6. Out of scope: the Mac mini's runtime layout and Windows (the install is documented for macOS and Linux; the
   detached start is POSIX). Updating the skill.

## Change

- `src/wirk_cli/update.py` (new, about 100 lines): `after(command)` for the command's process; `main()` for the
  detached one, with `targets()` (the table above) and `latest(name)`.
- `src/wirk_cli/cli.py`: `main()` calls `update.after(command)` once its answer is printed, never raising.
- `tests/conftest.py`: `WIRK_NO_UPDATE=1` for the whole suite. `scripts/release_assets.py`: the same in the release
  qualification's clean environment, so a qualifying `wirk --help` starts nothing.
- `README.md`: the install section says wirk keeps itself current; "What leaves your machine" names the hourly
  request to pypi.org (package name only); "Releasing" no longer says publication leaves installs behind; one
  "Next release" line.

## Acceptance conditions

1. uv tool install older than PyPI's latest (a fake `uv` on `PATH` and a fake PyPI): after `wirk status` the fake
   uv was called with `tool install wirk==LATEST` and the tool directory and bin directory of this install; with a
   sibling `wirk-mcp` tool, also `tool install wirk-mcp==LATEST_MCP`.
2. Virtual environment with wirk-mcp in it: the upgrade installs `wirk-mcp==LATEST_MCP` and never `wirk==` anything;
   without wirk-mcp it installs `wirk==LATEST`; without pip it uses `uv pip install --python`.
3. Installed version equal to or newer than PyPI's, an editable install, a PyPI timeout, a 500, or malformed JSON:
   no upgrade runs and no notice is recorded.
4. The upgrade fails, or none can run here: one notice is recorded; the next command prints it once on stderr; a
   second command within 24 hours prints nothing; stdout and the exit code are as without the update.
5. A second command within the hour starts no process; one after the hour does. `wirk login` and
   `WIRK_NO_UPDATE=1` start nothing and print nothing. A corrupt `update.json` changes nothing about the answer.
6. The process is started detached: new session, no inherited output, so `subprocess.run(["wirk", ...],
   capture_output=True)` returns without waiting for it (one real subprocess test).
7. The suite makes no network request; CI and release qualification run with `WIRK_NO_UPDATE=1`.
8. Verified by hand in scratch directories, against the real PyPI: a uv tool install of a branch wheel numbered
   below the release, with the installer's sibling wirk-mcp, reaches the release for both after one command; and a
   pip virtual environment with wirk-mcp the same.
