"""The wirk command: arguments in, the service's text out."""

import json
import os
import platform
import secrets
import socket
import stat
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from . import __version__, context, grammar, hooks, update, wake
from .client import AGENT, DEFAULT_URL, PERSON, Failure, Service, actor_binding, actor_path, check_url, choice_path, \
    chosen, config_dir, digest, file_digest, launcher, new_token, read_token, save_url, saved_url, signed_in, \
    write_private
from .grammar import UsageError, command
from .help import HELP, RETIRED

SWITCH, VALUE, MANY = "switch", "value", "many"
WRITE = {"--body": VALUE, "--body-file": VALUE, "--criterion": MANY, "--link": MANY, "--upload": MANY,
         "--allow-duplicate-of": MANY, "--title": VALUE, "--evidence": VALUE, "--reason": VALUE, "--propose": SWITCH,
         "--request-id": VALUE, "--request": VALUE, "--to": VALUE, "--on": VALUE, "--reply-to": VALUE}
MESSAGE_ONLY = {"--to", "--on", "--reply-to"}
FLAGS = {"status": {}, "query": {"--request": VALUE}, "write": WRITE,
         "review": {"--reason": VALUE, "--person": SWITCH, "--request-id": VALUE, "--request": VALUE},
         "show": {"--file": VALUE, "--revoke": VALUE, "--no-open": SWITCH},
         "upload": {"--description": VALUE, "--request-id": VALUE}, "download": {"-o": VALUE},
         "login": {"--person": SWITCH, "--url": VALUE, "--new": SWITCH}, "admin": {"--request": VALUE},
         "import": {"--dry-run": SWITCH, "--overwrite": SWITCH}, "wirkspace": {}}


def parse(words: list[str], flags: dict) -> tuple[list[str], dict]:
    """Options anywhere among the words; `--` ends them. A repeatable option collects a list."""
    flags, positionals, options, index = {**flags, "--json": SWITCH}, [], {}, 0
    while index < len(words):
        word, kind = words[index], flags.get(words[index])
        if word == "--":
            positionals += words[index + 1:]
            break
        if kind is None and word.startswith("--"):
            raise UsageError(f"unknown option {word}")
        if kind is None:
            positionals.append(word)
        elif kind == SWITCH:
            options[word] = True
        elif index + 1 == len(words):
            raise UsageError(f"{word} needs a value")
        else:
            index += 1
            if kind == MANY:
                options.setdefault(word, []).append(words[index])
            elif word in options:
                raise UsageError(f"{word} is given twice")
            else:
                options[word] = words[index]
        index += 1
    return positionals, options


def protected() -> set:
    """The device and inode of WIRK's own files: the two tokens and config.json."""
    found = set()
    for name in (AGENT, PERSON, "config.json"):
        try:
            info = os.stat(config_dir() / name)
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


def readable(name: str):
    """The one check for every file the CLI reads for an agent, standard input ("-") included: it opens the file and
    refuses it when it is WIRK's own token or configuration by device and inode, so no link, hard link, /dev/fd path
    or redirect reaches them; anything but a regular file or standard input is refused too."""
    try:
        if name != "-" and not stat.S_ISREG(os.stat(name).st_mode):
            raise UsageError(f"{name} is not a regular file")
        stream = sys.stdin.buffer if name == "-" else open(name, "rb")
    except OSError as error:
        raise UsageError(f"{name} cannot be read ({error.strerror})") from None
    info = os.fstat(stream.fileno())
    if (info.st_dev, info.st_ino) in protected():
        raise UsageError(f"{name} is one of WIRK's own token or configuration files; it is never read or sent")
    return stream


def text_of(name: str) -> str:
    with readable(name) as stream:
        return stream.read().decode()


def request_file(name: str) -> dict:
    try:
        body = json.loads(text_of(name))
    except ValueError as error:
        raise UsageError(f"{name} is not JSON: {error}") from None
    if not isinstance(body, dict):
        raise UsageError(f"{name} must hold one JSON object, the request body")
    return body


