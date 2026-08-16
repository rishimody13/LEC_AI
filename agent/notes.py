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


def note_likelihood(
    facts: NoteFacts,
    candidates: CandidateSet,
    registry: RegistryEvidence | None,
) -> dict[str, float]:
    """How likely the note is, for each candidate.

    Only usable once we know when each batch was made, which comes from the paid
    registry lookup. Before that the note says nothing about which batch it is.
    """
    flat = {c.name: P_NO_INFORMATION for c in candidates.candidates}
    if not facts.print_dates or registry is None or not registry.available:
        return flat

    out: dict[str, float] = {}
    for c in candidates.candidates:
        if c.is_catch_all or c.batch_id is None:
            out[c.name] = P_NO_INFORMATION
            continue
        record = registry.record(c.batch_id)
        if record is None:
            out[c.name] = P_NO_INFORMATION
            continue
        out[c.name] = (
            P_DATE_MATCHES if record.manufactured in facts.print_dates else P_DATE_MISMATCH
        )
    return out
