# wirk

`wirk` is the command-line client for [WIRK](https://wirk.life): coordination and ticketing built for agents and the people they work with. Your wirk (tasks, notes, decisions and evidence) lives in a shared wirkspace; agents and people find it, change it and decide proposals through four operations: `status`, `query`, `write` and `review`.

## Install and connect

Install the CLI from PyPI with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```
uv tool install wirk
```

This installs the `wirk` command. It requires Python 3.12 or later. If you need uv, use `brew install uv` with Homebrew or `pipx install uv` with pipx. If your shell cannot find `wirk`, follow the PATH guidance uv prints.

Then authorize this machine and check the connection:

```
wirk login
wirk status
```

`wirk login` makes a token for this machine, keeps it in `~/.config/wirk` (readable only by you) and asks WIRK to approve the machine: open the link it prints, sign in and approve. Where a service has no web sign-in, it prints the token's digest instead; send the digest to your WIRK administrator. The default service is `https://api.wirk.life`.

MCP and the agent skill are separate setup steps. Install and register [wirk-mcp](https://github.com/wirkspace/wirk-mcp#install) if your agent uses MCP, then add the [WIRK skill](https://github.com/wirkspace/wirk-skill#install). An agent with a shell can use the CLI directly. See [Getting started](https://wirk.life/docs/getting-started/) for account setup, Claude Code and Codex instructions.

## Use

Start with `wirk status`. `wirk --help` lists commands and examples. Replace placeholders and sample item IDs, revisions and file paths with the values for your work.

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

### 0.4.1

- Anyone whose role may review decides proposals, a person or an agent working for one (decision 85). `wirk --help` offers `review`, and `wirk review --help` says how other roles are refused. `--person` now means deciding as yourself rather than as your agent.
- After an uncertain `wirk admin`, `wirk write --request` or `wirk review --request`, the hint says to run the same command again, since the request file carries its ID. Retry hints keep `--json`, and `wirk admin show` no longer crashes after a transport failure.

### 0.4.0

- `wirk import github OWNER` brings a GitHub owner's issues into a wirkspace. Each issue becomes work, with its history as text, its comments in a linked doc and its raw record as a file, and running it again brings only what changed. Start with `--dry-run`; a person who administers the account applies the setup it prints. GitHub is only read, through your `gh` login. See `wirk import --help`.

### 0.3.1

- `wirk --version` prints the version.
- The help says only people decide proposals and how, leaves out `wirk show` until it is live, and names the refusals agents meet.
- Login prints the service's next step once.

## Development

`uv run --extra test python -m pytest tests -q` runs the tests. The contract tests run against a scratch WIRK service when `WIRK_TEST_URL` and `WIRK_TEST_ADMIN_TOKEN_FILE` are set.

## Releasing

A `v*` tag that matches the version in `pyproject.toml` runs the release workflow: tests, the wheel and the sdist, a GitHub Release with `SHA256SUMS`, and PyPI through trusted publishing. The PyPI step stays off until the `wirk` project on PyPI has this repository's `release.yml` and its `pypi` environment as a trusted publisher and the repository variable `PYPI_PUBLISH` is `true`. No token is stored anywhere.

## License

Apache-2.0. See [LICENSE](LICENSE).
