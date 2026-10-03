# Trimming the importers' shared code: plan

Plan only, for adversarial review before any code. Branch `importers-trim`, from main `f1cc8e5`. The shared code is `src/wirk_cli/importers/__init__.py`, `render.py` and `wirk.py`: 1,279 lines (279, 385 and 615) against decision 81's budget of about 1,120. The adapters are outside the budget (GitHub 485 against 490; Linear 530; Jira 375 and `adf.py` 169).

This is the second version, revised after the plan review of 3 October and approved on recheck with the additions marked in §2.2 and §2.3. The proof now comes first, the cuts are smaller, and no budget is fixed in advance (§6). Line numbers are at `f1cc8e5`.

— importer-implementer

## 1. Where the 1,279 lines are

| Module | Lines | Largest parts |
|---|---|---|
| `__init__.py` | 279 | `Run.report` 66 · `Run.go` 35 · `Run.connect` 32 · `Run.write_setup` 26 · `Run.__init__` 15 · `read_map` 13 · `main`, `problem`, `errors_of`, `tell` 26 · the docstring, imports, constants and blank lines between definitions 39 |
| `render.py` | 385 | the docstring, imports, regexes, constants and blank lines between definitions 124 (the credential shapes alone take 11) · data types 100 (`Record` 23, `Clean` 17, `Census` 11, `Context` 11) · `discussion` 41 · `work_item` 27 · field planning 29 · `archive` 16 · `refs` and `relation_lines` 15 |
| `wirk.py` | 615 | `Importer` 434: `plan_links` 38, `send` 33, `one` 28, `fix` 26, `files_for` 23, `operations` 22, `stale` 22, `write_links` 19, `gone` and `missing_one` 18, `fields_of` 17, `mirror` 17 · `Wirk` client 66 · `Index` 43 · small types 32 · the rest of the module 37 |

**How it grew past 1,120.** At the GitHub review the shared code was 1,156 lines, about 20 of them dead. Decision 81 set about 1,120 once that dead code was gone.
- The review's fixes, less the dead code, made it 1,199 (`0f4f1cf`).
- The file-storage check, the error summary and the setup wording made it 1,225 (`446e35e`, release 0.4.0), all in `__init__.py`.
- **Linear added 50** (`c97435d`, `10cde17`), making 1,275:
  - `render` +21: the census moved in from the GitHub adapter, comment marks, the archive reason, `kinds`/`docs`/`seen` on the context, and new relation names;
  - `wirk` +26: writing any kind of object, the archive mirror, missing reports for kinds other than issues, and escaped keys;
  - `__init__` +3: the adapter registry and wording per source.
- **Jira added 4**, making 1,279: the map file's `names` and `cancelled`. The history walk (#9) changed only `jira.py`.

## 2. Proof first

### 2.1 Baselines at `f1cc8e5`

- The full suite, `python -m pytest -q`: **437 passed, 20 skipped**.
- The importer tests, `python -m pytest -q tests/test_import_*.py tests/test_seed_tool.py`: **177 passed, 4 skipped**. The first version's 175/4 at `53dc930` was this same command.
- The contract tests against a scratch core at `60b84ae` are run before the first cut, and their count goes in the pull request.

### 2.2 Characterization tests, committed first

The cuts touch lines that no test runs today. Before any cut, one commit adds ordinary tests (in `tests/test_import_wirk.py` and `tests/test_import_command.py`) that pass on main unchanged and stay as lasting coverage. A line trace of the full suite at `f1cc8e5` never reaches `wirk.py` 152, 331, 335, 441, 523, 555–559, 577, 579–580 and 582, or `__init__.py` 253, 267 and 277. The tests cover these whole ranges:

