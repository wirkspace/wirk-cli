"""What `wirk --help` and each command's help say: examples an agent can run as printed."""

HELP = {"": """usage: wirk COMMAND [ARGS] [--json]

WIRK keeps people and agents aligned on their wirk: tasks, notes, decisions and evidence in one wirkspace.
Start with: wirk status

  status                               who you are, your wirk, what is in progress or needs review
  status 'fix the login bug'           the same, with what matters for your task
  query ID                             fetch an item; short IDs and exact titles work; ID@N is revision N
  query about='hook drain'             what matters for these words, ranked by meaning
  query status=open kind=work          list with filters; status shows the keys and values
  query kind=context                   the organization's context and its initiatives
  query proposal=proposed,deferred     proposals waiting for a decision
  query receipt=REQUEST_ID             the stored receipt of a write or review
  write new 'Title' --link related_to:ID          a doc; add kind=work for a task
  write edit ID@N status=completed --evidence 'tests pass'  complete it; N is the rN you read
  write link ID@N contributes_to PARENT@N          link two items
  write --request FILE                 any write as JSON; - reads standard input
  review ID@N ACTION --reason 'Why'    ACTION is accept, reject or defer
  show status                          a live page a person can open
  upload PATH                          store a file and print how to attach it
  download ITEM FILE                   save a stored file
  login                                connect this machine to https://api.wirk.life

Results are text; add --json for data. To run a result line "label: command", type wirk and what follows the colon.""",
        "status": """usage: wirk status [TASK WORDS] [task=… max_bytes=N workspace_id=ID] [--json]

One call from zero to useful: who you are, the context you serve, your wirk, what is in progress,
what needs your review, recent changes and how to ask for more. It writes nothing.

  status                               everything, within 8 KB
  status 'fix the login bug'           the same, with what matters for this task first
  status max_bytes=65536               room for every section""",
        "query": """usage: wirk query [REF…] [KEY=VALUE…] [--request FILE] [--json]

Fetch, list, find by meaning or look up a receipt; the keys you give choose which.

  query 5c1e7a90                       one item, with its links and the context it serves
  query 5c1e7a90@2                     revision 2 of it
  query 'Exact title'                  an item by its exact title
  query about='webhook retries'        what matters for these words, ranked by meaning
  query text='exact words'             items containing these words
  query status=open,in_progress kind=work owner=me     a list; a comma means any of
  query proposal=proposed,deferred     proposals waiting for a decision
  query kind=context state=active      active initiatives
  query receipt=w-3f9a2c41d0           the stored receipt of a write or review

Request keys: about, receipt, depth (card, full, all), sort, limit, max_bytes, cursor, workspace_id.
Every other KEY=VALUE is a filter; status lists the filters and their values.
Quote a title or words with spaces. A result line "label: command" is the next command to run.""",
        "write": """usage: wirk write new TITLE [KEY=VALUE…] [--body TEXT | --body-file PATH] [--criterion TEXT]…
                      [--link TYPE:ID[@N]]… [--upload UPLOAD_ID]… [--allow-duplicate-of ID]…
       wirk write edit ID@N [KEY=VALUE…] [--title TEXT] [--body TEXT | --body-file PATH]
                      [--link TYPE:ID[@N]]… [--upload UPLOAD_ID]… [--evidence TEXT]
       wirk write link FROM@N TYPE TO[@N]
       wirk write --request FILE           (- reads standard input)
  every form also takes --reason TEXT, --propose, --request-id ID and --json

  write new 'What I did' --body-file note.md --link related_to:5c1e7a90     a doc linked to wirk
  write new 'Rate-limit the API' owner=me --criterion 'Returns 429' --link contributes_to:2f9b3c4e@7
  write edit 5c1e7a90@3 status=in_progress                                  N is the rN you read
  write edit 5c1e7a90@4 status=completed --evidence 'tests/test_retry.py passes'
  write new 'Plan' kind=context level=initiative --body-file plan.md        an initiative

KEY=VALUE: kind=work|doc|context, level (with kind=context), owner (me or an ID), workspace_id,
and the wirkspace's fields (status=open; a comma list for several; KEY= clears on edit).
--link TYPE is related_to, contributes_to or requires. --evidence is a completion's note: the tests
that pass, a link, a file path, or a file attached with --upload. --propose needs --reason.
Every result names the IDs it created. After an uncertain result, run the same command again with
the --request-id it printed.

--request sends one JSON body. Operations:
  {"op": "item.create", "ref": "n", "data": {"title", "body", "work", "context", "fields", "uploads"}}
  {"op": "item.edit", "id": ID, "patch": {"title", "body", "work", "fields", "attach_uploads"}}
  {"op": "item.archive", "id": ID}   {"op": "item.restore", "id": ID}   (archive needs a "reason")
  {"op": "link.create", "data": {"type", "from", "to"}}   from or to may be "$n"
  {"op": "link.remove", "id": LINK_ID}
Body: {"request_id", "operations": [...], "expect": {ID: N}, "mode": "propose", "reason"}""",
        "review": """usage: wirk review ID@N… ACTION --reason TEXT [--request-id ID] [--json]

Decide proposals at the revision you read; ACTION is accept, reject or defer, and the reason is yours.
Nobody decides their own proposal.

  review c4a1e902@1 accept --reason 'Matches the agreed criteria'
  review c4a1e902@1 e7b35d16@2 defer --reason 'Wait for the load test'""",
        "show": """usage: wirk show status | --file FILE | --revoke LINK [--no-open] [--json]

A live, read-only page a person can open; the link comes first and anyone holding it can open it
until it expires.

  show status                          what is waiting, in progress and recent
  show --file view.json                a page from a view config
  show --revoke LINK                   end a link now""",
        "upload": """usage: wirk upload PATH [--description TEXT] [--request-id ID] [--json]

Store one file; the bytes go straight to storage, and the answer prints how to attach it.

  upload results.csv --description 'Load test, 2 October'""",
        "download": """usage: wirk download ITEM[@N] FILE [-o PATH]

Save a stored file, checked against its SHA-256; ITEM and FILE as shown in the item's Files block.
It never overwrites a file.

  download 5c1e7a90 file_4e1f0a2b
  download 5c1e7a90@2 file_4e1f0a2b -o old.pdf""",
        "login": """usage: wirk login [--url URL] [--new]

Connect this machine. It makes this machine's token, keeps it in ~/.config/wirk (or $WIRK_CONFIG_DIR),
readable only by you, and asks WIRK to approve the machine: open the link it prints, sign in and approve.
Where a service has no web sign-in, it prints the token's digest instead (safe to share; the token stays
here) for your WIRK administrator to register. The token is only ever sent to its address.

  login                                connect to https://api.wirk.life
  login --url https://wirk.example.org --new        another address needs a new token""",
        "admin": """usage: wirk admin show wirkspace|account [workspace_id=ID]
       wirk admin --request FILE

For people who administer WIRK, at a terminal, as themselves (the token made with
wirk login --person). Each asks you to type a confirmation naming the action.

A batch that brings a person and their agents in:
  {"request_id": "admin-alice-1", "operations": [
    {"op": "principal.create", "id": "alice", "kind": "person", "name": "Alice Chen"},
    {"op": "principal.create", "id": "alice-agents", "kind": "agent", "name": "Alice's agents", "person_id": "alice"},
    {"op": "token.add", "principal_id": "alice-agents", "sha256": "<the digest Alice sent>", "label": "laptop"},
    {"op": "member.set", "principal_id": "alice-agents", "role": "editor"}]}

Other operations: token.revoke, admin.set, wirkspace.create, field.create, field.edit, account.create."""}

RETIRED = {"read": "wirk query ID", "recover": "wirk query receipt=REQUEST_ID", "item": "wirk write new 'Title'",
           "setup": "wirk login", "schema": "wirk status (it lists the fields and their values)"}
