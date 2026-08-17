"""What is really on each shelf.

The ledger records what the warehouse *believes* is at each position. This keeps
the other half: which batch the units physically are. The two are the same thing
whenever a decision was right and different whenever it was wrong, and the gap
between them is the whole subject of this project.

It is deliberately a separate object rather than a field on the ledger. If the
ledger could see this, every stock figure would be right by construction and the
simulation would prove nothing.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ledger.ledger import Position


class TruthTracker:
    """Real batch composition, position by position."""

    def __init__(self) -> None:
        self._real: dict[Position, Counter[str]] = defaultdict(Counter)

    def add(self, position: Position, true_batch: str, quantity: int) -> None:
        self._real[position][true_batch] += quantity

    def at(self, position: Position) -> Counter[str]:
        return self._real[position]

    def units_at(self, position: Position) -> int:
        return sum(self._real[position].values())

    def take(self, position: Position, quantity: int) -> Counter[str]:
        """Remove units and say which batches they really were.

        Units of the same believed lot are interchangeable to a picker, so when a
        position holds more than one true batch the pick is split across them in
        proportion to what is there. Taking them in, say, alphabetical order
        instead would quietly ship one batch before another and bias every
        expiry number that follows.
        """
        held = self._real[position]
        total = sum(held.values())
        if quantity > total:
            raise ValueError(f"cannot take {quantity} from {position}, only {total} there")

        taken: Counter[str] = Counter()
        if total == 0 or quantity == 0:
            return taken

        # Largest remainder, so the split is fair and repeatable.
        exact = {batch: units * quantity / total for batch, units in held.items()}
        for batch, share in exact.items():
            taken[batch] = int(share)
        short = quantity - sum(taken.values())
        for batch, _ in sorted(exact.items(), key=lambda kv: (-(kv[1] % 1), kv[0])):
            if short <= 0:
                break
            taken[batch] += 1
            short -= 1

        for batch, units in taken.items():
            held[batch] -= units
            if held[batch] <= 0:
                del held[batch]
        return taken

    def move(self, source: Position, destination: Position, quantity: int) -> Counter[str]:
        """Move units between positions, carrying their real identity with them."""
        moved = self.take(source, quantity)
        for batch, units in moved.items():
            self.add(destination, batch, units)
        return moved

    def total(self, sku_id: str) -> int:
        return sum(
            units
            for position, held in self._real.items()
            if position.sku_id == sku_id
            for units in held.values()
        )
