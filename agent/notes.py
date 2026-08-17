"""Facts pulled out of the handwritten condition note.

The note is free text written by whoever received the goods. It sometimes holds a
fact that settles the case, such as a print date stamped on the inner cases.

Same split as the label reader: the model reads the text and reports what it
found, and plain code decides what that means for each candidate. Extractions are
recorded so this runs offline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .candidates import CandidateSet
from .evidence import RegistryEvidence

CASSETTE_PATH = Path("tests/cassettes/note_extractions.json")

PROMPT = """\
A warehouse worker wrote this note when a return arrived. Pull out only facts that
are actually stated. Do not guess.

- print_dates: any manufacturing or print dates written on the goods, as ISO
  dates. Warehouse shorthand like 12SEP25 means 12 September 2025.
- batch_codes_mentioned: any lot or batch code written out in the note, exactly as
  written. These are codes a database query would never find, because they are in
  prose rather than in a field.
- repacked: true if the note says the packaging was opened, re-taped, resealed or
  relabelled.
- mixed_pallet: true if the note says the pallet held more than one product or batch.
- off_site_origin: true if the note says the goods came via another site, a
  cross-dock, a transfer, or anywhere other than a direct delivery to this customer.
- summary: one sentence on what the note says about the goods.
"""


class NoteFacts(BaseModel):
    """What the model found in the note."""

    print_dates: list[date] = Field(default_factory=list)
    #: Lot codes written out in prose. A database query cannot find these.
    batch_codes_mentioned: list[str] = Field(default_factory=list)
    repacked: bool = False
    mixed_pallet: bool = False
    #: The goods arrived via somewhere other than a direct delivery.
    off_site_origin: bool = False
    summary: str = ""

    @property
    def is_informative(self) -> bool:
        return bool(self.print_dates or self.batch_codes_mentioned) or any(
            (self.repacked, self.mixed_pallet, self.off_site_origin)
        )

    @property
    def suggests_off_record_stock(self) -> bool:
        """The note hints the goods did not come from a shipment we have on file."""
        return self.mixed_pallet or self.off_site_origin


class NoteReader(Protocol):
    def read(self, note: str) -> NoteFacts: ...


def note_key(note: str) -> str:
    return hashlib.sha256(note.strip().encode()).hexdigest()[:16]


class CassetteNoteReader:
    """Replays recorded extractions. The default, so nothing needs a network."""

    def __init__(self, path: Path | str = CASSETTE_PATH) -> None:
        self._path = Path(path)
        self._facts: dict[str, NoteFacts] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            self._facts = {k: NoteFacts(**v) for k, v in raw.items()}

    def read(self, note: str) -> NoteFacts:
        if not note.strip():
            return NoteFacts()
        key = note_key(note)
        if key not in self._facts:
            raise KeyError(f"no recorded extraction for note {key}: {note[:60]!r}")
        return self._facts[key]


class ClaudeNoteReader:
    """Reads the note with Claude. Used to record; needs an API key."""

    def __init__(self, model: str = "claude-sonnet-5") -> None:
        self._model = model

    def read(self, note: str) -> NoteFacts:
        if not note.strip():
            return NoteFacts()
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=self._model,
            max_tokens=2048,
            messages=[{"role": "user", "content": f"{PROMPT}\n\nNote:\n{note}"}],
            output_format=NoteFacts,
        )
        facts = response.parsed_output
        assert facts is not None
        return facts


# How much a matching print date counts for. A note is handwritten and can be
# wrong or refer to something else, so this is suggestive, not decisive.
P_DATE_MATCHES = 0.55
P_DATE_MISMATCH = 0.14
P_NO_INFORMATION = 1.0

# How much it counts for when the note writes a lot code out in words.
#
# This is the one thing in the whole system that a database query cannot do: a
# warehouse note saying "inner cases stamped B-2296" is prose, and no `SELECT`
# will ever find it. Somebody opened the pallet and read the inner cases, which
# is a deliberate act and better evidence than most of what is here.
#
# It is not certain. They can transcribe a digit wrong, or read the code off a
# case that came from a different consignment on a mixed pallet. The ratio below
# is 16:1, which sits deliberately between the label's two extremes: about 5:1
# for a customer who repacks, and 99:1 for one who does not. A note is better
# evidence than a repacker's label and worse than an ordinary customer's.
#
# This existed as dead weight until it was measured. The model extracted the code
# and `candidates.build` turned it into a candidate, but nothing here ever
# mentioned it, so the candidate arrived with no evidence behind it and was then
# penalised for being absent from the records. It could not win, and across 1,500
# generated cases it changed no decision at all.
P_NOTE_NAMES_THIS_BATCH = 0.80
P_NOTE_NAMES_ANOTHER_BATCH = 0.05


def note_code_likelihood(facts: NoteFacts, candidates: CandidateSet) -> dict[str, float]:
    """How likely the note is, given a lot code written out in it.

    Applied straight away, with the records and the label, because it needs
    nothing bought. The note names a batch and `candidates.build` has already
    checked that batch is real; there is nothing left to look up.

    This was the bug. The whole thing used to live in one function that also
    handled print dates, and print dates *do* need the paid registry - so the
    function was only ever called after a lookup had been bought. On the one case
    built around this feature the agent escalated at the first decision, which
    meant the only evidence naming the true batch was never applied at all.
    """
    out = {c.name: P_NO_INFORMATION for c in candidates.candidates}
    named = {code.strip().upper() for code in facts.batch_codes_mentioned}
    if not named:
        return out

    for c in candidates.candidates:
        if c.is_catch_all or c.batch_id is None:
            # Somebody wrote a lot code down. That is evidence against "it is a
            # batch nobody named" in the same way it is against the others.
            out[c.name] = P_NOTE_NAMES_ANOTHER_BATCH
            continue
        out[c.name] = (
            P_NOTE_NAMES_THIS_BATCH if c.batch_id.upper() in named else P_NOTE_NAMES_ANOTHER_BATCH
        )
    return out


def note_date_likelihood(
    facts: NoteFacts,
    candidates: CandidateSet,
    registry: RegistryEvidence | None,
) -> dict[str, float]:
    """How likely the note is, given a print date stamped on the goods.

    Only usable once we know when each batch was made, which comes from the paid
    registry lookup. Before that a date says nothing about which batch it is.
    """
    out = {c.name: P_NO_INFORMATION for c in candidates.candidates}
    if not facts.print_dates or registry is None or not registry.available:
        return out

    for c in candidates.candidates:
        if c.is_catch_all or c.batch_id is None:
            continue
        record = registry.record(c.batch_id)
        if record is None:
            continue
        out[c.name] = (
            P_DATE_MATCHES if record.manufactured in facts.print_dates else P_DATE_MISMATCH
        )
    return out
