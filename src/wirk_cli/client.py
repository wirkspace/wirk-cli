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

from . import __version__
from .grammar import command

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


def read_private(path: Path) -> str:
    """One of WIRK's own files: a regular file of yours that only you can read; a link or another user's is refused."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure("insecure_token_file", f"{path} must be a regular file of yours that only you can read.",
                      f"Fix: {command('chmod', '600', str(path))}  (a link or another user's file is refused)")
    return path.read_text().strip()


def write_private(path: Path, text: str) -> None:
    """Written to a file only its owner can read; a planted file or link is refused, not followed."""
    temporary = path.with_name(path.name + ".new")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Failure("token_file_exists", f"{temporary} already exists; WIRK will not write through it.",
                      "Check what it is, remove it, then run the same command again.") from None
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_token(path: Path) -> str:
    try:
        token = read_private(path)
    except FileNotFoundError:
        if path.name == AGENT:
            raise Failure("not_configured", "This command needs your agents' sign-in on this machine.",
                          "Run: wirk login   (your own sign-in is separate)") from None
        if path.name == PERSON:
            raise Failure("not_configured", "This command needs your own sign-in on this machine.",
                          "Run: wirk login --person   (at your own terminal)") from None
        raise Failure("not_configured", "WIRK is not set up on this machine.",
                      f"Run: wirk login   (connects to {DEFAULT_URL})") from None
    except UnicodeDecodeError:
        token = ""
    if not TOKEN.fullmatch(token):  # anything else could reach a header, or an error that quotes one
        raise Failure("invalid_token_file", f"{path} does not hold a WIRK token.", "Make a new one: wirk login --new")
    return token


def new_token(path: Path) -> str:
    """32 random bytes, written to a file only its owner can read."""
    token = "wirk_" + secrets.token_urlsafe(32)
    write_private(path, token)
    return token


UUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")  # a chat's ID, never a lookalike like "--…dead"


def actor_binding(thread_id: str | None = None) -> str | None:
    """The agent chat this process serves, so it signs in as itself: Codex's thread (as MCP's request names it, else
    the environment's), Claude Code's session and Grok's session, each a UUID, joined in that order. A worker one chat
    starts carries an ID its launcher lacks, so it never acts as its launcher. None outside any chat: the machine's
    shared identity."""
    def valid(value: str | None) -> str | None:
        return value.lower() if value and UUID.fullmatch(value) else None
    ids = (valid(thread_id) or valid(os.environ.get("CODEX_THREAD_ID")),
           valid(os.environ.get("CLAUDE_CODE_SESSION_ID")), valid(os.environ.get("GROK_SESSION_ID")))
    return ",".join(i for i in ids if i) or None


def actor_path(url: str, binding: str) -> Path:
    """A chat's own token, private like the machine's: one file per service and chat in the private actors folder."""
    return config_dir() / "actors" / hashlib.sha256(json.dumps([url, binding]).encode()).hexdigest()


def launcher(url: str, binding: str) -> Path | None:
    """The token of the chat that started this one: a worker's binding holds its launcher's ID and its own, so the one
    part of it with a token kept here is the launcher. None when no part, or more than one, has one."""
    found = [path for one in binding.split(",") if one != binding and (path := actor_path(url, one)).exists()]
    return found[0] if len(found) == 1 else None


def signed_in(person: bool = False, actor_id: str | None = None) -> tuple[str, str]:
    """The service address and the token every command sends: in an agent chat the chat's own, never the machine's;
    otherwise this machine's agents' token, or the person's own."""
    directory = config_dir()
    token, url = read_token(directory / (PERSON if person else AGENT)), saved_url(directory)
    if url is None:
        raise Failure("not_configured", "WIRK is not set up on this machine.", f"Run: wirk login   (connects to {DEFAULT_URL})")
    if binding := None if person else actor_binding(actor_id):
        if not (path := actor_path(url, binding)).exists():
            raise Failure("actor_setup_required", "This chat has no WIRK token of its own yet.",
                          "Run: wirk login   (in this chat; no browser once the machine is approved), then retry")
        token = read_token(path)
    return url, token


