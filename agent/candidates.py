"""The list of possible answers, and where each one came from.

A candidate is one claim: "these units are batch X, which expires on date Y".
Every candidate carries a probability and they add up to 1.

The list has to be complete or the probabilities mean nothing, so it is built from
several sources and always ends with a catch-all. A candidate that nobody thought
of would otherwise sit at zero forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from common.coding import BATCH_ID_RE, batch_id_from_label

from .evidence import BatchSummary, LabelEvidence, RecordEvidence, ReturnIntake

if TYPE_CHECKING:
    from .notes import NoteFacts

#: Share of returns that do not come from a shipment we have on record:
#: mis-picks, cross-docked stock, returns of returns.
UNRECORDED_SHARE = 0.08

#: Used instead when something tells us the shipment records are incomplete for
#: this stock - either the note (mixed pallet, cross-dock) or the records
#: themselves reporting a stale replica or no matching shipment. In those cases
#: "the answer is not on this list" stops being a long shot. Judged at roughly
#: two and a half times the base rate.
#:
#: Leaving this out was a real failure: with a reused label and a stale replica
#: the true batch appears in neither source, and the agent was filing the
#: impostor at 99.9% while holding the evidence that the records were incomplete.
UNRECORDED_SHARE_OFF_RECORD = 0.20

#: Name used for the "none of the above" candidate.
CATCH_ALL = "other"


@dataclass(frozen=True)
class Candidate:
    """One possible answer."""

    batch_id: str | None
    best_before: date | None = None
    home_bin: str | None = None
    #: Where this candidate came from, for the decision log.
    source: str = "records"
    #: Units of this batch the records say were sent to this customer.
    shipped_quantity: int = 0

    @property
    def is_catch_all(self) -> bool:
        return self.batch_id is None

    @property
    def name(self) -> str:
        return self.batch_id or CATCH_ALL


@dataclass
class CandidateSet:
    """The candidates and their starting probabilities."""

    candidates: list[Candidate] = field(default_factory=list)
    prior: dict[str, float] = field(default_factory=dict)
    #: Candidates the label named that no known batch matches. Kept out of the
    #: main list and folded into the catch-all instead.
    rejected: list[str] = field(default_factory=list)

    def by_name(self, name: str) -> Candidate:
        return next(c for c in self.candidates if c.name == name)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.candidates]


def records_look_incomplete(records: RecordEvidence) -> bool:
    """Do the records say, in effect, that they may be missing something?

    A replica that has not caught up is missing recent shipments by definition.
    A query that matched nothing for a customer who is returning goods is either
    missing them or looking in the wrong place. Either way the chance that the
    answer is not on the list goes up.
    """
    from .evidence import RecordSymptom

    return bool(records.symptoms & {RecordSymptom.REPLICA_LAG, RecordSymptom.NO_MATCH})


def build(
    intake: ReturnIntake,
    records: RecordEvidence,
    label: LabelEvidence,
    catalogue: dict[str, BatchSummary],
    note: NoteFacts | None = None,
) -> CandidateSet:
    """Build the candidate list and its starting probabilities.

    Sources, in order:
      1. every batch the records say went to this customer
      2. every known batch whose code fits the characters we could read
      3. any batch code the model read out of the condition note
      4. a catch-all for everything else

    Rule 3 is the one job here that a database query cannot do. Warehouse notes are
    prose, and a lot code written in a sentence ("inner cases stamped B-2296") is
    invisible to a SELECT. The model reads it out; this code checks it against the
    real batch list before it becomes a candidate, so an invented code cannot win.

    Rule 2 matters more than it looks. In the case where the warehouse replica is
    stale, the batch that is actually in the box is missing from the records, and
    the label is too damaged to name it outright. Without this rule the true
    answer would never even be on the list.

    `catalogue` is every batch of this product that exists. A code the label
    names that is not in it is recorded as rejected and its mass goes to the
    catch-all, so an invented code cannot win.
    """
    candidates: list[Candidate] = []
    rejected: list[str] = []
    seen: set[str] = set()

    shipped: dict[str, int] = {}
    for s in records.shipments:
        shipped[s.batch_id] = shipped.get(s.batch_id, 0) + s.quantity

    for batch_id, quantity in shipped.items():
        summary = catalogue.get(batch_id)
        candidates.append(
            Candidate(
                batch_id=batch_id,
                best_before=summary.best_before if summary else None,
                home_bin=summary.home_bin if summary else None,
                source="records",
                shipped_quantity=quantity,
            )
        )
        seen.add(batch_id)

    for batch_id in _fits_the_fragment(label, catalogue):
        if batch_id in seen:
            continue
        summary = catalogue[batch_id]
        candidates.append(
            Candidate(
                batch_id=batch_id,
                best_before=summary.best_before,
                home_bin=summary.home_bin,
                source="label",
            )
        )
        seen.add(batch_id)

    for raw_code in note.batch_codes_mentioned if note else []:
        from_note = _as_batch_id(raw_code)
        if from_note is None or from_note not in catalogue:
            rejected.append(raw_code)
            continue
        if from_note in seen:
            continue
        summary = catalogue[from_note]
        candidates.append(
            Candidate(
                batch_id=from_note,
                best_before=summary.best_before,
                home_bin=summary.home_bin,
                source="note",
            )
        )
        seen.add(from_note)

    claimed = batch_id_from_label(label.code_text) if label.code_text else None
    if claimed and claimed not in seen:
        if catalogue:
            # We can check the batch list, and this code is not on it.
            rejected.append(claimed)
        else:
            # No catalogue to check against - the warehouse system is down. The
            # label still read a well-formed code, so it goes on the list with
            # what we know, which is nothing but the name. Without this a
            # timeout throws away a perfectly legible label and leaves the agent
            # with only the catch-all.
            candidates.append(
                Candidate(
                    batch_id=claimed,
                    best_before=label.best_before,
                    home_bin=None,
                    source="label_unverified",
                )
            )
            seen.add(claimed)

    candidates.append(Candidate(batch_id=None, source="catch_all"))

    off_record = bool(note and note.suggests_off_record_stock) or records_look_incomplete(records)
    return CandidateSet(
        candidates=candidates,
        prior=_prior(candidates, catalogue, off_record=off_record),
        rejected=rejected,
    )


def _as_batch_id(raw: str) -> str | None:
    """Turn a code written in prose into a batch id, if it looks like one.

    Accepts either the short form (B-2296) or the full printed code (B-2296-9).
    Anything else is refused rather than guessed at.
    """
    text = raw.strip().upper()
    if BATCH_ID_RE.match(text):
        return text
    return batch_id_from_label(text)


def _fits_the_fragment(label: LabelEvidence, catalogue: dict[str, BatchSummary]) -> list[str]:
    """Known batches whose printed code starts with the characters we could read."""
    from common.coding import label_code

    if not label.is_usable or not label.code_text:
        return []
    fragment = label.code_text
    return [b for b in sorted(catalogue) if label_code(b).startswith(fragment)]


def _prior(
    candidates: list[Candidate],
    catalogue: dict[str, BatchSummary],
    *,
    off_record: bool = False,
) -> dict[str, float]:
    """Starting probabilities, taken from how much of each batch exists.

    The prior must not be built from the shipment records, because those records
    are then used again as evidence. Using them twice counts the same fact twice
    and makes the agent far too sure of whatever the records say - which is
    exactly wrong when the records are the source that failed.

    So the prior comes from stock on hand instead: a batch with more units in the
    building is more likely to be the one that came back. The records then do
    their work as evidence, once.
    """
    unrecorded = UNRECORDED_SHARE_OFF_RECORD if off_record else UNRECORDED_SHARE
    named = [c for c in candidates if not c.is_catch_all]
    catch_all = [c for c in candidates if c.is_catch_all]

    prior: dict[str, float] = {}
    if named:
        weights = {
            c.name: float(catalogue[c.name].quantity_on_hand) if c.batch_id in catalogue else 1.0
            for c in named
        }
        total = sum(weights.values()) or 1.0
        share = (1.0 - unrecorded) if catch_all else 1.0
        for c in named:
            prior[c.name] = share * weights[c.name] / total

    if catch_all:
        each = unrecorded / len(catch_all) if named else 1.0 / len(catch_all)
        for c in catch_all:
            prior[c.name] = each

    total_p = sum(prior.values())
    return {k: v / total_p for k, v in prior.items()}
