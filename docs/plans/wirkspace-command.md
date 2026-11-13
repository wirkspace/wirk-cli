# Plan: wirk wirkspace [NAME], the chat's wirkspace

Status: plan, revision 2 (rewritten after adversarial review, on wirk-cli #69 as trimmed). Branch `wirkspace-command`
in wirk-cli and wirk-mcp. The client half; the service's half (its refusals and the wirkspace line) is planned in the
service repository. Starts once wirk-cli #69 and wirk-mcp #34 are merged and live, and after the request-wirkspace plan.

## Outcome

A principal in several wirkspaces runs `wirk wirkspace Sales` once in a chat; from then on every call from that chat,
CLI or MCP, goes to Sales without `workspace_id=`, and nothing but this command changes the choice.

## Behaviour

- `wirk wirkspace`: prints the chat's choice (`Wirkspace: Sales (4b2a91c0) · chosen in this chat`); with none,
  the service's answer: the only one (`· your only one`), or what you reach and `Choose: wirk wirkspace NAME`.
- `wirk wirkspace NAME|ID`: matches a wirkspace you reach by name (exact, any case) or full or short ID, saves it,
  prints `Now working in Sales (4b2a91c0), previously Support (7c3d2e1f)` (or `none before`). No match, or two
  wirkspaces with that name: exit 2, listing what you reach, nothing saved.
- Outside a chat (`actor_binding()` is `None`): showing works; setting is refused with exit 2, "the choice belongs to
  one chat; add workspace_id=ID to each command". A chat that has not run `wirk login` gets `connect()`'s
  `actor_setup_required`, as every command does.
- `--json`: `{"ok": true, "errors": [], "data": {"wirkspace": {"id", "name"}, "previous": ID or null}}`; refusals are
  the client's usual error envelope. Exit codes: 0 shown or set, 1 the service refused, 2 a usage refusal.
- `workspace_id=` stays accepted everywhere: given on a command or in a `--request` file it wins for that call and
  changes nothing saved. The client adds the chosen ID only to a body that names none.
- **Known limit:** a Claude Code Agent-tool helper shares its chat's identity, so it shares its wirkspace: a helper's
  `wirk wirkspace X` moves its launcher's next calls too. Stated in the help and README until helpers get identities
  of their own (tracked in WIRK).

## Change: wirk-cli

- `client.py`: the choice is one line, the wirkspace ID only (the service prints the current name; a stored name goes
  stale), in `actor_path(url, binding).parent / "wirkspace"`: beside the chat's token, so keyed by the service and the
  combined host IDs. One write helper, `write_private(path, text)`, takes `new_token()`'s O_EXCL|O_NOFOLLOW-then-replace
  body and serves both; one read helper, `read_private(path)`, takes `read_token()`'s lstat checks and serves both.
- `Service(url, token, transport, wirkspace=None)`: `post()` sets `workspace_id` on a body without one, for every
  route but `/v2/login` and `/v2/admin` and a show revoke; `same()` compares the wirkspace too (MCP's cache). When it
  added the ID, the `outcome_unknown` hint from `unknown()` carries `workspace_id=ID` in both printed commands (the
  retry and `query receipt=…`), so a retry after a change of choice still goes where the first attempt went.
- `cli.py`: `connect()` reads the choice; `send()`'s `--request` retry carries the sent `workspace_id` too. New
  `wirkspace(words, options, transport)` in `COMMANDS`: it lists what you reach with one `/v2/status` sent with no
  default; an ok answer is the only wirkspace (`data.you.wirkspace`), a `choose_wirkspace` refusal lists the rest
  (`choices[].key` is the ID; the name follows the first space of `choices[].name`).
- `help.py`: a `wirkspace` entry. `wirk --help` stays at 25 lines: its `login` line becomes `login · wirkspace NAME`
  "connect this machine; choose this chat's wirkspace". The `show` help's "workspace_id goes in the file" line adds
  "or wirk wirkspace NAME chooses it". `README.md`: one paragraph with the known limit.

## Change: wirk-mcp

`service(actor_id)` gets the chat's choice through `connect()`; no new tool and no code beyond the rebase. An MCP-only
host cannot set the choice; the service's refusals keep offering `workspace_id=ID`. README: one sentence.

## Tests first (RED)

IDs are illustrative. wirk-cli `tests/test_wirkspace.py`:
- `test_wirkspace_alone_prints_the_chosen_one`; `test_choosing_sets_it_for_this_chat_only` (another binding sees none).
- `test_after_choosing_query_write_review_and_upload_send_its_id`; `test_login_and_admin_never_get_it`.
- `test_an_explicit_workspace_id_wins_and_changes_nothing_saved`.
- `test_a_request_file_without_one_gets_the_choice_and_a_file_with_one_keeps_it`.
- `test_both_retry_hints_carry_the_wirkspace_sent`: an uncertain write, then a new choice; the printed retry and
  receipt commands name the first wirkspace.
- `test_a_helper_sharing_the_chat_moves_its_launchers_next_write` (the known limit, as behaviour).
- `test_an_unknown_or_repeated_name_is_refused_listing_what_you_reach`; `test_outside_a_chat_setting_is_refused`.
- `test_the_choice_file_holds_only_the_id_is_private_and_is_never_written_through_a_link`.
- `test_json_and_exit_codes`; `tests/test_help.py`: the main help stays at 25 lines and names `wirkspace`.
wirk-mcp: `test_a_chats_chosen_wirkspace_reaches_its_tool_calls`.

Left out: inferring the wirkspace from IDs, a config default, a choice outside a chat, an MCP tool, renaming.

## Interface note

New: one command, and a client default for `workspace_id`. Decided by Samuel with the root, 21 October 2026: one
deliberate command, shown or set per chat, kept in the chat's own store, read by MCP too, nothing else switches it.

## Dependency on the identity work

wirk-cli #69's `actor_binding()`, `actor_path()` and `connect()`'s `actor_setup_required`; wirk-mcp #34's
`service(actor_id)`. Estimate: about +70 code lines (helpers shared, so `new_token`/`read_token` shrink), +150 tests.
