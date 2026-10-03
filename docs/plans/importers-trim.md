# Trimming the importers' shared code: plan

Plan only, for adversarial review before any code. Branch `importers-trim`, from main `53dc930`. The shared code is `src/wirk_cli/importers/__init__.py`, `render.py` and `wirk.py`: 1,279 lines against decision 81's budget of about 1,120. The adapters are outside the budget (GitHub 485 against 490; Linear 530; Jira 375 and `adf.py` 169).

— importer-implementer

## 1. Where the 1,279 lines are

| Module | Lines | Largest parts |
|---|---|---|
| `__init__.py` | 279 | `Run.report` 66 · `Run.go` 35 · `Run.connect` 32 · `Run.write_setup` 26 · `Run.__init__` 15 · `read_map` 13 · `main`, `problem`, `errors_of`, `tell` 26 · the docstring, imports, constants and blank lines between definitions 39 |
| `render.py` | 385 | the docstring, imports, regexes, constants and blank lines between definitions 124 (the credential shapes alone take 11) · data types 100 (`Record` 23, `Clean` 17, `Census` 11, `Context` 11) · `discussion` 41 · `work_item` 27 · field planning 29 · `archive` 16 · `refs` and `relation_lines` 15 |
| `wirk.py` | 615 | `Importer` 434: `plan_links` 38, `send` 33, `one` 28, `fix` 26, `files_for` 23, `operations` 22, `stale` 22, `write_links` 19, `gone` and `missing_one` 18, `fields_of` 17, `mirror` 17 · `Wirk` client 66 · `Index` 43 · small types 32 · the rest of the module 37 |

**How it grew past 1,120.** Decision 81 counted the shared code once the dead code was gone: 1,156 − 20 = 1,136.
- The GitHub review's fixes made it 1,199 (`0f4f1cf`).
- The file-storage check, the error summary and the setup wording made it 1,225 (`446e35e`, release 0.4.0).
- **Linear added 50** (`c97435d`, `10cde17`):
  - `render` +21: the census moved in from the GitHub adapter, comment marks, the archive reason, `kinds`/`docs`/`seen` on the context, and new relation names;
  - `wirk` +26: writing any kind of object, the archive mirror, missing reports for kinds other than issues, and escaped keys;
  - `__init__` +3: the adapter registry and wording per source.
- **Jira added 4:** the map file's `names` and `cancelled`.

## 2. Cuts, ranked by lines saved against risk

| # | Cut | Saves | Risk |
|---|---|---|---|
| 1 | Inline the helpers that have a single caller: `render.small`, `render.sealed`, `Wirk.status`, `Importer.managed`, `Importer.key_of` | ~14 | Low: same expressions, inlined |
| 2 | One archive-or-restore writer for `stale` and `mirror`: both write `item.archive` or `item.restore` with `expect`, update the index and return the same outcome shapes. `stale` keeps one write per part and its own error text | ~8 | Low-medium |
| 3 | One `Outcome` for a planned object, built in one place: `one` builds it three times and `send` once (`record.key, plan.kind, word, message, held.item if held else None`) | ~4 | Low |
| 4 | One text for a WIRK refusal (`code: message`), used by `send`, `write_links` and `mirror` | ~3 | Low |
| 5 | Adapter-specific code back into the adapter. Jira's `cancelled` is set in `go` and written into the map template in `write_setup`; one adapter hook would hold both (`configure(mapped)`, `template()`). **Depends on #9**, since it touches `jira.py` | ~3 shared (+3 in `jira.py`) | Low |
| 6 | One helper in `connect` for the two `/v2/status` calls (the agent's token, then the importer's) | ~2 | Low |
| 7 | In `report`, compute the field and count lines once for both the JSON summary and the text. The output must stay byte for byte | ~4 | Medium: tests compare strings |

**Considered and not proposed:**
- **Merging `render.Rendered` into `wirk.Plan`** (~6 lines). It blurs "what was rendered" with "what will be written" and touches every sealing path.
- **Sharing a loop between `send` and `write_links`** (~6). Their refusal handling differs on purpose: an issue's write is fixed and resent, while a link falls back to `related_to`.
- **Shortening docstrings, constants or the report's wording.** That removes clarity or changes output.
- **Packing statements onto longer lines.** That games the count.

## 3. What must not change

- The provenance first line and its hash. Every archive's bytes, and so every content hash, stay as they are for GitHub, Linear and Jira.
- Refreshing only items whose latest revision is the importer's. Retargeting keyed by the importer's own revision. Forged lines ignored, and another importer's keys blocked.
- Withheld private and restricted content, wherever it would appear: headers, links, archives, history and file names.
- One issue per write; the `requires` → `related_to` fallback with its notes; links never made to items archived in WIRK.
- Heading escaping in comments and ADF; credential redaction; marker guards.
- Every report line and JSON field, every exit status, every current test, and the help text.

## 4. Acceptance, and how it is proved

1. **Line counts.** `wc -l` for each module before and after, in the pull request.
2. **The same tests.** The same tests, with the same results:
   - without a scratch service: 435 passed, 20 skipped;
   - against a scratch core at `60b84ae`: 455 passed;
   - the importer tests: 175 passed, 4 skipped.

   These are main's numbers at `53dc930`. If #9 merges first, the base is #9's numbers.
3. **Byte-identical output from the fixtures.** A golden harness is test code under `tests/golden/`, never shipped. It runs every adapter scenario in the test suite through the importer and a fake WIRK: the GitHub, Linear and Jira fakes, plus the sources of the Linear and Jira contract tests. Each scenario gets an import and then a re-run. The harness records:
   - every item's first line (with its hash), title, body SHA-256, fields and work;
   - every uploaded file's name and SHA-256;
   - every write body, with request IDs normalized;
   - every outcome line, and the text and JSON reports.

   It writes the goldens from `53dc930` (or main after #9), then compares them with the trim branch. **The diff must be empty.**
4. **Real core, GitHub.** The three GitHub seeds are imported into a fresh scratch core at `60b84ae`, before and after. Every item's first line and hash, and every link, must be equal, and the REST verifier must show zero differences. The Linear and Jira contract tests must pass unchanged.

## 5. The number I would defend

**About 1,235 lines, not 1,120.** Cuts 1–7 take about 38 lines from 1,279.
- What remains above 1,120 is behavior that was asked for after decision 81, and none of it is dead or duplicated:
  - the review's privacy and refusal fixes;
  - the file-storage stop and the error summary;
  - three sources, with any kind of object, archive mirroring and missing reports.
- Going further means removing that behavior, removing explanation, or packing lines.
- I propose setting decision 81's budget for three sources at **1,240**, with each new source's shared additions argued in its own plan.
