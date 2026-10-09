"""Release behavior: only matching reviewed bytes can become a public release."""

import hashlib
import importlib.util
import json
import io
import subprocess
import urllib.error
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "scripts" / "release_assets.py"
spec = importlib.util.spec_from_file_location("release_assets", SOURCE)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

TAG = "v0.4.1"
SHA = "a" * 40
WHEEL = "wirk-0.4.1-py3-none-any.whl"
SDIST = "wirk-0.4.1.tar.gz"


def sums(files):
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()


@pytest.fixture
def built(tmp_path):
    files = {WHEEL: b"wheel", SDIST: b"sdist"}
    files["SHA256SUMS"] = sums(files)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path, files


@pytest.mark.parametrize("body", [b"", b"garbage", b"a" * 64 + b"  ../escape\n", b"a" * 64 + b"  x\n" + b"b" * 64 + b"  x\n"])
def test_malformed_manifest_is_fatal(body):
    with pytest.raises(release.ReleaseError):
        release.parse_sums(body)


def test_missing_entry_and_wrong_version_fail(built):
    directory, files = built
    (directory / "SHA256SUMS").write_bytes(sums({WHEEL: b"wheel"}))
    with pytest.raises(release.ReleaseError, match="names"):
        release.local_assets("cli", TAG, directory)
    with pytest.raises(release.ReleaseError):
        release.local_assets("cli", "v0.4.2", directory)


def test_public_assets_must_match_local_bytes_not_just_public_manifest(built, tmp_path, monkeypatch):
    directory, files = built
    altered = {WHEEL: b"different", SDIST: files[SDIST]}
    altered["SHA256SUMS"] = sums(altered)
    monkeypatch.setattr(release, "get", lambda url: altered[url.rsplit("/", 1)[1]])
    with pytest.raises(release.ReleaseError, match="built"):
        release.public_assets("cli", TAG, tmp_path / "public", expected=files)


def test_public_checksum_mismatch_is_fatal(built, tmp_path, monkeypatch):
    _, files = built
    monkeypatch.setattr(release, "get", lambda url: b"corrupt" if url.endswith(".whl") else files[url.rsplit("/", 1)[1]])
    with pytest.raises(release.ReleaseError, match="checksum"):
        release.public_assets("cli", TAG, tmp_path / "public")


@pytest.mark.parametrize("status,retry", [(404, True), (429, True), (503, True), (401, False), (403, False), (410, False)])
def test_http_availability_and_permanent_errors(status, retry, monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.test/asset", status, "failure", {}, None)
    monkeypatch.setattr(release.urllib.request, "urlopen", fail)
    with pytest.raises(release.NotReady if retry else release.ReleaseError):
        release.get("https://example.test/asset")


def test_authenticated_requests_refuse_redirects():
    handler = release.NoRedirect()
    request = release.urllib.request.Request("https://api.github.com/releases", headers={"Authorization": "Bearer synthetic"})
    with pytest.raises(release.ReleaseError, match="redirect"):
        handler.redirect_request(request, None, 302, "redirect", {}, "https://unrelated.test/collect")


def test_oversized_download_is_refused(monkeypatch):
    monkeypatch.setattr(release, "MAX_BYTES", 8)
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"123456789"))
    with pytest.raises(release.ReleaseError, match="size"):
        release.get("https://example.test/asset")


def test_wait_cli_can_run_from_mcp_caller(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "wirkspace/wirk-mcp")
    monkeypatch.setattr(release, "wait_cli", lambda *args: calls.append(args))
    monkeypatch.setattr(release.sys, "argv", ["release_assets.py", "wait-cli", "--tag", TAG, "--dest", str(tmp_path)])
    release.main()
    assert calls == [(TAG, tmp_path)]


def test_bounded_wait_retries_404_then_success(monkeypatch):
    calls = []
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    def ready():
        calls.append(1)
        if len(calls) == 1:
            raise release.NotReady("404")
        return "ready"
    assert release.wait_for(ready, seconds=1) == "ready"
    assert len(calls) == 2


def test_wait_timeout_and_integrity_failure_are_visible(monkeypatch):
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    times = iter([0, 1, 2])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(times))
    def missing():
        raise release.NotReady("404")
    with pytest.raises(release.ReleaseError, match="Timed out"):
        release.wait_for(missing, seconds=1)
    calls = []
    def corrupt():
        calls.append(1)
        raise release.ReleaseError("checksum mismatch")
    with pytest.raises(release.ReleaseError, match="checksum"):
        release.wait_for(corrupt, seconds=1)
    assert len(calls) == 1


def test_wait_cli_waits_for_byte_identical_pypi_wheel(built, tmp_path, monkeypatch):
    _, files = built
    calls = []
    def get(url):
        if url.endswith("/json"):
            calls.append(1)
            if len(calls) == 1:
                raise release.NotReady("PyPI lag")
            return json.dumps({"urls": [{"filename": WHEEL, "url": "https://files.pythonhosted.org/wheel", "digests": {"sha256": hashlib.sha256(b"wheel").hexdigest()}}]}).encode()
        if url == "https://files.pythonhosted.org/wheel":
            return b"wheel"
        return files[url.rsplit("/", 1)[1]]
    monkeypatch.setattr(release, "get", get)
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    release.wait_cli(TAG, tmp_path / "dependency", seconds=1)
    assert len(calls) == 2
    assert (tmp_path / "dependency" / WHEEL).read_bytes() == b"wheel"


