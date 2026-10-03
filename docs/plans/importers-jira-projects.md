# Jira slice 2, projects and boards: plan

Plan only, for adversarial review before any code. Branch `importers-jira-projects`, from main `e930b04`. It builds on slice 1 (`docs/plans/importers.md` §6 and §6.1) and on the Jira research note (§2.1, §2.4, §2.5, §4.7). Every answer in the tests stays synthetic until a Jira Cloud site exists.

— importer-implementer

## 1. What becomes what

Slice 1 already imports the parts of Jira that issues carry. This slice adds only what has no home yet. Each choice follows the nearest equivalent in GitHub or Linear.

| Jira | WIRK | Why, by the other importers |
|---|---|---|
| Project | The `project` field on its issues (slice 1), and **one doc per selected project** (kind `project`) | A GitHub repository and a Linear team are fields. A Jira project also holds components and versions with leads, dates and descriptions. Those belong in one place, not repeated on every issue |
| Board | **One doc per board** that passes §3 (kind `board`), `related_to` its project's doc | A GitHub project (v2) is the same kind of thing, a view over issues with columns and iterations, and it becomes one doc |
| Sprint | The `sprint` field (slice 1: active and future sprints only), and one line on its board's doc: name, state, dates and goal | Linear cycles are a field. GitHub iterations are lines on their project doc |
| Version (release) | The `release` field (slice 1: unreleased fix versions only) and header names. One line on the project doc: name, start, release date, released or archived, and description | A GitHub milestone is a field. Its dates sit in one doc, so a slipped release date revises one doc, not every issue in the release |
| Component | The `component` field (slice 1), and one line on the project doc: name, lead and description | As labels, with the lead a team asks |
| Epic | Already an issue (work item), with children that `contribute_to` it (slice 1). Nothing new | Linear's and GitHub's parents |

**What is not linked.** Issues are not linked to project or board docs. The field already groups them, and thousands of links to one doc would bury the real relations. A board doc gets one `related_to`, to its project's doc, as Linear's documents and updates relate to their parent. No new hierarchy: items and links stay primary.

**Titles and keys.** The title is the name Jira shows (`Seed project`, `Rocket board`). The key line is `Jira project [SEED] · <url>` or `Jira board [Rocket board (12)] · <url>`. The archive is `jira-project-<id>.json` or `jira-board-<id>.json`.

## 2. What a team needs on day one, and what is cut

**Kept**, because a team asks for it in the first week:
- the project's description and lead;
- its components with their leads;
- its versions with their dates and released state;
- each board's type, its columns and the statuses in them, and its estimation field;
- every sprint's dates, state and goal. Sprint goals are context agents can use.

**Cut**, as churn or as faithfulness with no use:
- the statuses, issue types and link types a project uses, which the fields already show;
- the board's filter JQL, which can name projects and people outside the selection;
- issue counts per board, sprint or version, and the version's `overdue` flag. Both change daily without anything changing in Jira;
- the localized `userStartDate` and `userReleaseDate`;
- avatars, project categories, workflows, schemes, sprint reports and velocity.

## 3. Privacy: fail closed, as slice 1 does

- **Projects** follow slice 1's selection unchanged: the projects named, or every project the token browses. Project docs exist only for selected projects. Archived and trashed projects are not listed by `project/search`, so they get no doc.
- **Restricted and open projects.** Telling them apart needs each project's permission scheme. Reading a scheme needs project-administrator rights that a read token should not have. A fail-closed default would then skip every project, so this slice does not change the selection default (§5).
- **Leads and emails.** The project, component and version reads request no email. Leads are written by display name, and the archives drop every `emailAddress` and avatar, as slice 1's archives do.
- **A board is imported only when all three hold;** otherwise it is skipped and counted, never named:
  1. Its location is a selected project.
  2. Every project it reads (`/board/{id}/project`) is selected.
  3. Its filter is shared with the whole site, or with a selected project without a role (`/filter/{id}` `sharePermissions`).

  The census reads `boards: 2 imported · 1 skipped (reads a project not selected) · 1 skipped (filter not shared with the site)`.
