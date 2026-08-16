"""The stock ledger: what the system believes is on each shelf.

Append-only. Nothing is ever edited or deleted. Every row says where units came
from, where they went, and which decision caused the move, so any wrong number
can be traced back to the decision that produced it and undone.

Two ideas do most of the work here.

**Movements are transfers, not adjustments.** Every row moves a positive number
of units from one place to another. Places outside the warehouse (the customer,
the dispatch door, scrap) are named explicitly. So "units in equals units out"
is true by construction rather than by convention, and stock cannot be created
by a row that forgets to subtract somewhere else.

**A position is a lot at a location.** A lot is what we *believe* the units are:
a batch identity and a best-before date. Both may be unknown. Changing what we
believe is therefore a move between positions, which means an identification is
an event in the log with a decision attached to it, not a silent overwrite.

This module never sees ground truth. It records beliefs. Comparing those beliefs
against what is actually true is drift, and that lives in ``drift.py``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

#: Places outside the warehouse. Stock arriving from or leaving to one of these
#: is not an error - it is the boundary. Everything else must be a real bin.
OUTSIDE_PREFIX = "@"
CUSTOMER = "@customer"
SUPPLIER = "@supplier"
DISPATCH = "@dispatch"
SCRAP = "@scrap"

#: Where returns land before anyone decides what they are.
RECEIVING_BIN = "R-00-01"
#: Where stock waits when the agent hands the return to a person. The hold bin
#: is not named here on purpose: it comes from the action the agent chose, so
#: there is only ever one source of truth for where the stock was put.
REVIEW_BIN = "Q-01-01"


#: The two steps a return goes through. Both the code that writes movements and
#: the code that measures drift need to agree on what a placement is called, so
#: the name is defined once, here, rather than matched on by bin or by kind.
INTAKE_STEP = "intake"
PLACEMENT_STEP = "placement"


def decision_id(return_id: str, step: str) -> str:
    return f"{return_id}:{step}"


class Kind(StrEnum):
    RECEIPT = "receipt"
    """Units arriving from outside."""
    PUTAWAY = "putaway"
    """A move between bins that does not change what we believe the units are."""
    RECLASSIFY = "reclassify"
    """We changed our mind about which batch these units are, or when they expire."""
    PICK = "pick"
    """Units leaving to a customer order."""
    WRITE_OFF = "write_off"
    """Units leaving to scrap."""


@dataclass(frozen=True)
class Lot:
    """What we believe a group of units is.

    ``batch_id`` of ``None`` means we have not identified it. ``best_before`` of
    ``None`` means we have no date for it either. The two are separate on
    purpose: segregated stock has a working expiry date without a batch.
    """

    batch_id: str | None = None
    best_before: date | None = None

    @property
    def identified(self) -> bool:
        return self.batch_id is not None

    def __str__(self) -> str:
        batch = self.batch_id or "unidentified"
        return f"{batch}@{self.best_before.isoformat() if self.best_before else 'no-date'}"


@dataclass(frozen=True)
class Position:
    """A lot at a place. This is the thing that holds a quantity."""

    sku_id: str
    bin_id: str
    lot: Lot = Lot()

    @property
    def outside(self) -> bool:
        return self.bin_id.startswith(OUTSIDE_PREFIX)

    def __str__(self) -> str:
        return f"{self.sku_id} {self.lot} in {self.bin_id}"


@dataclass(frozen=True)
class Movement:
    seq: int
    at: date
    kind: Kind
    quantity: int
    source: Position
    destination: Position
    #: The return this movement belongs to, when it belongs to one.
    return_id: str | None = None
    #: The decision that caused it. This is the link back into the trace.
    decision: str | None = None
    #: Set when this row undoes an earlier one.
    reverses: int | None = None
    reason: str = ""

    @property
    def is_reversal(self) -> bool:
        return self.reverses is not None

    def undone(self, seq: int, at: date, reason: str) -> Movement:
        """The row that puts this one back."""
        return replace(
            self,
            seq=seq,
            at=at,
            source=self.destination,
            destination=self.source,
            reverses=self.seq,
            reason=reason,
        )


class LedgerError(Exception):
    """A movement was rejected. The ledger is unchanged."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_movements (
    seq            INTEGER PRIMARY KEY,
    at             TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    sku_id         TEXT    NOT NULL,
    from_bin       TEXT    NOT NULL,
    from_batch     TEXT,
    from_expiry    TEXT,
    to_bin         TEXT    NOT NULL,
    to_batch       TEXT,
    to_expiry      TEXT,
    return_id      TEXT,
    decision       TEXT,
    reverses       INTEGER REFERENCES stock_movements(seq),
    reason         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_movements_return ON stock_movements(return_id);
CREATE INDEX IF NOT EXISTS idx_movements_decision ON stock_movements(decision);