@pytest.mark.parametrize("annotated", [False, True])
def test_remote_tag_is_peeled_and_target_commitish_is_not_used(annotated, monkeypatch):
    listing = f"{SHA}\trefs/tags/{TAG}\n"
    if annotated:
        listing = f"{'b' * 40}\trefs/tags/{TAG}\n{SHA}\trefs/tags/{TAG}^{{}}\n"
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: listing)
    release.verify_tag("wirkspace/wirk-cli", TAG, SHA)
    with pytest.raises(release.ReleaseError, match="commit"):
        release.verify_tag("wirkspace/wirk-cli", TAG, "c" * 40)


def test_wrong_kind_and_repository_are_refused():
    with pytest.raises(release.ReleaseError):
        release.identity("other", TAG, "wirkspace/wirk-cli")
    with pytest.raises(release.ReleaseError):
        release.identity("cli", TAG, "somewhere/else")
    with pytest.raises(release.ReleaseError):
        release.identity("cli", "main", "wirkspace/wirk-cli")


def test_partial_release_and_upload_response_loss_reconcile_without_clobber(built, monkeypatch):
    directory, files = built
    uploaded = {WHEEL: files[WHEEL]}
    attempts = []
    monkeypatch.setattr(release, "verify_tag", lambda *args: None)
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    def api(path, method="GET", data=None, **kwargs):
        if method == "GET":
            return {"id": 7, "draft": False, "prerelease": True, "assets": [{"name": name} for name in uploaded]}
        raise AssertionError((method, path))
    def upload(repo, release_id, name, data):
        assert name not in uploaded
        attempts.append(name)
        uploaded[name] = data
        if name == SDIST:
            raise release.NotReady("response lost after upload")
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "upload", upload)
    monkeypatch.setattr(release, "get", lambda url: uploaded[url.rsplit("/", 1)[1]])
    release.publish("cli", TAG, "wirkspace/wirk-cli", SHA, directory)
    release.publish("cli", TAG, "wirkspace/wirk-cli", SHA, directory)
    assert uploaded == files
    assert sorted(attempts) == sorted([SDIST, "SHA256SUMS"])


def test_conflicting_existing_asset_never_uploads_or_promotes(built, monkeypatch):
    directory, _ = built
    monkeypatch.setattr(release, "verify_tag", lambda *args: None)
    monkeypatch.setattr(release, "api", lambda *args, **kwargs: {"id": 7, "draft": False, "assets": [{"name": WHEEL}]})
    monkeypatch.setattr(release, "get", lambda url: b"different")
    monkeypatch.setattr(release, "upload", lambda *args: pytest.fail("conflicting asset must never be overwritten"))
    with pytest.raises(release.ReleaseError, match="differs"):
        release.publish("cli", TAG, "wirkspace/wirk-cli", SHA, directory)


def test_new_release_starts_as_public_prerelease(built, monkeypatch):
    directory, files = built
    created = []
    monkeypatch.setattr(release, "verify_tag", lambda *args: None)
    def api(path, method="GET", data=None, **kwargs):
        if method == "GET":
            raise release.NotReady("HTTP 404", status=404)
        created.append(data)
        return {"id": 7, "draft": False, "assets": []}
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "upload", lambda *args: None)
    release.publish("cli", TAG, "wirkspace/wirk-cli", SHA, directory)
    assert created[0]["prerelease"] is True
    assert created[0]["draft"] is False
    assert created[0]["make_latest"] == "false"
    assert created[0]["target_commitish"] == SHA


def test_pypi_prepare_skips_only_verified_identical_assets(built, tmp_path, monkeypatch):
    directory, files = built
    entries = [{"filename": WHEEL, "url": "https://files.pythonhosted.org/wheel", "digests": {"sha256": hashlib.sha256(files[WHEEL]).hexdigest()}}]
    monkeypatch.setattr(release, "get", lambda url: json.dumps({"urls": entries}).encode() if url.endswith("/json") else files[WHEEL])
    release.pypi_prepare("cli", TAG, directory, tmp_path / "upload")
    assert sorted(p.name for p in (tmp_path / "upload").iterdir()) == [SDIST]
    monkeypatch.setattr(release, "get", lambda url: json.dumps({"urls": entries}).encode() if url.endswith("/json") else b"different")
    with pytest.raises(release.ReleaseError, match="PyPI"):
        release.pypi_prepare("cli", TAG, directory, tmp_path / "conflict")


def test_clean_install_failure_propagates(built, monkeypatch):
    directory, _ = built
    calls = []
    def fail(command, **kwargs):
        calls.append(command)
        raise release.ReleaseError("clean install failed")
    monkeypatch.setattr(release, "run", fail)
    with pytest.raises(release.ReleaseError, match="clean install"):
        release.qualify("cli", TAG, directory)
    assert calls