| Lines | Function | Scenario the tests pin down |
|---|---|---|
| `wirk.py` 143–155 | `Index.trust` | the importer's own item under a provenance line someone else wrote, where its own latest revision has no line of this source (152): neither held nor counted as forged; also another importer's key blocked and a person's forged line counted |
| `wirk.py` 214–217 · `__init__.py` 253 | `attempt` · `report` | an owner WIRK refuses: the issue is written without them, its counts once, and the `owners:` line names them |
| `wirk.py` 328–335 | `write_links` | a link refused for another reason (331); refused more times than it has operations (335); a cycle kept as related, with its note |
| `wirk.py` 441 | `mirror` | WIRK refuses the archive or restore: an error outcome with WIRK's code and words |
| `wirk.py` 514–523 | `send` | four fixable refusals in a row (523); a completion gated by prerequisites; a refusal `fix` cannot handle |
| `wirk.py` 537–565 | `fix`, `key_of` | a field option removed during the run, on create and on edit (555–559); a likely duplicate naming this run's key and a WIRK short ID; a duplicate of the issue itself |
| `wirk.py` 573–589 | `stale` | a discussion that shrinks, with a part already archived (577), a part a person changed (579–580), a dry run (582) and a refused archive |
| `wirk.py` 599–609 | `gone`, `missing_one` | an issue gone from a complete read; one outside a narrower selection; comment parts and objects the source still shows |
| `__init__.py` 267, 275, 277 | `report` | a forged line reported; more than 50 outcomes that need attention, cut at 50 with the `more:` line |
| `__init__.py` 127–129 (recheck) | `Run.connect` | the agent's status refused, in text and `--json`: stderr keeps WIRK's hint as the fix line, and the JSON code stays `import_stopped` |
| `__init__.py` 170–176 (recheck) | `check_statuses` | a status the wirkspace lacks, mapped to one it has in the map file: the check passes |
| `__init__.py` 185, 216 (recheck) | `write_setup`, `report` | a map whose `field_limit` makes one field header-only: left out of the setup, named on the `header only:` line and in the JSON `header_only` |
| `__init__.py` 270–273 (recheck) | `report` | a run with no outcomes, and a run whose outcomes include no issue: no `find one:` line |

From this commit on, both test counts above rise by the number of tests it adds, and every later commit keeps them exactly.

### 2.3 The golden recorder, run from scratch

The recorder lives outside the repository and is never committed, nor are its recordings. It is a pytest plugin over the importer command in §2.1, plus a scenario script. The pull request carries the evidence: the commands, how many tests and scenario steps were recorded, and an empty diff after each cut.

**What it runs.**
- Every existing fake-WIRK test, and the characterization tests of §2.2.
- Scenarios through `wirk import` for each source fake (GitHub, Linear, Jira, and the sources the Linear and Jira contract tests build), with the source changed between runs. Each scenario has a dry run, the setup applied as a person, an import and a re-run. Then come an edit (title, body, a field and a comment), fewer comments (a two-part discussion down to one), an archive then a restore (Linear, the source that archives), and a deletion from a complete read, each followed by a run. The dry run and one later run also run with `--json`; one run names `workspace_id=`; the Jira map names its own `cancelled`.

**What it records**, per test and per run:
- stdout, stderr and the exit status;
- the dry run's setup request, as parsed JSON, and its map template, as bytes;
- every request WIRK receives, in order: the route and the body as parsed JSON, so write bodies compare as parsed JSON;
- every request each source fake receives;
- at the end, every item: title, body bytes, fields, work, archived state, links, and file names with SHA-256. The `hash` on each first line is compared as it is.

**Normalization.** Every minted ID is replaced by its kind and the order of its first appearance, wherever it appears. This covers item, link, upload and file IDs and their 8-character short forms in outcome lines and reasons, the random tail of every request ID including the setup request's, the importer's token and its SHA-256 digest, and the fake's timestamps. Only the random part is replaced, so a changed ID format still shows in the diff.

**Proof that it catches changes.** Before the first cut, one mutation is seeded in a scratch copy for each function the cuts touch. The recorder's diff against the baseline must be non-empty for each; the mutation is then dropped.

| Function | Seeded mutation | Where the diff shows |
|---|---|---|
| `Run.write_setup` | the field limit taken as 1 | setup request, stdout |
| `Run.report` | the `find one:` line left out | stdout |
| `Run.connect` | `workspace_id` left out of the second status request | WIRK requests |
| `Run.connect` (recheck) | WIRK's hint left out of the stop when the agent's status is refused | stderr |
| `Run.check_statuses` (recheck) | the map's statuses ignored by the check | exit status, stderr |
| `Run.go`, `Jira.check` | the map's `cancelled` ignored | item fields and bodies |
| `Importer.start`, `operations` | `status` left out of the managed fields | write bodies |
| `Importer.links_of` | archived targets kept | write bodies |
| `Importer.write_links`, `rid` | the request ID without its source prefix | write bodies |
| `Importer.seal` | a part's version taken from its earliest comment, not its latest | item bodies |
| `Importer.stale` (recheck: one row each) | `expect` left out of a stale part's archive write | write bodies |
| `Importer.mirror` | the source's archive reason replaced by the restore wording | write bodies |
| `Importer.one`, `send` | an outcome's item left out | stdout |
| `Wirk.item` | `missing_one` reading the card, not the full body | stdout |

## 3. Cuts, one commit each

Estimates are in lines of the shared code. Each commit is followed by the importer command, the full suite and the recorder's diff, and is pushed.

