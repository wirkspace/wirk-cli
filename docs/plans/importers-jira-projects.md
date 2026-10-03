# Jira slice 2, project docs: plan

Plan only, for adversarial review before any code. Branch `importers-jira-projects`, from main `e930b04`. It builds on slice 1 (`docs/plans/importers.md` §6 and §6.1) and on the Jira research note (§2.1, §2.4, §2.5, §4.7). Every answer in the tests stays synthetic until a Jira Cloud site exists.

This is the second version, revised after the plan review of 3 October and approved on recheck with the conditions folded in below (§4, §5, §6.1, §8):
- boards are out of this slice;
- project docs come only from projects selected as projects;
- archives keep an allowlist;
- a failed read skips one doc and never stops the run.

— importer-implementer

## Release gate: settle restricted projects first (§6.1)

**Jira is not released until Samuel rules on §6.1.** Slice 1's default selection, used when no project is named, already includes every project the token browses, restricted ones too. Project docs add each project's description, lead, components, versions and sprints to what a default run shows. Until that ruling, the importer stays unreleased, as slice 1 is.

## 1. What becomes what

A Jira project is a container, like a GitHub repository or a Linear team. Those are fields on their issues, with no links to them, and so is a Jira project (slice 1's `project` field). This slice adds one doc per project: Jira keeps versions, components and sprint goals at the project's level, and slice 1 has no place for their dates, leads and goals except every issue's header.

| Jira | WIRK | Precedent |
|---|---|---|
| Project | The `project` field (slice 1), and **one doc per project selected as a project** (kind `project`). Issues are not linked to it | GitHub repositories and Linear teams: fields, no links |
| Sprint | The `sprint` field (slice 1: active and future only), and one line on the project doc | Linear cycles: a field |
| Version (release) | The `release` field (slice 1: unreleased fix versions only), and one line on the project doc | GitHub milestones: a field |
| Component | The `component` field (slice 1), and one line on the project doc | Labels: a field |
| Epic | An issue already, whose children `contribute_to` it (slice 1). Nothing new | Linear's and GitHub's parents |
| Board | **Not in this slice.** It waits in the backlog until a team asks for boards and Samuel has ruled on §6.1 | — |

**A project selected as a project** is one in slice 1's `wanted`: one named, or every project the token browses when none is named. The project of an issue named on its own (`wirk import jira ENG SEC-5`) never gets a doc.

**The doc.**
- Title: the project's name.
- Key line: `Jira project [SEED] · <url>`.
- Archive: `jira-project-<id>.json`.
- It holds, and nothing else:
  - the description;
  - the lead's display name, never an email;
  - each component with its lead's display name;
  - each version with its name, its state (released, unreleased or archived), its start date and its release date;
  - each sprint that holds an imported issue of the project, with its name, state, start, end and completion dates, and its goal.
- The sprints come from the sprint field of the issues slice 1 already reads. A sprint shared by two projects appears on both docs.

**Reads.** `project/search` gains `expand=description,lead`. Components come from `/project/{id}/components` and versions from `/project/{id}/version` (paged), one each per project selected as a project. All of this is under `read:jira-work`, which the token already has. There are no new scopes, no change to the key-file hint, and no board, filter or configuration reads.

## 2. What a team needs, and what is cut

**Kept:** who leads the project and its components, what each release is and when it is due, and what each sprint aimed at and when it ran. A team asks these in its first week.

**Cut**, as churn or as faithfulness with no use:
- boards, columns and filters;
- version descriptions and component descriptions;
- the statuses, issue types and link types a project uses, which the fields already show;
- issue counts;
- the version's `overdue` flag, and the localized `userStartDate` and `userReleaseDate`;
- avatars, categories, workflows, schemes and anything else that depends on who is viewing.

## 3. Privacy: fail closed, as slice 1 does

- **Docs only for projects selected as projects (§1).** A named issue never brings its project's details along.
- **Restricted projects** follow slice 1's selection unchanged, which is the release gate's question (§6.1).
- **No email is requested or written.** Leads are written by display name only.
- **Sprints come only from the issues kept,** after restricted issues are dropped. A sprint that holds only a skipped restricted issue never appears.
- **Each archive is an allowlist:** the fields the doc shows, plus IDs. The project archive holds:
  - from the project: `id`, `key`, `name`, `description`, and the lead's `accountId` and `displayName`;
  - from each component: `id`, `name`, and the lead's `accountId` and `displayName`;
  - from each version: `id`, `name`, `released`, `archived`, `startDate` and `releaseDate`;
  - from each sprint: `id`, `name`, `state`, `startDate`, `endDate`, `completeDate` and `goal`.

  Everything else is never written: emails, avatars, `self` URLs, `favourite`, `permissions`, `insight`, `issueCount`, `overdue`, `userStartDate` and `userReleaseDate`.
- **Text** (the description and sprint goals) is source content, cleaned as issue text is: credentials redacted, heading lines escaped.
- **Keys typed in text.** A key of an issue outside the selection, typed in the text, stays as typed in the doc and in its archive. This matches issue descriptions: slice 1 withholds reference objects, never keys typed in text.

## 4. Reads never stop the run

A read of a project's components or versions can fail. That project then gets no doc this run, and the report counts it (`project docs: 3 read · 1 skipped, its components or versions could not be read: OPS`). An earlier doc stays as it is.

The skip applies only to a 403, a 404, another 4xx, or no answer after the retries. A CAPTCHA lockout and a 401 (a dead token) still stop the run with exit status 2, and a 429 still waits. `Jira.call` tells them apart: the skippable failures raise `Refused`, a kind of `Stop`, so everywhere else they stop the run exactly as today. The project reads catch only `Refused`, never every `Stop` the way `download` does.

## 5. Refreshing

- **Provenance and hash.** The first line is `Jira project 10000, version <read time>, hash <12 hex>`. Jira keeps no update time for a project, so the version is the time of the read, from a clock the tests inject. The hash covers the body and the archive. Both are built from the allowlist, so an unchanged project is never rewritten.
- **No churn.** A re-run writes nothing when only viewer-dependent or volatile fields change. Sprints, components and versions are sorted by ID, and each sprint keeps one snapshot per ID, so issues moving between sprints change nothing the doc shows. A doc is revised only when something it shows changes:
  - a renamed component or a new lead;
  - a release date moved or a version released;
  - a sprint started, closed, added or given a new goal.
- **A narrower selection** on a later run reports no earlier project `missing`. Every project the token browses is "seen", as Linear's objects are.
- **A project the token no longer browses,** deleted or archived in Jira, is reported `missing` and is not archived (choice 17). Linear, by contrast, mirrors its archives.
- **A deleted sprint** leaves the docs of the projects that held it, which are revised. Its option stays in the `sprint` field (the importer never edits fields), and issues drop the value through their own updates.
- **Others' edits** follow the shared rules: a doc someone else revised is skipped unless `--overwrite`.

## 6. No interface change

Only what `wirk import jira` does changes: no new flag, argument, selection word, default or scope.

### 6.1 Open for Samuel: restricted projects (§5.1 of the first version)

Should restricted projects be left out by default? This is the release gate. Samuel's options:
1. **Keep the default** (every project the token browses), and have the dry run warn that each one will be readable by everyone in the wirkspace.
2. **Require projects to be named.** This changes a default, so it needs the interface discussion.
3. **Skip restricted projects.** This needs each project's permission scheme, and reading a scheme takes project-administrator rights.

`isPrivate` on a project cannot tell them apart: it is computed for the viewer, so it says only what this token sees.

## 7. Size

- **Shared code stays at 1,260 lines** (decision 82). Docs of any kind, their key lines, archives per kind, and `missing` for objects the source no longer shows already exist from Linear's slice. If the build finds a shared change unavoidable, it comes with a matching cut or a recorded reason.
- **`jira.py`** grows by about 50 lines (40–65), from 385 to about 435:
  - the component and version reads, with the skip on failure: about 12;
  - the project doc and its allowlisted archive: about 22;
  - sprints gathered from the issues: about 6;
  - the clock, kinds, docs and `seen`: about 5;
  - the census line: about 3.

## 8. Acceptance

**On fixtures.** `FakeJira` gains descriptions and leads (with emails, so their absence can be tested), components and versions. Each condition below is a test written RED first:
1. One doc for each project selected as a project, with every line of §1. No email appears anywhere, though the fixture's leads carry them.
2. `wirk import jira ENG SEC-5` writes ENG's doc and no doc for SEC.
3. Each archive holds only the allowlist of §3.
4. A re-run writes nothing, even when `overdue`, `userStartDate`, `userReleaseDate`, `issueCount`, `favourite` and the read time all change, and when issues move between sprints while nothing the doc shows changes.
5. A moved release date or a started sprint revises that project's doc and nothing else.
6. A project whose components or versions answer 403 or 404, or 500 after the retries, gets no doc and is counted, and the run goes on. Its earlier doc is untouched. A CAPTCHA lockout and a 401 during a components read each stop the run with exit status 2.
6a. A sprint that holds only a skipped restricted issue appears on no doc.
7. A narrower selection reports no earlier project `missing`. A project the token no longer browses is reported `missing` and stays unarchived.
8. Every request is a GET, apart from slice 1's two read POSTs.
9. The contract test against a scratch core at `60b84ae`: the docs created, a re-run that writes nothing.
10. Shared code is 1,260 lines. The trim's golden recorder, run from scratch, shows no difference in any GitHub or Linear recording.

**On a real site** (S), added to J1–J14:

| # | Complexity | Expected |
|---|---|---|
| J15 | Company- and team-managed projects; components with and without leads; versions released, archived and overdue | one doc each; dates as Jira states them; no email |
| J16 | The sprint field on issues: every sprint with its `goal` and dates (also part of J5) | sprints on the project doc match the board's own view |
| J17 | A narrower selection, and a project archived between runs | no `missing` for the first; `missing` for the second |
