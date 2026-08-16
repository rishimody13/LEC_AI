"""Turning one agent decision into stock movements.

Every return produces exactly two rows:

1. A **receipt**: the units arrive from the customer into goods-in. At this point
   nobody knows what they are, so they are booked as unidentified.
2. A **placement**: what the agent decided. Filing them under a batch is a
   reclassify, because it changes what we believe the units are. Holding them
   back is also a reclassify - it puts a working expiry date on them without
   naming a batch. Handing them to a person is a plain move to the review area,
   because nothing about our belief changed.

Escalation still posts. Stock a person is looking at is real stock sitting in a
real place, and leaving it out of the ledger would mean the units in the building
no longer matched the units in the log.

Both rows carry the decision that caused them, so ``ledger.undo_decision`` can
put a return back exactly as it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.evidence import ReturnIntake
from agent.loop import Result
from agent.policy import Kind as ActionKind

from .ledger import (
    CUSTOMER,
    INTAKE_STEP,
    PLACEMENT_STEP,
    RECEIVING_BIN,
    REVIEW_BIN,
    Kind,
    Ledger,
    Lot,
    Movement,
    Position,
    decision_id,
)


def post(ledger: Ledger, intake: ReturnIntake, result: Result) -> list[Movement]:
    """Record what happened to one return. Returns the rows written."""
    unidentified = Position(sku_id=intake.sku_id, bin_id=RECEIVING_BIN, lot=Lot())

    receipt = ledger.append(
        at=intake.arrived,
        kind=Kind.RECEIPT,
        quantity=intake.quantity,
        source=Position(sku_id=intake.sku_id, bin_id=CUSTOMER, lot=Lot()),
        destination=unidentified,
        return_id=intake.return_id,
        decision=decision_id(intake.return_id, INTAKE_STEP),
        reason="partial return received at goods-in",
    )

    landing = _destination(intake, result, unidentified)
    placement = ledger.append(
        return_id=intake.return_id,
        decision=decision_id(intake.return_id, PLACEMENT_STEP),
        at=intake.arrived,
        quantity=intake.quantity,
        source=unidentified,
        kind=landing.kind,
        destination=landing.destination,
        reason=landing.reason,
    )
    return [receipt, placement]


@dataclass(frozen=True)
class Landing:
    """Where a decision puts the stock, and what to call the move."""

    kind: Kind
    destination: Position
    reason: str


def _destination(intake: ReturnIntake, result: Result, unidentified: Position) -> Landing:
    if result.escalated or result.placement is None:
        return Landing(
            kind=Kind.PUTAWAY,
            destination=Position(sku_id=intake.sku_id, bin_id=REVIEW_BIN, lot=Lot()),
            reason="handed to a person; still unidentified",
        )

    action = result.placement.chosen.action
    lot = Lot(batch_id=action.batch_id, best_before=action.recorded_best_before)

    if action.kind is ActionKind.COMMIT:
        reason = f"filed as {action.batch_id}"
    else:
        reason = "held back under the earliest expiry any live candidate could have"

    if action.bin_id is None:
        raise ValueError(f"{action.kind} action has no bin to post the stock to")

    destination = Position(sku_id=intake.sku_id, bin_id=action.bin_id, lot=lot)
    # A segregate with no candidate dates at all leaves the lot unchanged, and a
    # move that changes nothing is a putaway, not a reclassify.
    kind = Kind.RECLASSIFY if destination.lot != unidentified.lot else Kind.PUTAWAY
    return Landing(kind=kind, destination=destination, reason=reason)
