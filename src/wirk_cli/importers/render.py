"""What the importer writes, rendered from plain records (docs/plans/importers.md §3.2, §3.3, §3.9). Not built yet."""

from dataclasses import dataclass

PART_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Ref:
    key: str
    scope: str
    public: bool
    ident: str | None = None
    note: str = ""


@dataclass
class Comment:
    author: str
    created: str
    edited: bool
    body: str
    reactions: str
    hidden: str | None
    version: str


@dataclass
class Record:
    source: str
    kind: str
    ident: str
    key: str
    url: str
    scope: str
    version: str
    title: str
    body: str
    state: str
    closed: str | None
    opened: list
    facts: list
    assignees: list
    fields: dict
    due: str | None
    relations: list
    comments: list
    attachments: list
    raw: dict
    raw_comments: list


@dataclass(frozen=True)
class Context:
    source: str
    selected: frozenset
    noun: tuple
    labels: dict


def line1(source, kind, ident, version, digest):
    raise NotImplementedError


def provenance(line):
    raise NotImplementedError


def work_item(ctx, record, users, notes):
    raise NotImplementedError


def discussion(ctx, record):
    raise NotImplementedError


def evidence(ctx, record):
    raise NotImplementedError


def archive(raw, is_withheld, counts):
    raise NotImplementedError


def digest(title, body, fields, work, files):
    raise NotImplementedError
