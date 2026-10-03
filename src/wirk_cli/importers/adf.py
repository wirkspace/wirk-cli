"""Jira's Atlassian Document Format as Markdown (docs/plans/importers.md §6; the Jira note's §2.8 contract): a pure
function over the tree. No text character is dropped: an unknown node gives its children's text and is counted, and every
line that would read as a heading or other structure is escaped, so imported text never makes structure."""

from datetime import datetime, timezone
import re

PANELS = {"info": "Info", "note": "Note", "warning": "Warning", "success": "Success", "error": "Error"}
STYLING = {"textColor", "backgroundColor", "border", "alignment", "indentation", "breakout", "annotation"}
WRAP = {"strong": ("**", "**"), "em": ("_", "_"), "strike": ("~~", "~~"), "underline": ("<u>", "</u>")}
STRUCTURE = re.compile(r"^([ \t]*)(#|>|[-+*][ \t]|\d+[.)][ \t])", re.M)  # a line that would read as a heading, quote or list
BLOCKS = {"paragraph", "heading", "bulletList", "orderedList", "taskList", "decisionList", "codeBlock", "blockquote", "rule",
          "panel", "table", "expand", "nestedExpand", "mediaSingle", "mediaGroup", "layoutSection", "layoutColumn",
          "extension", "bodiedExtension", "multiBodiedExtension", "extensionFrame", "syncBlock", "bodiedSyncBlock",
          "blockCard", "embedCard"}


def markdown(document: dict | None, counts, files=frozenset()) -> str:
    """`files` are the names of the item's attachments, for media an image names by its alt text."""
    return Writer(counts, files).blocks((document or {}).get("content") or [])


def names(document) -> set:
    """The file names a document's media point at, by their alt text."""
    if isinstance(document, list):
        return set().union(*map(names, document)) if document else set()
    if not isinstance(document, dict):
        return set()
    own = {document["attrs"]["alt"]} if document.get("type") in ("media", "mediaInline") and (document.get("attrs") or {}).get("alt") else set()
    return own | names(document.get("content") or [])


