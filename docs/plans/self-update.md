# Plan: wirk keeps itself current

Status: plan, revision 4 (after adversarial review, a recheck and the code review's fix round; matches the build). Branch `self-update` in wirk-cli only.

## Outcome

Direction, 21 October 2026: the wirk command "should just update every time", with no consent, prompt or effort;
nobody needs a stable version. Today an install falls behind each release until someone reruns the installer. After
this change, any `wirk` installed with `uv tool`, `pipx` or `pip` in a virtual environment reaches the latest PyPI
release within about an hour of its next use, and the answer of the command that triggered it is never delayed,
changed or interrupted.

## Behaviour

- After every command except `wirk login` (help, `--version` and usage errors included), once its answer is printed
  and its exit code decided, `wirk` reads `update.json` in its configuration directory (`~/.config/wirk` or
  `$WIRK_CONFIG_DIR`). When the last check is an hour or more away from now (a stamp in the future counts as stale)
  or there is none, it writes the new stamp and starts a detached process (`python -I -m wirk_cli.update`, new
  session, working directory the configuration directory, no inherited stdin, stdout or stderr) and exits. The
  command itself never touches the network for this: its cost is one small file read and write and one process
  start. Nothing happens on Windows (`os.name != "posix"`).
- The detached process takes the lock on `update.lock` in the configuration directory, waiting for an update that
  is still running, so two upgrades of one environment never run together and no hour's check is lost. For each target below it asks
  `https://pypi.org/pypi/NAME/json` for `info.version`, compares it with that target's installed version as integer
  tuples, and when PyPI's is newer runs the target's upgrade to exactly that version:

  | How this wirk was installed (from `sys.prefix`) | Target and upgrade |
  | --- | --- |
  | `uv tool` (`sys.prefix/uv-receipt.toml` exists) | `uv tool install --no-config --default-index https://pypi.org/simple [--python P] NAME==X`, `P` the receipt's Python when it names one, with `UV_TOOL_DIR` the parent of `sys.prefix` and `UV_TOOL_BIN_DIR` the folder of the receipt's first entry point (without it uv moves the command to its default folder; observed) |
  | the same, with a sibling `wirk-mcp` tool in that tool directory (the installer's layout) | a second, independent target: the same for `wirk-mcp==M` with that tool's own receipt, unless it is editable |
  | any other virtual environment (`pyvenv.cfg` in `sys.prefix`), `pipx`'s included | `python -I -m pip install --isolated --quiet --disable-pip-version-check --index-url https://pypi.org/simple NAME==X` with its interpreter; an unwritable one, or one without pip, fails and gets the notice |
  | anything else: no virtual environment, no `uv` for a uv tool | none; see the notice |

  `NAME` is `wirk-mcp` whenever wirk-mcp is installed in this environment, otherwise `wirk`. wirk-mcp pins `wirk==`
  its own version, so upgrading it to PyPI's latest wirk-mcp brings its matching wirk and never breaks the pair; its
  version is compared with PyPI's wirk-mcp, which can lag wirk's. Each target is checked and upgraded on its own, so
  a sibling wirk-mcp catches up even when wirk is current. `uv` is found on `PATH`, then in `~/.local/bin`, where uv's
  own installer puts it.
- The release always comes from pypi.org: an unattended upgrade inherits whatever a repository's direnv or `.env`
  exported, so uv runs without configuration files and without the `UV_INDEX*`, `UV_DEFAULT_INDEX`,
  `UV_EXTRA_INDEX_URL`, `UV_FIND_LINKS` and `UV_CONFIG_FILE` settings, and pip runs `--isolated`. The rest of
  uv's settings (its cache, its Python folder) stay. A uv tool's `--with` packages are not carried over: in the
  installer's layout that is the old wirk wheel, which the new wirk-mcp's pin replaces.
- The command's process takes the same lock briefly around its own read and write of `update.json` (and skips
  everything while an update holds it), so the stamp and the notice never overwrite each other.
- When an upgrade exits non-zero or times out (600 s), or a newer release exists and no upgrade can run here, the process records one
  notice line in `update.json` (written to a temporary file and renamed); finding every target current, or a later
  success, clears it. A brief network failure during an upgrade can therefore cause one notice, cleared by the next
  hourly success; accepted, because a lasting failure must not stay silent.
- A recorded notice is printed on standard error at most once in 24 hours, after a command's answer:
  `wirk 0.4.27 is out; this is 0.4.21. To update: uv tool install wirk==0.4.27`, naming the package: the
  documented install, which works wherever uv does, including where pip cannot. Standard output, and so `--json`, is
  never touched.
- Silent, with nothing recorded: PyPI unreachable, slow (10 s timeout) or answering something unexpected; a version
  that is not plain `N.N.N`; an installed version equal to or newer than PyPI's; an editable install of wirk or
  wirk-mcp (a developer's checkout is never replaced); any error reading or writing `update.json`.
- Never: during `wirk login` (no check, no notice); when `WIRK_NO_UPDATE` is non-empty, for CI and tests. `wirk
  --help` has no environment section, so it is documented nowhere new.
- A release that changes how agents sign in (as 0.4.25 did) can answer the next call after a silent upgrade with a
  one-time `actor_setup_required` and its `wirk login` hint, as a manual upgrade would.

## Design choices, against the brief

1. The PyPI request runs in the detached process, not after the command: agents wait for the process to exit, so
   a check after the answer would still delay them by a round trip, or by the timeout when offline.
2. pipx needs no branch of its own: pip runs in its environments through pipx's shared libraries.
3. The installer puts wirk and wirk-mcp in separate uv tool environments, so "wirk-mcp in the same environment"
   never matches the standard install; without the sibling target the MCP server would never update. It upgrades
   wirk-mcp's own environment, which holds its own pinned wirk, and never touches wirk's. uv replaces files in place,
   so a running MCP server keeps the modules it has imported (nothing on its paths imports lazily but httpx); new
   chats start the new one.
4. The detached process runs in the configuration directory and starts pip with `-I`, so a repository's `pip/`
   folder or `uv.toml` in the caller's working directory cannot steer the upgrade (both observed in review). The
   reviewer saw `uv tool install` ignore a planted `uv.toml`; running elsewhere makes that moot.
5. No retry bookkeeping and no log file: the process waits for the upgrade's exit status itself, and the notice
   carries the command, whose own output says what failed.
6. Provenance: the installer checks release wheels against the GitHub release's SHA-256 sums; updates come from
   PyPI over HTTPS through uv or pip, which check PyPI's published hashes, and PyPI is fed by trusted publishing from
   the same release workflow. After the first update the uv receipt names `wirk==X` instead of the kept wheel.
7. Known limits: a machine whose agents use only the MCP server never runs `wirk`, so it never checks; a pip upgrade
   killed halfway (a sandbox tearing down its processes) can leave a pip environment broken, as a manual one would;
   installs that share one configuration directory share the stamp and the notice.
8. Out of scope: hand-made layouts outside these three, uv virtual environments without pip (they get the notice), Windows,
   and the skill.

## Change

- `src/wirk_cli/update.py` (new, about 100 lines): `after(command)` in the command's process; `main()` in the
  detached one, built from `targets(prefix)` (the table, as `(name, installed, command, environment)` with `command`
  holding `{}` for the version) and `latest(name, transport)`. The detached argv is a module constant, so a test can
  replace it.
- `src/wirk_cli/cli.py`: today's `main` body becomes `run(argv, transport)`; `main` calls it, then
  `update.after(command)` inside `try/except Exception`, and returns the code.
- `tests/conftest.py`: `WIRK_NO_UPDATE=1` for the whole suite. `scripts/release_assets.py`: the same in the release
  qualification's clean environment, so a qualifying `wirk --help` starts nothing.
- `README.md`: the install section says wirk keeps itself current; "What leaves your machine" names the hourly
  request to pypi.org (package names only); "Releasing" no longer says publication leaves installs behind; one
  "Next release" line.

## Acceptance conditions

1. uv tool install older than PyPI's latest (a fake `uv` found the installer's way and a fake PyPI through
   `httpx.MockTransport`): the fake uv was called with `tool install wirk==LATEST`, this install's tool directory
   and bin directory, and the configuration directory as working directory; with a sibling `wirk-mcp` tool behind
   its PyPI latest, also `tool install wirk-mcp==LATEST_MCP`, even when wirk itself is current.
2. Virtual environment with wirk-mcp in it: the upgrade installs `wirk-mcp==LATEST_MCP` with `-I -m pip` and never
   `wirk==` anything; without wirk-mcp it installs `wirk==LATEST`; a failing pip records the notice with its command.
   While another process holds the lock, nothing runs. Every upgrade has a 600-second timeout, so a hung one cannot
   hold the lock.
3. Installed version equal to or newer than PyPI's, an editable install, a PyPI timeout, a 500, or malformed JSON:
   no upgrade runs and no notice is recorded.
4. The upgrade fails, or none can run here: one notice is recorded; the next command prints it once on stderr; a
   second command within 24 hours prints nothing; stdout and the exit code are as without the update.
5. A second command within the hour starts no process; one after the hour does. `wirk login` and
   `WIRK_NO_UPDATE=1` start nothing and print nothing. A corrupt `update.json` changes nothing about the answer.
6. The process is started detached: new session, no inherited output, so a caller capturing output returns
   without waiting for it (one real subprocess test, the detached argv replaced by a 30-second sleep, returning
   within 5 seconds). The real detached `wirk_cli.update` never runs in tests.
7. The suite makes no network request; CI and release qualification run with `WIRK_NO_UPDATE=1`.
8. Verified by hand in scratch directories, against the real PyPI: a uv tool install of a branch wheel numbered
   below the release, with the installer's sibling wirk-mcp, reaches the release for both after one command; and a
   pip virtual environment with wirk-mcp the same; a wirk-mcp server started before the upgrade still answers a
   tool call after it.
