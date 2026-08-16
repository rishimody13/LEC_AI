"""The cost model and break-even arithmetic."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.harm import (
    EXPIRED_UNIT,
    HUMAN_REVIEW,
    CostModel,
    CostModelError,
    LinearCost,
    break_even,
    load_costs,
    sensitivities,
)

COSTS = load_costs()


def test_every_figure_is_derived_from_the_basis_file():
    """Nothing in the cost model should be a number typed in twice."""
    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    b = raw["basis"]
    assert COSTS.unit_cost == b["unit_cost_gbp"]
    assert COSTS.scrap_unit == COSTS.unit_cost
    assert COSTS.human_review == pytest.approx(b["analyst_hourly_gbp"] * b["review_minutes"] / 60)
    assert COSTS.shelf_waste_unit_day == pytest.approx(COSTS.unit_cost / b["shelf_life_days"])


def test_human_review_is_a_realistic_wage():
    """The old figure implied £42/hour and made the agent escalate too little."""
    implied_hourly = COSTS.human_review * 3
    assert 20 <= implied_hourly <= 32, f"£{implied_hourly:.2f}/hour is not a real wage"


def test_costs_are_ordered_sensibly():
    assert COSTS.expired_unit > COSTS.scrap_unit > COSTS.stockout_unit


def test_backordering_beats_scrapping_for_a_long_time():
    """Otherwise a cost-minimising agent would destroy stock rather than wait."""
    assert COSTS.days_before_scrap_beats_backorder() >= 30


def test_a_bad_edit_to_the_cost_file_fails_loudly(tmp_path):
    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    raw["judged"]["expired_unit_gbp"] = 1.00  # cheaper than scrapping - nonsense
    path = tmp_path / "harm.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(CostModelError, match="must cost more than"):
        load_costs(path)


def test_stockout_cheaper_than_scrap_is_enforced(tmp_path):
    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    raw["basis"]["non_supply_penalty_fraction"] = 5.0  # absurd penalty
    path = tmp_path / "harm.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(CostModelError, match="rather than backorder"):
        load_costs(path)


# --------------------------------------------------------------------------
# Break-even
# --------------------------------------------------------------------------


def test_break_even_solves_the_crossing_point():
    """Two actions exposed to the same cost in different amounts cross once."""
    risky = LinearCost(exposure={EXPIRED_UNIT: 10.0})
    safe = LinearCost(exposure={HUMAN_REVIEW: 1.0})

    flips_at = break_even(risky, safe, EXPIRED_UNIT, COSTS)
    assert flips_at is not None

    # At the crossing point the two really do cost the same.
    tweaked = CostModel(**{**COSTS.__dict__, "expired_unit": flips_at})
    assert risky.total(tweaked) == pytest.approx(safe.total(tweaked))


def test_break_even_returns_nothing_when_the_cost_does_not_matter():
    """Equal exposure means the decision cannot turn on that figure."""
    a = LinearCost(exposure={EXPIRED_UNIT: 3.0}, fixed=1.0)
    b = LinearCost(exposure={EXPIRED_UNIT: 3.0}, fixed=2.0)
    assert break_even(a, b, EXPIRED_UNIT, COSTS) is None


def test_sensitivity_puts_the_binding_figure_first():
    risky = LinearCost(exposure={EXPIRED_UNIT: 10.0})
    safe = LinearCost(exposure={HUMAN_REVIEW: 1.0})
    found = sensitivities(risky, safe, COSTS)
    assert found, "expected at least one cost the decision depends on"
    assert found[0].slack <= found[-1].slack


def test_linear_costs_add_and_scale():
    a = LinearCost(exposure={EXPIRED_UNIT: 2.0}, fixed=1.0)
    b = LinearCost(exposure={EXPIRED_UNIT: 3.0, HUMAN_REVIEW: 1.0}, fixed=0.5)
    combined = a + b
    assert combined.exposure[EXPIRED_UNIT] == 5.0
    assert combined.fixed == 1.5
    assert a.scaled(2.0).exposure[EXPIRED_UNIT] == 4.0


def test_stock_filed_with_no_date_carries_no_expiry_risk():
    """Found when the warehouse system was down on a generated case.

    Picking runs first-expired-first-out, which cannot rank what it cannot date,
    and the hold area is not picked from anyway. So undated held stock cannot
    ship, and cannot ship expired. Charging it a coin flip on shipping expired
    stock priced the safest available response to a total outage as the most
    reckless one, and stopped the agent segregating when it should.

    The person who has to come back and identify it is not free - that is the
    deferred review, charged separately.
    """
    from datetime import date

    from agent.candidates import Candidate
    from agent.policy import filing_harm

    truth = Candidate(batch_id="B-2288", best_before=date(2026, 9, 30), home_bin="A-07-02")
    dated = filing_harm(
        date(2027, 5, 1), "H-01-01", truth, 84, date(2026, 8, 15), COSTS.sell_through_days
    )
    undated = filing_harm(None, "H-01-01", truth, 84, date(2026, 8, 15), COSTS.sell_through_days)

    assert dated.exposure.get(EXPIRED_UNIT, 0.0) > 0, "a date later than the truth is dangerous"
    assert undated.exposure.get(EXPIRED_UNIT, 0.0) == 0.0
    assert undated.total(COSTS) == 0.0