def entries(body: dict, key: str) -> list[dict]:
    """A person's --request list (decisions, operations), checked before it is shown for confirmation."""
    found = body.get(key)
    if not (isinstance(found, list) and found and all(isinstance(entry, dict) for entry in found)):
        raise UsageError(f"--request needs {key}: a list of JSON objects; nothing was sent")
    return found


def at_terminal() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("this is a person's command: run it yourself at a terminal; nothing was sent")


def confirm(phrase: str, purpose: str) -> None:
    """A person types the action back; a speed bump against being misled, not a boundary."""
    at_terminal()
    try:
        typed = input(f'Type "{phrase}" to {purpose}: ')
    except EOFError:
        typed = ""
    if typed.strip() != phrase:
        raise UsageError("not confirmed; nothing was sent")


def connect(transport, person: bool = False, actor_id: str | None = None) -> Service:
    """The connection every command uses; in an agent chat it carries the wirkspace the chat chose."""
    url, token = signed_in(person, actor_id)
    return Service(url, token, transport, None if person else chosen(url, actor_binding(actor_id)))


def fmt(options: dict) -> str:
    return "json" if options.get("--json") else "text"


def problem_text(answer: dict) -> str:
    problem = answer["errors"][0]
    lines = [f"Error {problem['code']}: {problem['message']}", *(f"  {c.get('name', c)}" for c in problem.get("choices") or [])]
    return "\n".join(lines + ([f"  {problem['hint']}"] if problem.get("hint") else []))


def emit(answer: dict, options: dict) -> int:
    if options.get("--json"):
        if "notifications" in answer:  # messages for you come last, as text ends with them
            answer["notifications"] = answer.pop("notifications")
        print(json.dumps(answer, ensure_ascii=False, separators=(",", ":")))
    else:
        print(answer["text"] if "text" in answer else problem_text(answer) if not answer["ok"] else "")
    return 0 if answer["ok"] else 1


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def local_zone() -> str | None:
    if os.environ.get("TZ"):
        return os.environ["TZ"].lstrip(":")
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None


def status(words, options, transport):
    body = {**grammar.status_body(words), "format": fmt(options)}
    header = context.header("wirk-cli", __version__, context.session())
    return emit(connect(transport).post("/v2/status", body, headers={"Wirk-Context": header}), options)


def request_body(options: dict, words: list[str], build) -> dict:
    """The exact --request body (format added only for --json), or the body a shortcut builds; never a mix of both."""
    if "--request" not in options:
        return {**build(), "format": fmt(options)}
    positionals, pairs = grammar.split(words)
    if positionals or set(pairs) - {"workspace_id"} or set(options) - {"--request", "--json", "--person"}:
        raise UsageError("--request sends the file's body as it is: give no other words or options with it, "
                         "except workspace_id=ID")
    body = request_file(options["--request"])
    if pairs.get("workspace_id"):
        if "workspace_id" in body:
            raise UsageError(f"{options['--request']} already names its wirkspace: give workspace_id in the file or on "
                             "the line, not both; nothing was sent")
        body["workspace_id"] = pairs["workspace_id"]
    if body.get("format") == "json":
        options["--json"] = True  # a file that asks for JSON is printed as --json prints
    return {**body, "format": "json"} if options.get("--json") and "format" not in body else body


def query(words, options, transport):
    return emit(connect(transport).post("/v2/query", request_body(options, words, lambda: grammar.query_body(words))), options)


def shortcut(words: list[str], options: dict) -> dict:
    action, rest = (words[0], words[1:]) if words else ("", [])
    if action in ("archive", "restore"):
        return grammar.write_archive(action, rest, options)
    if "--body" in options and "--body-file" in options:
        raise UsageError("give --body or --body-file, not both")
    if action != "message" and MESSAGE_ONLY & set(options):
        raise UsageError("--to, --on and --reply-to belong to write message")
    if action == "seen":
        return grammar.write_seen(rest, options)
    text = text_of(options["--body-file"]) if "--body-file" in options else options.get("--body")
    if action == "message":
        return grammar.write_message(rest, options, text)
    if action not in ("new", "edit", "link") or (action != "link" and not rest):
        raise UsageError("write takes new TITLE, edit ID@N, link FROM@N TYPE TO, message, seen ID…, archive ID@N, "
                         "restore ID@N, or --request FILE")
    if action == "link":
        return grammar.write_link(rest, options)
    return (grammar.write_new if action == "new" else grammar.write_edit)(rest[0], rest[1:], options, text)


