"""Which characters look like which.

Used to score how likely it is that a reader turned one batch code into another.
Plain edit distance is no good here: turning a 0 into an O is easy, turning a 0
into a 7 is not, and both are one character apart.
"""

from __future__ import annotations

# Characters that are easy to mistake for each other in print. Two mechanisms.
#
# Shapes that already look alike:
CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset("1lI7"),
    frozenset("0ODQ"),
    frozenset("5S"),
    frozenset("8B"),
    frozenset("2Z"),
    frozenset("6G"),
    frozenset("9gq"),
    # And digits that ink damage bridges: a blot fills the hole in a 0 or closes
    # the open side of a 3, 5, 6 or 9, and they all become an 8.
    frozenset("038"),
    frozenset("56"),
    frozenset("89"),
)

# Chance a reader gets a character right, mistakes it for a lookalike, or reads
# something unrelated. Deliberately blunt: the ordering matters, the exact values
# do not.
P_CORRECT = 0.97
P_LOOKALIKE = 0.02
P_UNRELATED = 0.0005


def looks_like(a: str, b: str) -> bool:
    """True if these two characters are easy to confuse."""
    if a == b:
        return True
    return any(a in g and b in g for g in CONFUSABLE_GROUPS)


def character_prob(actual: str, read: str) -> float:
    """Chance of reading `read` when `actual` was printed."""
    if actual == read:
        return P_CORRECT
    if looks_like(actual, read):
        return P_LOOKALIKE
    return P_UNRELATED


def misread_prob(actual: str, read: str) -> float:
    """Chance a reader turned `actual` into `read`.

    `read` may be shorter than `actual` when part of the label is missing. In that
    case only the characters we can see are scored, because the rest carries no
    information either way.
    """
    if not read:
        return 1.0
    if len(read) > len(actual):
        return 0.0

    p = 1.0
    for actual_char, read_char in zip(actual[: len(read)], read, strict=True):
        p *= character_prob(actual_char, read_char)
    return p
