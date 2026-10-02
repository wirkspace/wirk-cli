"""Fill the GitHub import seed repositories from github-manifest.json (docs/plans/importers.md §4.6).

Test code for the importer's evidence; the importer never imports it. It writes only to repositories of the manifest's
owner whose names start with import-seed-, never deletes, and paces content requests under GitHub's limits: one a second
and 480 in any hour. Every step checks GitHub first, so running it again creates only what is missing.

    python tests/seed/github.py            seed, or finish seeding
    python tests/seed/github.py --changes  apply the scripted changes for the re-run test (A8)
"""

from collections import deque
import base64
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import time

MANIFEST = Path(__file__).with_name("github-manifest.json")
PREFIX = "import-seed-"
LIMITED = re.compile(r"secondary rate limit|abuse detection|HTTP 429", re.I)
ISSUES = """query($owner: String!, $name: String!, $after: String) { repository(owner: $owner, name: $name) {
  issues(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { id number body state locked isPinned
    issueType { name } parent { id } blockedBy(first: 20) { nodes { id } } timelineItems(itemTypes: [CLOSED_EVENT]) { totalCount }
    issueFieldValues(first: 20) { totalCount }
    reactionGroups { content viewerHasReacted }
    comments(first: 100) { nodes { id body isMinimized lastEditedAt author { login } reactionGroups { content viewerHasReacted } } } } }
  pullRequests(first: 50) { nodes { number body state headRefName } } } }"""


class Refused(Exception):
    """A write outside the seed repositories; nothing was sent."""


def gh(args, stdin=None):
    done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


