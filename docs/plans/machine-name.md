# Plan: a machine is named by its hostname at login

Status: plan, revision 2 (after adversarial review). Branch `machine-name`. This is the client half; the service's
half (how the approval page and the account page show the name) is planned in the service repository.

## Outcome

`wirk login` sends the computer's hostname as the machine's label, so the person approving and the machine list on
the account page tell two computers apart by name instead of `wirk-cli on Darwin`.

## Change, `src/wirk_cli/cli.py`

- New `machine_name() -> str`, beside `login()`: `socket.gethostname()`, cut at the first `.` (no domain), then keep
  only the characters `A–Z a–z 0–9 -` (the hostname alphabet of RFC 1123), then cut to 40 characters (the
  service's limit for a label). An empty result, or `localhost` in any case, falls back to today's
  `f"wirk-cli on {platform.system() or 'unknown'}"`. Keeping a fixed safe alphabet means the client never needs a
  copy of the service's one-line rule, and no hostname can make `/v2/login` answer `invalid_input`.
- `login()`: the `request` dict's `"label"` becomes `machine_name()`. Nothing else in `login()` changes.
- `README.md`: the login paragraph says the machine is listed under its hostname.

## Tests first (RED), `tests/test_login.py`

The handler records the first `/v2/login` body; `socket.gethostname` is patched.
- `test_login_sends_the_hostname_as_the_label`: `studio-mini.local` sends `label` `studio-mini`.
- `test_a_hostname_keeps_only_hostname_characters_and_40_of_them`: `stu"dio\tmi ni` plus 60 more letters sends a
  label of `[A-Za-z0-9-]` only, exactly 40 characters.
- `test_no_usable_hostname_falls_back_to_the_os_label`: `""`, `localhost`, `LocalHost` and `...` each send
  `wirk-cli on <system>`.

## Left out

- Renaming a machine, and relabelling existing tokens: unused rows are revoked by hand and the machine logs in again.
  Re-running `wirk login` on an approved token does not change its label.
- The macOS "Computer Name": the decision says hostname.
- Chat names (`enrol()`'s `"wirk actor"` label): the agent-names slice.

## Interface note

What a person sees: their computer's hostname where a machine is named. What an agent sees: nothing new; `wirk login`
prints the same lines. The CLI's default label changes, which Samuel decided on 21 October 2026 ("the computer's
hostname at login"). The `/v2/login` request is unchanged: `label` is still 1 to 40 characters on one line.

## Dependency on the identity work

None in logic. It lands on `login()` as wirk-cli #69 leaves it: keep its `binding` line and the `enrol()` call; only
the label changes. Rebase after #69 merges.

## Estimate

About 10 code lines and 35 test lines.
