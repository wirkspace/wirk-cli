# Plan: `wirk import`, one command that brings a team's tracker into WIRK

Revision 2, 2 October 2026. Plan only: nothing here is built. Branch `importers` of `wirkspace/wirk-cli`.

— importer-implementer

**Goal (decision 74).** A team on GitHub Issues, Linear or Jira tells its agent "bring everything over", and one CLI command ports it faithfully: every issue with its discussion, people, times, relations and files, findable by its old key, safe to run again. The person who asked authorized adding this command and said agents may build it.

**Sources.**
- The research notes on `wirkspace/wirk`, branch `importers-research` at `67945c5`: `designs/importers/github-and-survey.md`, `linear.md` and `jira.md`. Section numbers below such as "GitHub note §7" point there. Those notes hold the real organization's name and measurements; this public plan keeps counts only.
- WIRK as published: the site's `docs/concepts.md`, `write.md`, `query.md`, `status.md` and `limits.md`.
- wirk-core `main` at `05770e3`, which the build verifies against: `admin.py` (setup operations, who may apply them, `last_membership`), `domain.py` (decision 66's role rule, completion), `links.py`, `transfer.py`, `render.py` (card lines), `jev.py` (duplicate judgment); and `docs/plans/schema-additions.md` on `schema-build` `4e204ba`.
- The connector pattern: wirk-integrations `main` at `75c77a4`, `granola/` (README, PLAN, `render.py`, `imports.py`) and `common/`.
- Decisions 26, 28, 57, 61, 66, 68 and 74 in the docs repository.

## What revision 2 changes

The adversarial review of revision 1 (`9812f63`) said revise. Each of the root's rulings on it, and where it lands:

| Ruling | Change | Where |
|---|---|---|
| 1. Real evidence | Seed repositories `wirkspace/import-seed-*`, seeded from a manifest with every complexity; the real organization stays read-only; `read:project` is a step for the person before VERIFY | §4.6, §4.7, §8, §10 |
| 2. Private references | Only references into selected or public repositories are rendered; the rest are counted and stripped from archives; A14 checks it, on the public blind trial too | §3.9, §4.2, §7 |
| 3. A second importer | Items another `*-<source>-import` principal made block their keys; the dry run stops and explains | §3.4 |
| 4. Retargeting | An item someone else revised last is keyed by the importer's own latest revision line (`ID@N`); A11 covers it | §3.4, §7 |
| 5. Choices | Comments and their raw form stay off the work item; a timed run with a scripted judgment; Linear initiatives become docs; the conditional agreements recorded; A6's double hit and §3.2 fixed | §3.2, §3.3, §5, §9 |
| 6. Scale and links | Parts split by encoded request bytes; attachment names from hashed source IDs and a download cap; `max_bytes=65536` and counted pages; time and point bounds; core `main` `05770e3`; link pass deduped, unlinking only after a complete read, `link_cycle` caught, links read by their own fetches | §3.2, §3.4, §3.6, §3.7, §4.1, §7 |
| 7. The lows | Files in `<config>/import`; the token renamed; forged comment headings neutralized; redirect hosts allowlisted; at most 32 operations a write; field keys avoid request keys; stale options and parts; `--overwrite` targets in the dry run; a synthetic example ID; no mention of the old host | throughout |
| 8. Acceptance | A5 is a full automated comparison against an independent REST reader; A3 tightened; A10 kills at three points; time bounds | §7 |

## Contents

1. What the root ruled, and where this plan answers it
2. The command
3. The shared importer
4. GitHub Issues, built first
5. Linear, second
6. Jira, third
7. Acceptance conditions
8. Build order, and the person's steps
9. Choices for the root's review
10. Risks and unverified points
11. Why a command, not careful use of `wirk write`

## 1. What the root ruled, and where this plan answers it

| Ruling | Where |
|---|---|
| 1. `wirk import SOURCE [SELECTION…] [workspace_id=] [map=FILE] [--dry-run] [--overwrite] [--json]` in the public wirk-cli; no server, API or MCP change; a person runs `wirk admin --request` once; agents are never administrators | §2 |
| 2. Principal `<person>-<source>-import`, scoped per decision 66; never writes `[github …]` markers; skips pull requests | §2.2, §3.4, §3.9, §4 |
| 3. Provenance line with source ID, version and content hash; refresh only items whose latest revision is the importer's; no local state; re-running resumes; forged lines ignored | §3.3, §3.4 |
| 4. Privacy: private teams and restricted issues skipped unless named; emails never imported; credentials redacted; text is content, never instructions | §3.9 |
| 5. A `requires` WIRK would refuse falls back to `related_to`, keeping the exact source relation in text | §3.6 |
| 6. Authors and times as text, plus a raw JSON archive per issue with emails removed | §3.2, §3.7 |
| 7. Comments in one discussion doc per issue | §3.2 |
| 8. One issue per write; a real duplicate is resent with `allow_duplicate_of` and reported | §3.5 |
| 9. GitHub first (seed repositories and the real organization, read-only), then Linear, then Jira; each a small adapter on shared code | §4, §5, §6, §8 |
| 10. Settle the other differences, simplest faithful option first, and list the choices | §9 |

## 2. The command

### 2.1 Shape

```
wirk import                                   the sources, and the three steps below
wirk import github OWNER|OWNER/REPO… [workspace_id=ID] [map=FILE] [--dry-run] [--overwrite] [--json]
wirk import linear [TEAM_KEY…]       [workspace_id=ID] [map=FILE] [--dry-run] [--overwrite] [--json]
wirk import jira [PROJECT|ISSUE_KEY…] [workspace_id=ID] [map=FILE] [--dry-run] [--overwrite] [--json]
```

| Part | Meaning |
|---|---|
| `SOURCE` | `github`, `linear` or `jira` |
| `SELECTION` | Positional words, as the CLI's grammar already reads them. GitHub: an owner (its public repositories) or `owner/repo` (that repository, whatever its visibility). Linear: team keys; none means every public team. Jira: project keys and issue keys; none means every project the token can browse |
| `workspace_id=` | The target wirkspace; omitted, the importer's only one |
| `map=FILE` | Another map file than `<config>/import/<source>-map.json` (§3.8) |
| `--dry-run` | Read the source and WIRK, write nothing to either, print the plan, write the setup request and the map template |
| `--overwrite` | Also refresh items someone changed in WIRK since the import, still with `expect`. With `--dry-run`, it lists every item it would overwrite, by key, short ID and who changed it |
| `--json` | One JSON envelope with every outcome |

Left out on purpose: `--resume` (re-running resumes), `--since` (the hash decides), per-type switches (one command brings everything), `--prune` (§3.6 keeps the importer's own links in step), `--include-restricted` (naming is the opt-in, §3.9), and any source credential as an argument or environment variable.

### 2.2 First run and every run after

`<config>` is the CLI's configuration directory (`~/.config/wirk`, or `$WIRK_CONFIG_DIR`). Everything the importer keeps on disk is in `<config>/import/`, never in the current directory, and the service address is the one the CLI already uses.

1. **Agent:** `wirk import github Acme --dry-run`. It reads GitHub and WIRK, the latter with the agent's own token, and writes nothing to either. It prints the plan (§2.3) and writes:
   - `<config>/import/github-setup.json`, the administration request. It holds `principal.create` for `<person>-github-import` (an agent of the person whose agent ran the dry run, from `status`), `member.set` with `editor` in the target wirkspace, the `field.create` operations of §3.8, and `token.add` **last**. On core `main` an agent without membership rows acts with its person's role in every wirkspace of the account (decision 66), so the row must exist before the token does. When the importer's token already authenticates, the file holds only what is still missing: fields, or `member.set` for a new wirkspace.
   - `<config>/import/wirk-token-github-import`, the importer's own WIRK token: generated here, owner-only, created exclusively, never printed. Only its SHA-256 digest is in the setup file. The name says whose token it is, so it is never mistaken for a GitHub token.
   - `<config>/import/github-map.json`, the map template (§3.8), written only when no such file exists, so a person's edits are never lost.
2. **Person:** reads both files and runs the line the dry run printed, `wirk admin --request ~/.config/wirk/import/github-setup.json`, at a terminal, typing its confirmation. An account administrator is needed: `principal.create` and `token.add` are account operations in `admin.py`.
3. **Agent:** `wirk import github Acme`. It writes as `<person>-github-import`, with the map from `<config>/import/` unless `map=` names another. Every later run is this step alone. After cutover the person revokes the token; the report's last lines print how.

If the person skips the field operations, the import still runs with the fields that exist and keeps everything else as header text (§3.8).

### 2.3 Output and exit status

Text output is a summary, then one line per object whose outcome needs attention: `skipped: …`, `blocked: …`, `ambiguous`, `missing in GitHub: …`, `error: …`, duplicates allowed, link fallbacks and unlinks. Routine `created`, `updated` and `current` outcomes are counted, not listed, because 4,000 lines would flood an agent's context. More than 50 such lines end with a count and the `--json` hint. `--json` gives every outcome. A long run prints progress to standard error every 100 issues.

```
GitHub Acme → wirkspace Acme (1a2b3c4d) as alice-github-import (agent of alice)
selection: 3 repositories with issues · 120 issues · pull requests skipped: 310
skipped, not public: Acme/api (812 issues), Acme/web (59). Everyone in the wirkspace would read them. To include them, name them:
  wirk import github Acme Acme/api Acme/web --dry-run
items: 120 work (12 open · 100 completed · 8 cancelled) · 98 discussion docs · 218 archives
fields: repository (3) · header only: label (94 names, over the limit of 50)
links: none · mentions kept as header lines: 220 · references withheld (repositories not imported, not public): 41
files: 0 attachments · 1 external image left as a link
people: 2 GitHub accounts, 0 mapped (map: ~/.config/wirk/import/github-map.json)
text: 3 possible credentials redacted · 0 [github markers guarded · 0 comment headings neutralized
cost: 30 GraphQL points · 120 WIRK writes · 218 uploads · read in 41 s
setup: a person runs: wirk admin --request ~/.config/wirk/import/github-setup.json
find one: wirk query text='GitHub issue [Acme/cli#12]'
```

Exit status, as the Granola connector's: 0 when every object went through; 1 when any ended in `error`; 2 when the run could not start or had to stop (no `gh` login, a missing scope, a rejected key, the setup not applied, another importer's items in the selection during a dry run (§3.4), another run in progress, WIRK not answering). Every outcome reached before a stop is still printed.

## 3. The shared importer

Everything that touches WIRK is shared, so every source gets the same provenance, re-run rules, files, links, privacy and report. An adapter only reads its source.

### 3.1 The adapter contract

An adapter is one module with four functions:

1. `check(selection)`: the credential, scopes and selection are usable; otherwise a stop with the fix to run.
2. `read(selection)` → a `Census` and a list of plain records.
   - The census holds counts, skipped private or restricted scopes with their counts, and pull requests skipped.
   - A record has: kind, immutable ID, human key, URL, version (source update time), title, Markdown body, ordered header lines, state (`open`, `in_progress`, `completed` with its evidence text, `cancelled`), people (source user IDs for owner candidates), field values by field name, relations (`parent`, `blocked_by`, `duplicate_of`, `related`) by target ID, comments, file references, an archived flag, and the raw JSON for the two archives.
   - Every reference carries its target's scope (repository, team or project) and whether that scope is public, so the shared renderer applies §3.9's reference rule in one place.
   - A read that ends early says so, so nothing looks missing after a partial read.
3. `vocabularies()`: the source's field definitions and option descriptions, for the field plan.
4. `download(reference)` → bytes, with the adapter's own authentication, the redirect allowlist and the size cap (§3.7).

Markdown conversion and pacing belong to the adapter.

### 3.2 What one issue becomes

- **One work item.**
  - Title: the source title, control characters stripped, on one line, cut to 200 characters with `…` (decision 57); the full title is a header line when cut.
  - Body: the provenance line, the key line, header lines, the content notice, a blank line, then the source body (Markdown as the source gives it; Jira's ADF converted, §6).
  - Work part: `owner_id` from the map, `due_at` by the source's rule. Fields: `status` by meaning (§3.8) and the source fields that exist and hold the value.
  - Files: the issue's archive and the attachments its body references.
  - **Nothing about comments** is on the work item: no count, no comment, no comment's raw form. The discussion doc is found through the `related_to` link. A new comment therefore never revises the work item.
- **One discussion doc**, when the issue has comments.
  - Title `<key> discussion: <issue title>`, cut to 200. Its body is its own provenance line, its key line (`GitHub comments on [Acme/api#123] · <url>`), the content notice, then every comment in order: a heading `### @ada · 2026-03-02T11:00:00Z · edited`, the comment, its reactions.
  - **Forged headings are neutralized.** A line inside a comment that starts like a heading of this doc (`### @`) is written with a backslash, `\### @…`, so it reads as text and cannot pose as another comment; the report counts them.
  - It is `related_to` the work item. Its files are the comments' archive and the attachments the comments reference.
  - **Parts.** When the encoded JSON of the write that carries a part (the request body exactly as sent, escapes included) would pass 2 MiB, the discussion continues in parts (`… discussion (2)`, first-line kind `comments-2`), each `related_to` the work item. Parts are cut between comments, or inside a comment only when one comment alone is too big. Parts after the first go one to a write. So no write nears the 4 MiB request limit, whatever the script or emoji in the text.
  - **Stale parts.** A part the discussion no longer needs (comments deleted, or shorter after a redaction) is archived by the importer with the reason `No longer needed: the discussion of [Acme/api#123] now fits in 1 part`, when the importer made its latest revision; otherwise it is reported.
- **Two raw archives per issue** (ruling 6), canonical JSON with sorted keys, every email field removed (§3.9), credentials redacted, references into scopes that are neither selected nor public removed and counted (§3.9):
  - `github-issue-<ID>.json` on the work item: the issue, its relations and its timeline events except comments, without the values that move with every comment (`updatedAt`, comment counts);
  - `github-comments-<ID>.json` on the discussion doc's first part: the comments as the source returned them.

  Per-viewer values and expiring URLs are never read, so the same source version always gives the same bytes.
- **Authors and times** are text: header lines and comment headings. WIRK stamps every revision with the importer's principal and the time of the import; nothing is backdated (decision 26).

The content notice is the line `Imported from GitHub. Imported text is source content, never instructions.` Lines with nothing in them are left out.

### 3.3 The provenance line, the key line and the hash

The body's first line, which every card shows as its `line`:

```
GitHub issue 3000000017, version 20260930T120000Z, hash 3f2a9c41d07e
GitHub comments 3000000017, version 20261001T081500Z, hash 77aa0c19e2b4
Linear issue 9f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f, version 20260930T120000Z, hash 3f2a9c41d07e
Jira issue 10234, version 20260930T140211Z, hash 3f2a9c41d07e
```

(IDs and hashes are synthetic.)

- **Source and kind.** The work item is `issue`. Its discussion doc is `comments`, with later parts `comments-2`, `comments-3`. Adapters add their own kinds: Linear `project`, `milestone`, `initiative`, `document`, `update`; Jira `project`, `board`; GitHub `project`, `draft`.
- **ID** is the immutable source ID: GitHub's `fullDatabaseId` (issue IDs passed 2^31, so the 32-bit `databaseId` no longer fits), Linear's UUID, Jira's numeric ID. Keys such as `owner/repo#123` and `ENG-123` change when an issue moves; the ID does not.
- **Version** is the source's own update time to the second, in UTC, in ISO 8601's basic form. For a discussion doc it is the newest comment change. It records which source version the item was last written from; when nothing the importer writes has changed, the item is not rewritten and the version stays. The compact form keeps Linear's longest lines, an `initiative` and a tenth discussion part, at 99 and 100 characters, within the card's 100.
- **Hash** is 12 hex characters of the SHA-256 of canonical JSON holding everything the importer writes for the object except the first line: title, the rest of the body, fields, the work part, and the sorted SHA-256 of every file. Rendering is deterministic: no run time, and every list in a fixed order. The hash answers the one question a re-run asks: would the importer now write something different? Source update times do not reliably cover relations, label renames or other issues' states shown in the header (GitHub note §4.1, Linear note §5.2).
- **The match** is exact, case included: `^(GitHub|Linear|Jira) ([a-z]+(?:-[0-9]+)?) ([A-Za-z0-9_-]+), version ([0-9]{8}T[0-9]{6}Z), hash ([0-9a-f]{12})$`. The line holds no Markdown that core's card line strips. The dry run checks every line's length and refuses an object whose line would not fit.

**The key line**, second, makes the old key exact to search. The work item and its discussion doc word it differently, so the exact search finds one item, not two:

```
GitHub issue [Acme/api#123] · https://github.com/Acme/api/issues/123
GitHub comments on [Acme/api#123] · https://github.com/Acme/api/issues/123
```

- `wirk query text='GitHub issue [Acme/api#123]'` finds the work item alone. The brackets keep `#123` from matching `#1234`.
- `text='[Acme/api#123]'` finds the work item, its discussion doc and every imported item whose header points at it.
- The report's last line teaches the first form.

### 3.4 The index and the decision for each object

**The index** (Granola's `imports.py`, shared, with its lessons):
1. **List.** For each kind the adapter writes, list items whose text contains `<Source> <kind> `, active and then `archived: true`. Each page asks for `limit=100` and `max_bytes=65536`, the most a card list may carry, so about 100 cards arrive a page; the loop follows `next_cursor` whatever the count. For the real organization that is about 22 pages of work items and 21 of discussion docs, plus one per archived listing: about 45 pages a run, measured in VERIFY.
2. **Match.** Keep the cards whose `line` matches §3.3 exactly. The key is (kind, ID).
3. **Trust: who created it.** A card the importer revised last is its own. For any other card, one `depth=all` fetch (32 refs a request, cached for the run) reads `created_by`.
   - Created by the importer's principal: one of its items.
   - Created by **another `*-<source>-import` principal** (another person's importer for the same source): that key is **blocked**. A real run writes nothing for it and reports `blocked: imported by bob-github-import`. A dry run that finds any blocked key stops after its plan (exit 2), writes no setup file, and explains: which principal, how many keys, and the two ways on (import as that principal, or into another wirkspace).
   - Created by anyone else: a forged or copied line. It never makes the importer read the source for it, write anything or list it; it is counted.
4. **Retargeting: which key it really holds.** An item of the importer's that someone else revised last could have had its first line edited to name another issue. So such an item is keyed by **the importer's own latest revision**: the same `depth=all` fetch lists the revisions and who made each; the importer's latest is `N`; one fetch of `ID@N` (32 a request) gives the line the importer wrote, and its key and hash count. The current first line of a person-revised item is never trusted.

**The decision** for each object of a complete read:

| WIRK holds, among the importer's own items (by the key above) | The importer |
|---|---|
| nothing | creates it |
| one active item whose hash (the importer's own) matches | `current`: writes nothing |
| one active item with another hash, last revised by the importer | `updated`: `item.edit` with `expect` at the card's revision |
| one active item with another hash, last revised by anyone else | `skipped: changed in WIRK since import (r7 by alice)`; `--overwrite` updates it, still with `expect`, and the dry run lists it |
| archived items only | `skipped: archived in WIRK`; never restored or created again (Linear's own archive mirroring is §5) |
| two or more active items | `ambiguous`, naming them; writes nothing |
| an item whose object is absent from a complete read of the same selection | `missing in GitHub: deleted, transferred out or no longer visible`; never archived |

A person's later edit wins: the team can keep re-running during cutover without losing its changes. A read that ended early marks nothing missing.

### 3.5 Writes

- **One issue per write** (ruling 8). Its files are uploaded first. Then one write holds the work item's create or edit, the discussion doc's first part, and the `related_to` between them when the doc is new: three operations in the common case. The duplicate judgment checks the first four new work items of a write (`jev.py`), so every issue is checked. Later parts and link changes go in further writes of the same issue. **No write holds more than 32 operations or 64 `expect` entries**; an issue with more link changes than that takes several writes.
- **Reason.**
  - A closed-as-completed issue gets its evidence: `Imported from GitHub: Acme/api#123 closed as completed by @ada at 2026-03-04T09:00:00Z; closed by Acme/api#130 (pull request, merged)`, cut to 2,048 characters. A closing pull request in a scope that is withheld (§3.9) is `closed by a pull request in a repository not imported`.
  - Every other write: `Imported from GitHub Acme/api#123 by wirk import github`.

  WIRK stores the reason on each revision the write makes.
- **Request IDs** are fresh per write, `github-<ID>-<16 hex>`, never derived from the body, so a refusal stored under an old ID is never replayed. After an uncertain answer the CLI's transport resends the identical body once; if it is still uncertain, the importer looks the ID up with `query receipt=`. Applied counts as done; anything else is that issue's `error`, and the next run's index decides again.
- **Refusals** apply nothing and name `operations[i]`:
  - `unknown_owner`: the owner is dropped, the write resent under a new ID, and the issue reported (`owner @ben → bob is not a member`).
  - `unknown_enum_option`: an option retired or removed since the run read `status`; that value becomes header-only, the write is resent, and the field is reported.
  - `likely_duplicate`: if a named item is the importer's own item for the same key, the object is `current`. Otherwise the importer resends with `allow_duplicate_of` naming the choices (at most 8) and the reason `Separate GitHub issues Acme/api#7 and Acme/api#9; imported as they are`, and reports the pair as `possible duplicates kept separate`. A faithful import never drops an issue because it resembles another.
  - Anything else: that issue's `error`; the run goes on.
- **Suggestions** in receipts are counted and ignored: the importer writes the source's links, not new ones.
- **Order.** Repositories, then issues by number. WIRK requests go one at a time.

### 3.6 Links

Links come in a second pass, after every issue's own write, so both ends exist.

| Source relation | WIRK link | Unless |
|---|---|---|
| parent, sub-issue | `contributes_to` from child to parent, `expect` on both | — |
| blocked by | `requires` from the blocked issue to its blocker | the blocked issue is completed (core refuses a prerequisite on completed work), the blocker is cancelled (it could never be completed, so the gate would never open), or the link would close a `requires` cycle. Then `related_to` |
| duplicate of, related | `related_to` | — |

- **Text always.** Both headers keep the source's exact relation, and a fallback says why: `Blocked by: [Acme/api#120] (kept as related: completed here)`. A relation whose other end is outside the selection stays text when its scope may be shown, and a count when it is withheld (§3.9). A later run that includes that end makes the link.
- **One plan per pair.** The same relation arrives from both ends (`blockedBy` on one issue, `blocking` on the other; a parent and its sub-issue lists). The plan dedupes by link type and ends, `related_to` as an unordered pair, so a link is made once.
- **Cycles** are found by a local graph check in ID order, so the same source gives the same links on every machine. Core still refuses a cycle the local check cannot see (one closed by a link a person made): a `link_cycle` refusal makes that link `related_to`, reported, and the rest of the write is resent.
- **Reading links.** Each work item with planned or existing relations is fetched on its own (one ref, `depth=full`, `max_bytes=65536`, following the continuation until the item is complete), so a long body never crowds its links out of a shared budget. The makers of the importer-candidate links are read by fetching the link IDs, 32 a request.
- **Each run keeps the importer's own links in step.** Missing links are added. A link the importer made whose relation is gone from the source, or whose type must change, is removed and reported as `unlinked`, **but only after a complete read of both ends' relations**; a partial read or a page that failed unlinks nothing. Links anyone else made are never touched. A removal and its replacement go in the same write.
- **Completing work that a stale gate blocks.** When the source completes an issue that still `requires` an incomplete issue through the importer's own link, the issue's write removes that link and adds `related_to` before it sets the status, because core refuses completion over an incomplete prerequisite. When a person made that `requires` link, the item is `skipped: completing it needs [Acme/api#120] completed (a link made in WIRK)`.

### 3.7 Files

- **Upload** reuses the CLI's presigned flow (`/v2/files` upload, PUT without the token, confirm), adding what Granola's connector sends: a one-line `description` and `metadata: {role: "original", origin: {uri: <source URL>, observed_at: <source update time>}}`. WIRK keeps one copy per content per wirkspace, so an image used twice is stored once.
- **Names.** An attachment's WIRK file name is `<source>-attachment-<12 hex>-<original name>`, the hex being the SHA-256 of the attachment's source ID (GitHub's asset ID, Linear's upload path, Jira's attachment ID). A re-run finds the same file by name whatever its bytes, and the name carries no URL or signature. The archives are `github-issue-<ID>.json` and `github-comments-<ID>.json`.
- **Attach** in the issue's own write: the issue archive and body attachments on the work item, the comments archive and comment attachments on the discussion doc's first part. On an update, a file whose bytes changed is swapped with `replace_files`, found by its name. A file the source no longer references stays and is reported; the importer detaches nothing.
- **Downloads** happen during the run through the adapter, since source links expire.
  - **Size cap:** at most 100 MiB a file, read in blocks, and refused beyond that.
  - **Redirects:** followed only over https, only to the adapter's allowlist, and never with a credential:
    - GitHub: `github.com`, `objects.githubusercontent.com`, `private-user-images.githubusercontent.com`, `user-images.githubusercontent.com`;
    - Linear: `uploads.linear.app` and the storage host it names, checked in the seed;
    - Jira: none, since `redirect=false`.

  The text keeps the source URL; WIRK lists the file beside it. Images on other hosts stay links and are counted. A file that cannot be downloaded, or is over the cap, stays a link, is reported with its issue, and the issue is still imported. Bytes are held in memory; nothing is written to disk.

### 3.8 Fields, status, people and the setup request

**Fields** are WIRK enums that only a person administrator creates (`admin.py`; agents are never administrators).
- **When a vocabulary becomes a field.** Only when it is in use and small: at most 50 options by default, `field_limit` in the map file. `status` lists every active option of every field, so a large vocabulary would lengthen every status call. Every value is also a header line, always, so `text=` finds values that are not fields.
- **Keys.**
  - Field keys are lowercase ASCII (`[a-z][a-z0-9_]*`), and names keep the source's spelling.
  - A field key is never one of:
    - the fixed filter keys: `kind`, `state`, `proposal`, `owner`, `linked`, `folder`, `changed_days`, `text`, `archived`;
    - `status`;
    - a key the CLI or the request reads as its own: `about`, `receipt`, `depth`, `sort`, `limit`, `max_bytes`, `cursor`, `workspace_id`, `task`, `format`, `fetch`, `fields`, `level`.

    A source field with such a name takes the source as prefix (`github_sort`).
  - A collision with an existing field of another meaning is prefixed the same way.
  - Option keys are slugs; one with no ASCII left becomes `x_` and 6 hex of its name's hash; collisions take `_2`.
- **The importer only ever creates fields; it never edits one.** An agent can see a field's active option keys in `status`, but not its revision or its options' names and meanings, and `field.edit` replaces the whole option list. So:
  - a value whose option is missing, because the field existed before or the source added it since, stays a header line. The report names the field, the missing values and the person's next step: `wirk admin show wirkspace`, then a `field.edit` adding them. The next run fills the field in;
  - an option that is stale (renamed or deleted in the source, or retired in WIRK) is left as it is: items that held it are updated to the values the source now has.
- `field.create` uses `applies_to: item`, `required: false`, no defaults, and the source's descriptions on options.

**Status** maps by meaning onto the wirkspace's standard four, `open`, `in_progress`, `completed` and `cancelled` (decision 68). The map file can point a meaning at another key the wirkspace has. The importer never edits `status`, so it never adds options to every status call or meets the rule that an option's completion meaning cannot change. The source's own state name (Linear's and Jira's workflow states, a GitHub project's Status column) is a `workflow` field when small, and a header line always. `workflow` has no completion meaning, so options merge by name without conflict.

**People.** WIRK owners must be members, and the importer never creates principals.
- The map file's `users` maps a source user (GitHub login, Linear user ID, Jira accountId) to a WIRK principal or `null`.
- The template lists every source user the selection mentions, by name and role counts (assignee, author, commenter), all `null`. Agents have no member list to match against, and a guess would assign work to the wrong person.
- The first mapped assignee becomes `owner_id`; every assignee is in the header (`@ada (owner ada), @ben (not in WIRK)`).
- Everyone else appears by name only: authors, closers, commenters, reactors, bots (`@deploy[bot] (bot)`) and deleted accounts (`@ghost (deleted account)`).

**The map file**, `<config>/import/<source>-map.json`, every key optional:

```json
{"users": {"ada": "ada", "ben": null},
 "field_limit": 50,
 "status": {"in_progress": "in_progress", "cancelled": "cancelled"}}
```

**The setup request**, `<config>/import/<source>-setup.json`, written by the dry run:

```json
{"request_id": "github-import-setup-5d1e9a0c", "workspace_id": "wsp_…",
 "operations": [
  {"op": "principal.create", "id": "alice-github-import", "kind": "agent", "name": "GitHub importer", "person_id": "alice"},
  {"op": "member.set", "principal_id": "alice-github-import", "role": "editor"},
  {"op": "field.create", "field": {"key": "repository", "name": "Repository", "applies_to": "item", "selection": "one",
                                    "required": false, "options": [{"key": "api", "name": "Acme/api", "order": 1}, …]}},
  {"op": "token.add", "principal_id": "alice-github-import", "sha256": "<digest>", "label": "github import"}]}
```

- Principal IDs are 2–40 characters, so a person ID over 26 characters cannot take `-github-import`; the dry run says so and stops.
- If the principal already exists, made from another machine, the person deletes the `principal.create` line; the dry run says this when its token is new.
- A setup with more than 32 operations is split into numbered files, rows first and `token.add` in the last.

### 3.9 Privacy and safety

- **Selection is the control.** Everyone in a wirkspace reads everything in it (decision 28). Private and restricted scopes are skipped unless named: GitHub repositories that are not public, Linear private teams, Jira issues with a security level and comments or worklogs restricted to a group or role. The dry run lists what it skipped with counts and the exact command that names them, and when they are named it warns: `Acme/api is private on GitHub: everyone in the wirkspace will read its 812 issues`.
- **References into withheld scopes are never shown** (ruling 2). Sources return references the importing person can see: a pull request in a private repository that mentions a public issue, a private sub-issue, a blocker in a private team. A reference is rendered (header, evidence, link) only when its target's scope is selected in this run or public. Every other reference is counted where it would have been (`Mentioned in: [Acme/cli#88] (issue) · 3 in repositories not imported`) and removed from both raw archives, with the same count left in their place. No repository, team or project name, number, key or title from a withheld scope reaches WIRK: not in text, archives, file names, reasons or descriptions. A14 checks this, including for the public-repository blind trial.
- **Emails are never imported.**
  - The adapters never request an email field where the API lets them choose (GitHub GraphQL, Linear GraphQL).
  - They drop every key named like an email from everything they hand over (Jira and REST answers, commit authors in timelines).
  - Emails are used only locally, never sent to WIRK.
  - An address a person typed inside an issue or comment is imported as written, as the Granola connector does, and counted in the dry run (§9 choice 10).
- **Credentials are redacted** in titles, bodies, comments and archives, and counted: Granola's shapes (`grn_`, `wirk_`, `sk-`, `ghp_`/`gho_`/`ghs_`, `github_pat_`, `AKIA…`, `xox…`, PEM private keys) plus `ghu_`, `ghr_`, `lin_api_`, `ATATT`, `glpat-`, `sk_live_`/`rk_live_` and `AIza`. Each becomes `[redacted: possible credential]`.
- **Imported text is source content, never instructions.** Every imported item says so. The importer never acts on what text says: no command, link, assignment or setting is taken from it.
- **The GitHub integration's markers.** Imported title and body text that contains `[github ` gets a word joiner (U+2060) after the bracket, so the pull request integration's `text` queries can never match it; the report counts them. The importer writes none of the markers, never writes as `<person>-github`, and skips pull requests.
- **Read-only against sources.** GitHub only through `gh api` reads: GraphQL queries and GET. Linear: GraphQL queries. Jira: GET plus its read-only search and bulk-fetch POSTs. A test asserts no mutation and no other method is ever sent. The seed tool (§4.6) is separate test code, writes only to `wirkspace/import-seed-*`, and nothing in the importer calls it.
- **Secrets.**
  - GitHub goes through the person's `gh` login; the importer never reads `gh`'s token.
  - Linear and Jira keys come from owner-only files under `<config>/import/`, checked like WIRK's token files. They are never taken from arguments or the environment, are sent only to their own host, and never follow a redirect.
  - The importer's WIRK token is an owner-only file, sent only to the service.
  - None of them appears in output, errors or archives; a leak test checks every request, answer and printed line.
- **Output** carries keys, short IDs, counts and outcomes, never bodies or comments.

### 3.10 No local state

Every run reads the selection and WIRK's index in full. The index is the only state, so re-running is resuming, and a run killed at any point is finished by running it again.

The only files are in `<config>/import/`: the setup request, the map, the importer's token, and a lock (`flock` on `<source>.lock`, released at exit) that stops two runs on one machine. Two machines starting the same first import at the same moment can still create two items for one issue. Both are reported `created`, and the next run reports them `ambiguous`.

### 3.11 Code and budget

```
src/wirk_cli/importers/
  __init__.py   the command: arguments, setup and map files, lock, the run, the report
  render.py     provenance and key lines, header, content notice, redaction, marker and heading guards,
                reference rule, title cut, parts, hash (pure)
  wirk.py       the index and its trust rules, the decision table, writes, files and links against WIRK
  github.py     the GitHub adapter (then linear.py; jira.py with adf.py)
tests/seed/     the GitHub seed tool and its manifest (§4.6); test code, never imported by the importer
tests/verify/   the independent REST reader (§7, A5); test code
```

- `wirk import` is one entry in the CLI's command table and help; the module loads only when the command runs, so `wirk --help` stays fast.
- It reuses the CLI's `Service` (no redirects, one identical resend), token files, request IDs and presigned PUT. It adds no dependency: GitHub goes through `gh`, Linear and Jira through `httpx`, which the CLI already has.
- **Budget:** shared code at most 1,000 lines and the GitHub adapter at most 450, excluding tests and help. The CLI's own budget (decision 68) is unchanged. Linear and Jira get their budgets in their own revisions of this plan.
- **Tests:** a fake `gh` runner answering synthetic GraphQL pages, and `httpx.MockTransport` for WIRK, as the CLI's tests do. Contract tests run against a scratch service when `WIRK_TEST_URL` is set, like the CLI's existing ones. Fixtures are synthetic, and the repository's privacy scan keeps passing.

## 4. GitHub Issues, built first

### 4.1 Reading

- **Checks.** `gh` is installed and logged in to github.com. The scopes come from one `gh api -i user`. Private repositories need `repo`; projects need `read:project`, which the person grants before VERIFY (§8). Without it the dry run reports projects as not read and prints `gh auth refresh -s read:project`; it never trusts an empty answer.
- **Selection.** An owner means its public repositories with issues enabled. Repositories that are private or internal are listed with their issue counts and skipped unless named as `owner/repo` (§3.9). Archived repositories are read like the rest, with a header line.
- **Issues** through GraphQL `Repository.issues`, 25 a page, ordered by creation, with the first page of each nested connection:
  - labels and assignees;
  - comments (100), with reactions, hidden state and edit time;
  - timeline items (100) except comments, through one fragment per event type, so unknown types still arrive with their type name and are counted;
  - parent, sub-issues, `blockedBy`, `blocking` and `duplicateOf`;
  - `closedByPullRequestsReferences`, issue type, milestone, issue field values, lock and pin state;
  - project items and their field values, when the scope allows.

  Every referenced node carries its repository's name and visibility, for §3.9's reference rule.
- **Paging.** A connection with more than a page is fetched on its own, issue by issue. A page that times out is retried at half the size. Pull requests are never read: only `pullRequests.totalCount`, for the report.
- **Pacing and bounds.**
  - Requests are serial. Each query asks for `rateLimit { cost remaining resetAt }` and waits for the reset when the next page would not fit.
  - A secondary-limit refusal waits a minute and doubles, three times, then stops with exit 2 and the outcomes so far.
  - A page of 25 issues with ten nested connections costs about 3 points, so the real organization's 2,200 issues are about 90 pages: bounded at **1,000 points a run**, a fifth of an hour's budget.
- **Attachments.** Rendered bodies (`bodyHTML`) are read only for issues and comments whose text holds a `user-attachments` URL. For a private repository they carry short-lived signed image URLs, downloaded at once, without any credential, within §3.7's allowlist and cap. Public attachments download directly. Anything else stays a link and is reported (GitHub note §2.6; proven or refuted on the seed, §4.6).

### 4.2 Mapping

| GitHub | WIRK |
|---|---|
| Issue | One work item; `repository` field |
| Open; reopened | `open`, or the project Status mapping (§4.4) |
| Closed as completed | `completed`, the closing record as the write's evidence |
| Closed as not planned | `cancelled` |
| Closed as duplicate | `cancelled`, `related_to` the original, `Duplicate of: […]` in both headers |
| Pull request | Not imported; counted; named in header lines where an issue refers to one and its repository may be shown |
| Title | Title, cut at 200 with the full title in the header |
| Body | Body under the header, unchanged except redaction and the marker guard |
| Labels | `label` field (many) when small; header always |
| Milestone | `milestone` field (one, merged by title) when small; header with due date and state |
| Assignees | First mapped one is `owner_id`; all in the header |
| Author, closer, times | Header lines |
| Issue type | `issue_type` field |
| Issue fields: single and multi select | One field each when small |
| Issue fields: text, number, date | Header lines; the default `Target date` becomes `due_at`, the end of that day in UTC |
| Sub-issue of | `contributes_to`; the parent's header lists sub-issues in GitHub's order |
| Blocked by | `requires`, or `related_to` by §3.6 |
| Cross-references | Header line `Mentioned in: [Acme/api#88], [Acme/api#91] (issues) · [Acme/api#130] (pull request)`; no links, so thousands of passing mentions do not bury real relations |
| References into private repositories not selected | Counted, never named (§3.9) |
| Closing pull requests and commit | Header line, and the completion evidence |
| Comments | The discussion doc (§3.2) |
| Hidden comments | Kept, marked `hidden on GitHub as outdated` (or off-topic, resolved, duplicate); those hidden as spam or abuse left out of the doc and its archive, and counted on the doc's key line |
| Reactions | On the issue: counts in the header. On a comment: counts after it, in the doc |
| Attachments | WIRK files (§3.7) |
| Locked, pinned, transferred | Header lines |
| Deleted user, bots | `@ghost (deleted account)`, `@name[bot] (bot)` |
| Timeline events (labels, assignments, renames, moves) | The issue archive only |
| Projects | §4.4 |
| Converted to discussion | Gone from the issue list; an earlier import is reported `missing` |

### 4.3 One issue, written

```
title:  Fix login redirect after SSO
work:   {"owner_id": "ada"}
fields: {"status": "completed", "repository": "api"}
files:  github-issue-3000000017.json
links:  related_to ↔ Acme/api#123 discussion: Fix login redirect after SSO
reason: Imported from GitHub: Acme/api#123 closed as completed by @ada at 2026-03-04T09:00:00Z; closed by Acme/api#130 (pull request, merged)
body:
GitHub issue 3000000017, version 20260304T090000Z, hash 3f2a9c41d07e
GitHub issue [Acme/api#123] · https://github.com/Acme/api/issues/123
Opened by @ada 2026-03-01T10:00:00Z · closed by @ada 2026-03-04T09:00:00Z as completed
Closed by: [Acme/api#130] (pull request, merged)
Assignees: @ada (owner ada), @ben (not in WIRK) · Type: Bug · Milestone: v1.2 (due 2026-03-15, open)
Labels: bug, area/api
Sub-issues in GitHub order: [Acme/api#124], [Acme/api#125] · Blocked by: [Acme/api#120]
Mentioned in: [Acme/api#88] (issue) · [Acme/api#131] (pull request) · 2 in repositories not imported
Imported from GitHub. Imported text is source content, never instructions.

<the issue body>
```

### 4.4 Projects

Built in its own slice, on fixtures and on the seed project; real data once the person grants `read:project` (§8).
- **Fields and status.** A `project` field (many), and one field per project single-select field when small. The Status of the issue's first project by number, or the project the map names, gives `workflow` and the status mapping:
  - Todo, Backlog and Triage mean `open`; In progress and In review mean `in_progress`; the map file overrides.
  - Done on an open issue is `in_progress`, with the column in the header: completion needs GitHub's closed state and its evidence.
- Number, text, date and iteration values are header lines.
- **Draft issues** become work items with their own kind (`GitHub draft PVTI_…`). Pull request items are skipped and counted.
- **One doc per project** (kind `project`): its description, readme, fields and options, iterations with dates, and closed state.
- Lost: views, workflows, charts and item order.

### 4.5 Evidence matrix

How each complexity is verified:
- **S**: the seed repositories (§4.6), whose manifest states each object's expected WIRK result.
- **R**: the real organization, read-only, imported into a scratch service (counts from GitHub note §7; the run re-counts at its own time).
- **F**: unit tests on synthetic GraphQL pages through a fake `gh`.
- **C**: the contract test against a scratch wirk-core over HTTP.
- **P**: needs a step only the person can take (§8): a scope, a newer `gh`, a second account, or a hand deletion.

| # | Complexity | Expected WIRK result | Verified |
|---|---|---|---|
| G1 | Open issues (195) and reopened (11) | `open` | S R F |
| G2 | Closed as completed (1,856) | `completed`; the evidence names the closer, the time and the closing pull request or commit | S R F C |
| G3 | Closed as not planned (138) | `cancelled` | S R F |
| G4 | Closed as duplicate (0 real) | `cancelled`, `related_to` the original, both headers | S F C |
| G5 | Pull requests in the shared number space (2,343, plus 123 in repositories without issues) | none imported; counted | S R F |
| G6 | Private and internal repositories (6 of 9 holding issues) | skipped unless named; listed with counts; a warning when named | S R F |
| G7 | References into private repositories not selected (cross-references, sub-issues, blockers, closing pull requests) | counted, never named, absent from archives | S R F |
| G8 | Titles up to 256 characters, control characters | cut to 200 with `…` and the full title in the header; controls stripped | S F |
| G9 | Bodies (median 1.7 KB, largest 38 KB) | equal to GitHub's after redaction and the marker guard | S R F |
| G10 | Labels (94 distinct names, over the limit); renamed after use | header only, or a field when the map raises the limit; merged by name; a renamed label leaves its old option alone | S R F |
| G11 | Milestones (0 real) | `milestone` field and header | S F |
| G12 | Assignees (16 issues, 1 person); unmapped; not a member | owner from the map; everyone in the header; `unknown_owner` resent without owner and reported | S R F C |
| G13 | Several assignees; comments by a second person | everyone in the header | F P |
| G14 | Authors: one person and one bot (46 issues, 128 comments); a deleted user | `@login`, `@name[bot] (bot)`, `@ghost (deleted account)` | S R F P |
| G15 | Issue types (the organization's three) | `issue_type` field only when used | S R F |
| G16 | Issue fields: single select and date (the organization's own); text, number, multi select | select fields; text, number and date as header lines; Target date as `due_at` | S F |
| G17 | Sub-issues: three levels, across repositories, 100 under one parent, reordered (0 real) | `contributes_to`; GitHub order in the parent's header | S F C |
| G18 | Blocked by: open by open, completed by open, by a cancelled issue, across repositories, a cycle if GitHub allows one (0 real) | `requires`; each fallback `related_to` with phrase and reason | S F C |
| G19 | Cross-references (7,941; one issue over 100) | `Mentioned in` header lines; no links; nested paging | S R F |
| G20 | Closing pull requests (668 issues), into the default branch and not | header line and evidence | S R F |
| G21 | Comments (14,080; up to 124 on one issue; 117 issues with none) | one discussion doc per issue with comments, in order, authors and times exact; none without; nothing about comments on the work item | S R F C |
| G22 | Discussions over the part budget, in a script that triples when escaped | parts cut by encoded request bytes, each under the budget | S F C |
| G23 | Edited comments (about 48) and bodies (111) | `edited` on the heading; history only in the archive | S R F |
| G24 | Hidden comments: outdated, spam | kept and marked; left out and counted | S F |
| G25 | Reactions (0 real) | counts | S F |
| G26 | A comment line that imitates a comment heading | neutralized and counted | S F |
| G27 | Attachments (0 real): images, video, PDF, the same file twice, over the cap, external images (1 real) | files with origin, named by hashed source ID, stored once; over the cap and external left as links and counted; private downloads proven or reported | S F C P |
| G28 | Locked, pinned, transferred (0 real) | header lines | S F |
| G29 | Timeline (28,939 events) | in each issue's archive; counts equal the independent REST reader's | S R F |
| G30 | The raw archives | two per issue with comments, deterministic bytes, no email field, credentials redacted, withheld references removed | S R F |
| G31 | Credential-shaped strings; instruction-shaped text | redacted and counted; kept as text, never acted on | S F |
| G32 | A pasted `[github pr …]` marker | guarded; the integration's `text` query finds no imported item | S F C R |
| G33 | Projects, drafts, iterations | §4.4 | S F P |
| G34 | Re-run with nothing changed | all `current`, no write, no upload | S R C |
| G35 | Changes in GitHub: a title, a comment, a label, a close, a new blocker, a removed sub-issue | only that work item or discussion doc updated; links in step | S F C |
| G36 | An edit in WIRK | `skipped` with who; `--overwrite` updates; the dry run lists it | S R C |
| G37 | Killed between upload and write, mid-parts, mid-link-pass | the same final state as an uninterrupted run | S C |
| G38 | A lost answer | identical resend or receipt lookup; one item | F C |
| G39 | `likely_duplicate` | resent with `allow_duplicate_of`, reported | S F C |
| G40 | A forged line; a retargeted line; another importer's items | ignored; keyed by the importer's own revision; blocked, and the dry run stops | S F C |
| G41 | An issue deleted in GitHub, transferred out, converted to a discussion | reported `missing`, never archived | S F P |
| G42 | A repository renamed; an issue transferred within the owner | found again by ID; the key line follows | S F |
| G43 | A field option retired in WIRK | header only and reported | F C |
| G44 | Rate limits; no `gh` login; missing scope | waits and finishes; exit 2 with the fix | F |
| G45 | GitHub never written | only `gh api` reads, checked on every call | F S R |
| G46 | Tokens never printed | leak test over requests, output and errors | F |

### 4.6 The seed repositories

Authorized by the root: scratch repositories in the `wirkspace` organization, filled from a manifest with every complexity of §4.5 that GitHub lets one account create. This restores the GitHub note's §8 plan.

- **Where.** Three repositories:
  - `wirkspace/import-seed-private-a` and `wirkspace/import-seed-private-b`, private: cross-repository sub-issues, blockers, references and transfers;
  - `wirkspace/import-seed-public`, public: unauthenticated attachments, the blind trial, and the target of references from the private two (the A14 test).

  Content is synthetic only: invented text, fake credential shapes that cannot be live keys, an instruction-shaped sentence.
- **Organization objects.** The seed uses only what the organization already has: the issue types Task, Bug and Feature, and the default issue fields Priority, Effort, Start date and Target date. It creates no organization-level type or field, because those would change every repository's pickers; text, number and multi-select field values stay fixture-only. One organization project is created only with the person's `project` scope (§8).
- **The manifest**, `tests/seed/github-manifest.json`: every seeded object with a stable seed ID (`seed:G-012`), its class (clean, convention, lost) and its expected WIRK result: kind, title, status, fields, owner, header lines that must be present, links and fallbacks, files with SHA-256, and what must be absent (withheld references). It is written from this plan's mapping, never from the importer's code.
- **The seed tool**, `tests/seed/github.py`, test code the importer never calls.
  - **Where it writes.** It writes through `gh api` only to repositories whose names start `wirkspace/import-seed-`, and refuses anything else before its first call.
  - **Idempotent.** Each issue, comment and pull request carries its seed ID, so a re-run creates only what is missing.
  - **Pacing.** At most one content-creating request a second, under GitHub's 80 a minute.
  - **It never deletes anything.** Deleting an issue is irreversible and the person's step (§8).
  - **Scripted changes.** It applies the changes of A8 (a title, a comment, a label, a close, a new blocker, a removed sub-issue, a repository rename, a transfer) for the re-run, when asked.
- **What it seeds, beyond the rows marked S in §4.5:**
  - about 250 small issues in one repository (pages past 100);
  - one issue with 105 cross-references;
  - 130 comments on one issue;
  - a discussion over the part budget, written in a script that triples when escaped;
  - pull requests interleaved with issues: one merged with `Fixes #1`, one into a non-default branch, one from the other private repository closing a public issue;
  - a bot comment, posted by a GitHub Actions workflow in the public repository on GitHub's own runners;
  - labels with descriptions, emoji and the same name in two repositories, one renamed after use;
  - milestones open, closed and without a due date;
  - locks of each reason and three pinned issues;
  - comments hidden as outdated and as spam;
  - reactions of all eight kinds.
- **Needs the person (§8):**
  - attachments, which need a `gh` from September 2026 or later (`--attach`) or a hand upload;
  - the organization project and its draft, which need the `project` scope;
  - several assignees and a second commenter, which need a second account;
  - a deleted user;
  - a deleted issue and a conversion to a discussion, both by hand.

  Until the person takes these steps, those rows stay F, and the report says so.

### 4.7 The evidence runs

All against a **scratch** wirk-core from `main` at `05770e3`, started locally:
- a fresh `WIRK_CONFIG_DIR` and data in the session's scratch directory;
- a loopback stand-in for the object store;
- a port confirmed free with `lsof -nP -iTCP:<port> -sTCP:LISTEN`, and never a port that tunnels to a live service.

Never the production service, never another person's WIRK. The importer never writes to GitHub; only the seed tool writes, and only to the seed repositories.

**On the seed repositories.**
1. **Seed** once from the manifest; a second seed run creates nothing.
2. **Dry runs.** `wirk import github wirkspace --dry-run` selects only `import-seed-public` among the seeds and lists the two private ones as skipped. With all three named, its census equals the manifest's counts exactly. The scratch database's receipt count is unchanged.
3. **Setup.** A scratch person applies the setup with `wirk admin --request` at a pseudo-terminal, typing the confirmation as a person would.
4. **Import, compared.** Every manifest row holds (A3, A4, A12), and the independent REST reader finds no difference (A5).
5. **Re-run:** zero writes, zero uploads (A7).
6. **Changes.** The seed tool's scripted changes, then a re-run that updates exactly the affected items and links (A8).
7. **WIRK edits, forgery, retargeting and a second importer.** Scratch persons make: two edited items; a forged item with a valid line; an imported item whose first line is edited to name another issue; and items by a second `*-github-import` principal. The dry run stops on the last, and the real run behaves as A9 and A11 state.
8. **Interruption** at the three points of A10, then re-runs.
9. **Blind trial.** A fresh agent gets only `wirk import --help`, a scratch service and the goal "bring wirkspace/import-seed-public over to WIRK". Its result must pass A14: nothing from the two private seeds appears.

**On the real organization,** read-only:

10. **Census.** Independent counts per repository with `gh api`, just before the dry run.
11. **Dry run** of the owner, then with every repository named: its census equals step 10.
12. **Import** of every named repository, timed. A3, A4 and A5 hold against the independent REST reader over every issue.
13. **Re-run,** timed: zero writes and zero uploads.
14. **A real kill.** On a second fresh scratch service, the process is killed with `SIGKILL` mid-import and run again; keys and hashes must equal step 12's.
15. **Timed with a scripted judgment.** A third fresh scratch service whose duplicate judgment is core's scripted stand-in on loopback (no text leaves the machine) answering "distinct" after a fixed delay. The full import is timed, giving the cost of one judgment per write.
16. **Coexistence.** `wirk query text='[github '` returns no imported item.

The build record in this plan keeps counts, timings and outcomes only, for both the seeds and the real organization: no titles, bodies, names, or the real organization's repository names.

## 5. Linear, second

Settled now from the Linear note and the rulings; revised against the real workspace once the read key exists.

- **Access.** A personal API key restricted to Read, saved owner-only at `<config>/import/linear-key`, sent only to `api.linear.app` and `uploads.linear.app`. Uploads are fetched with the key, with redirects only to the allowlisted storage host and without the key. Pacing follows Linear's complexity and request headers.
- **Selection.** Team keys. None means every public team. A private team only when named, with the dry run's warning (ruling 4); a wirkspace per private team is recommended. References into private teams not selected are counted, never shown (§3.9).
- **Issues** become work items with the key line `Linear issue [ENG-123] · <url>` and earlier identifiers in the header (`Previously: [OPS-45]`).
- **Status** follows the state type: triage, backlog and unstarted are `open`; started is `in_progress`; completed is `completed`; canceled and duplicate are `cancelled`. The state name goes to `workflow`.
- **Fields:** `priority` (urgent, high, medium, low), `team` (many), `label`, one field per label group, `estimate` named on each team's scale, `cycle` when small, `health`. All are created through the setup, never edited.
- **Due dates:** the end of the due day in the team's time zone, in UTC; the day stays in the header.
- **Relations:** sub-issues are `contributes_to`; blocks is `requires` or a §3.6 fallback; duplicate, related and similar are `related_to`.
- **Projects** are work items. Milestones are work items that `contribute_to` their project. Issues `contribute_to` their milestone, or else their project. Project relations are `requires` by §3.6. Project updates and documents are docs `related_to` their parent.
- **Initiatives become docs** (kind `initiative`), `related_to` the projects in them, with their status, owner, target date, health and parent initiative as header lines (§9 choice 16). They are not context: context is the wirkspace's stated direction, and its initiatives are written by its people.
- **Comments** (threads, resolved, edited, quoted text, reactions) go to the discussion doc, with their raw form on it. Issue history goes to the issue archive.
- **Archived and trashed:** imported, then archived by the importer in a second write with the reason `Archived in Linear on <date>`. The importer restores an item it archived itself when Linear restores the issue, and never touches another principal's archive.
- **Later:** customers, releases, templates and views (§9 choice 16).

| # | Complexity (Linear note §1, §7.4) | Expected | Verified |
|---|---|---|---|
| L1 | Teams, public and private; sub-teams; references into private teams | `team` field; private skipped unless named; references counted, never shown | R F |
| L2 | Workflow states of every type, the same name with two meanings | `status` by type; `workflow` by name | R F |
| L3 | Labels, groups (single and multi), retired labels, reserved names, unicode | fields when small; header always | R F |
| L4 | Priority 0–4, estimates on each scale | `priority`, `estimate` | R F |
| L5 | Cycles, many of them | `cycle` when small, header otherwise | R F |
| L6 | Due dates, daylight-saving days, coarse project target dates | `due_at` at the end of the day in the team's zone | F |
| L7 | Sub-issues five deep, across teams | `contributes_to` chains | R F C |
| L8 | Blocks, a completed dependent, a canceled blocker, a mutual block | `requires` and the fallbacks | F C |
| L9 | Duplicate, related, similar | `related_to` and the Duplicate state as `cancelled` | R F |
| L10 | Projects, milestones, project relations, updates, documents | work items, links and docs as above | R F |
| L11 | Initiatives, nesting, long content | docs `related_to` their projects; nesting as header lines | R F |
| L12 | Comments, threads, resolved, edited, reactions, forged headings | the discussion doc | R F C |
| L13 | Uploads and link cards | files with origin; link cards as header lines | R F C |
| L14 | Moved issues | found by UUID; earlier identifiers searchable | R F |
| L15 | Archived and trashed | imported and archived; restored only from the importer's own archive | R F |
| L16 | History | the issue archive | R F |
| L17 | Rate limiting (`RATELIMITED`) | waits for the reset | F |
| L18 | Re-run, change, WIRK edit, forgery, retargeting, second importer, interruption | as G34 to G40 | R C |

R is the person's own Linear workspace, read-only; its dry-run census decides which rows it really exercises. The rest stay F and C, and the report says which.

## 6. Jira, third

Settled now from the Jira note and the rulings; it needs a Jira Cloud site the person creates.

- **Access.** A scoped read-only API token. Site, login and token sit in one owner-only file, `<config>/import/jira-key`, as JSON. The login goes only to Atlassian. Requests go to `api.atlassian.com/ex/jira/<cloudId>`, attachments are fetched with `redirect=false`, and a CAPTCHA lockout (`X-Seraph-LoginReason`) stops the run.
- **Selection.** Project keys and issue keys. Issues with a security level, and comments or worklogs with a visibility restriction, are skipped unless their issue key is named, and the dry run lists them (ruling 4). This replaces the note's `--include-restricted`. Its `--resume` and `--archived` are dropped: re-running resumes, and archived issues wait for a Premium seed. Jira has no public projects, so a reference to an issue outside the selection is counted, never shown (§3.9).
- **Issues** become work items with the key line `Jira issue [SEED-123] · <url>` and previous keys in the header. ADF converts to Markdown by the note's §2.8 contract, with its property that no text character is dropped.
- **Status** follows the category: new and indeterminate are `open` and `in_progress`; done is `completed`, or `cancelled` when the resolution is in the map file's cancelled list (Won't Do and Duplicate by default). The status name goes to `workflow`.
- **Fields:** `resolution`, `issue_type`, `priority`, `project`, `component`, `sprint` (active and future only), `release` (unreleased fix versions), `story_points` when it has at most 20 values, and selects when small. Everything else is header lines; paragraph fields are body sections.
- **Comments** go to the discussion doc with their raw form. This changes the Jira note's `## Comments` section, by ruling 7. Worklogs and the changelog go to the issue archive; watchers and votes are header lines as of the import.
- **Links:** parent is `contributes_to`; Blocks is `requires` or a §3.6 fallback, its direction resolved per the note's §2.7; Relates, Duplicate, Cloners and custom types are `related_to` with Jira's phrase. Remote links are header lines.
- **Projects and boards** become one doc each, with components, versions, columns and every sprint's dates and goal.
- **Data Center** is out of scope.

| # | Complexity (Jira note §7.3) | Expected | Verified |
|---|---|---|---|
| J1 | Company- and team-managed projects; same-named fields and statuses | fields and `workflow` merged by name | S F |
| J2 | Hierarchy: epic, story, sub-task; cross-project parent; reparenting | `contributes_to` to the current parent | S F C |
| J3 | Statuses, categories, resolutions, a reopened issue | `status`, `workflow`, `resolution` | S F |
| J4 | Every custom field type, a 60-option select, a field named Owner, one named Sort | fields when small; header otherwise; reserved keys prefixed | S F |
| J5 | Labels (70), components, versions, sprints in every state | as above | S F |
| J6 | Every link type in both directions; a cycle; a link outside the selection | links and fallbacks pointing the right way; counted outside | S F C |
| J7 | Hostile ADF: every node and mark, tables with merged cells, unknown nodes | Markdown with no text dropped; unknown nodes counted | S F |
| J8 | Attachments, inline media, duplicate bytes, odd names, over the cap | files; media mapped or placeholders counted; over the cap as links | S F C |
| J9 | Comments (120 on one issue), restricted comments, internal service-desk notes | discussion doc; restricted left out unless named; internal marked | S F |
| J10 | Worklogs, changelog, watchers, votes | archive; header counts | S F |
| J11 | Security levels | skipped unless named | S F |
| J12 | Moved issue with a new key | found by ID; previous keys in the header | S F |
| J13 | 429 with `Retry-After`; a CAPTCHA lockout | waits; stops with exit 2 | F |
| J14 | Re-run, change, WIRK edit, forgery, retargeting, second importer, interruption | as G34 to G40 | S C |

S is the seed site of the Jira note's §7 (phase A, free plan). Its seeding tool writes only to that site, with its own token, once the person creates the site and authorizes the seeding.

## 7. Acceptance conditions

Each is observed by a test or a run, not inferred. GitHub first, on the seeds and the real organization; Linear and Jira repeat A3 to A14 and A19 on their sources.

1. **A1 Dry run.** The census equals independent `gh api` counts taken at the same time (the manifest's counts on the seeds). Private repositories are listed with counts and the naming command. The setup, map and token files are written in `<config>/import/`, the token owner-only. WIRK's receipt count and GitHub are unchanged.
2. **A2 Setup.** `wirk admin --request` applies the setup, rows before the token. The importer then authenticates as `<person>-github-import`, an editor in exactly that wirkspace and nowhere else.
3. **A3 Import, counted exactly.** All of these hold with zero errors:
   - the importer's principal created exactly as many items as the selection has issues plus discussion docs plus parts;
   - every selected issue is exactly one work item with exactly one issue archive;
   - every issue with comments has exactly one discussion doc (or its parts), `related_to` its work item, with exactly one comments archive;
   - no work item carries anything about comments;
   - the numbers of `current`, `created` and `updated` outcomes sum to the census.
4. **A4 State and evidence.** Statuses follow §4.2. Every completed item's reason names the closer and time, and the closing pull request or commit when GitHub gives one and its repository may be shown. `status` options are unchanged.
5. **A5 Full comparison.** An independent reader in `tests/verify/` reads GitHub's REST API, not GraphQL, and shares no code with the importer. For every imported issue it compares WIRK with GitHub:
   - title (by the cut rule), state, body (by the documented redaction, control and guard rules), labels, assignees, milestone, type and closer;
   - every comment's author, time, order and text;
   - sub-issues and blockers against the links;
   - the issue archive's timeline count against REST's timeline without comments.

   It finds zero differences. On the seeds it also checks every manifest row.
6. **A6 Lookup.** `wirk query text='GitHub issue [owner/repo#N]'` returns exactly one item, the work item, for every imported issue on the seeds and for a sample of 200 on the real organization.
7. **A7 Re-run.** No write, no upload, every object `current`, exit 0.
8. **A8 Changes.** After the seed tool's scripted changes in GitHub, a re-run updates exactly the affected work items and discussion docs, and keeps links in step. A comment alone never revises a work item.
9. **A9 WIRK wins.** An item a person edited is skipped, naming the revision and who. `--dry-run --overwrite` lists it. `--overwrite` updates it with `expect`.
10. **A10 Interruption.** Runs are killed at three points:
    - after an upload is confirmed and before its issue's write;
    - after the first part of a discussion is written and before the next;
    - in the middle of the link pass.

    Each is killed through a fault-injecting transport in the contract test on the seeds, and once with `SIGKILL` on the real organization. Each run again ends with the same keys, hashes and links as an uninterrupted run, with no duplicate, nothing missing and nothing `ambiguous`.
11. **A11 Forgery, retargeting and a second importer.**
    - An item another principal created with a valid line is never read for, written or listed.
    - An imported item whose first line a person edited to name another issue is keyed by the importer's own latest revision: its own issue reports it `skipped: changed in WIRK`, and the other issue is imported or left exactly as if the edit had not happened.
    - Items made by another `*-github-import` principal block their keys: the dry run stops with exit 2 and the explanation, and a real run reports them `blocked` and writes nothing for them.
12. **A12 Links.** Sub-issues, blocks, duplicates and every fallback of §3.6 come out as the manifest states, once each, with the exact relation text.
    - A `link_cycle` refusal falls back to `related_to`.
    - A re-run adds a missing link and removes only the importer's own stale ones, after a complete read. A person's link is never touched.
13. **A13 Duplicates.** Against a scratch service with a scripted judgment, a refused create is resent with `allow_duplicate_of` and reported.
14. **A14 Privacy.**
    - **Nothing from a withheld scope:** no name, number, key or title from a scope neither selected nor public appears anywhere in WIRK: titles, bodies, reasons, file names, descriptions, archives. This is checked by scanning everything the import wrote for the private seed repositories' names and keys, after importing the public seed alone, both in the contract test and in the blind trial.
    - No email address from any user record or commit is in any request to WIRK (sentinel test).
    - Every credential shape is redacted and counted.
    - No imported title or body contains `[github `, and no comment heading can be forged.
    - Only reads reach GitHub from the importer.
    - No token appears in output, errors or archives.
15. **A15 Fields.** Fields are created only through the setup and never edited by the importer. A vocabulary over the limit stays header-only; a key never takes a reserved or request key; missing and retired options are reported with the person's next step.
16. **A16 Exit status.** 0, 1 and 2 as in §2.3. A missing `gh` login or scope gives 2 and the command that fixes it.
17. **A17 Blind trial** (build contract step 7). A fresh agent with only `wirk import --help` completes the import of `wirkspace/import-seed-public` into a scratch service, and the result passes A14. Its friction is recorded and fixed.
18. **A18 Simplicity.** The code is within budget, and the simplify pass is recorded with what it removed.
19. **A19 Time and cost bounds**, measured on this machine against a local scratch service, each a finding to report if missed:
    - seeds: import within 10 minutes, re-run within 3;
    - real organization:
      - dry run within 15 minutes and 1,000 GraphQL points;
      - import within 60 minutes;
      - re-run within 20 minutes and 1,000 points;
    - the scripted-judgment run (§4.7 step 15): measured and reported, against the import's own time.

## 8. Build order, and the person's steps

After the root approves, each slice runs RED → GREEN → SIMPLIFY → VERIFY, one concern per commit, each pushed. A draft pull request opens after the first slice and grows. Progress is recorded in WIRK at every hand-back.

| Slice | What | Acceptance |
|---|---|---|
| G0 seeds | the manifest and the seed tool; the seed repositories filled | §4.6 |
| G1 render | provenance and key lines, header, notice, redaction, guards, reference rule, title cut, parts, hash | A14 parts, determinism |
| G2 WIRK side | index, trust, blocked keys, retargeting, decision table, writes, refusals, duplicates, receipts, files | A7, A9, A11, A13 on fakes |
| G3 GitHub reader | `gh` runner, checks, selection, GraphQL pages and nested paging, pacing, records, archives | A1 on fakes, §4.5 rows |
| G4 the command | arguments, files in `<config>/import`, token, lock, dry run, report, exit status, help | A1, A2, A16 |
| G5 links | the second pass: dedupe, own fetches, `link_cycle`, complete-read unlinking, the stale-gate rule | A12 |
| G6 attachments | references, allowlisted downloads, cap, hashed names, uploads | G27 |
| G7 verifier | the independent REST reader | A5 |
| G8 projects | §4.4 | G33 |
| VERIFY | contract tests and §4.7 runs 1–8 and 10–16, against core `main` `05770e3` | A1–A16, A19 |
| Review and blind trial | independent review, fixes, §4.7 run 9 | A17, A18 |
| L1–L3 | Linear reader, mapping, real workspace | §5 |
| J1–J4 | Jira reader, ADF converter, seed, real site | §6 |

**The person's steps,** none of which an agent can take:
1. **Before VERIFY:** `gh auth refresh -s read:project`, so the importer can read projects (G33). To let the seed tool create the seed project, `gh auth refresh -s project` instead, which includes reading. Otherwise the person makes the project by hand from the manifest.
2. **For attachments (G27):** a `gh` from September 2026 or later, which has `--attach`, or uploading the manifest's files by hand in the browser.
3. **Optional:**
   - a second GitHub account in the organization, for several assignees and a second commenter (G13);
   - a third, deleted afterwards, for `@ghost` (G14);
   - deleting one seed issue and converting one to a discussion by hand after the first import (G41).
4. **Linear:** a Read-only API key saved owner-only. **Jira:** the Cloud site, its tokens and the seeding go-ahead (§6).

## 9. Choices for the root's review

Settled here, simplest faithful option first. Where the root's rulings on revision 1 changed or conditioned a choice, the choice says so.

1. **The provenance line** holds kind, immutable ID, version and hash. The human key sits on the second line in brackets, worded differently for the work item and its discussion doc. Putting the key on the first line as well would push lines past the card's 100 characters for long GitHub names and every Linear UUID.
2. **Version** is the source update time in compact ISO form. It is informational; the hash decides.
3. **Comments** go to a separate discussion doc for every source, including Jira (ruling 7). Its first-line kind is `comments`, so Linear lines fit.
4. **Archives** (ruled): comments and their raw form stay off the work item. The issue archive is on the work item; the comments archive is on the discussion doc; the work item's header has no comment count.
5. **Status** is never edited. It maps onto the standard four by meaning, and the source's state names go to a `workflow` field. Both the Linear and Jira notes proposed adding options to `status`; that needs `field.edit` with a revision agents cannot read, and would lengthen every status call.
6. **Fields** are only created, never edited, by the importer's setup. Values missing from an existing field, and stale or retired options, stay header text and are reported with the person's next step.
7. **Field limit** of 50 options by default. The real organization's 94 labels stay header text unless the map raises it.
8. **Titles** stay the source's own. There is no `ENG-123` prefix and no `title_key` option; the key line and the report teach the exact search.
9. **GitHub's restricted unit** is a repository that is not public: skipped unless named. Agreed with the review's condition, ruling 2: references into scopes that are neither selected nor public are counted, never shown, and stripped from archives.
10. **Emails.** Email fields are never requested or written. Addresses people typed into issue text are imported as written and counted, as Granola's connector does. The stricter reading of ruling 4 would replace them with `[email removed]` in text too.
11. **Links.** Each run adds missing links and removes the importer's own links whose relation is gone or must change type, with no `--prune`. Agreed with the review's conditions: planned links are deduped, nothing is unlinked without a complete read of both ends, a `link_cycle` refusal falls back to `related_to`, and links are read by their own fetches.
12. **No manifest doc** per run. Every item carries its provenance, and the report is printed.
13. **On disk** are only the setup request, the map, the importer's token and a lock. Agreed with the review's conditions: all in `<config>/import/`, and the token named `wirk-token-<source>-import`.
14. **Output** is a summary plus the outcomes that need attention; `--json` has every outcome.
15. **The duplicate judgment** (ruled). The full import of the real organization runs on a scratch service without a judgment, so its `not_checked` notices are counted. One more full run is timed with a scripted judgment on loopback. The resend path is proven against a scripted judgment in the contract test.
16. **Linear scope** (ruled). Initiatives become docs, not context. Customers, releases, templates and views wait for a later slice.
17. **Linear archives** are mirrored by the importer on its own items. GitHub and Jira never archive, except the importer's own stale discussion parts.
18. **Jira switches.** `--include-restricted`, `--resume` and `--archived` are dropped; naming an issue key is the opt-in.
19. **GitHub projects** are built on fixtures and the seed project. Agreed with the review's condition: the person grants `read:project` before VERIFY (§8), so VERIFY covers real project data.
20. **The GitHub timeline** is read through GraphQL, with one fragment per event type: about 300 points a run for the real organization, against the REST timeline's call per issue on every run. Agreed with the review's conditions: the run is bounded at 1,000 points, and the archive's timeline counts are checked against the independent REST reader (A5).
21. **Discussion parts** (agreed with the review's condition) are cut by the encoded bytes of the write that carries them, at 2 MiB, and written one to a write.
22. **Owners** come from the map only, with no automatic matching, because agents have no member list.
23. **A second importer** (ruled): another `*-<source>-import` principal's items block their keys, and the dry run stops and explains.
24. **Retargeting** (ruled): an item someone else revised last is keyed by the importer's own latest revision.

## 10. Risks and unverified points

- **Private attachments.** Downloading them through `bodyHTML`'s signed URLs is documented loosely. The seed's attachments prove it or refute it, once the person enables them (§8); otherwise they stay links.
- **Unknown source behavior.** Whether `fullDatabaseId` survives a transfer (the seed's transfer measures it), and which changes move `updatedAt`. The hash makes the second harmless.
- **GitHub limits.** GraphQL's 10-second limit with every nested connection; pages are retried at half size. Whether GitHub allows a cycle of blockers is unknown; the seed records it.
- **The index and big runs.** About 45 list pages a run for the real organization, plus a `depth=all` fetch for each item someone else revised last; time is measured (A19). The duplicate judgment's latency on real data is estimated only through the scripted run.
- **Recent changes.** For 30 days `status`'s Recent changes and sorting by change mostly show the import.
- **Authorship** survives as text only. Names enter a store that keeps every revision, with no erasure path yet (Jira note §9, question 4).
- **Long runs.** A large organization's first import can take hours; re-runs read everything again. A source cache is the measured addition if reading proves to be the bottleneck.
- **The CLI release** that carries the command follows the review. The site's CLI page and the skill learn `wirk import` afterwards, in their own repositories.

## 11. Why a command, not careful use of `wirk write`

AGENTS.md asks for the case behind any interface addition. The person asked for this command and authorized it (decision 74).

An agent could port a backlog with `wirk write` alone, as an earlier rewrite between WIRK services did (decision 26). But a faithful port of 2,200 issues is thousands of identical mechanical decisions: the same provenance line, hash, index, fallbacks, file pipeline, privacy rules and pacing every time. Done by hand, it burns an agent's context, differs between teams and cannot be re-run safely.

The command adds no server operation, argument or default, and no MCP tool. It is a client of `/v2/status`, `/v2/query`, `/v2/write`, `/v2/files` and, through the person, `/v2/admin`, as documented. Considered and not proposed, each needing that discussion first: lookup by external key, backdated authors and times, an import mode for the duplicate judgment, number and date fields, and an erasure path for imported names.

— importer-implementer