def write(words, options, transport):
    body = request_body(options, words, lambda: {"request_id": options.get("--request-id") or new_id("w"),
                                                 **shortcut(words, options)})
    return send(connect(transport), "/v2/write", body, options, body.get("request_id", "?"), words=words)


def review(words, options, transport):
    body = request_body(options, words, lambda: {"request_id": options.get("--request-id") or new_id("r"),
                                                 **grammar.review_body(words, options)})
    if options.get("--person"):
        confirm(decision_phrase(body), "decide it as yourself")
    service = connect(transport, person=bool(options.get("--person")))
    return send(service, "/v2/review", body, options, body.get("request_id", "?"), words=words)


def send(service: Service, route: str, body: dict, options: dict, uncertain: str | None, receipt: bool = True,
         words: list[str] = ()) -> int:
    """POST a body that may change something. A body from --request FILE carries its own request ID, so after an
    unknown outcome the retry is the same command again; writes and reviews can also be found by their receipt."""
    try:
        return emit(service.post(route, body, uncertain=uncertain), options)
    except Failure as failure:
        if failure.code == "outcome_unknown" and "--request" in options:  # the retry goes where this went
            added = [f"workspace_id={service.wirkspace}"] if service.wirkspace and "workspace_id" not in body else []
            again = command("wirk", route.rsplit("/", 1)[1], "--request", options["--request"], *words, *added,
                            *present(options, "--person", "--json"))
            failure.hint = (f"Run the same command again: {again}: it applies once or returns the stored receipt."
                            + (failure.hint[failure.hint.index(" Or: "):] if receipt else ""))
        raise


def present(options: dict, *switches: str) -> list[str]:
    """The given switches that this command had, so a printed retry repeats them."""
    return [switch for switch in switches if options.get(switch)]


def decision_phrase(body: dict) -> str:
    """What a person types to confirm a review: each decision's action and proposal, as it will be sent."""
    return " ".join(f"{decision.get('action')} {decision.get('id')}" for decision in entries(body, "decisions"))


def show(words, options, transport):
    modes = (words == ["status"]) + ("--file" in options) + ("--revoke" in options)
    if modes != 1 or words not in ([], ["status"]):
        raise UsageError("show takes exactly one of: status, --file FILE, --revoke LINK. To read an item: wirk query ID")
    if "--revoke" in options:
        body = {"revoke": options["--revoke"]}
    else:
        body, zone = {"preset": "status"} if words else request_file(options["--file"]), local_zone()
        body.update({"timezone": zone} if zone else {})
    answer = connect(transport).post("/v2/show", {**body, "format": fmt(options)}, resend=False)
    code, link = emit(answer, options), (answer.get("text") or "").split("\n")[0]
    if code == 0 and link.startswith(("https://", "http://127.0.0.1")) and not options.get("--no-open") and not webbrowser.open(link):
        print("Could not open a browser; open the link above.", file=sys.stderr)
    return code


def upload(words, options, transport):
    if len(words) != 1:
        raise UsageError("upload takes one file; several files for one item go through wirk write --request")
    if words[0] == "-":
        raise UsageError("upload takes a file, not standard input")
    request_id, description = options.get("--request-id") or new_id("u"), options.get("--description")
    retry = command("wirk", "upload", words[0], "--request-id", request_id,  # the path as given, the same ID and description
                    *(["--description", description] if description is not None else []), *present(options, "--json"))
    service = connect(transport)  # the wirkspace named here, so an uncertain confirm's retry adds no word upload refuses
    with readable(words[0]) as source:
        answer = store(source, words[0], request_id, retry, service, description, service.wirkspace, fmt(options))
    return emit(answer, options)


