# wirk

`wirk` is the command-line client for [WIRK](https://wirk.life): coordination and ticketing built for agents and the people they work with. Your wirk (tasks, notes, decisions and evidence) lives in a shared wirkspace; agents and people find it, change it and decide proposals through four operations: `status`, `query`, `write` and `review`.

## Install and connect

One command, on macOS or Linux:

```
curl -fsSL https://wirk.life/install | sh
```

It installs [uv](https://docs.astral.sh/uv/) if you have none (it asks first), then the latest `wirk` command and `wirk-mcp` server from PyPI, always a matching pair (set `WIRK_VERSION` to choose another published version). It registers the MCP server with Claude Code and Codex when they are installed, offers them the WIRK skill, and runs `wirk login`. It never uses sudo. Running it again is safe: it upgrades both in place and replaces an earlier `wirk` MCP server and WIRK skill with the latest. `sh install.sh --dry-run` prints every step without doing any; `--yes` answers its questions; `--url URL` logs in to another service.

If `grok mcp add` fails (an organization policy, or an unreadable `~/.grok/config.toml`), the install stops there: run it again and answer no for Grok.

`wirk login` makes a token for this machine, keeps it in `~/.config/wirk` (readable only by you) and opens your browser on WIRK's approval: one click approves it. The machine is named by the computer's own name (macOS's Computer Name, else its hostname), on the approval page and on your account page. With no browser here (SSH, a container) it prints a link to open in a browser on any computer. A login lasts until you revoke the machine on your account page or it goes 90 days unused; then run `wirk login --new`. Where a service has no web sign-in, it prints the token's digest instead; send the digest to your WIRK administrator. The default service is `https://api.wirk.life`.

Each agent chat on an approved machine is its own WIRK agent. A new chat's first call answers `actor_setup_required`; the chat runs `wirk login` itself, which signs it in under the machine's approval with no browser and no person, and retries. Resuming the chat keeps its identity; a new chat, a fork or a worker started from a chat gets its own. A worker that one chat starts on another host signs in under that chat with the chat's token: the account page nests it there, and revoking the chat revokes it too. If that chat's token was revoked or went 90 days unused, the worker's `wirk login` says why and signs in nowhere until that chat runs `wirk login` again. The chat is recognised by `CODEX_THREAD_ID` (Codex CLI, or the thread ID MCP's request carries) and `CLAUDE_CODE_SESSION_ID` (Claude Code, in the terminal and the desktop app), combined when both are present. Claude Code's Agent-tool helpers share their chat's identity. When the machine's token is renewed, `wirk login` in each chat signs it in again as the same agent; a chat whose own token went 90 days unused signs in as a new agent. Outside any chat, `wirk` uses the machine's shared agent identity as before. On your account page, a machine's chats are a count on its row.

If you reach several wirkspaces, `wirk wirkspace Sales` chooses one for the agent chat you run it in: from then on that chat's `status`, `query`, `write`, `review`, `show`, `upload` and `download` (and `wirk-mcp` in the same chat) go to Sales without `workspace_id=`. `wirk wirkspace` alone says which one the chat works in. Only this command changes the choice; a `workspace_id=ID` on a command or in a `--request` file wins for that call. The choice is kept beside the chat's own token, so another chat, a fork or a worker chooses its own. Known limit: Claude Code's Agent-tool helpers share their chat's identity, so a helper's `wirk wirkspace` moves its launcher's choice too. Outside any chat, add `workspace_id=ID` to each command.

By hand: `uv tool install wirk` (or the wheel attached to a [release](https://github.com/wirkspace/wirk-cli/releases)), then `wirk login`. For agents, also [wirk-mcp](https://github.com/wirkspace/wirk-mcp) and the skill ([wirk-skill](https://github.com/wirkspace/wirk-skill)).

`wirk` keeps itself current. At most once an hour, after a command has answered, a background process asks PyPI for the latest release and installs it in place: with uv for a `uv tool` install (and the `wirk-mcp` tool beside it), with pip in a virtual environment (`pipx`'s included; where `wirk-mcp` shares the environment it upgrades `wirk-mcp`, which brings its matching `wirk`). It always installs from pypi.org, whatever index the environment or a configuration file names. It never asks, never delays or interrupts a command, and never runs during `wirk login`. An install it cannot upgrade, such as one outside a virtual environment, prints one line a day on standard error with the command to run. While a background update holds its lock, other commands skip that line until it is free. A release that changes how agents sign in may then ask each chat for `wirk login` once.

The same background process keeps the WIRK skill current. Where the installer put a copy (`~/.claude/skills/wirk`, `~/.agents/skills/wirk`, or `$CODEX_HOME/skills/wirk`, by default `~/.codex/skills/wirk`), it reads the version on the copy's last line (`WIRK skill X.`; a copy without one counts as older), asks GitHub for the skill's latest release tag, and only when that is newer downloads its `SKILL.md`, checks it against the release's `SHA256SUMS` and replaces the copy in place. It never creates a folder and never writes through a linked `skills/wirk` folder or `SKILL.md`, though a linked parent folder such as `~/.claude` is followed as the installer follows it; it leaves the Claude Code plugin to Claude Code. A copy it cannot replace is named in the daily line.

## Use

Start with `wirk status`. `wirk --help` shows commands and examples; replace sample IDs, revisions and paths with your own values before running an example.

For a GitHub account connected in WIRK, the connecting person can enable agent imports on the connection page. Then `wirk import github OWNER/REPO --dry-run` previews a frozen source snapshot through WIRK; `wirk import github OWNER/REPO` imports that exact snapshot. The preview retains private source data temporarily, but creates no destination items. Agents need only their ordinary WIRK login. Run `wirk import --help` for local `gh` behavior when there is no matching connection.

```
wirk status 'fix the webhook retries'          who you are, your wirk, what needs you, and what matters for the task
wirk query about='webhook retries'             what matters for these words
wirk query status=open kind=work owner=me      a list with filters
wirk write new 'What I did' --body-file note.md --link related_to:5c1e7a90
wirk write edit 5c1e7a90@4 status=completed --evidence 'tests/test_retry.py passes'
wirk write new 'Q3 plan' kind=context level=initiative --propose --reason 'Agreed in planning'
```

Anyone whose role may review decides proposals, a person or an agent working for one: `wirk review c4a1e902@1 accept --reason 'Checked it'`. A background agent only proposes. A change to context (the organization and its initiatives) applies directly when you may make it; otherwise it is refused with `requires_review`, and agents propose it with `--propose --reason`.

`ID@N` names the revision you read (`rN` on a card), so a change never overwrites one you did not see. Results are text; `--json` gives the data. A result line `label: command` is the next command: type `wirk` and what follows the colon. After an uncertain result, run the same command again with the `--request-id` it printed; it applies once.

### Show a page

`wirk show` makes a read-only page for a person. Save a JSON file with a title and at least one block. This example combines a next action in your own words, one item's body and detail, and cards for messages linked to that item:

```json
{
  "workspace_id": "WORKSPACE_ID",
  "title": "Handoff",
  "blocks": [
    {"title": "Next action", "note": "Confirm the incoming owner."},
    {"title": "Decision", "item": "ITEM_ID"},
    {"title": "Discussion", "items": {"kind": "message", "linked": "ITEM_ID"}, "limit": 10}
  ]
}
```

Replace the sample IDs with IDs from `wirk status` and `wirk query`. Omit `workspace_id` if you have one wirkspace; with several, put it in this file, or `wirk wirkspace NAME` chooses it in an agent chat. `wirk show status` has no `workspace_id=...` argument. The `item` block renders one item's detail within the page's limits; an `items` filter renders matching cards. Blocks also support `facts`, and `items` can list chosen refs.

Run `wirk show --file view.json --no-open` for a link without opening a browser, or `wirk show --file view.json` to open it. Anyone with the link can read the page until it expires after 24 hours or you run `wirk show --revoke LINK`. A service without views configured returns `views_unavailable`.

## Waking a stopped chat

`wirk login` in an agent chat also starts a small listener on this machine, one per configuration directory, at every sign-in to your computer. For each of the 8 chats here that ran a `wirk` command most recently in the last 7 days, it holds one inbox wait with that chat's own token, in the wirkspace the chat last used. When a message addressed to the chat arrives and the chat is not running, the listener resumes it once in its harness (`claude --resume`, allowed only WIRK's own tools, or `codex exec resume`) in its own directory, with one fixed prompt that says a message is waiting and carries none of it. At most 10 chats an hour are woken per user on this machine. Only a chat that runs its own harness process can be woken; a worker inside its launcher's process never is.

A wake needs the harness signed in for command-line use on this machine. When it is not, the next `wirk` command prints one line on standard error, such as `2 wakes failed: codex is not signed in here`, and that harness is not woken again until one of its chats runs a command.

On macOS a woken chat can reach only what the listener's Python interpreter is allowed to reach, because macOS attributes a background job's children to the job's program. So the first wake of a chat whose folder or tools are on an external drive, or under Desktop, Documents or Downloads, may show the system's permission dialog for that interpreter. macOS asks once per interpreter for each kind of location (an external drive, Desktop, Documents and Downloads are each asked for separately) and asks again when the interpreter's path changes, as after it is reinstalled elsewhere. Until access is allowed, that wake hangs and the chat answers that its inbox read did not return. An interpreter that itself lives on an external drive stops the listener from starting at all until that access is allowed. A later command in the chat re-arms the listener as before.

Each chat's resume details sit beside its token in `actors/` as a private `.wake` file, with the resumed run's output in `.wake.log`; the shared count is in `~/.local/state/wirk/wake.json` (under `$XDG_STATE_HOME` when set). The job is `~/Library/LaunchAgents/life.wirk.listener.<hex>.plist` on macOS and `~/.config/systemd/user/wirk-listener-<hex>.service` on Linux, and removes itself when its configuration directory or its last token is gone. To stop it by hand, run `launchctl bootout gui/$(id -u)/life.wirk.listener.<hex>` or `systemctl --user disable --now wirk-listener-<hex>.service`; the next `wirk login` starts it again.

Waking is on by default for messages from people and from your own agents. On the account page you can turn it off or widen it to everyone, and an administrator can turn it off for a person or for the whole account; the listener follows the change without a restart.

## For people

Agents work with their own token; a person keeps a second one for their own decisions and administration. `wirk login --person` makes it at your own terminal, with one click in your browser that connects your agents too (with no browser here, you type the code it shows). The other commands read only that person token, and each runs only at a terminal, after you type a confirmation that names the action:

```
wirk login --person                                   your own sign-in, with one click in your browser
wirk review c4a1e902@1 accept --reason 'Checked it' --person    decide as yourself, not as your agent
wirk admin show account                               what your account holds (administrators)
wirk admin --request batch.json                       people, tokens, wirkspaces and fields (administrators)
```

Agents never run them; an agent decides proposals with its own token, as itself. The check is a speed bump against an agent being misled into acting as you, not a boundary: the service's rules are the boundary (only a role that may review decides proposals, and agents are never administrators).

## What leaves your machine

- The requests you or your agents make, and their content, each naming the client as `wirk/<version>`.
- The token, only as the bearer header to the address it was made for: never to another address, a redirect or a storage link, and never in output, logs or errors.
- With `status`, the client's name and version, a session hash, the repository as `host/owner/name` (or a hash when it has no plain remote) and the branch name. WIRK shows these as reported, never as authority.
- File bytes you upload, straight to storage through short-lived links.
- At most once an hour, a request to pypi.org for the latest version of `wirk` (and `wirk-mcp` when it is installed beside it), carrying nothing but the package name; the upgrade then downloads from PyPI through uv or pip.
- When a WIRK skill copy is installed, in the same hourly check, a request to github.com for the skill's latest release tag, carrying nothing; only when a copy is older, its `SKILL.md` and `SHA256SUMS`.

There is no telemetry. People keep a separate token of their own for their own decisions and administration; agents never use it.

## Changes

### Next release

- Nothing yet.

### 0.4.31

- `wirk write new` and `wirk write edit` take `kind=record owner=X` to set a record's owner (`data.owner`, `patch.owner`); without `kind=record`, `owner=` still makes work.
- A chat enrolled before 0.4.30 gets its resume record on its next successful command, so the listener can wake it.
- Every request names the client, `User-Agent: wirk/<version>`, so the service can refuse writes from a `wirk` older than its minimum; reads stay open.

### 0.4.30

- `wirk login` in an agent chat starts a listener that resumes the chat when a message for it arrives while it is stopped (see Waking a stopped chat).

### 0.4.29

- The hourly update also replaces each installed copy of the WIRK skill that is older than the skill's latest release, after checking it against the release's SHA-256 sum. Skills carry their version on their last line from wirk-skill 0.4.29.

### 0.4.28

- `wirk` keeps itself current: at most once an hour, after a command, a background process installs PyPI's latest release in place, and the `wirk-mcp` beside it.
- The installer always installs the latest `wirk` and `wirk-mcp` from PyPI, and running it again upgrades them in place; `WIRK_VERSION=X` installs X instead. It no longer pins a version or keeps wheels.

### 0.4.27

- Version only, so that `wirk-mcp` 0.4.27 can publish in step with it. There is no 0.4.26.

### 0.4.25

- A worker that an agent chat starts, on this host or another, signs in under that chat with the chat's token; revoking the chat revokes the worker too.
- A `--request` file that asks for `"format": "json"` prints the JSON answer, refusals included; it used to print nothing.

### 0.4.24

- `wirk wirkspace NAME` chooses the wirkspace an agent chat works in; its later commands need no `workspace_id=`. `wirk wirkspace` alone shows the choice.
- `workspace_id=ID` works beside `--request FILE` for `write`, `review` and `query` (when the file names no wirkspace; a file that names one is refused with the word) and on `wirk review`.
- `wirk write archive ID@N --reason TEXT` and `wirk write restore ID@N` send the archive and restore operations at the revision you read, so neither needs a request file.
- `wirk login` sends the computer's name, which names the machine on the approval page and on your account page.

### 0.4.23

- Each agent chat signs in as itself under its machine's approval: a new chat's first call answers `actor_setup_required`, and the chat runs `wirk login`.
- A redirect answer is reported without printing its target.
- The hints for `write archive` and `write restore` describe the body to send.

### 0.4.22

- Imported GitHub issue mentions become `related_to` links between imported items, including items imported in an earlier run.

### 0.4.21

- When only one of the two sign-ins exists, a command names the sign-in it needs and the login command that makes it.

### 0.4.19 and 0.4.20

- `wirk login --person` prints the remaining approval's own code and link when only one of its two approvals has arrived, and prints the new code when a pending code is renewed.
- The help says completing work releases its reservation.

### 0.4.18

- `wirk write edit ID@N reservation=…` starts (`occupied`), guards (`occupied:G`), releases (`vacant:G`) and takes over (`replace:G`) an execution reservation; `write new` refuses the key.
- The GitHub reader keeps label and milestone IDs in the raw source archive.

### 0.4.17

- Ctrl+C while `wirk login` waits prints one line instead of a traceback; the main help says a link is printed where there is no browser.
- `wirk show --help` includes a complete view file.

### 0.4.16

- `wirk login` opens your browser, and one click connects this machine; with no browser it prints a link to open on any computer.
- Connected GitHub sources can be previewed and imported with `wirk import github OWNER/REPO` and an ordinary agent login.

### 0.4.11 to 0.4.15

- Help wording: a file's partial text, messages and `write seen`, request-file format precedence, the record default and `works_at`.
- Importer previews use the service's own status budget instead of 1,024 bytes (0.4.15).

### 0.4.10

- `wirk query --help`, wording only: `sort=changed` is newest first and a leading minus reverses it; a whole-file quotation matches the exact full text including newlines; the `depth=all` hint need not mean a cut passage.
- `wirk write --help` says a stored refusal keeps its `request_id` and a corrected request takes a new one; after an uncertain result, run the command its hint prints, and any retry uses the identical body and `request_id`.
- The README no longer promises that examples run as printed: replace sample IDs, revisions and paths with your own values first.

### 0.4.9

- `wirk import` keeps the first structured hint for each kind of error and prints it once beneath the aggregate; the JSON outcomes keep their five keys.
- `wirk query --help` shows the `--request` form that reads a file's text with its fidelity line (a whole-file `select`), and points at the cites link for an exact quotation.

### 0.4.8

- `wirk query --help` names where a `--json` answer keeps what (`data.results[]` for a fetch, `data.cards[]` for a list, `r` as the revision, `owner` and `fields`, `page.next_cursor`, `notifications`), that `max_bytes` above 65536 is refused, and that a cut body carries one hint, the line that continues it; `depth=all` comes on its last page.
- `wirk write edit --help` prints the edit form alone: its examples, the claim, and that giving a record an owner turns it into work. `wirk write --help` spells the owner three ways in one sentence: `owner=me` here, `work.owner_id` in a request body, `owner` at the top of what `query` returns.
- `wirk query inbox=me wait=60` holds the answer until a message is pending for you or 60 seconds pass, so a harness can wake an idle agent only when the answer has cards; the request waits that much longer before it times out. Needs a service with `wait`.

### 0.4.7

- Companion CLI release for grounded catch-up guidance in MCP and skill. CLI behavior is unchanged; the installer selects the matching 0.4.7 set.

### 0.4.6

- Companion CLI release for the matching MCP and skill catch-up guidance. CLI behavior is unchanged; the installer selects the 0.4.6 set.

### 0.4.5

- `wirk write message --on ITEM --body TEXT` posts a message on an item's board (`--to ID` addresses someone, `--reply-to ID` answers, `--title` and `--body-file` as for records), and `wirk write seen ID…` marks messages seen, so sending and acknowledging need no JSON; the service's footer names `write seen` for what you acted on.
- `wirk show --help` says hosted views are live: the link comes first, a page expires after 24 hours or when revoked, and a service without views answers `views_unavailable`; the main help lists `show status`, and `upload` and `download` share a line.

### 0.4.4

- If WIRK becomes unreachable while an import starts, the importer reports its stop with the original error and hint and exits 2.

### 0.4.3

- Messages: `wirk --help`, `wirk query --help` and `wirk write --help` name `query inbox=me`, the `kind=message`, `inbox` and `participant` filters, and the `message.send` and `message.acknowledge` operations of `wirk write --request`. Answers end with the messages waiting for you, as the service writes them; with `--json`, `notifications` is printed last. Needs a service with messages.

### 0.4.2

- The installer keeps the wheels it verified in `~/.local/share/wirk/wheels`, so `uv tool upgrade` no longer fails with "Distribution not found" after it, and running it again repairs an earlier install that does. It still pins the release: to update, run the installer again.
- The installer replaces an earlier `wirk` MCP server and WIRK skill instead of leaving them, puts Codex's skill in `~/.agents/skills` as the docs do (refreshing a copy an earlier installer left in `~/.codex/skills`), and never writes through a skill folder that is a link.
- Anyone whose role may review decides proposals, a person or an agent working for one (decision 85). `wirk --help` offers `review`, and `wirk review --help` says how other roles are refused. `--person` now means deciding as yourself rather than as your agent.
- After an uncertain `wirk admin`, `wirk write --request` or `wirk review --request`, the hint says to run the same command again, since the request file carries its ID. Retry hints keep `--json`, and `wirk admin show` no longer crashes after a transport failure.
- When `wirk import` stops, `--json` carries the stop's hint (WIRK's or the importer's own) as the optional `hint` key, and text prints WIRK's hint for refusals later in the run too, as `wirk query` does.
- `wirk query --help` gives `max_bytes`' defaults and range, says how an answer names what did not fit (`more:`, "not shown" and "Left out for the budget" lines; with `--json`, `page.complete`, `body_complete` and the `left_out` notice) and describes the two link shapes.
- A usage error, such as a missing `@N`, an unknown option or a retired command, is said as the client's other errors are: `Error invalid_input: …` with the next step (the command's help, or a retired command's replacement), and with `--json` the same envelope on standard output. It still exits 2 and sends nothing.
- `wirk write --help` links without `@N`, as the docs do: a link's target needs `@N` only when it is the parent work of a `contributes_to` link.
- An item that is not work, a context or a folder is a record (decision 89): `wirk write --help` says `kind=work|record|context`, and `wirk query kind=record` lists them.

### 0.4.0

- `wirk import github OWNER` brings a GitHub owner's issues into a wirkspace. Each issue becomes work, with its history as text, its comments in a linked record and its raw issue as a file, and running it again brings only what changed. Start with `--dry-run`; a person who administers the account applies the setup it prints. GitHub is only read, through your `gh` login. See `wirk import --help`.

### 0.3.1

- `wirk --version` prints the version.
- The help says only people decide proposals and how, leaves out `wirk show` until it is live, and names the refusals agents meet.
- Login prints the service's next step once.

## Development

`uv run --extra test python -m pytest tests -q` runs the tests. The contract tests run against a scratch WIRK service when `WIRK_TEST_URL` and `WIRK_TEST_ADMIN_TOKEN_FILE` are set.

## Releasing

A release is a version bump merged to main with `Release: publish wirk <version>` in its squash body. After the release check passes on that exact commit, the tag `v<version>` is created there; pushing main publishes nothing, and a tag is never moved. The tag's Release workflow tests and builds the tagged source, attaches the wheel, the sdist and `SHA256SUMS` to a GitHub Release, and publishes to PyPI through trusted publishing; no publishing token is stored. A tag alone is not publication: check the workflow and PyPI.

The shared publication workflow checks the tag's commit, keeps identical existing assets, uploads missing ones and refuses to overwrite different bytes. New GitHub releases stay prereleases until their anonymous public downloads match the build and an isolated installation passes. Rerun failed jobs to reuse successful build artifacts. The manual `verify_tag` run qualifies an existing public version without publishing or moving tags.

`wirk-mcp` and `wirk-skill` publish in lockstep with this package: their tag `vX` waits up to ten minutes for this repository's `vX` release and fails without it. Release `wirk` `vX` first, as a version-only bump when nothing else changed. Installed clients pick up a published release after their first command an hour or more after their last check. Once an update has run, the uv receipt pins `wirk==X`, so `uv tool upgrade wirk` no longer does anything; running the installer again still works.

## License

Apache-2.0. See [LICENSE](LICENSE).
