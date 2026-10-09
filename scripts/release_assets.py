"""Publish reviewed client bytes, reconcile retries, and qualify public installations.

Only publish/promote use GH_TOKEN. All artifact and PyPI reads are anonymous.
"""

import argparse
import hashlib
import http.server
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOS = {"cli": "wirkspace/wirk-cli", "mcp": "wirkspace/wirk-mcp", "skill": "wirkspace/wirk-skill"}
PACKAGES = {"cli": "wirk", "mcp": "wirk_mcp"}
MAX_BYTES = 128 * 1024 * 1024


class ReleaseError(Exception):
    pass


class NotReady(ReleaseError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError("Refusing an authenticated API redirect")


def identity(kind, tag, repo=None):
    if kind not in REPOS or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ReleaseError("Expected kind cli/mcp/skill and a reviewed vMAJOR.MINOR.PATCH tag")
    if repo is not None and repo != REPOS[kind]:
        raise ReleaseError("Release kind does not match the caller repository")
    return tag[1:]


def names(kind, tag):
    version = identity(kind, tag)
    if kind == "skill":
        return {"SKILL.md"}
    package = PACKAGES[kind]
    return {f"{package}-{version}-py3-none-any.whl", f"{package}-{version}.tar.gz"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_sums(data):
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ReleaseError("Malformed SHA256SUMS") from error
    result = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in result or match[2] in {".", "..", "SHA256SUMS"}:
            raise ReleaseError("Malformed or duplicate SHA256SUMS entry")
        result[match[2]] = match[1]
    if not result:
        raise ReleaseError("Empty SHA256SUMS")
    return result


def check_assets(kind, tag, assets):
    expected = names(kind, tag)
    manifest = parse_sums(assets["SHA256SUMS"])
    if set(manifest) != expected or set(assets) != expected | {"SHA256SUMS"}:
        raise ReleaseError("Asset names do not match the release kind/version")
    for name, checksum in manifest.items():
        if digest(assets[name]) != checksum:
            raise ReleaseError(f"Asset checksum mismatch: {name}")
    return assets


def local_assets(kind, tag, directory):
    try:
        assets = {path.name: path.read_bytes() for path in Path(directory).iterdir() if path.is_file()}
        return check_assets(kind, tag, assets)
    except (OSError, KeyError) as error:
        raise ReleaseError("Missing release files or SHA256SUMS") from error


def request(url, *, method="GET", data=None, authenticated=False):
    headers = {"User-Agent": "wirk-release", "Accept": "application/vnd.github+json"}
    if authenticated:
        if urllib.parse.urlsplit(url).hostname not in {"api.github.com", "uploads.github.com"}:
            raise ReleaseError("Refusing to send release credentials to another host")
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    if data is not None:
        headers["Content-Type"] = "application/octet-stream" if isinstance(data, bytes) else "application/json"
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
    try:
        open_url = urllib.request.build_opener(NoRedirect()).open if authenticated else urllib.request.urlopen
        with open_url(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=20) as response:
            result = response.read(MAX_BYTES + 1)
            if len(result) > MAX_BYTES:
                raise ReleaseError("Download exceeds release asset size limit")
            return result
    except urllib.error.HTTPError as error:
        message = f"HTTP {error.code} for {urllib.parse.urlsplit(url).hostname}"
        if error.code in {404, 408, 429} or error.code >= 500:
            raise NotReady(message, status=error.code) from error
        raise ReleaseError(message) from error
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        raise NotReady(f"Network unavailable for {urllib.parse.urlsplit(url).hostname}") from error


def get(url):
    return request(url)


def api(path, method="GET", data=None):
    return json.loads(request("https://api.github.com/" + path, method=method, data=data, authenticated=True))


def upload(repo, release_id, name, data):
    request(f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={name}",
            method="POST", data=data, authenticated=True)


def wait_for(operation, seconds=600):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return operation()
        except NotReady as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseError(f"Timed out waiting for public release: {error}") from error
            print(f"Waiting: {error}; at most {int(remaining)}s remain", flush=True)
            time.sleep(min(10, remaining))


def public_assets(kind, tag, directory, expected=None):
    identity(kind, tag)
    base = f"https://github.com/{REPOS[kind]}/releases/download/{tag}/"
    assets = {name: get(base + name) for name in sorted(names(kind, tag) | {"SHA256SUMS"})}
    check_assets(kind, tag, assets)
    if expected is not None and assets != expected:
        raise ReleaseError("Public assets differ from the built artifacts")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in assets.items():
        (directory / name).write_bytes(data)
    return assets


def pypi_entries(kind, tag):
    identity(kind, tag)
    if kind not in PACKAGES:
        raise ReleaseError("Only Python packages are published to PyPI")
    data = json.loads(get(f"https://pypi.org/pypi/{PACKAGES[kind]}/{tag[1:]}/json"))
    return {entry["filename"]: entry for entry in data["urls"]}


def check_pypi(assets, entries):
    for name, data in assets.items():
        if name == "SHA256SUMS":
            continue
        if name not in entries:
            raise NotReady(f"PyPI does not have {name} yet")
        entry = entries[name]
        if urllib.parse.urlsplit(entry["url"]).scheme != "https" or urllib.parse.urlsplit(entry["url"]).hostname != "files.pythonhosted.org":
            raise ReleaseError("Unexpected PyPI artifact host")
        if entry["digests"]["sha256"] != digest(data) or get(entry["url"]) != data:
            raise ReleaseError(f"PyPI asset differs from built/public GitHub bytes: {name}")


def wait_cli(tag, directory, seconds=600):
    def ready():
        assets = public_assets("cli", tag, directory)
        check_pypi({name: data for name, data in assets.items() if name.endswith(".whl")}, pypi_entries("cli", tag))
    wait_for(ready, seconds)


def run(command, *, timeout=180, env=None, cwd=None):
    try:
        return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, env=env, cwd=cwd).stdout
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"Command failed ({command[0]}): {error.stderr[-2000:]}") from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"Command timed out ({command[0]})") from error


