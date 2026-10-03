"""The wirk command: arguments in, the service's text out."""

import json
import os
import platform
import secrets
import stat
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__, context, grammar
from .client import AGENT, DEFAULT_URL, PERSON, Failure, Service, check_url, config_dir, digest, file_digest, new_token, \
    read_token, save_url, saved_url
from .grammar import UsageError, command
from .help import HELP, RETIRED

SWITCH, VALUE, MANY = "switch", "value", "many"
WRITE = {"--body": VALUE, "--body-file": VALUE, "--criterion": MANY, "--link": MANY, "--upload": MANY,
         "--allow-duplicate-of": MANY, "--title": VALUE, "--evidence": VALUE, "--reason": VALUE, "--propose": SWITCH,
         "--request-id": VALUE, "--request": VALUE}
FLAGS = {"status": {}, "query": {"--request": VALUE}, "write": WRITE,
         "review": {"--reason": VALUE, "--person": SWITCH, "--request-id": VALUE, "--request": VALUE},
         "show": {"--file": VALUE, "--revoke": VALUE, "--no-open": SWITCH},
         "upload": {"--description": VALUE, "--request-id": VALUE}, "download": {"-o": VALUE},
         "login": {"--person": SWITCH, "--url": VALUE, "--new": SWITCH}, "admin": {"--request": VALUE},
         "import": {"--dry-run": SWITCH, "--overwrite": SWITCH}}


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


def connect(transport, person: bool = False) -> Service:
    directory = config_dir()
    token, url = read_token(directory / (PERSON if person else AGENT)), saved_url(directory)
    if url is None:
        raise Failure("not_configured", "WIRK is not set up on this machine.", f"Run: wirk login   (connects to {DEFAULT_URL})")
    return Service(url, token, transport)


def fmt(options: dict) -> str:
    return "json" if options.get("--json") else "text"


def problem_text(answer: dict) -> str:
    problem = answer["errors"][0]
    lines = [f"Error {problem['code']}: {problem['message']}", *(f"  {c.get('name', c)}" for c in problem.get("choices") or [])]
    return "\n".join(lines + ([f"  {problem['hint']}"] if problem.get("hint") else []))


def emit(answer: dict, options: dict) -> int:
    if options.get("--json"):
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
    if words or set(options) - {"--request", "--json", "--person"}:
        raise UsageError("--request sends the file's body as it is: give no other words or options with it")
    body = request_file(options["--request"])
    return {**body, "format": "json"} if options.get("--json") and "format" not in body else body


def query(words, options, transport):
    return emit(connect(transport).post("/v2/query", request_body(options, words, lambda: grammar.query_body(words))), options)


def shortcut(words: list[str], options: dict) -> dict:
    action, rest = (words[0], words[1:]) if words else ("", [])
    if "--body" in options and "--body-file" in options:
        raise UsageError("give --body or --body-file, not both")
    text = text_of(options["--body-file"]) if "--body-file" in options else options.get("--body")
    if action in ("archive", "restore"):
        raise UsageError(f"{action} needs a reason: use wirk write --request with item.{action} (see wirk write --help)")
    if action not in ("new", "edit", "link") or (action != "link" and not rest):
        raise UsageError("write takes new TITLE, edit ID@N, link FROM@N TYPE TO, or --request FILE")
    if action == "link":
        return grammar.write_link(rest, options)
    return (grammar.write_new if action == "new" else grammar.write_edit)(rest[0], rest[1:], options, text)


def write(words, options, transport):
    body = request_body(options, words, lambda: {"request_id": options.get("--request-id") or new_id("w"),
                                                 **shortcut(words, options)})
    return send(connect(transport), "/v2/write", body, options, body.get("request_id", "?"))


def review(words, options, transport):
    body = request_body(options, words, lambda: {"request_id": options.get("--request-id") or new_id("r"),
                                                 **grammar.review_body(words, options)})
    if options.get("--person"):
        confirm(decision_phrase(body), "decide it as yourself")
    service = connect(transport, person=bool(options.get("--person")))
    return send(service, "/v2/review", body, options, body.get("request_id", "?"))


def send(service: Service, route: str, body: dict, options: dict, uncertain: str | None, receipt: bool = True) -> int:
    """POST a body that may change something. A body from --request FILE carries its own request ID, so after an
    unknown outcome the retry is the same command again; writes and reviews can also be found by their receipt."""
    try:
        return emit(service.post(route, body, uncertain=uncertain), options)
    except Failure as failure:
        if failure.code == "outcome_unknown" and "--request" in options:
            again = command("wirk", route.rsplit("/", 1)[1], "--request", options["--request"],
                            *(["--person"] if options.get("--person") else []))
            failure.hint = (f"Run the same command again: {again}: it applies once or returns the stored receipt."
                            + (f" Or: {command('wirk', 'query', f'receipt={uncertain}')}" if receipt else ""))
        raise


