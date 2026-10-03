"""Jira's ADF to Markdown (docs/plans/importers.md §6; the Jira note's §2.8 contract): pure, deterministic, and no text
character ever dropped. Nodes and attributes follow Atlassian's ADF document format."""

from collections import Counter

from wirk_cli.importers import adf


def doc(*content):
    return {"version": 1, "type": "doc", "content": list(content)}


def text(value, *marks):
    return {"type": "text", "text": value, **({"marks": [m if isinstance(m, dict) else {"type": m} for m in marks]} if marks else {})}


def para(*content):
    return {"type": "paragraph", "content": list(content)}


def md(document, **options):
    counts = Counter()
    return adf.markdown(document, counts, **options), counts


def test_paragraphs_marks_and_breaks():
    out, counts = md(doc(para(text("bold", "strong"), text(" "), text("it", "em"), text(" "), text("x()", "code"), text(" "),
                              text("site", {"type": "link", "attrs": {"href": "https://example.com"}}), text(" "),
                              text("gone", "strike"), {"type": "hardBreak"}, text("next", "underline"),
                              text("red", {"type": "textColor", "attrs": {"color": "#f00"}})),
                         para(text("second"))))
    assert out == "**bold** _it_ `x()` [site](https://example.com) ~~gone~~\\\n<u>next</u>red\n\nsecond"
    assert counts["dropped styling"] == 1


def test_every_heading_line_is_escaped_and_a_leading_hash_in_text_too():
    out, _ = md(doc({"type": "heading", "attrs": {"level": 2}, "content": [text("Steps")]}, para(text("# not a heading"))))
    assert out == "\\## Steps\n\n\\# not a heading"


def test_lists_ordered_from_their_start_nested_and_tasks():
    out, _ = md(doc({"type": "orderedList", "attrs": {"order": 3}, "content": [
                    {"type": "listItem", "content": [para(text("three")), {"type": "bulletList", "content": [
                        {"type": "listItem", "content": [para(text("inner"))]}]}]},
                    {"type": "listItem", "content": [para(text("four"))]}]},
                    {"type": "taskList", "content": [{"type": "taskItem", "attrs": {"state": "DONE"}, "content": [text("shipped")]},
                                                     {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [text("docs")]}]}))
    assert out == "3. three\n   - inner\n4. four\n\n- [x] shipped\n- [ ] docs"


def test_code_quotes_rules_panels_and_expands():
    out, _ = md(doc({"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("print('```')")]},
                    {"type": "blockquote", "content": [para(text("quoted"))]}, {"type": "rule"},
                    {"type": "panel", "attrs": {"panelType": "warning"}, "content": [para(text("careful"))]},
                    {"type": "expand", "attrs": {"title": "More"}, "content": [para(text("hidden"))]}))
    assert out == ("````python\nprint('```')\n````\n\n> quoted\n\n---\n\n> **Warning:** careful\n\n"
                   "<details><summary>More</summary>\n\nhidden\n\n</details>")


def test_tables_simple_as_pipes_merged_as_html():
    cell = lambda kind, value, **attrs: {"type": kind, "attrs": attrs, "content": [para(text(value))]}
    simple = {"type": "table", "content": [{"type": "tableRow", "content": [cell("tableHeader", "a|b"), cell("tableHeader", "c")]},
                                           {"type": "tableRow", "content": [cell("tableCell", "1"), cell("tableCell", "2")]}]}
    merged = {"type": "table", "content": [{"type": "tableRow", "content": [cell("tableCell", "wide", colspan=2)]}]}
    out, _ = md(doc(simple, merged))
    assert out == "| a\\|b | c |\n| --- | --- |\n| 1 | 2 |\n\n<table><tr><td colspan=\"2\">wide</td></tr></table>"


def test_inline_nodes_media_and_unknown_nodes():
    out, counts = md(doc(para({"type": "mention", "attrs": {"id": "5b10ac8d82e05b22cc7d4ef5", "text": "@Ada Example"}}, text(" "),
                              {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}}, text(" "),
                              {"type": "date", "attrs": {"timestamp": "1790812800000"}}, text(" "),
                              {"type": "status", "attrs": {"text": "In Review"}}, text(" "),
                              {"type": "inlineCard", "attrs": {"url": "https://acme.atlassian.net/browse/SEED-1"}}),
                         {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m-1", "alt": "shot.png"}}]},
                         {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m-2"}}]},
                         {"type": "futureThing", "content": [para(text("kept text"))]}),
                     files={"shot.png"})
    assert out == ("@Ada Example 🎉 2026-09-30 [IN REVIEW] <https://acme.atlassian.net/browse/SEED-1>\n\n[attached: shot.png]\n\n"
                   "[image not resolved]\n\nkept text")
    assert counts["unresolved images"] == 1 and counts["unknown ADF nodes"] == 1


def test_no_text_character_is_ever_dropped():
    words = ["alpha", "βeta", "日本", "👩🏽‍💻", "#tag", "a|b", "`tick`", "<html>"]
    hostile = doc({"type": "layoutSection", "content": [{"type": "layoutColumn", "content": [para(text(words[0], "strong"))]}]},
                  {"type": "table", "content": [{"type": "tableRow", "content": [
                      {"type": "tableCell", "content": [{"type": "bulletList", "content": [
                          {"type": "listItem", "content": [para(text(words[1]))]}]}]}]}]},
                  {"type": "nestedExpand", "content": [para(text(words[2]))]},
                  {"type": "extension", "attrs": {"extensionKey": "jira-chart"}, "content": [para(text(words[3]))]},
                  para(text(words[4])), para(text(words[5])), para(text(words[6], "code")), para(text(words[7])))
    out, _ = md(hostile)
    position = 0
    for word in words:
        found = out.find(word.replace("#", "\\#") if word.startswith("#") else word, position)
        assert found >= position, word
        position = found
    assert md(hostile)[0] == out  # deterministic
