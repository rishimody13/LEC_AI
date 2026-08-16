"""Physical impossibilities, kept separate from the hand-set likelihoods.

Some facts are not soft hints. A batch that had not cleared quality control when a
shipment left cannot have been in that shipment. Writing that as a likelihood of
0.02 buries it among numbers we picked by hand.

One rule: a broken constraint never drives a candidate to zero. Nothing recovers
from zero, and the source that told us about the constraint can itself be wrong.
So a broken constraint collapses the candidate to the chance that source is
mistaken.
"""

from __future__ import annotations

from dataclasses import dataclass

from .candidates import Candidate
from .evidence import RecordEvidence, RegistryEvidence, ReturnIntake

#: How often the batch registry is wrong. It is a regulated source, so rarely.
REGISTRY_ERROR_RATE = 0.01

#: How often the recorded shipment quantity is wrong.
QUANTITY_ERROR_RATE = 0.02


@dataclass(frozen=True)
class Violation:
    """A candidate that broke a hard rule, and how sure we are about the rule."""

    candidate: str
    rule: str
    detail: str
    #: What the candidate's likelihood is multiplied by. This is the chance the
    #: source that told us about the constraint got it wrong.
    survives_with: float


def check(
    candidates: list[Candidate],
    intake: ReturnIntake,
    records: RecordEvidence,
    registry: RegistryEvidence | None,
    off_record: bool = False,
) -> list[Violation]:
    """Find candidates that break a hard rule.

    Not every rule here is equally hard, and the difference matters.

    `quality_release_after_shipment` is a fact about the physical world: a batch
    that had not been made yet cannot have been in a lorry. Nothing makes that
    untrue.

    `never_allocated_to_customer` and `returned_more_than_shipped` are only
    impossible **if our records of what went where are complete**. Cross-docked
    stock, site transfers and returns of returns all break that assumption, and
    when the paperwork says the goods arrived that way (`off_record`) these two
    stop being rules at all.

    What is *not* here: anything about the label's characters. The label
    likelihood already models misreads and wrong labels, so treating the printed
    code as a hard rule would contradict it and count the same evidence twice.
    """
    out: list[Violation] = []
    out += _quality_release(candidates, records, registry)
    if not off_record:
        out += _never_allocated(candidates, intake, registry)
        out += _quantity(candidates, intake, records)
    return out


def _quality_release(
    candidates: list[Candidate], records: RecordEvidence, registry: RegistryEvidence | None
) -> list[Violation]:
    """A batch cannot be in a shipment that left before the batch was released."""
    if registry is None or not registry.available or not records.shipments:
        return []

    earliest = min(s.dispatched for s in records.shipments)
    out: list[Violation] = []
    for c in candidates:
        if c.is_catch_all or c.batch_id is None:
            continue
        record = registry.record(c.batch_id)
        if record is None:
            continue
        if record.qa_released > earliest:
            out.append(
                Violation(
                    candidate=c.name,
                    rule="quality_release_after_shipment",
                    detail=(
                        f"{c.batch_id} cleared quality control on "
                        f"{record.qa_released.isoformat()}, after the earliest shipment "
                        f"to this customer left on {earliest.isoformat()}"
                    ),
                    survives_with=REGISTRY_ERROR_RATE,
                )
            )
    return out


def _never_allocated(
    candidates: list[Candidate], intake: ReturnIntake, registry: RegistryEvidence | None
) -> list[Violation]:
    """A customer cannot return a batch that was never allocated to them."""
    if registry is None or not registry.available:
        return []

    out: list[Violation] = []
    for c in candidates:
        if c.is_catch_all or c.batch_id is None:
            continue
        record = registry.record(c.batch_id)
        # An empty allocation list means the lookup came back partial, not that
        # the batch went nowhere.
        if record is None or not record.allocated_to:
            continue
        if intake.customer_id not in record.allocated_to:
            out.append(
                Violation(
                    candidate=c.name,
                    rule="never_allocated_to_customer",
                    detail=(f"{c.batch_id} has never been allocated to {intake.customer_id}"),
                    survives_with=REGISTRY_ERROR_RATE,
                )
            )
    return out


def _quantity(
    candidates: list[Candidate], intake: ReturnIntake, records: RecordEvidence
) -> list[Violation]:
    """You cannot return more of a batch than was sent."""
    if not records.available:
        return []

    out: list[Violation] = []
    for c in candidates:
        if c.is_catch_all or c.source != "records":
            continue
        if c.shipped_quantity and intake.quantity > c.shipped_quantity:
            out.append(
                Violation(
                    candidate=c.name,
                    rule="returned_more_than_shipped",
                    detail=(
                        f"{intake.quantity} units returned but only "
                        f"{c.shipped_quantity} of {c.batch_id} were sent"
                    ),
                    survives_with=QUANTITY_ERROR_RATE,
                )
            )
    return out


def multipliers(violations: list[Violation]) -> dict[str, float]:
    """Combine violations into one multiplier per candidate."""
    out: dict[str, float] = {}
    for v in violations:
        out[v.candidate] = out.get(v.candidate, 1.0) * v.survives_with
    return out
