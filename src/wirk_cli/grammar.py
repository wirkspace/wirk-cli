"""Words to v2 request bodies: one grammar for arguments and for the commands the service prints."""

import re

ID = re.compile(r"(?:[a-z]+_)?[0-9a-fA-F]{6,32}")
KEY = re.compile(r"([a-z][a-z0-9_]*)=(.*)", re.S)
STATUS_KEYS = {"task", "max_bytes", "workspace_id"}
QUERY_KEYS = {"about", "receipt", "depth", "sort", "limit", "max_bytes", "cursor", "workspace_id"}
NUMBERS = {"limit", "max_bytes"}
SINGLE = {"text", "linked", "folder"}  # filters that take one string, commas included
LINKS = ("related_to", "contributes_to", "requires")
PLACEHOLDERS = {"", "REASON", "WHY", "…", "..."}
ACTIONS = ("accept", "reject", "defer")


SAFE = re.compile(r"[A-Za-z0-9_.,:/@%+=-]+")


def shell_word(value: str) -> str:
    """A word as sh and zsh read it back, the service's rule: bare when every character is safe and it does not start
    with = (zsh would expand it to a path), otherwise single-quoted with each ' written '\\''."""
    return value if SAFE.fullmatch(value) and not value.startswith("=") else "'" + value.replace("'", "'\\''") + "'"


def command(*words: str) -> str:
    """A command line to print, each word quoted by that rule."""
    return " ".join(shell_word(word) for word in words)


class UsageError(Exception):
    """A request the CLI can tell is wrong without asking the service; nothing is sent."""


def ref(token: str):
    """ID@N is revision N of an ID; anything else, a title with an @ included, is sent as written."""
    head, at, tail = token.rpartition("@")
    return {"ref": head, "revision": int(tail)} if at and tail.isdigit() and ID.fullmatch(head) else token


def revision(token: str, what: str) -> tuple[str, int]:
    found = ref(token)
    if not isinstance(found, dict):
        raise UsageError(f"{what} needs the revision you read, {command(token + '@N')}, the rN on its card. Fetch it first: "
                         f"{command('wirk', 'query', token)}")
    return found["ref"], found["revision"]


def split(words: list[str]) -> tuple[list[str], dict]:
    positionals, pairs = [], {}
    for word in words:
        match = KEY.fullmatch(word)
        if not match:
            positionals.append(word)
        elif match[1] in pairs:
            raise UsageError(f"{match[1]} is given twice; give a list once: {match[1]}=a,b")
        else:
            pairs[match[1]] = match[2]
    return positionals, pairs


def value(key: str, text: str):
    if key in NUMBERS:
        if not text.isdigit():
            raise UsageError(f"{key} takes a whole number, not {text!r}")
        return int(text)
    return text


def status_body(words: list[str]) -> dict:
    positionals, pairs = split(words)
    if set(pairs) - STATUS_KEYS:
        raise UsageError(f"status takes no filters ({', '.join(sorted(set(pairs) - STATUS_KEYS))}); use wirk query")
    body = {key: value(key, text) for key, text in pairs.items()}
    if positionals:
        body["task"] = " ".join(positionals)
    return body


def query_body(words: list[str]) -> dict:
    positionals, pairs = split(words)
    if len(positionals) > 1 and all(not ID.fullmatch(word.split("@")[0]) and len(word.split()) == 1 for word in positionals):
        words_given = " ".join(positionals)
        raise UsageError(f"Two or more words were given as separate titles. To find what matters for them: "
                         f"{command('wirk', 'query', f'about={words_given}')}. To fetch one title: "
                         f"{command('wirk', 'query', words_given)}")
    body = {"fetch": [ref(word) for word in positionals]} if positionals else {}
    fields = {key: text if key in SINGLE or "," not in text else text.split(",")
              for key, text in pairs.items() if key not in QUERY_KEYS}
    body.update({key: value(key, text) for key, text in pairs.items() if key in QUERY_KEYS})
    return {**body, "fields": fields} if fields else body


def own_words(text: str, flag: str) -> str:
    if text.strip() in PLACEHOLDERS:
        raise UsageError(f"{flag} needs your own words, not a placeholder")
    return text


def write_body(operations: list, expect: dict, options: dict, workspace: str | None) -> dict:
    """The parts every write shortcut shares: expect, the reason or evidence, propose, the wirkspace."""
    body = {"operations": operations, **({"expect": expect} if expect else {})}
    if "--evidence" in options and "--reason" in options:
        raise UsageError("give --evidence (a completion's note) or --reason, not both")
    flag = "--evidence" if "--evidence" in options else "--reason"
    if flag in options:
        body["reason"] = own_words(options[flag], flag)
    if options.get("--propose"):
        if "reason" not in body:
            raise UsageError("--propose needs --reason: say why a reviewer should accept it")
        body["mode"] = "propose"
    return {**body, "workspace_id": workspace} if workspace else body


