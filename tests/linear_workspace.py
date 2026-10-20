"""Synthetic, to be replaced by the L0 recording: a small Linear workspace in the shapes of Linear's published schema,
for the poll tests. ENG-1 has children (one archived, one in another chosen team, one in a private team) and relations
both ways; the relation list is out of ID order. ENG-6 changed only through a comment."""

from test_import_linear import comment, issue, ref

OLD, NEW, SINCE = "2026-09-30T12:00:00.000Z", "2026-10-14T20:00:00.000Z", "2026-10-14T19:55:00.000Z"


def relation(ident, kind, one, other):
    return {"id": ident, "type": kind, "issue": one, "relatedIssue": other}


ISSUES = [issue(1, title="Parent", updatedAt=NEW), issue(4, "OPS", parent=ref(1)), issue(2, parent=ref(1)),
          issue(3, parent=ref(1), archivedAt="2026-10-01T00:00:00.000Z"), issue(10, "SEC", parent=ref(1, "ENG")),
          issue(5), issue(6), issue(7, updatedAt=NEW, parent=ref(9, "SEC", private=True))]
RELATIONS = [relation("r-9", "blocks", ref(1), ref(5)), relation("r-2", "blocks", ref(6), ref(1)),
             relation("r-5", "duplicate", ref(1), ref(7)), relation("r-1", "similar", ref(5), ref(1)),
             relation("r-0", "related", ref(1), None), relation("r-3", "blocks", ref(8, "SEC", private=True), ref(1))]
COMMENTS = [comment(1, 1, "On the parent", updatedAt=OLD), comment(2, 6, "Changed since", updatedAt=NEW),
            comment(3, 5, "Old news", updatedAt=OLD)]
