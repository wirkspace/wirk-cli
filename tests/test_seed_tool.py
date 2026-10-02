"""The GitHub seed tool's guard rails (docs/plans/importers.md §4.6): it writes only to the seed repositories, paces
itself under GitHub's content limits, keeps credential shapes and the sentinel where they belong, and is idempotent."""

import json
import re

import pytest

from seed import github as seed


def manifest():
    return json.loads(seed.MANIFEST.read_text())


class FakeGh:
    """Answers `gh api` calls from a table; records every call."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        for prefix, answer in self.answers.items():
            if " ".join(args).startswith(prefix):
                return answer(args, stdin) if callable(answer) else answer
        return 0, "{}", ""


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_writes_only_to_seed_repositories():
    tool = seed.Seeder(manifest(), run=FakeGh(), pace=seed.Pace(clock=Clock(), sleep=lambda s: None))
    with pytest.raises(seed.Refused):
        tool.rest("POST", "repos/wirkspace/wirk-cli/issues", {"title": "x"})
    with pytest.raises(seed.Refused):
        tool.rest("POST", "repos/someone/import-seed-public/issues", {"title": "x"})
    with pytest.raises(seed.Refused):
        tool.rest("POST", "orgs/wirkspace/repos", {"name": "wirk-core"})
    with pytest.raises(seed.Refused):
        tool.rest("DELETE", "repos/wirkspace/import-seed-public/issues/1")
    with pytest.raises(seed.Refused):
        tool.graphql("mutation { closeIssue(input: {issueId: $id}) { clientMutationId } }", {"id": "I_not_ours"}, write=True)
    tool.rest("POST", "repos/wirkspace/import-seed-public/issues", {"title": "x"})  # allowed


def test_pace_keeps_one_a_second_and_480_an_hour():
    clock = Clock()
    pace = seed.Pace(clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(1000):
        pace.wait()
        stamps.append(clock.now)
    assert all(b - a >= 1.0 for a, b in zip(stamps, stamps[1:]))
    for index, stamp in enumerate(stamps):
        window = [s for s in stamps[:index + 1] if stamp - s < 3600]
        assert len(window) <= 480


def test_secondary_limit_backs_off_and_retries():
    clock, tries = Clock(), []

    def limited(args, stdin):
        tries.append(1)
        if len(tries) < 3:
            return 1, json.dumps({"message": "You have exceeded a secondary rate limit"}), "gh: HTTP 403"
        return 0, json.dumps({"number": 7}), ""

    tool = seed.Seeder(manifest(), run=FakeGh({"api -X POST": limited}), pace=seed.Pace(clock=clock, sleep=clock.sleep))
    assert tool.rest("POST", "repos/wirkspace/import-seed-public/issues", {"title": "x"}) == {"number": 7}
    assert len(tries) == 3 and sum(s for s in clock.slept if s >= 60) >= 60 + 120


def test_sentinel_in_every_private_text_and_never_in_the_public_seed():
    m = manifest()
    tool = seed.Seeder(m, run=FakeGh(), pace=seed.Pace(clock=Clock(), sleep=lambda s: None))
    sentinel = m["sentinel"]
    for issue in m["issues"]:
        title, body = tool.expand(issue["title"]), tool.expand(issue["body"])
        private = m["repos"][issue["repo"]]["private"]
        assert (sentinel in title and sentinel in body) == private, issue["seed"]
    for repo, labels in m["labels"].items():
        for label in labels:
            assert (sentinel in tool.expand(label["name"])) == m["repos"][repo]["private"], label
    for pull in m["pulls"]:
        assert (sentinel in tool.expand(pull["title"])) == m["repos"][pull["repo"]]["private"], pull
    for commit in m["commits"]:
        assert (sentinel in tool.commit_message(commit)) == m["repos"][commit["repo"]]["private"]


def test_credential_shapes_only_in_private_seeds():
    m = manifest()
    tool = seed.Seeder(m, run=FakeGh(), pace=seed.Pace(clock=Clock(), sleep=lambda s: None))
    shape = re.compile(r"ghp_|AKIA|xox[abprs]-|sk_live_|lin_api_|PRIVATE KEY")
    for issue in m["issues"]:
        text = tool.expand(issue["title"] + issue["body"])
        if not m["repos"][issue["repo"]]["private"]:
            assert not shape.search(text), issue["seed"]
    assert len(shape.findall(tool.expand(next(i for i in m["issues"] if i["seed"] == "A05")["body"]))) == 6
    assert tool.expand("{fake36}") == tool.expand("{fake36}")  # the same text on every run, so re-runs are idempotent


def test_an_existing_seed_issue_is_not_created_again():
    m = manifest()
    fake = FakeGh()
    tool = seed.Seeder(m, run=fake, pace=seed.Pace(clock=Clock(), sleep=lambda s: None))
    tool.issues = {"P01": {"number": 1, "id": "I_1", "repo": "P"}}
    tool.create_issue(next(i for i in m["issues"] if i["seed"] == "P01"))
    assert not [call for call in fake.calls if "-X" in call[0] and "POST" in call[0]]