def choice_path(token: Path) -> Path:
    """The wirkspace a chat chose with wirk wirkspace NAME: its ID alone, beside the chat's own token."""
    return token.with_suffix(".wirkspace")


def chosen(token: Path) -> str | None:
    """The wirkspace the chat whose token is TOKEN chose, or None."""
    try:
        return read_private(choice_path(token)) or None
    except FileNotFoundError:
        return None


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

    def __init__(self, url: str, token: str, transport=None, wirkspace: str | None = None):
        """`wirkspace` is the chat's choice: sent with every body that names none (a show revoke takes the link alone)."""
        self.url, self._token, self._transport, self.wirkspace, self._http = url, token, transport, wirkspace, None

    @property
    def http(self):
        """Made on first use, so a Service that is only compared (as the MCP server's cache does) opens nothing."""
        if self._http is None:
            import httpx
            self._http = httpx.Client(transport=self._transport, follow_redirects=False, timeout=httpx.Timeout(60, connect=10),
                                      headers={"User-Agent": f"wirk/{__version__}"})  # the service refuses writes from an old wirk
        return self._http

    def same(self, other: "Service") -> bool:
        return (self.url, self._token, self.wirkspace) == (other.url, other._token, other.wirkspace)

    def post(self, route: str, body: dict, *, uncertain: str | None = None, resend: bool = True,
             headers: dict | None = None) -> dict:
        import httpx
        added = bool(self.wirkspace) and "workspace_id" not in body and "revoke" not in body
        body = {**body, "workspace_id": self.wirkspace} if added else body
        def lost(reason: str) -> Failure:  # an uncertain write's outcome, with retries that go where this went
            return unknown(self.url, reason, uncertain, body.get("workspace_id"), added)
        for attempt in range(2 if resend else 1):
            try:
                answer = self.http.post(self.url + route, json=body, timeout=httpx.Timeout(60 + body.get("wait", 0), connect=10),
                                        headers={"Authorization": f"Bearer {self._token}", **(headers or {})})
                return self.envelope(answer, route, lost if uncertain else None)
            except httpx.TransportError as error:
                failure = error
        sent = not isinstance(failure, (httpx.ConnectError, httpx.ConnectTimeout))
        if uncertain and sent:
            raise lost(type(failure).__name__)  # the type only: a message can quote a header
        raise Failure("service_unavailable", f"could not reach {self.url} ({type(failure).__name__}).",
                      f"Check your network, then: curl {self.url}/health")

    def envelope(self, answer, route: str, lost) -> dict:
        try:
            data = answer.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "ok" in data and "errors" in data:
            return data
        status = answer.status_code
        if 300 <= status < 400:
            raise Failure("unexpected_redirect", f"{self.url} answered {status} for POST {route}; "
                          "WIRK clients never follow redirects, so nothing was sent there.",
                          "Ask your WIRK administrator for the service's address.")
        if lost and status >= 500:
            raise lost(f"HTTP {status} with a page that is not from WIRK")
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


def unknown(url: str, reason: str, request_id: str, sent: str | None = None, added: bool = False) -> Failure:
    """Both commands go where the first attempt went: the receipt names the wirkspace sent, and the retry names it
    when the chat's choice supplied it rather than the command."""
    place = [f"workspace_id={sent}"] if sent else []
    again = command("--request-id", request_id, *(place if added else []))
    receipt = command("wirk", "query", f"receipt={request_id}", *place)
    return Failure("outcome_unknown", f"{url} did not answer ({reason}); request {request_id} may or may not have been "
                   "applied.", f"Run the same command again with {again}: it applies once or returns the stored receipt. "
                   f"Or: {receipt}")
