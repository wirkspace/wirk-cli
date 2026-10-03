# wirk

`wirk` is the command-line client for [WIRK](https://wirk.life): coordination and ticketing built for agents and the people they work with. Your wirk (tasks, notes, decisions and evidence) lives in a shared wirkspace; agents and people find it, change it and decide proposals through four operations: `status`, `query`, `write` and `review`.

## Install and connect

One command, on macOS or Linux:

```
curl -fsSL https://wirk.life/install | sh
```

It installs [uv](https://docs.astral.sh/uv/) if you have none (it asks first), then the `wirk` command and the `wirk-mcp` server from this release's wheels after checking their SHA-256 sums. It registers the MCP server with Claude Code and Codex when they are installed, offers them the WIRK skill, and runs `wirk login`. It never uses sudo, and running it again is safe. `sh install.sh --dry-run` prints every step without doing any; `--yes` answers its questions; `--url URL` logs in to another service.

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

Only people decide proposals: an agent's proposal waits for a person, who decides it with the commands under [For people](#for-people). Context (the organization and its initiatives) is added by the wirkspace's administrators; agents propose it.

`ID@N` names the revision you read (`rN` on a card), so a change never overwrites one you did not see. Results are text; `--json` gives the data. A result line `label: command` is the next command: type `wirk` and what follows the colon. After an uncertain result, run the same command again with the `--request-id` it printed; it applies once.

## For people

Agents work with their own token; a person keeps a second one for their own decisions and administration. These commands read only that person token, and each runs only at a terminal, after you type a confirmation that names the action:

```
wirk login --person                                   make or check your own token
wirk review c4a1e902@1 accept --reason 'Checked it' --person    decide a proposal, as yourself
wirk admin show account                               what your account holds (administrators)
wirk admin --request batch.json                       people, tokens, wirkspaces and fields (administrators)
```

Agents never run them. The agent help, the skill and the MCP server name only the review command, so an agent can tell you how to decide what waits for you. The check is a speed bump against an agent being misled into acting as you, not a boundary: the service's rules are the boundary (only people decide proposals, nobody decides their own, and agents are never administrators).

## What leaves your machine

- The requests you or your agents make, and their content.
- The token, only as the bearer header to the address it was made for: never to another address, a redirect or a storage link, and never in output, logs or errors.
- With `status`, the client's name and version, a session hash, the repository as `host/owner/name` (or a hash when it has no plain remote) and the branch name. WIRK shows these as reported, never as authority.
- File bytes you upload, straight to storage through short-lived links.

There is no telemetry. People keep a separate token of their own for their own decisions and administration; agents never use it.

## Development

`uv run --extra test python -m pytest tests -q` runs the tests. The contract tests run against a scratch WIRK service when `WIRK_TEST_URL` and `WIRK_TEST_ADMIN_TOKEN_FILE` are set.

## Releasing

A `v*` tag that matches the version in `pyproject.toml` runs the release workflow: tests, the wheel and the sdist, a GitHub Release with `SHA256SUMS`, and PyPI through trusted publishing. The PyPI step stays off until the `wirk` project on PyPI has this repository's `release.yml` and its `pypi` environment as a trusted publisher and the repository variable `PYPI_PUBLISH` is `true`. No token is stored anywhere.

## License

Apache-2.0. See [LICENSE](LICENSE).