-- Append-only, enforced by the database rather than by good manners. Code that
-- tries to edit history fails loudly instead of quietly rewriting it.
CREATE TRIGGER IF NOT EXISTS stock_movements_no_update
BEFORE UPDATE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'the stock ledger is append-only: rows cannot be updated');
END;
CREATE TRIGGER IF NOT EXISTS stock_movements_no_delete
BEFORE DELETE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'the stock ledger is append-only: rows cannot be deleted');
END;
"""


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Ledger:
    """Append-only stock movements, held in SQLite.

    Pass an existing connection to share the warehouse database, or nothing for
    a standalone in-memory one.
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn if conn is not None else sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # Running totals, so appending stays cheap when the simulation posts
        # tens of thousands of movements. `check_balances` proves the cache
        # still agrees with the rows.
        self._held: dict[Position, int] = defaultdict(int)
        self._last: Movement | None = None
        for movement in self.movements():
            self._apply(movement)

    # ---------------------------------------------------------------- writing

    def append(
        self,
        at: date,
        kind: Kind,
        quantity: int,
        source: Position,
        destination: Position,
        return_id: str | None = None,
        decision: str | None = None,
        reason: str = "",
        reverses: int | None = None,
    ) -> Movement:
        """Add one movement, or raise and change nothing.

        Every rule below is a thing that would otherwise show up much later as a
        stock figure nobody can explain.
        """
        if quantity <= 0:
            raise LedgerError(f"quantity must be positive, got {quantity}")
        if source.sku_id != destination.sku_id:
            raise LedgerError(
                f"a movement cannot change the product: {source.sku_id} -> {destination.sku_id}"
            )
        if source == destination:
            raise LedgerError(f"movement goes nowhere: {source}")
        if source.outside and destination.outside:
            raise LedgerError("a movement between two places outside the warehouse is not stock")

        moved_lot = source.lot != destination.lot
        inside = not source.outside and not destination.outside
        if moved_lot and inside and kind is not Kind.RECLASSIFY:
            raise LedgerError(
                f"changing what we believe the units are is a {Kind.RECLASSIFY}, not a {kind}: "
                f"{source.lot} -> {destination.lot}"
            )
        if kind is Kind.RECLASSIFY and not moved_lot:
            raise LedgerError("a reclassify that changes nothing is not a reclassify")

        last = self._last
        if last is not None and at < last.at:
            raise LedgerError(
                f"movements are posted in order: {at} is before the last movement on {last.at}"
            )

        if not source.outside:
            held = self.quantity_at(source)
            if held < quantity:
                raise LedgerError(
                    f"only {held} units at {source}, cannot move {quantity}. "
                    f"Stock cannot go negative."
                )

        seq = (last.seq + 1) if last is not None else 1
        movement = Movement(
            seq=seq,
            at=at,
            kind=kind,
            quantity=quantity,
            source=source,
            destination=destination,
            return_id=return_id,
            decision=decision,
            reverses=reverses,
            reason=reason,
        )
        self._insert(movement)
        self._apply(movement)
        return movement

    def _apply(self, m: Movement) -> None:
        """Fold one movement into the running totals."""
        if not m.source.outside:
            self._held[m.source] -= m.quantity
        if not m.destination.outside:
            self._held[m.destination] += m.quantity
        self._last = m

    def _insert(self, m: Movement) -> None:
        self.conn.execute(
            "INSERT INTO stock_movements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.seq,
                m.at.isoformat(),
                str(m.kind),
                m.quantity,
                m.source.sku_id,
                m.source.bin_id,
                m.source.lot.batch_id,
                _iso(m.source.lot.best_before),
                m.destination.bin_id,
                m.destination.lot.batch_id,
                _iso(m.destination.lot.best_before),
                m.return_id,
                m.decision,
                m.reverses,
                m.reason,
            ),
        )
        self.conn.commit()

    def reverse(self, seq: int, at: date, reason: str) -> Movement:
        """Undo one movement by appending its opposite.

        History is never rewritten. The original row stays exactly as it was and
        a new row puts the units back, so the log shows both that the mistake was
        made and that it was corrected.
        """
        original = self.get(seq)
        if original is None:
            raise LedgerError(f"no movement {seq}")
        already = self.conn.execute(
            "SELECT seq FROM stock_movements WHERE reverses = ?", (seq,)
        ).fetchone()
        if already is not None:
            raise LedgerError(f"movement {seq} was already reversed by {already['seq']}")
        if original.is_reversal:
            raise LedgerError(f"movement {seq} is itself a reversal; reverse the original instead")

        undo = original.undone(seq=0, at=at, reason=reason)
        return self.append(
            at=at,
            kind=undo.kind,
            quantity=undo.quantity,
            source=undo.source,
            destination=undo.destination,
            return_id=undo.return_id,
            decision=undo.decision,
            reason=reason,
            reverses=seq,
        )

    def undo_decision(self, decision: str, at: date, reason: str) -> list[Movement]:
        """Undo everything one decision caused, newest first.

        Newest first matters: the movements form a chain, and putting the last
        one back first is the only order that never asks a position for units it
        does not currently hold.
        """
        caused = [m for m in self.caused_by(decision) if not m.is_reversal]
        already = {
            row["reverses"]
            for row in self.conn.execute(
                "SELECT reverses FROM stock_movements WHERE reverses IS NOT NULL"
            )
        }
        out = []
        for m in sorted(caused, key=lambda m: m.seq, reverse=True):
            if m.seq in already:
                continue
            out.append(self.reverse(m.seq, at, reason))
        return out

    # ---------------------------------------------------------------- reading

    def _row(self, row: sqlite3.Row) -> Movement:
        return Movement(
            seq=row["seq"],
            at=date.fromisoformat(row["at"]),
            kind=Kind(row["kind"]),
            quantity=row["quantity"],
            source=Position(
                sku_id=row["sku_id"],
                bin_id=row["from_bin"],
                lot=Lot(row["from_batch"], _parse(row["from_expiry"])),
            ),
            destination=Position(
                sku_id=row["sku_id"],
                bin_id=row["to_bin"],
                lot=Lot(row["to_batch"], _parse(row["to_expiry"])),
            ),
            return_id=row["return_id"],
            decision=row["decision"],
            reverses=row["reverses"],
            reason=row["reason"],
        )

    def movements(self) -> list[Movement]:
        return [
            self._row(r) for r in self.conn.execute("SELECT * FROM stock_movements ORDER BY seq")
        ]

    def get(self, seq: int) -> Movement | None:
        row = self.conn.execute("SELECT * FROM stock_movements WHERE seq = ?", (seq,)).fetchone()
        return self._row(row) if row else None

    def last(self) -> Movement | None:
        row = self.conn.execute(
            "SELECT * FROM stock_movements ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return self._row(row) if row else None

    def caused_by(self, decision: str) -> list[Movement]:
        return [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM stock_movements WHERE decision = ? ORDER BY seq", (decision,)
            )
        ]

    def for_return(self, return_id: str) -> list[Movement]:
        return [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM stock_movements WHERE return_id = ? ORDER BY seq", (return_id,)
            )
        ]

    # ---------------------------------------------------------------- balances

    def balances(self, on: date | None = None) -> dict[Position, int]:
        """Units held at every position inside the warehouse.

        Pass ``on`` to see the position as at the end of that day, which is what
        the drift chart walks over. Positions holding nothing are left out.
        """
        if on is None:
            return {p: units for p, units in self._held.items() if units != 0}
        held: dict[Position, int] = defaultdict(int)
        for m in self.movements():
            if m.at > on:
                break
            if not m.source.outside:
                held[m.source] -= m.quantity
            if not m.destination.outside:
                held[m.destination] += m.quantity
        return {position: units for position, units in held.items() if units != 0}

    def quantity_at(self, position: Position) -> int:
        if position.outside:
            raise LedgerError(f"{position.bin_id} is outside the warehouse; it has no balance")
        return self._held.get(position, 0)

    def on_hand(self, sku_id: str, on: date | None = None) -> int:
        """Total units of one product inside the warehouse."""
        return sum(units for p, units in self.balances(on).items() if p.sku_id == sku_id)

    def flow(self, sku_id: str) -> tuple[int, int, int]:
        """Units in from outside, units out to outside, units currently inside.

        The first two minus each other must equal the third. That is the "units
        in equals units out" property, and because every row is a transfer it is
        the only place stock can be created or destroyed.
        """
        arrived = departed = 0
        for m in self.movements():
            if m.source.sku_id != sku_id:
                continue
            if m.source.outside:
                arrived += m.quantity
            if m.destination.outside:
                departed += m.quantity
        return arrived, departed, self.on_hand(sku_id)

    def check_balances(self) -> None:
        """Prove the running totals still match the rows they came from.

        The totals are a cache. A cache that silently falls out of step with the
        log would make every stock figure wrong in a way nothing else here would
        notice, so it is checked rather than assumed.
        """
        from_rows: dict[Position, int] = defaultdict(int)
        for m in self.movements():
            if not m.source.outside:
                from_rows[m.source] -= m.quantity
            if not m.destination.outside:
                from_rows[m.destination] += m.quantity
        live = {p: n for p, n in from_rows.items() if n != 0}
        cached = {p: n for p, n in self._held.items() if n != 0}
        if live != cached:
            raise LedgerError(f"running totals do not match the rows: {live} vs {cached}")
