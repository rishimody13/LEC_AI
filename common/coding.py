"""Batch code formatting and check digits.

A batch has a short id like B-2288. The carton prints the full label code
B-2288-0, where the last digit is a check digit over the four body digits.
Changing any single body digit always breaks the check digit. That is how a reader
tells a misread character apart from a genuinely different code.
"""

from __future__ import annotations

import re

BATCH_ID_RE = re.compile(r"^B-(\d{4})$")
LABEL_CODE_RE = re.compile(r"^B-(\d{4})-(\d)$")

# Weights alternate 3,1,3,1 across the four body digits (same idea as GS1 mod-10).
_WEIGHTS = (3, 1, 3, 1)


def check_digit(body: str) -> int:
    """Check digit for a four-digit batch body, e.g. ``"2288"`` -> ``0``."""
    if len(body) != 4 or not body.isdigit():
        raise ValueError(f"batch body must be four digits, got {body!r}")
    total = sum(int(d) * w for d, w in zip(body, _WEIGHTS, strict=True))
    return (10 - total % 10) % 10


def label_code(batch_id: str) -> str:
    """``"B-2288"`` -> ``"B-2288-0"``."""
    m = BATCH_ID_RE.match(batch_id)
    if not m:
        raise ValueError(f"not a batch id: {batch_id!r}")
    body = m.group(1)
    return f"B-{body}-{check_digit(body)}"


def batch_id_from_label(code: str) -> str | None:
    """``"B-2288-0"`` -> ``"B-2288"``. Returns None if the code is malformed."""
    m = LABEL_CODE_RE.match(code)
    return f"B-{m.group(1)}" if m else None


def is_well_formed(code: str) -> bool:
    """True if the code has the right shape, ignoring whether the check digit agrees."""
    return LABEL_CODE_RE.match(code) is not None


def check_digit_ok(code: str) -> bool:
    """True if the code is well formed *and* its check digit agrees with its body."""
    m = LABEL_CODE_RE.match(code)
    if not m:
        return False
    return check_digit(m.group(1)) == int(m.group(2))
