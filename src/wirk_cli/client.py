"""Configuration, the two token files and the one connection to the WIRK service.

The token is sent only as the bearer header to the address it was made for: never to another host, a presigned
storage link or a redirect, and never into output, logs or errors.
"""

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .grammar import command, shell_word

DEFAULT_URL = "https://api.wirk.life"
TOKEN = re.compile(r"[!-~]{1,256}")  # printable ASCII without spaces, as the service takes a bearer token
AGENT, PERSON = "agent-token", "person-token"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class Failure(Exception):
    """What the client says itself when the service could not answer for it."""

    def __init__(self, code: str, message: str, hint: str | None = None, exit_code: int = 1):
        super().__init__(message)
        self.code, self.hint, self.exit_code = code, hint, exit_code

    def envelope(self) -> dict:
        return {"ok": False, "errors": [{"code": self.code, "message": str(self), **({"hint": self.hint} if self.hint else {})}]}

    def text(self) -> str:
        return f"Error {self.code}: {self}" + (f"\n  {self.hint}" if self.hint else "")


def config_dir() -> Path:
    return Path(os.environ.get("WIRK_CONFIG_DIR") or Path.home() / ".config" / "wirk").expanduser()


def secure(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if not secure(url) or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        raise Failure("invalid_url", f"{url} is not a WIRK service address",
                      f"Use an https origin such as {DEFAULT_URL}, or http on 127.0.0.1 for a local service.", 2)
    return f"{parts.scheme}://{parts.netloc}"


def saved_url(directory: Path) -> str | None:
    try:
        return json.loads((directory / "config.json").read_text())["service_url"]
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Failure("config_invalid", f"{directory / 'config.json'} is not a WIRK configuration",
                      "Move it aside and run: wirk login") from None


def save_url(directory: Path, url: str) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"service_url": url}) + "\n")


def read_token(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise Failure("not_configured", "WIRK is not set up on this machine.",
                      f"Run: wirk login   (connects to {DEFAULT_URL})") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure("insecure_token_file", f"{path} must be a regular file of yours that only you can read.",
                      f"Fix: {command('chmod', '600', str(path))}  (a link or another user's file is refused)")
    try:
        token = path.read_text().strip()
    except UnicodeDecodeError:
        token = ""
    if not TOKEN.fullmatch(token):  # anything else could reach a header, or an error that quotes one
        raise Failure("invalid_token_file", f"{path} does not hold a WIRK token.", "Make a new one: wirk login --new")
    return token


def new_token(path: Path) -> str:
    """32 random bytes, written to a file only its owner can read; a planted file or link is refused, not followed."""
    token, temporary = "wirk_" + secrets.token_urlsafe(32), path.with_name(path.name + ".new")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Failure("token_file_exists", f"{temporary} already exists; WIRK will not write through it.",
                      f"Check what it is, remove it, then run wirk login again.") from None
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return token


def file_digest(stream) -> tuple[int, str]:
    """The size and SHA-256 of an open file, read in blocks; the file is left at its start."""
    check, size = hashlib.sha256(), 0
    while block := stream.read(1 << 20):
        check.update(block)
        size += len(block)
    stream.seek(0)
    return size, check.hexdigest()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Service:
    """One HTTP client for one address; follows no redirect and resends identical bytes once when no answer came."""

    def __init__(self, url: str, token: str, transport=None):
        self.url, self._token, self._transport, self._http = url, token, transport, None

    @property
    def http(self):
        """Made on first use, so a Service that is only compared (as the MCP server's cache does) opens nothing."""
        if self._http is None:
            import httpx
            self._http = httpx.Client(transport=self._transport, follow_redirects=False, timeout=httpx.Timeout(60, connect=10))
        return self._http

    def same(self, other: "Service") -> bool:
        return (self.url, self._token) == (other.url, other._token)

    def post(self, route: str, body: dict, *, uncertain: str | None = None, resend: bool = True,
             headers: dict | None = None) -> dict:
        import httpx
        for attempt in range(2 if resend else 1):
            try:
                answer = self.http.post(self.url + route, json=body,
                                        headers={"Authorization": f"Bearer {self._token}", **(headers or {})})
                return self.envelope(answer, route, uncertain)
            except httpx.TransportError as error:
                failure = error
        sent = not isinstance(failure, (httpx.ConnectError, httpx.ConnectTimeout))
        if uncertain and sent:
            raise unknown(self.url, type(failure).__name__, uncertain)  # the type only: a message can quote a header
        raise Failure("service_unavailable", f"could not reach {self.url} ({type(failure).__name__}).",
                      f"Check your network, then: curl {self.url}/health")

    def envelope(self, answer, route: str, uncertain: str | None) -> dict:
        try:
            data = answer.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "ok" in data and "errors" in data:
            return data
        status = answer.status_code
        if 300 <= status < 400:
            raise Failure("unexpected_redirect", f"{self.url} answered {status} for POST {route}, pointing to "
                          f"{answer.headers.get('location', 'nowhere')}; WIRK clients never follow redirects, so nothing "
                          "was sent there.", "Ask your WIRK administrator for the service's address.")
        if uncertain and status >= 500:
            raise unknown(self.url, f"HTTP {status} with a page that is not from WIRK", uncertain)
        if status == 404:
            raise Failure("missing_route", f"{self.url} answered 404 for POST {route}; this service does not have that "
                          "route yet.", "This client needs a newer service; tell your WIRK administrator.")
        raise Failure("service_error", f"{self.url} answered HTTP {status} with a page that is not from WIRK.",
                      f"Try again shortly; check: curl {self.url}/health")

    def put(self, put: dict, source, size: int, retry: str) -> None:
        """Stream an open file to a presigned storage link with exactly its signed headers, and no token."""
        def chunks():
            while block := source.read(1 << 20):
                yield block
        with self.storage("PUT", put["url"], retry, content=chunks(), headers={**put["headers"], "Content-Length": str(size)}):
            pass

    def fetch(self, url: str, target, sha256: str, retry: str) -> None:
        """Stream a presigned download into an open file; refuse unless its SHA-256 matches."""
        check = hashlib.sha256()
        with self.storage("GET", url, retry) as answer:
            for block in answer.iter_bytes():
                check.update(block)
                target.write(block)
        if check.hexdigest() != sha256:
            raise Failure("storage_mismatch", "the downloaded bytes did not match the SHA-256 WIRK recorded; nothing was "
                          "kept.", f"Run the same command again: {retry}")

    @contextmanager
    def storage(self, method: str, url: str, retry: str, **sending):
        """A request to a presigned link: https only, no token, no redirect; a refusal never prints the link."""
        import httpx
        if not secure(url):
            raise Failure("storage_failed", "WIRK gave a storage link that is not https; nothing was sent.",
                          "Tell your WIRK administrator.")
        try:
            with self.http.stream(method, url, **sending) as answer:
                if answer.status_code >= 300:
                    raise Failure("storage_failed", f"storage answered {answer.status_code}; nothing was stored or kept.",
                                  f"Run the same command again: {retry}")
                yield answer
        except httpx.TransportError as error:
            raise Failure("storage_failed", f"storage could not be reached ({type(error).__name__}).",
                          f"Run the same command again: {retry}") from None


def unknown(url: str, reason: str, request_id: str) -> Failure:
    return Failure("outcome_unknown", f"{url} did not answer ({reason}); request {request_id} may or may not have been "
                   "applied.", f"Run the same command again with --request-id {shell_word(request_id)}: it applies once or "
                   f"returns the stored receipt. Or: {command('wirk', 'query', f'receipt={request_id}')}")
