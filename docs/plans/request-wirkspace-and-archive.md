# Plan: workspace_id= beside --request and on review; write archive and write restore

Status: plan, revision 2 (after adversarial review). Branch `request-wirkspace-archive`. Client only.

## Outcome

`workspace_id=ID` works on every command line the same way, beside `--request FILE` and on `wirk review`; and
`wirk write archive ID@N --reason WHY` / `wirk write restore ID@N` send the archive and restore bodies the service's
hints describe, so no agent writes that JSON by hand.

## Change

`src/wirk_cli/cli.py`
- `request_body(options, words, build)`: with `--request`, the one word allowed is a single `workspace_id=X`
  (`grammar.split`); it becomes the body's top-level `workspace_id`. If the file has a `workspace_id` key at all, the
  word is refused and nothing is sent: the client cannot tell whether a full and a short ID name the same wirkspace,
  so it never merges two. Any other word or option is refused as today. The branch that refused the word and named
  the JSON key goes. `query --request` gets this too, through the same function.
- `send(...)`: the `outcome_unknown` retry it prints for `--request` repeats the `workspace_id=X` word when one was
  given, so the retry sends the identical body. Pass the words in; nothing else changes.
- `shortcut()`: `archive` and `restore` dispatch first, before `--body`/`--body-file` are read, to
  `grammar.write_archive(action, rest, options)`. They accept only `--reason`, `--propose`, `--request-id` and
  `--json`; any other option (`--body`, `--body-file`, `--evidence`, `--title`, `--link`, `--upload`, `--criterion`,
  `--to`, `--on`, `--reply-to`, `--allow-duplicate-of`) is refused naming it, nothing sent. The closing usage error
  lists `archive ID@N` and `restore ID@N`.

`src/wirk_cli/grammar.py`
- `review_body(words, options)`: `positionals, pairs = split(words)`; `workspace_id` goes into the body; any other
  `KEY=` is refused ("review takes IDs, ACTION, --reason and workspace_id="); decisions come from the positionals as
  today, so `workspace_id=` may stand before or after ACTION.
- New `write_archive(action, words, options)`: exactly one `ID@N` (through `revision()`, so a bare ID says "fetch it
  first") and an optional `workspace_id=`; operations `[{"op": f"item.{action}", "id": ID}]`, `expect {ID: N}`,
  through `write_body()`. The client does not check for an archive reason: the service already refuses an archive
  without one (`reason_required`), and one rule lives in one place. A restore passes a given `--reason` through; the
  service accepts it.

`src/wirk_cli/help.py`, within `test_help.py`'s 41-line limit for `write` (no line added): the usage line
`wirk write seen ID…` becomes `wirk write seen ID… · archive ID@N --reason TEXT · restore ID@N`, and
`wirk write --request FILE` becomes `wirk write --request FILE [workspace_id=ID]`. The `review` usage gains
`[workspace_id=ID]` (10 lines of 25). `wirk --help` is unchanged.

## Tests first (RED)

All IDs are illustrative (`4d5e6f70` for a wirkspace, `5c1e7a90` for an item).
`tests/test_write.py`
- `test_request_with_workspace_word_sends_it_in_the_body` (write, review, query): the posted body is the file plus
  `"workspace_id": "4d5e6f70"`.
- `test_request_workspace_word_with_a_file_that_names_one_is_refused`: same or different value, exit 2, no request.
- `test_an_uncertain_request_retry_repeats_the_workspace_word`: a transport failure on write prints a retry with it.
- `test_write_archive_sends_one_archive_at_the_revision_read`: `write archive 5c1e7a90@3 --reason 'Done elsewhere'`
  posts `item.archive`, `expect {5c1e7a90: 3}`, the reason, a `w-` request ID.
- `test_write_restore_sends_a_restore_with_or_without_a_reason`.
- `test_archive_and_restore_refuse_other_options_before_reading_any_file`: `--body-file missing.md` is refused
  naming the option, the file is never opened, no request.
- `test_write_archive_without_a_revision_says_fetch_it_first`.
`tests/test_review_fixes.py`
- `test_review_accepts_workspace_id_on_its_line`: `review c4a1e902@1 reject --reason R workspace_id=4d5e6f70` posts
  one decision and the wirkspace; `test_review_refuses_other_keys`.
`tests/test_help.py`: `test_write_help_names_archive_restore_and_the_request_wirkspace` (and the 41-line limit holds).
Replaced: `test_request_with_workspace_word_names_the_json_key` (which also drops a real wirkspace ID from a public
test) and `test_write_archive_and_restore_refusals_describe_the_body`.
VERIFY against a local service: archive without `--reason` prints the service's `reason_required`; archive then
restore round-trips.

Left out: `workspace_id=` on `write link` (not chosen); the service's hints (they keep describing the JSON body);
the `show` help (show is not touched here); MCP, whose tools take the JSON already.

## Interface note

New: two `write` actions, and `workspace_id=` accepted in two more places. Decided by Samuel on 21 October 2026: both
interface choices were "yes" (the key beside `--request` and on review; the archive and restore shortcuts).

Dependency on the identity work: none; rebase over wirk-cli #69's edits to `cli.py`. The wirkspace command builds on this.

Estimate: about +28 / -14 code lines, 3 help lines changed and none added, +75 / -15 test lines: about +75 net.
