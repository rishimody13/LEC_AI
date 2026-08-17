"""First-expired-first-out picking.

This is where a wrong batch record turns into a real consequence, and the
mechanism is worth stating plainly because the whole harm argument rests on it.

Picking ranks stock by the expiry **the record says**. What actually ships is
whatever is physically in that location. When the two disagree the picker is not
malfunctioning - it is doing exactly its job, on a wrong number. Stock recorded
as lasting longer than it really does sinks to the back of the queue, waits until
the recorded date, and ships after it has really gone off. Nobody sees a fault at
any point.

Two things are deliberately not picked, and the cost model depends on both:

- **Stock with no recorded expiry.** First-expired-first-out cannot rank what it
  cannot date, so undated stock is invisible to this. The agent is allowed to
  place a hold with no date and be charged nothing for expiry risk, and that is
  only honest if the picker really does skip it.
- **Bins that are not active.** Hold and quarantine areas are not pick faces.

Both are asserted in the tests rather than assumed, because the segregate price
is built on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ledger.ledger import Ledger, Position
from world.types import Bin, BinStatus


@dataclass(frozen=True)
class Take:
    """Units to remove from one position."""

    position: Position
    quantity: int


@dataclass
class Plan:
    takes: list[Take]
    #: Units the order asked for that no pickable stock could cover.
    shortfall: int

    @property
    def picked(self) -> int:
        return sum(t.quantity for t in self.takes)


def pickable_bins(bins: list[Bin]) -> set[str]:
    """Pick faces. Hold and quarantine are not among them."""
    return {b.bin_id for b in bins if b.status is BinStatus.ACTIVE}


def available(ledger: Ledger, bins: list[Bin], sku_id: str, on: date) -> list[tuple[Position, int]]:
    """Stock that could be picked today, earliest recorded expiry first.

    Anything the record says has already expired is left out: a warehouse writes
    that off rather than shipping it. The dangerous case is the opposite one,
    where the record says there is time left and there is not, and that stock
    looks perfectly pickable here.
    """
    faces = pickable_bins(bins)
    out = [
        (position, units)
        for position, units in ledger.balances().items()
        if position.sku_id == sku_id
        and position.bin_id in faces
        and units > 0
        and position.lot.best_before is not None
        and position.lot.best_before >= on
    ]
    # Earliest recorded expiry first; bin id only to make ties repeatable.
    out.sort(key=lambda item: (item[0].lot.best_before or on, item[0].bin_id))
    return out


def plan(ledger: Ledger, bins: list[Bin], sku_id: str, quantity: int, on: date) -> Plan:
    """Work out where an order would be picked from."""
    takes: list[Take] = []
    remaining = quantity
    for position, units in available(ledger, bins, sku_id, on):
        if remaining <= 0:
            break
        take = min(units, remaining)
        takes.append(Take(position=position, quantity=take))
        remaining -= take
    return Plan(takes=takes, shortfall=remaining)
