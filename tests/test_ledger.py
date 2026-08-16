"""What must be true of the stock record, whatever decisions went into it.

These are properties rather than expected values. Nothing here says which batch
anything should be filed under; the ledger has no opinion about that. It says the
log cannot be rewritten, stock cannot be created or lost, and any decision can be
put back exactly as it was.

The last few tests build cases from seeds and post whatever the agent decides, so
they cover placements nobody wrote down in advance.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agent.harm import load_costs
from agent.reliability import load_reliability
from harness import generate
from ledger import posting
from ledger.drift import TruthBook, measure
from ledger.ledger import (
    CUSTOMER,
    DISPATCH,
    INTAKE_STEP,
    PLACEMENT_STEP,
    RECEIVING_BIN,
    Kind,
    Ledger,
    LedgerError,
    Lot,
    Movement,
    Position,
    decision_id,
)

SKU = "SKU-4471"
DAY = date(2026, 3, 2)
COSTS = load_costs()
RELIABILITY = load_reliability()


def outside() -> Position:
    return Position(SKU, CUSTOMER)


def goods_in() -> Position:
    return Position(SKU, RECEIVING_BIN)


def shelf(batch: str = "B-2291", when: date = date(2027, 5, 1)) -> Position:
    return Position(SKU, "A-07-02", Lot(batch, when))


@pytest.fixture
def book() -> Ledger:
    return Ledger()


def receive(book: Ledger, quantity: int = 84, at: date = DAY) -> Movement:
    return book.append(
        at=at,
        kind=Kind.RECEIPT,
        quantity=quantity,
        source=outside(),
        destination=goods_in(),
        return_id="RET-1",
        decision="RET-1:intake",
    )


# --------------------------------------------------------------- append-only


def test_rows_cannot_be_edited(book):
    receive(book)
    with pytest.raises(Exception, match="append-only"):
        book.conn.execute("UPDATE stock_movements SET quantity = 1 WHERE seq = 1")


def test_rows_cannot_be_deleted(book):
    receive(book)
    with pytest.raises(Exception, match="append-only"):
        book.conn.execute("DELETE FROM stock_movements WHERE seq = 1")


def test_history_survives_a_correction(book):
    """The wrong answer stays in the log next to the correction."""
    receive(book)
    placed = book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=shelf("B-2291"),
        return_id="RET-1",
        decision="RET-1:placement",
    )
    book.reverse(placed.seq, DAY + timedelta(days=3), "identified as B-2288")
    kept = [m for m in book.movements() if m.seq == placed.seq]
    assert kept and kept[0].destination.lot.batch_id == "B-2291"


# ------------------------------------------------------- units in, units out


def test_units_in_equals_units_out(book):
    receive(book, 84)
    book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=shelf(),
        return_id="RET-1",
        decision="RET-1:placement",
    )
    book.append(
        at=DAY + timedelta(days=1),
        kind=Kind.PICK,
        quantity=30,
        source=shelf(),
        destination=Position(SKU, DISPATCH, Lot("B-2291", date(2027, 5, 1))),
    )
    arrived, departed, inside = book.flow(SKU)
    assert (arrived, departed, inside) == (84, 30, 54)


def test_stock_cannot_go_negative(book):
    receive(book, 10)
    with pytest.raises(LedgerError, match="negative"):
        book.append(
            at=DAY,
            kind=Kind.RECLASSIFY,
            quantity=11,
            source=goods_in(),
            destination=shelf(),
        )


def test_nothing_is_written_when_a_movement_is_rejected(book):
    receive(book, 10)
    before = len(book.movements())
    with pytest.raises(LedgerError):
        book.append(DAY, Kind.RECLASSIFY, 11, goods_in(), shelf())
    assert len(book.movements()) == before
    book.check_balances()


def test_a_movement_cannot_change_the_product(book):
    receive(book, 10)
    with pytest.raises(LedgerError, match="cannot change the product"):
        book.append(DAY, Kind.PUTAWAY, 5, goods_in(), Position("SKU-9999", "A-07-02"))


def test_quantities_must_be_positive(book):
    with pytest.raises(LedgerError, match="must be positive"):
        book.append(DAY, Kind.RECEIPT, -5, outside(), goods_in())


def test_movements_are_posted_in_order(book):
    receive(book, 10, at=DAY)
    with pytest.raises(LedgerError, match="in order"):
        book.append(DAY - timedelta(days=1), Kind.PUTAWAY, 5, goods_in(), Position(SKU, "A-07-05"))


# ------------------------------------------------ changing our mind is a event


def test_changing_the_believed_batch_must_be_called_a_reclassify(book):
    """A quiet relabel is how a stock record becomes untraceable."""
    receive(book, 10)
    with pytest.raises(LedgerError, match="reclassify"):
        book.append(DAY, Kind.PUTAWAY, 10, goods_in(), shelf())


def test_a_reclassify_must_actually_change_something(book):
    receive(book, 10)
    with pytest.raises(LedgerError, match="changes nothing"):
        book.append(DAY, Kind.RECLASSIFY, 10, goods_in(), Position(SKU, "A-07-05"))


# ------------------------------------------------------------------ reversal


def test_reversing_puts_the_stock_back(book):
    receive(book, 84)
    before = dict(book.balances())
    placed = book.append(DAY, Kind.RECLASSIFY, 84, goods_in(), shelf(), decision="d1")
    book.reverse(placed.seq, DAY, "changed our mind")
    assert book.balances() == before
    book.check_balances()


def test_a_movement_cannot_be_reversed_twice(book):
    receive(book, 84)
    placed = book.append(DAY, Kind.RECLASSIFY, 84, goods_in(), shelf(), decision="d1")
    book.reverse(placed.seq, DAY, "first")
    with pytest.raises(LedgerError, match="already reversed"):
        book.reverse(placed.seq, DAY, "second")


def test_a_reversal_cannot_itself_be_reversed(book):
    receive(book, 84)
    placed = book.append(DAY, Kind.RECLASSIFY, 84, goods_in(), shelf(), decision="d1")
    undo = book.reverse(placed.seq, DAY, "first")
    with pytest.raises(LedgerError, match="itself a reversal"):
        book.reverse(undo.seq, DAY, "again")


def test_undoing_a_decision_undoes_everything_it_caused(book):
    receive(book, 84)
    before = dict(book.balances())
    book.append(DAY, Kind.RECLASSIFY, 84, goods_in(), shelf(), decision="d1")
    book.append(
        DAY, Kind.PUTAWAY, 84, shelf(), Position(SKU, "A-07-05", shelf().lot), decision="d1"
    )
    undone = book.undo_decision("d1", DAY, "wrong call")
    assert len(undone) == 2
    assert book.balances() == before


def test_every_movement_names_the_decision_that_caused_it(book):
    """Drift you cannot trace back to a decision is drift you cannot fix."""
    case = generate.build(11)
    rows = posting.post(book, case.intake, _decide(case))
    assert all(m.decision for m in rows)
    assert all(m.return_id == case.intake.return_id for m in rows)


# ------------------------------------------------------------------- drift


def test_drift_is_zero_when_the_agent_is_right(book):
    receive(book, 84)
    book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=shelf("B-2291", date(2027, 5, 1)),
        return_id="RET-1",
        decision=decision_id("RET-1", PLACEMENT_STEP),
    )
    truth = TruthBook(
        true_batch={"RET-1": "B-2291"},
        best_before={"B-2291": date(2027, 5, 1)},
        home_bin={"B-2291": "A-07-02"},
    )
    drift = measure(book, truth)
    assert drift.clean
    assert drift.overstated_unit_days == 0
    assert drift.units_misattributed == 0


def test_drift_counts_a_late_expiry_in_unit_days(book):
    """The headline number: units times days of expiry on the dangerous side."""
    receive(book, 84)
    book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=shelf("B-2291", date(2027, 5, 1)),
        return_id="RET-1",
        decision=decision_id("RET-1", PLACEMENT_STEP),
    )
    truth = TruthBook(
        true_batch={"RET-1": "B-2288"},
        best_before={"B-2288": date(2026, 11, 16), "B-2291": date(2027, 5, 1)},
        home_bin={"B-2288": "A-07-05", "B-2291": "A-07-02"},
    )
    drift = measure(book, truth)
    assert drift.overstated_unit_days == 84 * (date(2027, 5, 1) - date(2026, 11, 16)).days
    assert drift.units_misattributed == 84
    assert drift.wrong_bin_units == 84
    assert not drift.clean


def test_a_corrected_decision_stops_counting_as_drift(book):
    """Drift measures where the stock is now, not every mistake ever made."""
    receive(book, 84)
    wrong = book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=shelf("B-2291", date(2027, 5, 1)),
        return_id="RET-1",
        decision=decision_id("RET-1", PLACEMENT_STEP),
    )
    book.reverse(wrong.seq, DAY, "found the paperwork")
    book.append(
        at=DAY,
        kind=Kind.RECLASSIFY,
        quantity=84,
        source=goods_in(),
        destination=Position(SKU, "A-07-05", Lot("B-2288", date(2026, 11, 16))),
        return_id="RET-1",
        decision=decision_id("RET-1", PLACEMENT_STEP),
    )
    truth = TruthBook(
        true_batch={"RET-1": "B-2288"},
        best_before={"B-2288": date(2026, 11, 16), "B-2291": date(2027, 5, 1)},
        home_bin={"B-2288": "A-07-05", "B-2291": "A-07-02"},
    )
    assert measure(book, truth).clean


# --------------------------------------------- against cases nobody wrote


def _decide(case: generate.Case):
    from agent import loop

    return loop.run(
        case.intake,
        generate.GeneratedServices(case),
        COSTS,
        RELIABILITY,
        generate.FixedNoteReader(case.note),
    )


@pytest.mark.parametrize("seed", range(30))
def test_any_decision_can_be_undone(seed):
    """Whatever the agent decided, the ledger goes back to how it was.

    Reversibility is the part of R7 that is easy to claim and easy to get wrong,
    so it is checked against generated placements rather than a chosen few.
    """
    case = generate.build(seed)
    book = Ledger()
    posting.post(book, case.intake, _decide(case))
    dock = Position(case.intake.sku_id, RECEIVING_BIN)
    day = case.intake.arrived

    undone = book.undo_decision(decision_id(case.intake.return_id, PLACEMENT_STEP), day, "test")
    assert undone, "the placement produced nothing to undo"
    assert book.balances() == {dock: case.intake.quantity}

    book.undo_decision(decision_id(case.intake.return_id, INTAKE_STEP), day, "test")
    assert book.balances() == {}
    book.check_balances()


@pytest.mark.parametrize("seed", range(30))
def test_a_generated_return_is_never_lost(seed):
    case = generate.build(seed)
    book = Ledger()
    posting.post(book, case.intake, _decide(case))

    arrived, departed, inside = book.flow(case.intake.sku_id)
    assert arrived == case.intake.quantity
    assert departed == 0
    assert inside == case.intake.quantity
    assert all(units > 0 for units in book.balances().values())
    book.check_balances()


@pytest.mark.parametrize("seed", range(30))
def test_held_stock_is_never_dated_later_than_the_truth(seed):
    """Segregating is only worth doing if it is safe on the expiry.

    Holding stock buys nothing except a date that cannot be too late. A hold
    dated later than the stock really lasts is worse than useless, because it
    looks careful. The sweep found exactly that failure, which is why this is
    asserted here as well as measured there.
    """
    case = generate.build(seed)
    result = _decide(case)
    if result.escalated or result.placement is None:
        return
    if result.placement.chosen.action.kind.value != "segregate":
        return
    recorded = result.placement.chosen.action.recorded_best_before
    if recorded is None:
        return  # undated stock cannot be picked at all
    assert recorded <= case.best_before(case.truth), (
        f"seed {seed}: held under {recorded}, stock really expires {case.best_before(case.truth)}"
    )
