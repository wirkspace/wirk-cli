"""Attachments (docs/plans/importers.md §3.7, §4.1; conditions 1 and the cap): found in bodies and comments, named by a hash
of their source ID, downloaded only when their item is written, through allowlisted redirects without credentials."""

import hashlib
import json

import httpx
import pytest

from import_fakes import FakeWirk
from test_import_github import FakeGh, node, page, repo
from test_import_wirk import issue, make
from wirk_cli.importers import github, render

ASSET = "https://github.com/user-attachments/assets/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
PNG = b"\x89PNG fake image bytes"


def read_with(issue_node, http=None, extra=None):
    adapter = github.GitHub(run=FakeGh([repo("acme/web")], {"acme/web": [issue_node]}, extra=extra), sleep=lambda s: None,
                            http=http)
    adapter.check({})
    return adapter, adapter.read(["acme/web"])[1][0]


def comment(body):
    return {"fullDatabaseId": "77", "id": "IC_77", "author": {"login": "ben"}, "body": body, "createdAt": "2026-09-02T00:00:00Z",
            "lastEditedAt": None, "isMinimized": False, "minimizedReason": None, "reactionGroups": []}


def test_attachments_are_found_and_named_by_their_source_id():
    _, record = read_with(node(1, body=f"See ![screen shot]({ASSET}) and ![x](https://example.com/cat.png)",
                               comments=page([comment(f"Log: [build.log](https://github.com/acme/web/files/12/build.log)")])))
    names = {(a.name, a.comment) for a in record.attachments}
    digest = hashlib.sha256(ASSET.encode()).hexdigest()[:12]
    assert (f"github-attachment-{digest}-screen-shot", False) in names
    assert any(name.endswith("-build.log") and comment for name, comment in names)
    assert len(record.attachments) == 2  # the image on another host stays a link


def store(routes):
    """A GitHub file host: each URL answers bytes or a redirect; every request is kept."""
    seen = []

    def handle(request):
        seen.append(request)
        answer = routes.get(str(request.url))
        if answer is None:
            return httpx.Response(404)
        if isinstance(answer, str):
            return httpx.Response(302, headers={"location": answer})
        return httpx.Response(200, content=answer)

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def test_a_public_attachment_follows_allowlisted_redirects_without_credentials():
    signed = "https://objects.githubusercontent.com/github-production-user-asset/abc?sig=1"
    http, seen = store({ASSET: signed, signed: PNG})
    adapter, record = read_with(node(1, body=f"![a]({ASSET})"), http=http)
    assert adapter.download(record.attachments[0]) == PNG
    assert all("authorization" not in request.headers for request in seen)


def test_a_redirect_to_another_host_is_not_followed():
    http, seen = store({ASSET: "https://evil.example.com/steal"})
    adapter, record = read_with(node(1, body=f"![a]({ASSET})"), http=http)
    assert adapter.download(record.attachments[0]) is None
    assert [str(r.url) for r in seen] == [ASSET]


def test_a_file_over_the_cap_is_left_as_a_link(monkeypatch):
    monkeypatch.setattr(github, "CAP", 10)
    http, _ = store({ASSET: PNG})
    adapter, record = read_with(node(1, body=f"![a]({ASSET})"), http=http)
    assert adapter.download(record.attachments[0]) is None


def test_a_private_attachment_comes_through_the_signed_link_in_the_rendered_body():
    signed = "https://private-user-images.githubusercontent.com/1/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b.png?jwt=abc"
    http, seen = store({signed: PNG})
    html = f'<p><a href="{signed}"><img src="{signed}" alt="a"></a></p>'
    adapter, record = read_with(node(1, body=f"![a]({ASSET})"), http=http, extra={("I_1", "html"): {"bodyHTML": html}})
    assert adapter.download(record.attachments[0]) == PNG


def test_attachments_are_downloaded_only_when_their_item_is_written():
    fake = FakeWirk(fields={"repository": {"acme_api": "active"}})
    fetched = []
    attachment = render.Attachment(name="github-attachment-0123456789ab-shot.png", url=ASSET, comment=False)

    def download(found):
        fetched.append(found.name)
        return PNG

    records = [issue(1, attachments=[attachment])]
    make(fake, download=download).run(records)
    work = next(iter(fake.mine().values()))["revisions"][-1]
    assert sorted(f["filename"] for f in work["files"]) == ["github-attachment-0123456789ab-shot.png", "github-issue-3000000001.json"]
    assert work["files"][-1]["metadata"]["origin"]["uri"] == ASSET or work["files"][0]["metadata"]["origin"]["uri"] == ASSET
    make(fake, download=download).run(records)
    assert fetched == ["github-attachment-0123456789ab-shot.png"]  # the re-run decided current by name, without downloading


def test_a_failed_download_leaves_the_link_and_imports_the_issue():
    fake = FakeWirk(fields={"repository": {"acme_api": "active"}})
    attachment = render.Attachment(name="github-attachment-0123456789ab-shot.png", url=ASSET, comment=False)
    importer = make(fake, download=lambda found: None)
    result = importer.run([issue(1, attachments=[attachment])])
    assert result[0].outcome == "created" and importer.counts["left as links"] == 1