def store(source, path: str, request_id: str, retry: str, service: Service, description: str | None = None,
          workspace_id: str | None = None, form: str = "json") -> dict:
    """Declare, PUT unless present, confirm: the answer of the confirm (or of a refused declare), printing nothing.
    The service keeps only the base name."""
    size, sha256 = file_digest(source)
    declared = {"bytes": size, "sha256": sha256}
    place = {"workspace_id": workspace_id} if workspace_id else {}
    for attempt in (1, 2):
        issued = service.post("/v2/files", {"upload": declared, **place, "format": "json"})
        if not issued["ok"]:
            return issued
        if not issued["data"]["present"]:
            source.seek(0)
            service.put(issued["data"]["put"], source, size, retry)
        confirm = {"filename": Path(path).name, **declared, **({"description": description} if description is not None else {})}
        confirmed = service.post("/v2/files", {"request_id": request_id, "confirm": confirm, **place, "format": form},
                                 uncertain=request_id)
        if attempt == 1 and not confirmed["ok"] and confirmed["errors"][0]["code"] == "upload_incomplete":
            continue
        return confirmed


def download(words, options, transport):
    if len(words) != 2:
        raise UsageError("download takes ITEM[@N] FILE, as shown in the item's Files block")
    found, service = grammar.ref(words[0]), connect(transport)
    item = {"item": found["ref"], "revision": found["revision"]} if isinstance(found, dict) else {"item": found}
    answer = service.post("/v2/files", {"download": {**item, "file": words[1]}, "format": "json"})
    if not answer["ok"]:
        return emit(answer, {})
    link = answer["data"]["download"]
    name = options.get("-o") or link["filename"]
    if "-o" not in options and (name.startswith(".") or "/" in name or "\\" in name):
        raise UsageError(f"will not write {name!r}: choose a file with -o PATH")
    try:  # made here, exclusively: an existing file is never written over, and only this file is ever removed
        target = os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb")
    except FileExistsError:
        raise UsageError(f"{name} already exists; choose a new file with -o PATH") from None
    except OSError as error:
        raise UsageError(f"cannot write {name}: {error.strerror}; choose a file with -o PATH") from None
    retry = command("wirk", "download", *words, *(["-o", name] if "-o" in options else []))
    try:
        with target:
            service.fetch(link["url"], target, link["sha256"], retry)
    except BaseException:
        os.unlink(name)
        raise
    print(f"Saved {name} · {link['bytes']} bytes · sha256 {link['sha256'][:8]}")
    return 0


def login(words, options, transport):
    code = sign_in(words, options, transport)
    if code == 0 and not options.get("--person"):
        wake.install()  # the listener for this configuration directory; it says one line when it cannot start
        hooks.install()  # session hooks for each harness set up for WIRK here; one line when it adds any
    return code


def sign_in(words, options, transport):
    if words:
        raise UsageError("login takes --url URL, --new and nothing else")
    person = bool(options.get("--person"))
    if person:
        at_terminal()
    directory = config_dir()
    saved = saved_url(directory)
    url = check_url(options.get("--url") or saved or DEFAULT_URL)
    path, other = directory / (PERSON if person else AGENT), directory / (AGENT if person else PERSON)
    if saved and url != saved and (not options.get("--new") or other.exists()):
        raise Failure("other_address", f"This machine's tokens belong to {saved}; they are only ever sent there.",
                      f"A new address needs a new token: wirk login --url {url} --new" + (
                          f" (after removing {other}, which belongs to {saved})" if other.exists() else ""), 2)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = new_token(path) if options.get("--new") or not path.exists() else read_token(path)
    save_url(directory, url)
    service = Service(url, token, transport)
    binding = None if person else actor_binding()
    request = {"for": "person" if person else "agent", "label": machine_name()}
    try:
        answer = service.post("/v2/login", {**request, "format": "json"})
    except Failure as failure:
        if failure.code != "missing_route":
            raise
        answer = None
    if answer is None or (not answer["ok"] and answer["errors"][0]["code"] == "not_available"):
        return digest_login(service, url, token)  # no browser approval on this service: the digest stays the way in
    pending = answer["ok"] and answer["data"]["state"] == "pending"
    listener = None
    if pending and browser_here():
        from .loopback import Listener
        listener = Listener()
    asks, refused = [(service, request, answer)], False
    try:
        if person and listener:  # the same click connects your agents here, unless they are already
            agents = Service(url, read_token(other) if other.exists() else new_token(other), transport)
            theirs = {**request, "for": "agent"}
            answer = agents.post("/v2/login", {**theirs, "format": "json"})
            refused = not answer["ok"]
            if refused:  # say why, and never exit 0 as if both were connected
                print(problem_text(answer), flush=True)
            elif answer["data"]["state"] == "pending":
                asks.append((agents, theirs, answer))
        if pending:
            listener = show_link(asks, listener)
        result = approve(asks, listener, "wirk login --person" if person else "wirk login") or int(refused)
        return enrol(service, actor_path(url, binding), launcher(url, binding)) if result == 0 and binding else result
    finally:
        if listener:
            listener.close()


