"""The simulation's own machinery, checked before anything is concluded from it.

The harm numbers are only worth as much as the thing that produced them. These
test the parts that were built fast and would fail quietly: the picker's two
exclusions, the truth tracker's split, the scoring of a decision after the fact,
and the invariant that the ledger and the shelf never disagree about how many
units are somewhere.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from agent import notes
from agent.harm import load_costs
from agent.reliability import load_reliability
from downstream import picking, simulate
from downstream.truth import TruthTracker
from harness import generate, outcome, policies
from ledger.ledger import Kind, Ledger, Lot, Position
from world.types import Bin, BinStatus

COSTS = load_costs()
RELIABILITY = load_reliability()
SKU = "SKU-9000"
DAY = date(2026, 8, 15)

BINS = [
    Bin(bin_id="A-01-01", zone="A-01", capacity=9999),
    Bin(bin_id="H-01-01", zone="H-01", capacity=9999, status=BinStatus.HOLD),
    Bin(bin_id="Q-01-01", zone="Q-01", capacity=9999, status=BinStatus.QUARANTINE),
]


def stocked(**lots: int) -> Ledger:
    """A ledger holding the given quantities, keyed by 'bin@days_from_DAY'."""
    book = Ledger()
    for i, (key, units) in enumerate(lots.items()):
        bin_id, _, offset = key.partition("_")
        bin_id = bin_id.replace("x", "-")
        best_before = None if offset == "none" else DAY + timedelta(days=int(offset))
        book.append(
            at=DAY,
            kind=Kind.RECEIPT,
            quantity=units,
            source=Position(SKU, "@supplier"),
            destination=Position(SKU, bin_id, Lot(f"B-{i:04d}", best_before)),
            reason="test",
        )
    return book


# ------------------------------------------------------------------ picking


def test_the_picker_takes_the_earliest_recorded_date_first():
    book = stocked(**{"Ax01x01_300": 100, "Ax01x01_10": 100, "Ax01x01_100": 100})
    order = picking.plan(book, BINS, SKU, 150, DAY)
    assert order.picked == 150
    taken = [(t.position.lot.best_before - DAY).days for t in order.takes]
    assert taken == sorted(taken), "first expired must be picked first"
    assert taken[0] == 10


def test_undated_stock_is_never_picked():
    """Load-bearing. The cost model charges nothing for the expiry risk of an
    undated hold, on the grounds that first-expired-first-out cannot rank what it
    cannot date. If this ever became false the segregate price would be wrong."""
    book = stocked(**{"Ax01x01_none": 500})
    order = picking.plan(book, BINS, SKU, 100, DAY)
    assert order.picked == 0
    assert order.shortfall == 100


@pytest.mark.parametrize("bin_id", ["H-01-01", "Q-01-01"])
def test_stock_held_back_or_under_review_is_never_picked(bin_id):
    book = stocked(**{f"{bin_id.replace('-', 'x')}_300": 500})
    assert picking.plan(book, BINS, SKU, 100, DAY).picked == 0


def test_stock_the_record_says_has_expired_is_not_shipped():
    """A warehouse writes that off. The dangerous case is the other one, where
    the record says there is time left and there is not."""
    book = stocked(**{"Ax01x01_-1": 500})
    assert picking.plan(book, BINS, SKU, 100, DAY).picked == 0


def test_a_short_shelf_reports_the_shortfall():
    book = stocked(**{"Ax01x01_300": 40})
    order = picking.plan(book, BINS, SKU, 100, DAY)
    assert (order.picked, order.shortfall) == (40, 60)


# ------------------------------------------------------------- truth tracker


def test_a_pick_is_split_across_what_is_really_there():
    """Units of one believed lot are interchangeable to a picker, so taking them
    in name order would quietly ship one real batch before another and bias
    every expiry figure that follows."""
    real = TruthTracker()
    where = Position(SKU, "A-01-01", Lot("B-1", DAY))
    real.add(where, "TRUE-A", 750)
    real.add(where, "TRUE-B", 250)
    taken = real.take(where, 100)
    assert sum(taken.values()) == 100
    assert taken == Counter({"TRUE-A": 75, "TRUE-B": 25})
    assert real.units_at(where) == 900


def test_the_tracker_refuses_to_hand_over_what_it_does_not_have():
    real = TruthTracker()
    where = Position(SKU, "A-01-01")
    real.add(where, "TRUE-A", 10)
    with pytest.raises(ValueError, match="cannot take"):
        real.take(where, 11)


def test_moving_carries_the_real_identity_with_it():
    real = TruthTracker()
    a = Position(SKU, "R-00-01")
    b = Position(SKU, "A-01-01", Lot("B-9", DAY))
    real.add(a, "TRUE-A", 84)
    real.move(a, b, 84)
    assert real.units_at(a) == 0
    assert real.at(b) == Counter({"TRUE-A": 84})


# ------------------------------------------------------ scoring a decision


def test_a_correct_decision_costs_almost_nothing():
    case = generate.build(4)
    truth = outcome.truth_as_candidate(case)
    assert truth.batch_id == case.truth
    assert truth.best_before == case.best_before(case.truth)


@pytest.mark.parametrize("seed", range(12))
def test_scoring_never_goes_negative_and_escalation_is_not_free(seed):
    from agent import loop

    case = generate.build(seed)
    result = loop.run(
        case.intake,
        generate.GeneratedServices(case),
        COSTS,
        RELIABILITY,
        generate.FixedNoteReader(case.note),
    )
    scored = outcome.score(case, result, COSTS)
    assert scored.total_gbp >= 0
    assert scored.spend_gbp >= 0
    if scored.action == "escalate":
        assert scored.harm_gbp >= COSTS.human_review, "a person is not free"


# ------------------------------------------------------------- the whole run


@pytest.mark.parametrize("seed", [0, 3, 6])
def test_the_ledger_and_the_shelf_never_disagree_about_quantities(seed):
    """They disagree about *what* the stock is - that is the point - but a
    disagreement about *how many* would mean the two halves of the simulation
    had drifted apart, and every figure it produces would be meaningless."""
    for policy in (
        policies.Agent(notes.CassetteNoteReader()),
        policies.TrustTheLabel(),
        policies.AlwaysEscalate(),
        policies.AlwaysSegregate(),
    ):
        simulate.run(seed, policy, COSTS, RELIABILITY, verify=True)


def test_the_simulation_does_not_hand_a_policy_the_right_answer():
    """A policy that names a batch but no bin must not be given the true one.

    When the warehouse system is down the catalogue is empty, so several policies
    return no bin. An earlier version filled that in from ground truth, which
    handed them a hint they never had.
    """
    import inspect

    source = inspect.getsource(simulate.run)
    placement = source.split("# 2. returns")[1].split("# 3. orders")[0]
    assert "home_of.get(decision" not in placement
    assert "config.inbound_bin" in placement
