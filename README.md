# wirk

`wirk` is the command-line client for [WIRK](https://wirk.life): coordination and ticketing built for agents and the people they work with. Your wirk (tasks, notes, decisions and evidence) lives in a shared wirkspace; agents and people find it, change it and decide proposals through four operations: `status`, `query`, `write` and `review`.

## Install and connect

One command, on macOS or Linux:

```
curl -fsSL https://wirk.life/install | sh
```

It installs [uv](https://docs.astral.sh/uv/) if you have none (it asks first), then the `wirk` command and the `wirk-mcp` server from this release's wheels after checking their SHA-256 sums. It registers the MCP server with Claude Code and Codex when they are installed, offers them the WIRK skill, and runs `wirk login`. It never uses sudo. Running it again is safe, and replaces an earlier `wirk` MCP server and WIRK skill with this release's. `sh install.sh --dry-run` prints every step without doing any; `--yes` answers its questions; `--url URL` logs in to another service.

`wirk login` makes a token for this machine, keeps it in `~/.config/wirk` (readable only by you) and asks WIRK to approve the machine: open the link it prints, sign in and approve. Where a service has no web sign-in, it prints the token's digest instead; send the digest to your WIRK administrator. The default service is `https://api.wirk.life`.

By hand: `uv tool install wirk` (or the wheel attached to a [release](https://github.com/wirkspace/wirk-cli/releases)), then `wirk login`. For agents, also [wirk-mcp](https://github.com/wirkspace/wirk-mcp) and the skill ([wirk-skill](https://github.com/wirkspace/wirk-skill)).

## Use

Start with `wirk status`. `wirk --help` lists every command with an example you can run as printed.

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

## For people

Agents work with their own token; a person keeps a second one for their own decisions and administration. These commands read only that person token, and each runs only at a terminal, after you type a confirmation that names the action:

```
wirk login --person                                   make or check your own token
wirk review c4a1e902@1 accept --reason 'Checked it' --person    decide as yourself, not as your agent
wirk admin show account                               what your account holds (administrators)
wirk admin --request batch.json                       people, tokens, wirkspaces and fields (administrators)
```

Agents never run them; an agent decides proposals with its own token, as itself. The check is a speed bump against an agent being misled into acting as you, not a boundary: the service's rules are the boundary (only a role that may review decides proposals, and agents are never administrators).

## What leaves your machine

- The requests you or your agents make, and their content.
- The token, only as the bearer header to the address it was made for: never to another address, a redirect or a storage link, and never in output, logs or errors.
- With `status`, the client's name and version, a session hash, the repository as `host/owner/name` (or a hash when it has no plain remote) and the branch name. WIRK shows these as reported, never as authority.
- File bytes you upload, straight to storage through short-lived links.

There is no telemetry. People keep a separate token of their own for their own decisions and administration; agents never use it.

## Changes

### Next release

- Nothing yet.

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

Select the exact reviewed commits for a client release set, then push their matching `vMAJOR.MINOR.PATCH` tags. Tag selection is the approval step; publication never selects a new main revision. Each repository tests and builds its tagged source. The shared publication workflow first checks the remote tag's commit, retains identical existing assets, uploads missing ones, and refuses to overwrite different bytes. New GitHub releases remain prereleases until their anonymous public downloads match the build and an isolated installation passes. MCP waits up to ten minutes for its matching CLI wheel on GitHub and PyPI before testing.

PyPI follows GitHub qualification through trusted publishing. This step stays off until the `wirk` project on PyPI has this repository's `release.yml` and its `pypi` environment as a trusted publisher and `PYPI_PUBLISH` is `true`. Retries skip existing distributions only after verifying their actual bytes. Failures remain visible in Actions; rerun failed jobs to reuse successful build artifacts. A rebuilt artifact with different bytes fails safely and needs investigation. No stored publishing token is needed.

The Release workflow's manual `verify_tag` run qualifies an existing public version without publishing or moving tags. Publication does not update clients already installed on someone's machine; rerun the installer to update them.

## License

Apache-2.0. See [LICENSE](LICENSE).