def links(source: str, options: dict, expect: dict) -> list:
    operations = []
    for link in options.get("--link", []):
        kind, _, target = link.partition(":")
        if kind not in LINKS or not target:
            raise UsageError(f"--link takes TYPE:ID, TYPE being {', '.join(LINKS[:-1])} or {LINKS[-1]}; "
                             "cites and relies_on need --request")
        found = ref(target)
        if isinstance(found, dict):
            expect[found["ref"]] = found["revision"]
        operations.append({"op": "link.create", "data": {"type": kind, "from": source,
                                                          "to": found["ref"] if isinstance(found, dict) else found}})
    return operations


def fields_and_owner(pairs: dict, *, clearing: bool) -> tuple[dict, dict | None]:
    fields, work = {}, None
    for key, text in pairs.items():
        if not text and not clearing:
            raise UsageError(f"{key}= needs a value")
        if key == "owner":
            work = {"owner_id": text or None}
        else:
            fields[key] = None if not text else text.split(",") if "," in text else text
    return fields, work


def write_new(title: str, words: list[str], options: dict, body_text: str | None) -> dict:
    positionals, pairs = split(words)
    if positionals:
        raise UsageError(f"write new takes one title (quote it): {' '.join(positionals)}")
    workspace, kind, level = pairs.pop("workspace_id", None), pairs.pop("kind", "record"), pairs.pop("level", None)
    if kind not in ("record", "doc", "work", "wirk", "context"):  # doc and wirk are older words, read and never shown
        raise UsageError(f"kind is work, record or context here; folders and the rest need --request, not {kind}")
    if (level is None) != (kind != "context") or level not in (None, "organization", "initiative"):
        raise UsageError("kind=context needs level=organization or level=initiative, and level needs kind=context")
    fields, work = fields_and_owner(pairs, clearing=False)
    data = {"title": title, **({"body": body_text} if body_text is not None else {}), **({"fields": fields} if fields else {})}
    criteria = [{"text": text} for text in options.get("--criterion", [])]
    if work or criteria or kind in ("work", "wirk"):
        data["work"] = {**(work or {}), **({"criteria": criteria} if criteria else {})}
    if level:
        data["context"] = {"level": level}
    if options.get("--upload"):
        data["uploads"] = options["--upload"]
    create = {"op": "item.create", "ref": "new", "data": data}
    if options.get("--allow-duplicate-of"):
        create["allow_duplicate_of"] = options["--allow-duplicate-of"]
    expect: dict = {}
    operations = [create, *links("$new", options, expect)]
    return write_body(operations, expect, options, workspace)


def write_edit(target: str, words: list[str], options: dict, body_text: str | None) -> dict:
    item, read = revision(target, "write edit")
    positionals, pairs = split(words)
    if positionals or "kind" in pairs or "level" in pairs or "--criterion" in options:
        raise UsageError("write edit changes fields, owner, title, body, links and files; for kind, context or "
                         "criteria use wirk write --request")
    workspace = pairs.pop("workspace_id", None)
    fields, work = fields_and_owner(pairs, clearing=True)
    patch = {**({"title": options["--title"]} if "--title" in options else {}),
             **({"body": body_text} if body_text is not None else {}), **({"fields": fields} if fields else {}),
             **({"work": work} if work else {}), **({"attach_uploads": options["--upload"]} if options.get("--upload") else {})}
    expect = {item: read}
    operations = [{"op": "item.edit", "id": item, "patch": patch}, *links(item, options, expect)]
    return write_body(operations, expect, options, workspace)


def write_link(words: list[str], options: dict) -> dict:
    if len(words) != 3:
        raise UsageError("write link takes FROM@N TYPE TO")
    source, read = revision(words[0], "write link")
    expect = {source: read}
    return write_body(links(source, {"--link": [f"{words[1]}:{words[2]}"]}, expect), expect, options, None)


def review_body(words: list[str], options: dict) -> dict:
    if len(words) < 2 or words[-1] not in ACTIONS:
        raise UsageError("Replace ACTION with accept, reject or defer, and REASON with why: wirk review ID@N ACTION --reason WHY")
    reason = own_words(options.get("--reason", ""), "--reason")
    decisions = [{"id": item, "revision": read, "action": words[-1], "reason": reason}
                 for item, read in (revision(word, "review") for word in words[:-1])]
    return {"decisions": decisions}
