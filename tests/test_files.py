"""Files go straight between the machine and storage; the CLI reads only files outside its configuration directory."""

import hashlib
import json

import httpx
import pytest

from conftest import TOKEN, envelope, refusal

STORE = "https://storage.test"


def files_service(present=False, incomplete_once=False, put_status=200):
    state = {"stored": {}, "confirms": 0}

    def answer(request):
        if request.url.host == "storage.test":
            assert "authorization" not in request.headers
            if request.method == "PUT":
                state["stored"][request.url.path] = request.content
                return httpx.Response(put_status)
            return httpx.Response(200, content=state["stored"].get(request.url.path, b"hello"))
        body = json.loads(request.content)
        if "upload" in body:
            up = body["upload"]
            return envelope(data={"present": True} if present else {"present": False, "put": {
                "url": f"{STORE}/obj/{up['sha256']}?X-Amz-Signature=s", "headers": {"x-amz-checksum-sha256": "c"},
                "expires_at": "2999-01-01T00:00:00Z"}})
        if "confirm" in body:
            state["confirms"] += 1
            if incomplete_once and state["confirms"] == 1:
                return refusal("upload_incomplete", "The bytes have not arrived", text="Error upload_incomplete")
            return envelope("Uploaded report.pdf\nattach with: write new report.pdf --upload upload_7e8f")
        if "download" in body:
            return envelope(data={"download": {"url": f"{STORE}/obj/x?X-Amz-Signature=g", "filename": body["download"]["file"] + ".txt",
                                               "bytes": 5, "sha256": hashlib.sha256(b"hello").hexdigest(),
                                               "media_type": "text/plain", "expires_at": "2999-01-01T00:00:00Z"}})
        raise AssertionError(body)
    return answer, state


def test_upload_asks_puts_and_confirms_without_printing_the_link(run, tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF data")
    answer, state = files_service()
    code, out, err, fake = run(["upload", "report.pdf", "--description", "Load test"], answer)
    sha = hashlib.sha256(b"%PDF data").hexdigest()
    bodies = [json.loads(r.content) if r.url.host != "storage.test" else None for r in fake.requests]
    assert bodies[0] == {"upload": {"bytes": 9, "sha256": sha}, "format": "json"}
    assert fake.requests[1].method == "PUT" and fake.requests[1].headers["x-amz-checksum-sha256"] == "c"
    assert fake.requests[1].headers["content-length"] == "9"
    assert bodies[2]["confirm"] == {"filename": "report.pdf", "bytes": 9, "sha256": sha, "description": "Load test"}
    assert bodies[2]["request_id"].startswith("u-") and code == 0
    assert "attach with: write new report.pdf" in out and "storage.test" not in out + err


def test_present_bytes_are_not_sent_again(run, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    answer, state = files_service(present=True)
    code, out, err, fake = run(["upload", "a.txt"], answer)
    assert code == 0 and [r.method for r in fake.requests] == ["POST", "POST"]


def test_an_incomplete_upload_is_put_again_once(run, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    answer, state = files_service(incomplete_once=True)
    code, out, err, fake = run(["upload", "a.txt"], answer)
    assert code == 0 and [r.method for r in fake.requests] == ["POST", "PUT", "POST", "POST", "PUT", "POST"]


def test_storage_refusing_the_bytes_is_reported_without_the_link(run, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    answer, state = files_service(put_status=403)
    code, out, err, fake = run(["upload", "a.txt", "--request-id", "u-9"], answer)
    assert code == 1 and "storage_failed" in err and "--request-id u-9" in err and "storage.test" not in out + err


def test_the_json_answer_leaves_the_links_out(run, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    answer, state = files_service()
    code, out, err, fake = run(["upload", "a.txt", "--json"], answer)
    assert code == 0 and "storage.test" not in out and "X-Amz" not in out


def test_download_checks_the_bytes_and_never_overwrites(run, tmp_path):
    answer, state = files_service()
    code, out, err, fake = run(["download", "5c1e7a90@2", "file_4e1f"], answer)
    assert json.loads(fake.requests[0].content) == {"download": {"item": "5c1e7a90", "file": "file_4e1f", "revision": 2},
                                                    "format": "json"}
    assert code == 0 and (tmp_path / "file_4e1f.txt").read_bytes() == b"hello" and "storage.test" not in out + err
    code, out, err, fake = run(["download", "5c1e7a90", "file_4e1f"], answer)
    assert code == 2 and "-o" in err


@pytest.mark.parametrize("name", [".bashrc", "../up.txt", "a/b.txt"])
def test_download_refuses_unsafe_default_names(run, tmp_path, name):
    def answer(request):
        if request.url.host == "storage.test":
            return httpx.Response(200, content=b"hello")
        return envelope(data={"download": {"url": f"{STORE}/o?s=1", "filename": name, "bytes": 5,
                                           "sha256": hashlib.sha256(b"hello").hexdigest()}})
    code, out, err, fake = run(["download", "5c1e7a90", "file_4e1f"], answer)
    assert code == 2 and "-o" in err and not any(p.name == "up.txt" for p in tmp_path.parent.iterdir())


def test_download_deletes_a_mismatched_file(run, tmp_path):
    def answer(request):
        if request.url.host == "storage.test":
            return httpx.Response(200, content=b"tampered")
        return envelope(data={"download": {"url": f"{STORE}/o?s=1", "filename": "f.txt", "bytes": 5,
                                           "sha256": hashlib.sha256(b"hello").hexdigest()}})
    code, out, err, fake = run(["download", "5c1e7a90", "file_4e1f"], answer)
    assert code == 1 and not (tmp_path / "f.txt").exists()


def test_presigned_links_must_be_https(run, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    code, out, err, fake = run(["upload", "a.txt"], lambda r: envelope(data={"present": False, "put": {
        "url": "http://storage.test/o", "headers": {}, "expires_at": "x"}}))
    assert code == 1 and all(r.url.host != "storage.test" for r in fake.requests)


@pytest.mark.parametrize("argv", [
    ["write", "new", "T", "--body-file", "{cfg}/agent-token"],
    ["write", "new", "T", "--body-file", "{cfg}/person-token"],
    ["write", "--request", "{cfg}/config.json"],
    ["query", "--request", "{cfg}/agent-token"],
    ["show", "--file", "{cfg}/agent-token", "--no-open"],
    ["upload", "{cfg}/agent-token"],
    ["upload", "link-to-token"],
    ["upload", "{cfg}"],
])
def test_one_check_refuses_the_configuration_directory(run, home, tmp_path, argv):
    (home / "person-token").write_text("wirk_" + "b" * 43)
    (tmp_path / "link-to-token").symlink_to(home / "agent-token")
    code, out, err, fake = run([part.replace("{cfg}", str(home)) for part in argv])
    assert code == 2 and fake.requests == [] and TOKEN not in out + err


def test_every_file_read_goes_through_one_function():
    """Only `readable` turns a path the agent gave into bytes; nothing else in the CLI reads a named file."""
    import ast
    import inspect
    from wirk_cli import cli
    tree = ast.parse(inspect.getsource(cli))
    readers = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               for call in ast.walk(node) if isinstance(call, ast.Attribute) and call.attr in ("read_text", "read_bytes")}
    assert readers <= {"readable"}