def machine_name() -> str:
    """This computer's name for its row on the account page: macOS's Computer Name, else the hostname without its
    domain, as one line of at most 40 characters without quotes; or the CLI's own label when neither gives one."""
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    name = "".join(c if c.isprintable() and c != '"' else " " for c in name or socket.gethostname().split(".")[0])
    name = " ".join(name.split())[:40].rstrip()
    return name if name.lower() not in ("", "localhost") else f"wirk-cli on {platform.system() or 'unknown'}"


def enrol(machine: Service, path: Path, started_by: Path | None) -> int:
    """A chat signs in as itself under this machine's approved token: no browser, no person. A worker sends the token
    of the chat that started it instead, so it is filed under that chat; when the service refuses that token, the
    worker says why and signs in nowhere. A kept token the service refuses (revoked, or 90 days unused) is replaced by
    a new one, which is a new agent, and says so."""
    bearer = {"Authorization": f"Bearer {read_token(started_by)}"} if started_by else {}
    def send(child: str) -> dict:
        return machine.post("/v2/login", {"for": "actor", "label": "wirk actor"},
                            headers={**bearer, "Wirk-Actor-Authorization": f"Bearer {child}"})
    path.parent.mkdir(mode=0o700, exist_ok=True)
    answer = send(read_token(path)) if path.exists() else None
    if answer is None or not answer["ok"] and answer["errors"][0]["code"] == "not_authorized":
        if answer is not None:
            print("This chat's old WIRK token was refused, so it signs in as a new agent.", flush=True)
        answer = send(new_token(path))
    if started_by and not answer["ok"] and answer["errors"][0]["code"] == "unauthenticated":
        answer["errors"][0].pop("hint", None)  # the service's hint is for the launcher's chat, not this one
        print("The chat that started this one was refused, so this one does not sign in. Run wirk login in that chat, "
              "then here again.", flush=True)
    if answer["ok"]:
        wake.record(machine.url, actor_binding())
    return emit(answer, {})


