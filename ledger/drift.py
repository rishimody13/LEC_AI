"""Drift: the gap between what the ledger believes and what is actually true.

The ledger records beliefs and never sees the answers. This module is the only
place the two meet, which is why it lives outside the ledger itself: if the
ledger could read ground truth, a drift of zero would prove nothing.

Drift is measured at the moment stock is placed, per return, so every number
here points back at the decision that caused it. Measuring it from the shelf
balances instead would be neater to look at and useless to act on, because once
units of the same believed batch pool together you can no longer say which
decision put the wrong ones there.

The headline number is **overstated expiry in unit-days**. Picking runs
first-expired-first-out, so stock recorded as lasting longer than it really does
sits on the shelf past the point it should have gone, then ships. Understated
expiry is counted separately because it is a different problem: it wastes shelf
life, it does not send anyone bad stock.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from world.types import World

from .ledger import PLACEMENT_STEP, Ledger, Movement, decision_id


@dataclass
class TruthBook:
    """The real answers. Kept in one object so it is obvious what is ground truth."""

    #: return id -> the batch the stock really is
    true_batch: dict[str, str] = field(default_factory=dict)
    #: batch id -> its real best-before date
    best_before: dict[str, date] = field(default_factory=dict)
    #: batch id -> the bin that batch really lives in
    home_bin: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_world(cls, world: World) -> TruthBook:
        return cls(
            true_batch={r.return_id: r.true_batch_id for r in world.returns},
            best_before={b.batch_id: b.best_before for b in world.batches},
            home_bin={b.batch_id: b.home_bin for b in world.batches},
        )


@dataclass
class ReturnDrift:
    """What one decision got wrong, and which decision it was."""

    return_id: str
    decision: str
    at: date
    quantity: int
    recorded_batch: str | None
    recorded_expiry: date | None
    recorded_bin: str
    true_batch: str
    true_expiry: date
    true_bin: str

    @property
    def wrong_bin(self) -> bool:
        """Only meaningful once we have named a batch.

        Stock we deliberately held back is in the hold area on purpose. Calling
        that a wrong bin would report a correct decision as drift.
        """
        return self.identified and self.recorded_bin != self.true_bin

    @property
    def identified(self) -> bool:
        return self.recorded_batch is not None

    @property
    def misattributed(self) -> bool:
        return self.identified and self.recorded_batch != self.true_batch

    @property
    def expiry_error_days(self) -> int | None:
        """Positive means the ledger thinks the stock lasts longer than it does."""
        if self.recorded_expiry is None:
            return None
        return (self.recorded_expiry - self.true_expiry).days

    @property
    def overstated_unit_days(self) -> int:
        days = self.expiry_error_days
        return self.quantity * days if days is not None and days > 0 else 0

    @property
    def understated_unit_days(self) -> int:
        days = self.expiry_error_days
        return self.quantity * -days if days is not None and days < 0 else 0


@dataclass
class Drift:
    entries: list[ReturnDrift] = field(default_factory=list)

    @property
    def units_placed(self) -> int:
        return sum(e.quantity for e in self.entries)

    @property
    def units_identified(self) -> int:
        return sum(e.quantity for e in self.entries if e.identified)

    @property
    def units_misattributed(self) -> int:
        return sum(e.quantity for e in self.entries if e.misattributed)

    @property
    def units_without_a_date(self) -> int:
        """Stock sitting in the warehouse with no expiry against it at all.

        Not wrong, but not safe either: first-expired-first-out cannot rank what
        it cannot date, so these units need a person before they can be picked.
        """
        return sum(e.quantity for e in self.entries if e.recorded_expiry is None)

    @property
    def overstated_unit_days(self) -> int:
        """The dangerous total. Zero is the only good value."""
        return sum(e.overstated_unit_days for e in self.entries)

    @property
    def understated_unit_days(self) -> int:
        return sum(e.understated_unit_days for e in self.entries)

    @property
    def wrong_bin_units(self) -> int:
        """Only counts identified stock: held stock is meant to be elsewhere."""
        return sum(e.quantity for e in self.entries if e.wrong_bin)

    @property
    def misattribution_rate(self) -> float:
        return self.units_misattributed / self.units_identified if self.units_identified else 0.0

    @property
    def clean(self) -> bool:
        """No stock filed under the wrong batch, and no expiry on the late side."""
        return self.units_misattributed == 0 and self.overstated_unit_days == 0

    def worst(self, limit: int = 5) -> list[ReturnDrift]:
        return sorted(self.entries, key=lambda e: e.overstated_unit_days, reverse=True)[:limit]

    def over_time(self) -> list[tuple[date, int]]:
        """Running total of overstated unit-days, by day.

        This is the compounding curve: each wrong decision adds to it and nothing
        takes it away, which is the visual argument for "fails silently".
        """
        daily: dict[date, int] = defaultdict(int)
        for e in self.entries:
            daily[e.at] += e.overstated_unit_days
        running = 0
        out = []
        for day in sorted(daily):
            running += daily[day]
            out.append((day, running))
        return out

    def summary(self) -> str:
        return (
            f"{self.units_placed} units placed, "
            f"{self.units_misattributed} under the wrong batch "
            f"({self.misattribution_rate:.1%} of identified stock), "
            f"{self.overstated_unit_days} unit-days of expiry on the dangerous side, "
            f"{self.understated_unit_days} on the wasteful side, "
            f"{self.units_without_a_date} units with no date, "
            f"{self.wrong_bin_units} units in the wrong bin"
        )


def _live_placements(ledger: Ledger) -> list[Movement]:
    """The placement still in force for each return.

    A reversed placement did not happen as far as the shelf is concerned, and a
    replacement posted afterwards is the one that counts. Ignoring this would
    charge a return twice for a mistake that was corrected.
    """
    movements = ledger.movements()
    reversed_seqs = {m.reverses for m in movements if m.reverses is not None}
    live: dict[str, Movement] = {}
    for m in movements:
        if m.return_id is None or m.is_reversal or m.seq in reversed_seqs:
            continue
        if m.decision != decision_id(m.return_id, PLACEMENT_STEP):
            continue
        live[m.return_id] = m
    return list(live.values())


def measure(ledger: Ledger, truth: TruthBook) -> Drift:
    """Compare every placement in the ledger against the answer."""
    drift = Drift()
    for m in _live_placements(ledger):
        return_id = m.return_id
        assert return_id is not None  # _live_placements filtered these out
        true_batch = truth.true_batch.get(return_id)
        if true_batch is None:
            raise KeyError(f"no ground truth recorded for return {return_id}")
        drift.entries.append(
            ReturnDrift(
                return_id=return_id,
                decision=m.decision or "",
                at=m.at,
                quantity=m.quantity,
                recorded_batch=m.destination.lot.batch_id,
                recorded_expiry=m.destination.lot.best_before,
                recorded_bin=m.destination.bin_id,
                true_batch=true_batch,
                true_expiry=truth.best_before[true_batch],
                true_bin=truth.home_bin[true_batch],
            )
        )
    return drift