class Pace:
    """At most one content request a second and `per_hour` in any hour."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep, per_hour=480, gap=1.0):
        self.clock, self.sleep, self.per_hour, self.gap, self.sent = clock, sleep, per_hour, gap, deque()

    def wait(self):
        while True:
            now = self.clock()
            while self.sent and now - self.sent[0] >= 3600:
                self.sent.popleft()
            if len(self.sent) >= self.per_hour:
                self.sleep(3600 - (now - self.sent[0]))
            elif self.sent and now - self.sent[-1] < self.gap:
                self.sleep(self.gap - (now - self.sent[-1]))
            else:
                self.sent.append(now)
                return


class Seeder:
    def __init__(self, manifest: dict, run=gh, pace=None):
        self.m, self.run, self.pace = manifest, run, pace or Pace()
        self.owner, self.sentinel = manifest["owner"], manifest["sentinel"]
        self.names = {key: f"{self.owner}/{repo['name']}" for key, repo in manifest["repos"].items()}
        self.issues, self.pulls, self.nodes, self.noted = {}, {}, set(), []

    # ---------------------------------------------------------------- requests

    def ours(self, path: str, body) -> bool:
        if path == f"orgs/{self.owner}/repos":
            return str((body or {}).get("name", "")).startswith(PREFIX)
        return path.startswith(f"repos/{self.owner}/{PREFIX}")

    def rest(self, method: str, path: str, body=None):
        if method != "GET" and (method == "DELETE" or not self.ours(path, body)):
            raise Refused(f"{method} {path}")
        return self.send(["api", "-X", method, path, *(["--input", "-"] if body is not None else [])],
                         json.dumps(body) if body is not None else None, content=method != "GET")

    def graphql(self, query: str, variables: dict, write: bool = False):
        if write and not set(value for value in variables.values() if isinstance(value, str) and value[:2] in
                             ("I_", "IC", "PR", "MD")) <= self.nodes:
            raise Refused("a mutation on a node the seed did not make")
        return self.send(["api", "graphql", "--input", "-"], json.dumps({"query": query, "variables": variables}), content=write)

    def send(self, args, stdin, content: bool):
        for attempt in range(5):
            if content:
                self.pace.wait()
            code, out, err = self.run(args, stdin)
            if code == 0:
                return json.loads(out) if out.strip() else {}
            if not LIMITED.search(out + err):
                raise RuntimeError(f"gh {' '.join(args[:4])}: {(out or err).strip()[:300]}")
            self.pace.sleep(60 * 2 ** attempt)
        raise RuntimeError("GitHub kept refusing for its secondary rate limit")

    # ---------------------------------------------------------------- text

    def fake(self, size: int) -> str:
        """Credential-shaped text that is the same on every run and never a valid token (random checksums)."""
        rng = random.Random(f"fake-{size}")
        return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(size))

    def expand(self, text: str) -> str:
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n" + base64.b64encode(random.Random("pem").randbytes(96)).decode()
               + "\n-----END RSA PRIVATE KEY-----")
        text = (text.replace("{sentinel}", self.sentinel).replace("{fake_pem}", pem)
                .replace("{long_title}", ("A title that keeps going far past what WIRK keeps " * 6)[:256]))
        text = re.sub(r"\{fake(\d+)\}", lambda m: self.fake(int(m[1])), text)
        text = re.sub(r"\{n:(\w+)\}", lambda m: str(self.number(m[1])), text)
        return re.sub(r"\{key:(\w+)\}", lambda m: f"{self.names[self.repo_of(m[1])]}#{self.number(m[1])}", text)

    def number(self, seed: str) -> int:
        return (self.issues.get(seed) or self.pulls.get(seed) or {"number": 0})["number"]

    def repo_of(self, seed: str) -> str:
        found = next((x for x in self.m["issues"] + self.m["pulls"] if x["seed"] == seed), None)
        return found["repo"] if found else self.m["bulk"]["repo"]

    def commit_message(self, commit: dict) -> str:
        private = self.m["repos"][commit["repo"]]["private"]
        return self.expand(commit["message"]) + (f"\n\n{self.sentinel}" if private else "")

    # ---------------------------------------------------------------- reading what exists

    def load(self):
        for key, full in self.names.items():
            owner, name = full.split("/")
            if self.rest_or_none("GET", f"repos/{full}") is None:
                continue
            after = None
            while True:
                found = self.graphql(ISSUES, {"owner": owner, "name": name, "after": after})["data"]["repository"]
                for issue in found["issues"]["nodes"]:
                    marker = re.search(r"seed:(\w+)\s*$", issue["body"] or "")
                    if marker:
                        self.issues[marker[1]] = {**issue, "repo": key}
                        self.nodes.add(issue["id"])
                        self.nodes.update(comment["id"] for comment in issue["comments"]["nodes"])
                for pull in found["pullRequests"]["nodes"]:
                    marker = re.search(r"seed:(\w+)\s*$", pull["body"] or "")
                    if marker:
                        self.pulls[marker[1]] = {**pull, "repo": key}
                if not found["issues"]["pageInfo"]["hasNextPage"]:
                    break
                after = found["issues"]["pageInfo"]["endCursor"]

    def rest_or_none(self, method, path):
        try:
            return self.rest(method, path)
        except RuntimeError as error:
            if "Not Found" in str(error) or "404" in str(error):
                return None
            raise

    # ---------------------------------------------------------------- seeding

    def seed(self):
        self.load()
        for key, repo in self.m["repos"].items():
            self.repository(key, repo)
        for key, labels in self.m["labels"].items():
            self.labels(key, labels)
        self.milestones = {key: self.milestone_numbers(key, wanted) for key, wanted in self.m.get("milestones", {}).items()}
        self.me = self.rest("GET", "user")["login"]
        named = self.m["issues"]
        for issue in named[:[i["seed"] for i in named].index("P36") + 1]:
            self.create_issue(issue)
        for index in range(1, self.m["bulk"]["count"] + 1):
            spec = self.m["bulk"]
            self.create_issue({"seed": f"BULK{index:03d}", "repo": spec["repo"], "title": spec["title"].replace("{i}", str(index)),
                               "body": spec["body"].replace("{i}", str(index))})
        for issue in named[[i["seed"] for i in named].index("P36") + 1:]:
            self.create_issue(issue)
        self.load()  # comments and node IDs of everything that now exists
        for issue in named:
            self.details(issue)
        for pull in self.m["pulls"]:
            self.pull(pull)
        for commit in self.m["commits"]:
            self.commit(commit)
        self.load()
        for issue in named:
            self.state(issue)
        self.bot_comment()
        print(json.dumps({"issues": len(self.issues), "pulls": len(self.pulls), "content_requests": len(self.pace.sent),
                          "observed": self.noted}, indent=1))

    def repository(self, key: str, repo: dict):
        if self.rest_or_none("GET", f"repos/{self.names[key]}") is None:
            self.rest("POST", f"orgs/{self.owner}/repos", {
                "name": repo["name"], "private": repo["private"], "auto_init": True, "has_issues": True,
                "description": "Synthetic issues for testing the WIRK importer" + (f" ({self.sentinel})" if repo["private"] else "")})
            time.sleep(2)

    def labels(self, key: str, labels: list):
        have = {label["name"] for label in self.rest("GET", f"repos/{self.names[key]}/labels?per_page=100")}
        for label in labels:
            name = self.expand(label["name"])
            if name not in have:
                self.rest("POST", f"repos/{self.names[key]}/labels",
                          {"name": name, "description": self.expand(label["description"]), "color": "5319e7"})

    def milestone_numbers(self, key: str, wanted: list) -> dict:
        have = {m["title"]: m for m in self.rest("GET", f"repos/{self.names[key]}/milestones?state=all&per_page=100")}
        for milestone in wanted:
            if milestone["title"] not in have:
                body = {"title": milestone["title"], "description": self.expand(milestone["description"]),
                        **({"due_on": milestone["due"]} if milestone.get("due") else {})}
                have[milestone["title"]] = self.rest("POST", f"repos/{self.names[key]}/milestones", body)
            if milestone.get("state") == "closed" and have[milestone["title"]]["state"] != "closed":
                self.rest("PATCH", f"repos/{self.names[key]}/milestones/{have[milestone['title']]['number']}", {"state": "closed"})
        return {title: m["number"] for title, m in have.items()}

    def create_issue(self, issue: dict):
        if issue["seed"] in self.issues:
            return
        body = {"title": self.expand(issue["title"]), "body": self.expand(issue["body"]) + f"\n\nseed:{issue['seed']}",
                **({"labels": [self.expand(label) for label in issue["labels"]]} if issue.get("labels") else {}),
                **({"milestone": self.milestones[issue["repo"]][issue["milestone"]]} if issue.get("milestone") else {}),
                **({"assignees": [self.me]} if issue.get("assign") else {})}
        made = self.rest("POST", f"repos/{self.names[issue['repo']]}/issues", body)
        self.issues[issue["seed"]] = {"number": made["number"], "id": made["node_id"], "repo": issue["repo"], "comments": {"nodes": []},
                                      "body": body["body"], "state": "OPEN"}
        self.nodes.add(made["node_id"])

    def details(self, issue: dict):
        """Comments, edits, types, fields, sub-issues and blockers: each only when GitHub does not have it yet."""
        found, full = self.issues[issue["seed"]], self.names[issue["repo"]]
        listed = found["comments"]["nodes"]
        if len(listed) >= 100:  # the query holds the first 100; the rest come from the REST list
            pages = self.send(["api", "--paginate", "--slurp", f"repos/{full}/issues/{found['number']}/comments?per_page=100"],
                              None, content=False)
            listed = listed + [{"id": c["node_id"], "body": c["body"]} for page in pages for c in page]
        comments = {re.search(r"seed:(\w+)\s*$", c["body"])[1]: c for c in reversed(listed)
                    if re.search(r"seed:(\w+)\s*$", c["body"] or "")}
        for index, comment in enumerate(self.comments_of(issue), 1):
            tag = f"{issue['seed']}c{index}"
            if tag not in comments:
                made = self.rest("POST", f"repos/{full}/issues/{found['number']}/comments", {"body": comment["body"] + f"\n\nseed:{tag}"})
                comments[tag] = {"id": made["node_id"], "body": comment["body"], "isMinimized": False, "lastEditedAt": None,
                                 "reactionGroups": [], "rest_id": made["id"]}
                self.nodes.add(made["node_id"])
            have = comments[tag]
            if comment.get("edit") and not have.get("lastEditedAt"):
                self.graphql("mutation($id: ID!, $body: String!) { updateIssueComment(input: {id: $id, body: $body}) { clientMutationId } }",
                             {"id": have["id"], "body": comment["edit"] + f"\n\nseed:{tag}"}, write=True)
            if comment.get("minimize") and not have.get("isMinimized"):
                self.graphql("mutation($id: ID!, $c: ReportedContentClassifiers!) { minimizeComment(input: {subjectId: $id, classifier: $c}) "
                             "{ clientMutationId } }", {"id": have["id"], "c": comment["minimize"]}, write=True)
            self.react(have, comment.get("react", []))
        if issue.get("edit_body") and "(Edited once.)" not in (found.get("body") or ""):
            body = self.expand(issue["body"]) + f"\n\n(Edited once.)\n\nseed:{issue['seed']}"
            self.rest("PATCH", f"repos/{full}/issues/{found['number']}", {"body": body})
        if issue.get("type") and (found.get("issueType") or {}).get("name") != issue["type"]:
            kind = self.issue_type(issue["type"])
            self.graphql("mutation($i: ID!, $t: ID!) { updateIssueIssueType(input: {issueId: $i, issueTypeId: $t}) { clientMutationId } }",
                         {"i": found["id"], "t": kind}, write=True)
        if issue.get("fields") and not (found.get("issueFieldValues") or {}).get("totalCount"):
            self.set_fields(found, issue["fields"])
        if issue.get("parent") and not found.get("parent"):
            self.graphql("mutation($p: ID!, $c: ID!) { addSubIssue(input: {issueId: $p, subIssueId: $c}) { clientMutationId } }",
                         {"p": self.issues[issue["parent"]]["id"], "c": found["id"]}, write=True)
        have = {node["id"] for node in (found.get("blockedBy") or {}).get("nodes", [])}
        for blocker in issue.get("blocked_by", []):
            if self.issues[blocker]["id"] not in have:
                try:
                    self.graphql("mutation($i: ID!, $b: ID!) { addBlockedBy(input: {issueId: $i, blockingIssueId: $b}) "
                                 "{ clientMutationId } }", {"i": found["id"], "b": self.issues[blocker]["id"]}, write=True)
                except RuntimeError as error:
                    self.noted.append(f"{issue['seed']} blocked by {blocker} refused: {str(error)[-160:]}")
        self.react(found, issue.get("react", []))

    def comments_of(self, issue: dict) -> list:
        if "comment_count" not in issue:
            return [{**c, "body": self.expand(c["body"])} for c in issue.get("comments", [])]
        fill = issue.get("comment_fill")
        return [{"body": (fill["text"] * (fill["chars"] // len(fill["text"]) + 1))[:fill["chars"]] if fill
                 else issue["comment_body"].replace("{i}", str(i))} for i in range(1, issue["comment_count"] + 1)]

    def react(self, subject: dict, contents: list):
        done = {group["content"] for group in subject.get("reactionGroups") or [] if group.get("viewerHasReacted")}
        for content in contents:
            if content not in done:
                self.graphql("mutation($s: ID!, $c: ReactionContent!) { addReaction(input: {subjectId: $s, content: $c}) "
                             "{ clientMutationId } }", {"s": subject["id"], "c": content}, write=True)

    def issue_type(self, name: str) -> str:
        found = self.graphql("query($o: String!) { organization(login: $o) { issueTypes(first: 25) { nodes { id name } } } }",
                             {"o": self.owner})["data"]["organization"]["issueTypes"]["nodes"]
        return next(t["id"] for t in found if t["name"] == name)

    def set_fields(self, found: dict, wanted: dict):
        fields = self.graphql("query($o: String!) { organization(login: $o) { issueFields(first: 20) { nodes { __typename "
                              "... on IssueFieldSingleSelect { id name options { id name } } ... on IssueFieldDate { id name } } } } }",
                              {"o": self.owner})["data"]["organization"]["issueFields"]["nodes"]
        values = []
        for name, value in wanted.items():
            field = next(f for f in fields if f.get("name") == name)
            values.append({"fieldId": field["id"], **({"dateValue": value} if field["__typename"] == "IssueFieldDate" else
                                                       {"singleSelectOptionId": next(o["id"] for o in field["options"] if o["name"] == value)})})
        self.graphql("mutation($i: ID!, $f: [IssueFieldCreateOrUpdateInput!]!) { setIssueFieldValue(input: {issueId: $i, issueFields: $f}) "
                     "{ clientMutationId } }", {"i": found["id"], "f": values}, write=True)

    def branch(self, full: str, name: str, base: str = "main") -> None:
        if self.rest_or_none("GET", f"repos/{full}/git/ref/heads/{name}") is None:
            sha = self.rest("GET", f"repos/{full}/git/ref/heads/{base}")["object"]["sha"]
            self.rest("POST", f"repos/{full}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha})

    def put_file(self, full: str, path: str, text: str, message: str, branch: str) -> None:
        if self.rest_or_none("GET", f"repos/{full}/contents/{path}?ref={branch}") is None:
            self.rest("PUT", f"repos/{full}/contents/{path}",
                      {"message": message, "content": base64.b64encode(text.encode()).decode(), "branch": branch})

    def pull(self, pull: dict):
        full, private = self.names[pull["repo"]], self.m["repos"][pull["repo"]]["private"]
        title = self.expand(pull["title"])
        if pull["seed"] not in self.pulls:
            base = pull.get("base", "main")
            if base != "main":
                self.branch(full, base)
            head = f"seed-{pull['seed'].lower()}"
            self.branch(full, head, base)
            self.put_file(full, f"seed/{pull['seed']}.md", f"Change for {pull['seed']}.\n", title, head)
            made = self.rest("POST", f"repos/{full}/pulls", {"title": title, "head": head, "base": base,
                                                             "body": self.expand(pull["body"]) + f"\n\nseed:{pull['seed']}"})
            self.pulls[pull["seed"]] = {"number": made["number"], "state": "OPEN", "repo": pull["repo"]}
        if pull.get("merge") and self.pulls[pull["seed"]]["state"] == "OPEN":
            number = self.pulls[pull["seed"]]["number"]
            self.rest("PUT", f"repos/{full}/pulls/{number}/merge", {"commit_title": title + (f" {self.sentinel}" if private else ""),
                                                                    "merge_method": "squash"})
            self.pulls[pull["seed"]]["state"] = "MERGED"

    def commit(self, commit: dict):
        self.put_file(self.names[commit["repo"]], f"seed/{commit['seed']}.md", f"Change for {commit['seed']}.\n",
                      self.commit_message(commit), "main")

    def state(self, issue: dict):
        """Closes, reopens, locks and pins, after the links and pull requests they depend on."""
        found = self.issues[issue["seed"]]
        close = issue.get("close")
        if close and (found["state"] == "OPEN") and not (issue.get("reopen") and found["timelineItems"]["totalCount"]):
            reason, duplicate = ("DUPLICATE", self.issues[close["duplicate"]]["id"]) if isinstance(close, dict) else (close.upper(), None)
            self.graphql("mutation($i: ID!, $r: IssueClosedStateReason, $d: ID) { closeIssue(input: {issueId: $i, stateReason: $r, "
                         "duplicateIssueId: $d}) { clientMutationId } }", {"i": found["id"], "r": reason, "d": duplicate}, write=True)
            if issue.get("reopen"):
                self.graphql("mutation($i: ID!) { reopenIssue(input: {issueId: $i}) { clientMutationId } }", {"i": found["id"]}, write=True)
        if issue.get("lock") and not found.get("locked"):
            self.graphql("mutation($i: ID!, $r: LockReason!) { lockLockable(input: {lockableId: $i, lockReason: $r}) { clientMutationId } }",
                         {"i": found["id"], "r": issue["lock"]}, write=True)
        if issue.get("pin") and not found.get("isPinned"):
            self.graphql("mutation($i: ID!) { pinIssue(input: {issueId: $i}) { clientMutationId } }", {"i": found["id"]}, write=True)

    def bot_comment(self):
        """A comment by github-actions[bot], from a workflow that runs only when dispatched and may only write issues."""
        target = next((i for i in self.m["issues"] if i.get("bot_comment")), None)
        if not target:
            return
        full, found = self.names[target["repo"]], self.issues[target["seed"]]
        workflow = ("name: seed bot comment\non:\n  workflow_dispatch:\n    inputs:\n      issue:\n        required: true\n"
                    "permissions:\n  issues: write\njobs:\n  comment:\n    runs-on: ubuntu-latest\n    steps:\n"
                    "      - run: gh issue comment \"$ISSUE\" --repo \"$GITHUB_REPOSITORY\" --body \"A comment from the seed "
                    "workflow, posted as the Actions bot.\"\n        env:\n          GH_TOKEN: ${{ github.token }}\n"
                    "          ISSUE: ${{ inputs.issue }}\n")
        self.put_file(full, ".github/workflows/seed-comment.yml", workflow, "Add the seed comment workflow", "main")
        if not any((c.get("author") or {}).get("login") == "github-actions" for c in found["comments"]["nodes"]):
            self.rest("POST", f"repos/{full}/actions/workflows/seed-comment.yml/dispatches",
                      {"ref": "main", "inputs": {"issue": str(found["number"])}})

    def changes(self):
        """The scripted changes for A8, each applied once."""
        self.load()
        for change in self.m["changes"]:
            found, full = self.issues[change["seed"]], self.names[self.repo_of(change["seed"])]
            if "title" in change:
                self.rest("PATCH", f"repos/{full}/issues/{found['number']}", {"title": change["title"]})
            if "comment" in change and not any(change["comment"] in (c["body"] or "") for c in found["comments"]["nodes"]):
                self.rest("POST", f"repos/{full}/issues/{found['number']}/comments", {"body": change["comment"]})
            if "add_label" in change:
                self.rest("POST", f"repos/{full}/issues/{found['number']}/labels", {"labels": [change["add_label"]]})
            if change.get("close") and found["state"] == "OPEN":
                self.graphql("mutation($i: ID!) { closeIssue(input: {issueId: $i, stateReason: COMPLETED}) { clientMutationId } }",
                             {"i": found["id"]}, write=True)
            if "blocked_by" in change:
                blocker = self.issues[change["blocked_by"]]["id"]
                if blocker not in {n["id"] for n in found["blockedBy"]["nodes"]}:
                    self.graphql("mutation($i: ID!, $b: ID!) { addBlockedBy(input: {issueId: $i, blockingIssueId: $b}) "
                                 "{ clientMutationId } }", {"i": found["id"], "b": blocker}, write=True)
            if "remove_parent" in change and found.get("parent"):
                self.graphql("mutation($p: ID!, $c: ID!) { removeSubIssue(input: {issueId: $p, subIssueId: $c}) { clientMutationId } }",
                             {"p": self.issues[change["remove_parent"]]["id"], "c": found["id"]}, write=True)


if __name__ == "__main__":
    tool = Seeder(json.loads(MANIFEST.read_text()))
    tool.changes() if "--changes" in sys.argv else tool.seed()