| Order | Cut | Shared lines | What changes |
|---|---|---|---|
| 1 | **#1** Single-use helpers | about −9 | inline `render.small` into `write_setup` (−4) and `Wirk.status` into `Importer.start` (−3); `managed` becomes an attribute set in `start` (−2). `key_of` and `render.sealed` stay |
| 2 | **#6 + `scoped`** `connect` asks through `wirk.Wirk` | about −4 (3–5) | both status requests go through `Wirk.post`, which already scopes them, so `Run.scoped` goes; the second answer is checked inline; `go` passes the `Wirk` it gets. A refused agent status still raises the same `Stop` with WIRK's hint, never a `WirkError` |
| 3 | **`check_statuses`** reads `importer.statuses` | about −1 | the same map, built by the same expression from the same `self.mapped`; only its values are used, as a set |
| 4 | **`field_limit`** read once | about −1 | read in `go`, used by `write_setup` and `report` |
| 5 | **The `if outcomes:` guard** in `report` | about −1 | it adds nothing to `next(…, None)` |
| 6 | **`links_of`** in one pass | about −1 | the targets found and filtered in one comprehension, in the same order |
| 7 | **#5** The map passed to `check(mapped)` | about −2 shared, +1 in `jira.py` | `go` stops setting `adapter.cancelled`; Jira's `check` takes it from the map. No new adapter hooks: a relocation, not a saving. GitHub's and Linear's `check` take the map and ignore it. Tests that call `check()` pass a map; no assertion changes |
| 8 | **#3** One `Outcome` builder for `one` and `send` | about −1 (0 to −2) | one place builds an outcome from a plan, its word, its message and the held item |
| 9 | **#2** One archive-or-restore body for `stale` and `mirror` | about 0 (+1 to −1) | only the body is shared: request ID, reason, `expect` and operations. Each keeps its own batching, reason and outcomes |
| 10 | One request-ID format | 0 | `rid` takes an ID, so `write_links` uses it too |
| 11 | The comment version on `Rendered` | 0 | `discussion` computes it once and `seal` reads it, rather than computing it again |
| 12 | Two unused defaults | 0 | `Wirk.item`'s `depth` and `send`'s `said`: every caller passes them |

**Total: about −20 shared (−17 to −23), so about 1,259 lines.** The first version's sum was wrong (1,279 − 38 is 1,241, not 1,235). Its estimates for #2, #4 and #7 were too high.

**Considered and not taken:**
- **#4, one text for a WIRK refusal.** A helper costs about two lines while every call site stays one line.
- **#7, report lines computed once.** The JSON branch returns before any text line is built, so there is nothing to share.
- **Reusing `connect`'s status in `start`.** It asks with a 1,024-byte limit, and `start` needs the whole field list. `Wirk.status` is inlined instead.
- **Merging `render.Rendered` into `wirk.Plan`.** It blurs what was rendered with what will be written, and touches every sealing path.
- **Sharing a loop between `send` and `write_links`.** Their refusal handling differs on purpose: an issue's write is fixed and resent, while a link falls back to `related_to`.
- **Shortening docstrings, constants or wording, or packing statements onto longer lines.** That removes clarity, changes output or games the count.

## 4. What must not change

- The provenance first line and its hash. Every archive's bytes, and so every content hash, stay as they are for GitHub, Linear and Jira.
- Refreshing only items whose latest revision is the importer's. Retargeting keyed by the importer's own revision. Forged lines ignored, and another importer's keys blocked.
- Withheld private and restricted content, wherever it would appear: headers, links, archives, history and file names.
- One issue per write; the `requires` → `related_to` fallback with its notes; links never made to items archived in WIRK.
- Heading escaping in comments and ADF; credential redaction; marker guards.
- Every request WIRK and the sources receive, in order, compared as parsed JSON.
- The dry run's setup request and map template.
- stdout, stderr and the exit status of every run; every report line and JSON field; the help text.
- Every test, apart from the `check()` calls in #5.

## 5. Acceptance

1. **Line counts.** `wc -l` for each module before and after each cut, in the pull request. No touched line grows past 141 characters (stricter than today's longest, 148 at `render.py`:195), and no two statements share a line.
2. **Tests.** The counts of §2.1, plus the characterization tests, unchanged after every commit. The contract tests against the scratch core give the same count before and after.
3. **The recorder.** An empty diff after every cut against the baseline recorded at the characterization commit, and a non-empty diff for every seeded mutation (§2.3).
4. **Real core, GitHub.** The three GitHub seeds are imported into a fresh scratch core at `60b84ae`, before and after. Every item's first line and hash, and every link, must be equal, and the REST verifier must show zero differences.

One pull request carries the commits in the order of §3, with the evidence for each.

## 6. Budget

No number is fixed in advance. The budget for three sources becomes the count measured after the trim, which the coordinator records as decision 82. The estimates in §3 put it at about 1,259. Each later source argues its own shared additions in its plan.