def verify_tag(repo, tag, sha):
    if repo not in REPOS.values() or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("Invalid reviewed tag identity")
    listing = run(["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], timeout=30)
    refs = dict(line.split()[::-1] for line in listing.splitlines())
    commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
    if commit != sha:
        raise ReleaseError("Remote tag commit does not equal the reviewed workflow commit")


def publish(kind, tag, repo, sha, directory):
    identity(kind, tag, repo)
    assets = local_assets(kind, tag, directory)
    path = f"repos/{repo}/releases"

    def reconcile():
        verify_tag(repo, tag, sha)
        try:
            release = api(f"{path}/tags/{tag}")
        except NotReady as error:
            if error.status != 404:
                raise
            release = api(path, "POST", {"tag_name": tag, "target_commitish": sha, "name": tag,
                                         "body": "Install: curl -fsSL https://wirk.life/install | sh. Verify SHA256SUMS.",
                                         "prerelease": True, "draft": False, "make_latest": "false"})
        if release["draft"]:
            raise ReleaseError("Existing release is a private draft; inspect it before publishing")
        existing = {asset["name"] for asset in release["assets"]}
        if existing - set(assets):
            raise ReleaseError("Existing release contains unexpected assets")
        # Verify every existing byte before making any further changes.
        for name in sorted(existing):
            if get(f"https://github.com/{repo}/releases/download/{tag}/{name}") != assets[name]:
                raise ReleaseError(f"Existing release asset differs: {name}; nothing is overwritten")
        for name in sorted(set(assets) - existing):
            upload(repo, release["id"], name, assets[name])
    wait_for(reconcile, seconds=180)


def promote(kind, tag, repo, sha):
    identity(kind, tag, repo)
    verify_tag(repo, tag, sha)
    release = api(f"repos/{repo}/releases/tags/{tag}")
    api(f"repos/{repo}/releases/{release['id']}", "PATCH", {"prerelease": False, "make_latest": "true"})


def pypi_prepare(kind, tag, directory, destination):
    assets = local_assets(kind, tag, directory)
    try:
        entries = pypi_entries(kind, tag)
    except NotReady as error:
        if error.status != 404:
            raise
        entries = {}
    distributions = {name: data for name, data in assets.items() if name != "SHA256SUMS"}
    check_pypi({name: data for name, data in distributions.items() if name in entries}, entries)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in distributions.items():
        if name not in entries:
            (destination / name).write_bytes(data)
    pending = bool(list(destination.iterdir()))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write(f"pending={str(pending).lower()}\n")
    print(f"PyPI distributions pending upload: {len(list(destination.iterdir()))}")


def mcp_smoke(executable, environment, home):
    """An actual stdio process, with only one synthetic read-only HTTP endpoint."""
    requests = []
    text = "Release fixture: compact status text."

    class Fixture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            answer = json.dumps({"ok": True, "text": text, "errors": [], "notices": [],
                                 "page": {"complete": True, "next_cursor": None}}).encode()
            self.send_response(200 if self.path == "/v2/status" else 405)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = home / "config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(json.dumps({"service_url": f"http://127.0.0.1:{server.server_port}"}))
    token = config / "agent-token"
    token.write_text("wirk_" + "a" * 43)
    token.chmod(0o600)
    environment = {**environment, "WIRK_CONFIG_DIR": str(config)}
    incoming = queue.Queue()
    try:
        with tempfile.TemporaryFile(mode="w+") as stderr:
            process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=stderr, text=True, env=environment, cwd=home)
            def read_messages():
                for line in process.stdout:
                    incoming.put(line)

            reader = threading.Thread(target=read_messages, daemon=True)
            reader.start()
            try:
                def send(message):
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                    process.stdin.flush()

                def rpc(number, method, params):
                    send({"id": number, "method": method, "params": params})
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            answer = json.loads(incoming.get(timeout=max(0, deadline - time.monotonic())))
                        except queue.Empty as error:
                            raise ReleaseError(f"MCP stdio timed out during {method}") from error
                        if answer.get("id") == number:
                            if "error" in answer:
                                raise ReleaseError(f"MCP stdio refused {method}: {answer['error']}")
                            return answer["result"]

                rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "release-qualification", "version": "1"}})
                send({"method": "notifications/initialized"})
                tools = rpc(2, "tools/list", {})
                if {tool["name"] for tool in tools["tools"]} != {"wirk_status", "wirk_query", "wirk_write", "wirk_review", "wirk_show"}:
                    raise ReleaseError("MCP did not expose the five supported tools")
                result = rpc(3, "tools/call", {"name": "wirk_status", "arguments": {}})
                if result.get("isError") or result.get("content") != [{"type": "text", "text": text}]:
                    raise ReleaseError("MCP default result is not compact status text")
                if requests != [("/v2/status", {"format": "text"})]:
                    raise ReleaseError("MCP fixture saw unexpected requests")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
                reader.join(timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def qualify(kind, tag, directory):
    local_assets(kind, tag, directory)
    if kind == "skill":
        return
    wheel = Path(directory).resolve() / next(name for name in names(kind, tag) if name.endswith(".whl"))
    with tempfile.TemporaryDirectory(prefix="wirk-release-") as scratch:
        home = Path(scratch)
        # Inherit no credentials, Python paths, pip indexes or installed tool state.
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": scratch,
                       "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull}
        run([sys.executable, "-m", "venv", str(home / "venv")], env=environment, cwd=home)
        binary = home / "venv" / "bin"
        run([str(binary / "python"), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
             "--no-cache-dir", "--timeout", "20", "--retries", "2", "--index-url", "https://pypi.org/simple",
             str(wheel)], timeout=240, env=environment, cwd=home)
        installed = run([str(binary / "python"), "-c",
                         "import importlib.metadata as m; print(m.version('wirk')); " +
                         ("print(m.version('wirk-mcp'))" if kind == "mcp" else "")], env=environment, cwd=home)
        if installed.splitlines() != [tag[1:]] * (2 if kind == "mcp" else 1):
            raise ReleaseError("Clean installation resolved an unexpected client version")
        help_text = run([str(binary / "wirk"), "--help"], env=environment, cwd=home)
        if not all(word in help_text for word in ("status", "query", "write", "review")):
            raise ReleaseError("Installed CLI help is incomplete")
        if kind == "mcp":
            mcp_smoke(binary / "wirk-mcp", environment, home)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["wait-cli", "publish", "qualify", "verify", "promote", "pypi-prepare", "pypi-verify"])
    parser.add_argument("--kind", choices=REPOS, default="cli")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--dest", type=Path, default=Path("public-assets"))
    args = parser.parse_args()
    identity(args.kind, args.tag, args.repo if args.command != "wait-cli" else None)
    if args.command == "wait-cli":
        wait_cli(args.tag, args.dest)
    elif args.command == "publish":
        publish(args.kind, args.tag, args.repo, args.sha, args.dist)
    elif args.command == "promote":
        promote(args.kind, args.tag, args.repo, args.sha)
    elif args.command == "pypi-prepare":
        pypi_prepare(args.kind, args.tag, args.dist, args.dest)
    elif args.command == "pypi-verify":
        assets = local_assets(args.kind, args.tag, args.dist)
        wait_for(lambda: check_pypi(assets, pypi_entries(args.kind, args.tag)))
    else:
        expected = local_assets(args.kind, args.tag, args.dist) if args.command == "qualify" else None
        wait_for(lambda: public_assets(args.kind, args.tag, args.dest, expected=expected))
        qualify(args.kind, args.tag, args.dest)
    print(f"{args.command}: {args.kind} {args.tag} verified")


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, ValueError, KeyError, OSError) as error:
        print(f"Release failed: {error}", file=sys.stderr)
        sys.exit(1)