class Writer:
    def __init__(self, counts, files):
        self.counts, self.files = counts, files

    def blocks(self, nodes: list) -> str:
        return "\n\n".join(part for part in (self.block(node) for node in nodes) if part)

    def block(self, node: dict) -> str:
        kind, attrs, content = node.get("type"), node.get("attrs") or {}, node.get("content") or []
        if kind == "paragraph":
            return STRUCTURE.sub(r"\1\\\2", self.inline(content))
        if kind == "heading":  # written as text: imported text never makes structure
            return "\\" + "#" * min(6, max(1, attrs.get("level", 1))) + " " + self.inline(content)
        if kind in ("bulletList", "orderedList", "taskList", "decisionList"):
            return self.listing(kind, attrs, content)
        if kind == "codeBlock":
            code = "".join(child.get("text", "") for child in content)
            fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", code)), default=0))
            return f"{fence}{attrs.get('language') or ''}\n{code}\n{fence}"
        if kind == "blockquote":
            return quote(self.blocks(content))
        if kind == "rule":
            return "---"
        if kind == "panel":
            name = PANELS.get(attrs.get("panelType"), str(attrs.get("panelType") or "Note").title())
            return quote(f"**{name}:** {self.blocks(content)}")
        if kind == "table":
            return self.table(content)
        if kind in ("expand", "nestedExpand"):
            return f"<details><summary>{attrs.get('title') or ''}</summary>\n\n{self.blocks(content)}\n\n</details>"
        if kind in ("mediaSingle", "mediaGroup"):
            return "\n\n".join(self.media(child) for child in content)
        if kind in ("layoutSection", "layoutColumn"):
            return self.blocks(content)
        if kind in ("blockCard", "embedCard"):
            return f"<{attrs.get('url', '')}>"
        if kind in ("extension", "bodiedExtension", "multiBodiedExtension", "extensionFrame", "syncBlock", "bodiedSyncBlock"):
            label = f"[Jira extension {attrs.get('extensionKey') or kind} not rendered]"
            return "\n\n".join(filter(None, [label, self.blocks(content)]))
        self.counts["unknown ADF nodes"] += 1  # a new kind of node: its text, never nothing
        if any(child.get("type") in BLOCKS or child.get("content") for child in content):
            return self.blocks(content)
        return STRUCTURE.sub(r"\1\\\2", self.inline(content) or node.get("text", ""))

    def listing(self, kind: str, attrs: dict, items: list) -> str:
        lines, number = [], attrs.get("order") or 1
        for item in items:
            if kind == "orderedList":
                marker = f"{number}. "
                number += 1
            elif kind == "taskList":
                marker = "- [x] " if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ] "
            else:
                marker = "- Decision: " if kind == "decisionList" else "- "
            inner = item.get("content") or []
            body = self.inline(inner) if kind in ("taskList", "decisionList") else \
                "\n".join(part for part in (self.block(child) for child in inner) if part)
            first, *rest = (body or "").split("\n")
            lines.append(marker + first)
            lines += [(" " * len(marker) + line) if line else "" for line in rest]
        return "\n".join(lines)

    def table(self, rows: list) -> str:
        cells = [row.get("content") or [] for row in rows]
        simple = all((cell.get("attrs") or {}).get(span, 1) in (None, 1) for row in cells for cell in row for span in ("colspan", "rowspan")) \
            and all(child.get("type") == "paragraph" for row in cells for cell in row for child in cell.get("content") or [])
        if simple and cells:
            text = [["<br>".join(self.inline(p.get("content") or []) for p in cell.get("content") or []).replace("|", "\\|")
                     for cell in row] for row in cells]
            width = max(len(row) for row in text)
            line = lambda row: "| " + " | ".join(row + [""] * (width - len(row))) + " |"
            return "\n".join([line(text[0]), "| " + " | ".join(["---"] * width) + " |", *map(line, text[1:])])
        html = []
        for row in cells:
            parts = []
            for cell in row:
                tag = "th" if cell.get("type") == "tableHeader" else "td"
                spans = "".join(f' {span}="{value}"' for span in ("colspan", "rowspan")
                                if (value := (cell.get("attrs") or {}).get(span)) not in (None, 1))
                parts.append(f"<{tag}{spans}>{self.blocks(cell.get('content') or [])}</{tag}>")
            html.append("<tr>" + "".join(parts) + "</tr>")
        return "<table>" + "".join(html) + "</table>"

    def media(self, node: dict) -> str:
        name = (node.get("attrs") or {}).get("alt")
        if name in self.files:
            return f"[attached: {name}]"
        self.counts["unresolved images"] += 1
        return "[image not resolved]"

    def inline(self, nodes: list) -> str:
        return "".join(self.span(node) for node in nodes)

    def span(self, node: dict) -> str:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            return self.marked(node.get("text", ""), node.get("marks") or [])
        if kind == "hardBreak":
            return "\\\n"
        if kind == "mention":
            return attrs.get("text") or "@someone"  # a display name, never an email
        if kind == "emoji":
            return attrs.get("text") or attrs.get("shortName", "")
        if kind == "date":
            return datetime.fromtimestamp(int(attrs.get("timestamp", 0)) / 1000, timezone.utc).strftime("%Y-%m-%d")
        if kind == "status":
            return f"[{str(attrs.get('text', '')).upper()}]"
        if kind == "inlineCard":
            return f"<{attrs.get('url', '')}>"
        if kind == "mediaInline":
            return self.media(node)
        self.counts["unknown ADF nodes"] += 1
        return self.inline(node.get("content") or []) or attrs.get("text", "") or node.get("text", "")

    def marked(self, value: str, marks: list) -> str:
        link = None
        for mark in marks:
            kind = mark.get("type")
            if kind == "code":
                fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
                value = f"{fence} {value} {fence}" if "`" in value else f"`{value}`"
            elif kind in WRAP:
                value = WRAP[kind][0] + value + WRAP[kind][1]
            elif kind == "subsup":
                tag = "sup" if (mark.get("attrs") or {}).get("type") == "sup" else "sub"
                value = f"<{tag}>{value}</{tag}>"
            elif kind == "link":
                link = (mark.get("attrs") or {}).get("href", "")
            elif kind in STYLING:
                self.counts["dropped styling"] += 1
            else:
                self.counts["unknown ADF marks"] += 1
        return f"[{value}]({link})" if link is not None else value


def quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))
