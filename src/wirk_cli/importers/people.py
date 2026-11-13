"""Who in the source might be who in WIRK: a suggestion, never a decision.

The importer asks a Suggester for each person nobody matched by verified email and turns an answer into a WIRK proposal
that an administrator accepts or rejects with `wirk review`. To let a model make the suggestions, give the importer
another object with the same `suggest` method; nothing else changes.
"""

from difflib import SequenceMatcher
import re

MINIMUM, MARGIN = 0.8, 0.1  # the best member must resemble the person this much, and beat the next by this much


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


class NameSimilarity:
    """The deterministic suggester: the person's name, handle and email name against each member's ID."""

    def suggest(self, person: dict, members: list) -> tuple | None:
        """(member, score, reason) for the one clear best member, else None. `person` holds name, handle and email."""
        names = {squash(person.get("name")), squash(person.get("handle")), squash((person.get("email") or "").split("@")[0])} - {""}
        ranked = sorted(((max(SequenceMatcher(None, name, member).ratio() for name in names), member) for member in members),
                        reverse=True) if names else []
        if not ranked or ranked[0][0] < MINIMUM or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MARGIN):
            return None
        score, member = ranked[0]
        return member, score, f"name or handle resembles {member} ({score:.2f})"