- **Private boards are never requested.** The board list is read without `includePrivate`, so Jira leaves them out.
- **No scope, no boards.** A token without the board and sprint read scopes still imports issues and project docs. The census then notes `boards not read: this token lacks read:board-scope:jira-software and read:sprint:jira-software`, as GitHub's report does without `read:project`. The key file's hint names those scopes.
- **Text** (descriptions and goals) is source content, cleaned as issue text is: credentials redacted and heading lines escaped. A key of an issue outside the selection inside such text is written as typed, as in issue descriptions. In the archives it is withheld by slice 1's rule.

## 4. Refreshing

- **Provenance and hash.** `Jira project 10000, version <read time>, hash <12 hex>`, and the same for a board. Jira keeps no update time for either, so the version is the time of the read. The hash decides, so an unchanged doc is never rewritten.
- **No churn.** A re-run on an unchanged site writes nothing. What a doc holds changes only when Jira's configuration does:
  - a renamed component;
  - a moved release date;
  - a sprint started, closed, added or deleted;
  - a column changed.
- **Deleted in Jira.**
  - A board or project that the token no longer sees anywhere is reported `missing` and is not archived, as with a missing issue or Linear object (choice 17).
  - A board that is still listed but now fails §3 is reported `skipped` and is not touched.
  - A deleted sprint leaves its board doc, which is revised.
  - The sprint's option stays in the `sprint` field (the importer never edits fields), and issues drop the value through their own updates.
- **Others' edits** follow the shared rules: a doc someone else revised is skipped unless `--overwrite`.

## 5. No interface change

Only what `wirk import jira` does changes. There is no new flag, argument, selection word or default: boards come with their projects. Two things would need one, and are **not done; they are for a discussion with Samuel**:
1. **Skipping restricted projects by default** (§3) would change the selection default, and it needs administrator reads.
2. **Importing a restricted board on purpose** would need a way to name a board, which is a new selection word.

## 6. Size

- **Shared code stays at 1,260 lines** (decision 82). Docs of any kind, their key lines, `related_to` by `of`, archives per kind, and `missing` for objects the source no longer shows all exist from Linear's slice. If the build finds a shared change unavoidable, it comes with a matching cut or a recorded reason.
- **`jira.py`** grows by about 100 lines (80–120), from 385 to about 485:
  - reading components, versions, boards, configurations, board projects, filters and sprints: about 35;
  - the project doc: about 25;
  - the board doc: about 25;
  - the census lines and the missing-scope note: about 10;
  - kinds, docs and `seen`: about 5.

## 7. Acceptance

**On fixtures.** `FakeJira` gains components, versions, boards, configurations, board projects, filters and sprints. Each condition below is a test written RED first:
1. One project doc per selected project, with:
   - the lead and component leads by display name;
   - versions with their dates and state;
   - no email anywhere, though the fixture's leads carry emails.
2. A board in a selected project becomes a doc, `related_to` its project's doc, with columns and every sprint's dates, state and goal.
3. A board that reads an unselected project is skipped and counted, and so is a board whose filter is shared only with a group. No request asks for `includePrivate`.
4. A token whose board reads are refused imports issues and project docs, and notes the missing scopes.
5. A re-run writes nothing, even when issue counts, `overdue` and `userReleaseDate` change.
6. A started sprint and a moved release date each revise one doc and nothing else.
7. A deleted board is reported `missing` and stays unarchived. A deleted sprint revises its board doc.
8. Every request is a GET, apart from slice 1's two read POSTs.
9. The contract test against a scratch core at `60b84ae`:
   - the docs created with their `related_to`;
   - a board left out by §3;
   - a re-run that writes nothing.
10. Shared code is 1,260 lines. The golden recorder of the trim, run from scratch, shows no difference in any GitHub or Linear recording.

**On a real site** (S), added to J1–J14:

| # | Complexity | Expected |
|---|---|---|
| J15 | Company- and team-managed projects; components with and without leads; versions released, archived and overdue | one doc each; dates as Jira states them; no email |
| J16 | Scrum and kanban boards; a board over two projects with one unselected; a private board; a filter shared with one group | docs for the first two kinds; the rest skipped and counted |
| J17 | Sprints future, active and closed, with goals; a sprint and a board deleted between runs | board docs revised; the deleted board `missing` |
| J18 | A token without the board and sprint scopes | issues and project docs imported; the scopes noted |
| J19 | Real cost of the reads per board, and whether `sharePermissions` is readable with a read token | measured; if it is not readable, every board fails closed and the plan comes back for review |
