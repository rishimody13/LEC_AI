"""Ways of deciding what a returned carton is.

The agent is one of these. The others are the things a warehouse actually does
today, and they are here to be beaten - or not. A comparison against nothing
proves nothing, and a comparison against a straw man proves less than nothing.

Each policy gets exactly the same evidence and returns the same shape of answer,
so the simulation can run any of them through the same days and the same orders
and attribute the difference to the decision rather than to luck.

`oracle` is not a policy anyone could run. It knows the answer. It is the floor:
the harm that remains when every identification is correct, which is what the
others should be measured against rather than against zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from agent import loop, notes, policy
from agent.evidence import BatchSummary, ReturnIntake
from agent.harm import CostModel
from agent.reliability import ReliabilityModel
from common.coding import batch_id_from_label


@dataclass
class Decision:
    """What a policy decided to do with one return."""

    #: Batch it filed the stock under, or None if it did not file it.
    batch_id: str | None
    bin_id: str | None
    #: Expiry written against the stock. None means undated, which cannot be picked.
    best_before: date | None
    #: True when it handed the return to a person instead of deciding.
    escalated: bool = False
    #: What it spent on lookups.
    spend_gbp: float = 0.0
    #: How sure it was, where it says. Used only for reporting.
    confidence: float | None = None


class Policy(Protocol):
    name: str

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision: ...


def _summary(
    catalogue: Mapping[str, BatchSummary], batch_id: str | None
) -> tuple[date | None, str | None]:
    entry = catalogue.get(batch_id) if batch_id else None
    if entry is None:
        return None, None
    return entry.best_before, entry.home_bin


@dataclass
class Agent:
    """RECONCILE: probabilities, then the cheapest action."""

    note_reader: notes.NoteReader
    name: str = "agent"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        # A generated stream carries its own note facts; the recorded cassettes
        # only cover the hand-written cases.
        reader = getattr(services, "note_reader", self.note_reader)
        result = loop.run(intake, services, costs, rel, reader)
        if result.escalated or result.placement is None:
            return Decision(
                batch_id=None,
                bin_id=None,
                best_before=None,
                escalated=True,
                spend_gbp=result.spend_gbp,
            )
        action = result.placement.chosen.action
        return Decision(
            batch_id=action.batch_id,
            bin_id=action.bin_id,
            best_before=action.recorded_best_before,
            spend_gbp=result.spend_gbp,
            confidence=result.belief.best()[1],
        )


@dataclass
class TrustTheLabel:
    """What most warehouses do: read the box, believe the box.

    This is the policy the hero case is built to defeat, and it is not a straw
    man - it is right most of the time, which is exactly why it survives in real
    operations and why its failures go unnoticed.
    """

    name: str = "trust the label"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        label = services.read_label(intake)
        catalogue = services.batch_catalogue(intake.sku_id)
        claimed = batch_id_from_label(label.code_text) if label.code_text else None
        if claimed is None or claimed not in catalogue:
            # Nothing legible. Falls back to a person, because there is nothing
            # else this policy can do.
            return Decision(batch_id=None, bin_id=None, best_before=None, escalated=True)
        best_before, home_bin = _summary(catalogue, claimed)
        return Decision(batch_id=claimed, bin_id=home_bin, best_before=best_before)


@dataclass
class TrustTheRecords:
    """Believe the warehouse system: file it as whatever we shipped most of."""

    name: str = "trust the records"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        records = services.query_records(intake)
        catalogue = services.batch_catalogue(intake.sku_id)
        totals: dict[str, int] = {}
        for shipment in records.shipments:
            totals[shipment.batch_id] = totals.get(shipment.batch_id, 0) + shipment.quantity
        if not totals:
            return Decision(batch_id=None, bin_id=None, best_before=None, escalated=True)
        best = max(totals, key=lambda b: totals[b])
        best_before, home_bin = _summary(catalogue, best)
        return Decision(batch_id=best, bin_id=home_bin, best_before=best_before)


@dataclass
class AlwaysEscalate:
    """Send every return to a person. Safe, and the most expensive thing here."""

    name: str = "always escalate"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        return Decision(batch_id=None, bin_id=None, best_before=None, escalated=True)


@dataclass
class AlwaysSegregate:
    """Hold everything under the earliest expiry any batch could have.

    Never ships expired stock and never sells anything either, because held stock
    is not on a pick face. The point of including it is to show that the safety
    property on its own is trivially satisfiable and therefore not the target.
    """

    name: str = "always segregate"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        catalogue = services.batch_catalogue(intake.sku_id)
        dates = [b.best_before for b in catalogue.values() if b.best_before is not None]
        return Decision(
            batch_id=None,
            bin_id=policy.HOLD_BIN,
            best_before=min(dates) if dates else None,
        )


@dataclass
class Oracle:
    """Knows the answer. Not runnable - it is the floor for everything else."""

    truth: dict[str, str]
    name: str = "oracle"

    def decide(
        self, intake: ReturnIntake, services: loop.Services, costs: CostModel, rel: ReliabilityModel
    ) -> Decision:
        batch = self.truth[intake.return_id]
        catalogue = services.batch_catalogue(intake.sku_id)
        best_before, home_bin = _summary(catalogue, batch)
        return Decision(batch_id=batch, bin_id=home_bin, best_before=best_before, confidence=1.0)