def decision_phrase(body: dict) -> str:
    """What a person types to confirm a review: each decision's action and proposal, as it will be sent."""
    return " ".join(f"{decision.get('action')} {decision.get('id')}" for decision in entries(body, "decisions"))


def show(words, options, transport):
    modes = (words == ["status"]) + ("--file" in options) + ("--revoke" in options)
    if modes != 1 or words not in ([], ["status"]):
        raise UsageError("show takes exactly one of: status, --file FILE, --revoke LINK")
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
    request_id = options.get("--request-id") or new_id("u")
    with readable(words[0]) as source:
        return send_file(source, words[0], request_id, options, connect(transport))


def send_file(source, path: str, request_id: str, options: dict, service: Service) -> int:
    """The service keeps the base name; a retry repeats the path as given, with the same request ID and description."""
    size, sha256 = file_digest(source)
    declared = {"bytes": size, "sha256": sha256}
    described = {"description": options["--description"]} if "--description" in options else {}
    retry = command("wirk", "upload", path, "--request-id", request_id,
                    *(["--description", options["--description"]] if described else []))
    for attempt in (1, 2):
        issued = service.post("/v2/files", {"upload": declared, "format": "json"})
        if not issued["ok"]:
            return emit(issued, options)
        if not issued["data"]["present"]:
            source.seek(0)
            service.put(issued["data"]["put"], source, size, retry)
        confirm_body = {"filename": Path(path).name, **declared, **described}
        confirmed = service.post("/v2/files", {"request_id": request_id, "confirm": confirm_body, "format": fmt(options)},
                                 uncertain=request_id)
        if attempt == 1 and not confirmed["ok"] and confirmed["errors"][0]["code"] == "upload_incomplete":
            continue
        return emit(confirmed, options)


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
    if person:
        confirm("person token", "make or check your own token")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = new_token(path) if options.get("--new") or not path.exists() else read_token(path)
    save_url(directory, url)
    service = Service(url, token, transport)
    request = {"for": "person" if person else "agent", "label": f"wirk-cli on {platform.system() or 'unknown'}"}
    try:
        answer = service.post("/v2/login", {**request, "format": "json"})
    except Failure as failure:
        if failure.code != "missing_route":
            raise
        answer = None
    if answer is None or (not answer["ok"] and answer["errors"][0]["code"] != "not_authorized"):
        return digest_login(service, url, token)  # no browser approval on this service: the digest stays the way in
    return approve(service, request, person, answer)


def approve(service: Service, request: dict, person: bool, answer: dict) -> int:
    """Poll until the person approves: 10 minutes at a terminal, 100 seconds for an agent's tool call."""
    deadline, shown = time.monotonic() + (600 if sys.stdout.isatty() else 100), False
    while True:
        if not answer["ok"]:
            return emit(answer, {})
        data = answer["data"]
        if data["state"] == "logged_in":
            print(login_text(service, request), flush=True)  # the service's words end with what to run next
            return 0
        if not shown:
            page = data["url"].split("?")[0] if person else data["url"]  # a person types the code; no link carries it
            print(f"Type this code at {page}: {data['code']}" if person else login_text(service, request), flush=True)
            if sys.stdout.isatty():
                webbrowser.open(page)
            shown = True
        if time.monotonic() + data["interval"] > deadline:
            raise Failure("login_pending", "This machine is still waiting for approval.", "after approving, run: wirk login")
        time.sleep(data["interval"])
        answer = service.post("/v2/login", {**request, "format": "json"})


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
        print(f"Logged in to {url} as {you['principal']} · wirkspace {space['name']} ({space['id'].split('_')[-1][:8]}). "
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


def import_(words, options, transport):
    from . import importers  # loaded only for this command, so the rest of the CLI stays as fast as before
    return importers.main(words, options, transport)


COMMANDS = {"status": status, "query": query, "write": write, "review": review, "show": show, "upload": upload,
            "download": download, "login": login, "admin": admin, "import": import_}


def main(argv: list[str] | None = None, transport=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command, words = (argv[0], argv[1:]) if argv and not argv[0].startswith("-") else ("", argv)
    if command in RETIRED:
        print(f"wirk {command} is gone; use: {RETIRED[command]}", file=sys.stderr)
        return 2
    if command and command not in COMMANDS:
        print(f"wirk: unknown command {command}; see wirk --help", file=sys.stderr)
        return 2
    if words == ["--version"] and not command:
        print(f"wirk {__version__}")
        return 0
    if not command or "--help" in words or "-h" in words:
        print(HELP[command])
        return 0
    try:
        positionals, options = parse(words, FLAGS[command])
        return COMMANDS[command](positionals, options, transport)
    except UsageError as error:
        print(f"wirk {command}: {error}", file=sys.stderr)
        return 2
    except Failure as failure:
        print(json.dumps(failure.envelope()) if "--json" in words else failure.text(),
              file=sys.stdout if "--json" in words else sys.stderr)
        return failure.exit_code