def browser_here() -> bool:
    """A display is likely: not over SSH, and on Linux only with X11 or Wayland (else a text browser would answer)."""
    return not os.environ.get("SSH_CONNECTION") and (
        sys.platform != "linux" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def bound_link(asks: list, port: int) -> str:
    query = urlencode([("code", answer["data"]["code"]) for *_, answer in asks] + [("port", port)])
    return f"{asks[0][2]['data']['url'].split('?')[0]}?{query}"


def show_link(asks: list, listener):
    """Open the browser on one link with every code and the port, answering the listener; or close it, answer None and
    show each login's own link without a port: one click for an agent, the typed code for a person."""
    codes = [f"Code {answer['data']['code']} · for {'you' if request['for'] == 'person' else 'your agents'} · "
             f"expires {answer['data']['expires_at'][11:16]} UTC" for _, request, answer in asks]
    if listener:
        link = bound_link(asks, listener.port)
        try:
            opened = webbrowser.open(link)
        except webbrowser.Error:
            opened = False
        if opened:
            print(f"Approve this machine in your browser: {link}", *codes, sep="\n", flush=True)
            return listener
        listener.close()
    links = [line for (*_, answer), code in zip(asks, codes) for line in (answer["data"]["url"], code)]
    print("No browser on this machine. Open this link in your browser on any computer and approve:", *links,
          sep="\n", flush=True)
    return None


def approve(asks: list, listener, rerun: str) -> int:
    """Until the person approves: the value their click brings to the listener finishes each login, and polling sees
    an approval finished another way. 10 minutes at a terminal, 100 seconds for an agent's tool call."""
    deadline = time.monotonic() + (600 if sys.stdout.isatty() else 100)
    changed = False
    while True:
        waiting, finished = [], False
        for service, request, answer in asks:
            if not answer["ok"]:
                return emit(answer, {})
            if answer["data"]["state"] == "logged_in":
                print(login_text(service, request), flush=True)  # the service's words end with what to run next
                finished = True
            else:
                waiting.append((service, request, answer))
                interval = answer["data"]["interval"]
        if not waiting:
            return 0
        if finished or changed:
            if changed:
                print("The approval code changed. Use the current link and code below.", flush=True)
            for service, request, answer in waiting:
                who = "your own sign-in" if request["for"] == "person" else "your agents"
                data = answer["data"]
                url = bound_link([(service, request, answer)], listener.port) if listener else data["url"]
                print(f"Still waiting for {who}: {url}\nCode {data['code']} · expires {data['expires_at'][11:16]} UTC", flush=True)
        if time.monotonic() + interval > deadline:
            raise Failure("login_pending", "This machine is still waiting for approval.", f"after approving, run: {rerun}")
        try:
            value = listener.get(interval) if listener else time.sleep(interval)  # time.sleep answers None
        except KeyboardInterrupt:
            print("Stopped waiting. The approval link and this machine's token stay valid until the code expires; "
                  f"after approving, run: {rerun}", file=sys.stderr)
            return 130
        latest = [(service, request, service.post("/v2/login", {**request, "format": "json",
                                                                 **({"complete": value} if value else {})}))
                  for service, request, _ in waiting]
        changed = any(new[2]["ok"] and new[2]["data"]["state"] == "pending" and
                      old[2]["data"]["code"] != new[2]["data"]["code"]
                      for old, new in zip(waiting, latest))
        asks = latest


def login_text(service: Service, request: dict) -> str:
    """The service's own words for the login's state."""
    answer = service.post("/v2/login", {**request, "format": "text"})
    return answer.get("text") or problem_text(answer)


def digest_login(service: Service, url: str, token: str) -> int:
    """Without web sign-in: check the token, or show its digest for an administrator to register."""
    answer = service.post("/v2/status", {"format": "json", "max_bytes": 1024})
    problem = answer["errors"][0] if answer["errors"] else {}
    if answer["ok"]:
        you = answer["data"]["you"]
        space = you["wirkspace"]
        print(f"Logged in to {url} as {you['principal']} · wirkspace {space['name']} ({short(space['id'])}). "
              "Next: wirk status")
    elif problem.get("code") == "unauthenticated":
        print("This machine's token is not registered yet. Send this digest to your WIRK administrator "
              f"(it is safe to share; the token stays here):\n{digest(token)}\nThen run: wirk status")
    else:
        print(problem_text(answer))
        return 0 if problem.get("code") == "choose_wirkspace" else 1
    return 0


def admin(words, options, transport):
    at_terminal()
    if "--request" in options:
        body = request_file(options["--request"])
        for operation in entries(body, "operations"):
            print("  " + " ".join(str(value) for value in operation.values()))
        phrase = f"admin {body.get('request_id')}"
    elif len(words) >= 2 and words[0] == "show" and words[1] in ("wirkspace", "account"):
        body, phrase = {"show": words[1], **grammar.split(words[2:])[1]}, f"show {words[1]}"
    else:
        raise UsageError("admin takes show wirkspace|account, or --request FILE")
    confirm(phrase, "send it as yourself")
    body.setdefault("format", fmt(options))
    uncertain = body.get("request_id") if "--request" in options else None  # a show reads; nothing is uncertain
    return send(connect(transport, person=True), "/v2/admin", body, options, uncertain, receipt=False)


def wirkspace(words, options, transport):
    """Show this chat's wirkspace, or choose one by name or ID: the only way the choice changes."""
    if len(words) > 1:
        raise UsageError("wirkspace takes one NAME or ID; quote a name with spaces")
    service, binding = connect(transport), actor_binding()
    previous, service.wirkspace = service.wirkspace, None  # ask with no default: the service lists what you reach
    if words and not binding:
        raise UsageError("the choice belongs to one agent chat, and this is not one; "
                         "add workspace_id=ID to each command")
    answer = service.post("/v2/status", {"format": "json", "max_bytes": 1024})
    if answer["ok"]:
        reach = [answer["data"]["you"]["wirkspace"]]
    elif answer["errors"][0]["code"] == "choose_wirkspace":
        reach = [{"id": c["key"], "name": c["name"].split(" ", 1)[1]} for c in answer["errors"][0]["choices"]]
    else:
        return emit(answer, options)
    listed = ", ".join(map(named, reach))
    mine = [space for space in reach if space["id"] == previous]
    if not words:
        if previous and not mine:
            raise Failure("not_available", f"this chat's chosen wirkspace {short(previous)} is no longer reachable",
                          f"You reach {listed}. Choose: wirk wirkspace NAME")
        if mine or len(reach) == 1:
            space, how = (mine[0], "chosen in this chat") if mine else (reach[0], "your only one")
            return said(f"Wirkspace: {named(space)} · {how}", {"wirkspace": space}, options)
        text = (f"No wirkspace chosen in this chat. You reach {listed}.\nChoose: wirk wirkspace NAME" if binding
                else f"You reach {listed}.\nAdd workspace_id=ID to each command.")
        return said(text, {"wirkspace": None, "reach": reach}, options)
    wanted = words[0].casefold()
    matches = [space for space in reach if wanted in (space["name"].casefold(), space["id"], short(space["id"]))]
    if len(matches) != 1:
        problem = f"{len(matches)} wirkspaces you reach are" if matches else "no wirkspace you reach is"
        raise Failure("invalid_input", f"{problem} called {words[0]}; nothing changed",
                      f"You reach {listed}. Choose one by name or ID: wirk wirkspace NAME", 2)
    write_private(choice_path(service.url, binding), matches[0]["id"])
    before = named(mine[0]) if mine else previous and short(previous)
    return said(f"Now working in {named(matches[0])}, " + (f"previously {before}" if before else "none before"),
                {"wirkspace": matches[0], "previous": previous}, options)


def short(identifier: str) -> str:
    return identifier.split("_", 1)[-1][:8]


def named(space: dict) -> str:
    return f"{space['name']} ({short(space['id'])})"


def said(text: str, data: dict, options: dict) -> int:
    return emit({"ok": True, "errors": [], "data": data} if options.get("--json") else {"ok": True, "text": text}, options)


def import_(words, options, transport):
    from . import importers  # loaded only for this command, so the rest of the CLI stays as fast as before
    return importers.main(words, options, transport)


COMMANDS = {"status": status, "query": query, "write": write, "review": review, "show": show, "upload": upload,
            "download": download, "login": login, "wirkspace": wirkspace, "admin": admin, "import": import_}


def main(argv: list[str] | None = None, transport=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = run(argv, transport)
    update.after(argv[0] if argv and not argv[0].startswith("-") else "")
    wake.after(code)
    return code


def run(argv: list[str], transport) -> int:
    command, words = (argv[0], argv[1:]) if argv and not argv[0].startswith("-") else ("", argv)
    try:
        if command in RETIRED:
            raise Failure("invalid_input", f"wirk {command} is gone", f"Use: {RETIRED[command]}", 2)
        if command and command not in COMMANDS:
            raise Failure("invalid_input", f"unknown command {command}", "See: wirk --help", 2)
        if words == ["--version"] and not command:
            print(f"wirk {__version__}")
            return 0
        if not command or "--help" in words or "-h" in words:
            print(HELP.get(f"{command} {words[0]}", HELP[command]) if words else HELP[command])  # write edit has its own
            return 0
        positionals, options = parse(words, FLAGS[command])
        return COMMANDS[command](positionals, options, transport)
    except UsageError as error:  # said as the client's other errors are, with exit 2
        failure = Failure("invalid_input", str(error), f"See: wirk {command} --help", 2)
    except Failure as error:
        failure = error
    print(json.dumps(failure.envelope()) if "--json" in words else failure.text(),
          file=sys.stdout if "--json" in words else sys.stderr)
    return failure.exit_code
