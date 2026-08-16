"""Is a lookup worth buying, and does it look far enough ahead?

The point of checking every combination rather than one lookup at a time is that
two lookups can be decisive together while neither is worth it alone. That case
is constructed here, because none of the real scenarios happens to produce it.
"""

from __future__ import annotations

import pytest

from agent import voi
from agent.belief import Belief
from agent.candidates import Candidate, CandidateSet
from agent.harm import load_costs

COSTS = load_costs()
TODAY = __import__("datetime").date(2026, 8, 15)


def belief_over(probabilities: dict[str, float]) -> Belief:
    from datetime import date

    candidates = [
        Candidate(
            batch_id=name if name != "other" else None,
            best_before=date(2026, 9, 30) if name == "A" else date(2027, 3, 15),
            home_bin="A-07-02" if name == "A" else "C-04-01",
            source="records",
        )
        for name in probabilities
    ]
    return Belief(
        candidates=CandidateSet(candidates=candidates, prior=dict(probabilities)),
        probability=dict(probabilities),
    )


def test_all_affordable_combinations_are_considered():
    combos = voi.tool_subsets(voi.ALL_TOOLS, budget=2.50, prices=COSTS.prices)
    assert set(combos) == {
        (voi.REGISTRY,),
        (voi.LEDGER,),
        (voi.REGISTRY, voi.LEDGER),
    }


def test_combinations_are_offered_cheapest_first():
    combos = voi.tool_subsets(voi.ALL_TOOLS, budget=2.50, prices=COSTS.prices)
    fees = [sum(COSTS.prices[t] for t in c) for c in combos]
    assert fees == sorted(fees)


def test_a_budget_rules_out_what_it_cannot_afford():
    combos = voi.tool_subsets(voi.ALL_TOOLS, budget=0.35, prices=COSTS.prices)
    assert combos == [(voi.REGISTRY,)]


def test_a_lookup_that_teaches_nothing_is_not_worth_buying():
    unsure = belief_over({"A": 0.5, "B": 0.5})

    def learns_nothing(combo):
        return [(1.0, unsure)]

    options = voi.evaluate(unsure, 84, COSTS, learns_nothing, TODAY)
    assert options
    for option in options:
        assert not option.worth_it
        assert option.saving == pytest.approx(0.0)


def test_a_lookup_that_settles_it_is_worth_buying():
    unsure = belief_over({"A": 0.5, "B": 0.5})
    certain = belief_over({"A": 0.999, "B": 0.001})

    def settles_it(combo):
        return [(1.0, certain)]

    options = voi.evaluate(unsure, 84, COSTS, settles_it, TODAY)
    assert options[0].worth_it
    assert options[0].saving > options[0].fee


def test_two_lookups_can_be_worth_buying_when_neither_is_alone():
    """The reason every combination is priced, not one at a time.

    Each lookup on its own barely moves the belief, so a one-step-ahead check
    would reject both and fall back on something more expensive. Together they
    settle it.
    """
    unsure = belief_over({"A": 0.5, "B": 0.5})
    barely = belief_over({"A": 0.56, "B": 0.44})
    decisive = belief_over({"A": 0.99, "B": 0.01})

    def jointly_decisive(combo):
        return [(1.0, decisive if len(combo) == 2 else barely)]

    options = voi.evaluate(unsure, 84, COSTS, jointly_decisive, TODAY)
    by_combo = {o.tools: o for o in options}

    assert not by_combo[(voi.REGISTRY,)].worth_it
    assert not by_combo[(voi.LEDGER,)].worth_it
    assert by_combo[(voi.REGISTRY, voi.LEDGER)].worth_it

    best = min(options, key=lambda o: o.expected_after + o.fee)
    assert best.tools == (voi.REGISTRY, voi.LEDGER)


def test_uncertain_outcomes_are_averaged():
    unsure = belief_over({"A": 0.5, "B": 0.5})
    good = belief_over({"A": 0.99, "B": 0.01})

    def half_the_time(combo):
        return [(0.5, good), (0.5, unsure)]

    options = voi.evaluate(unsure, 84, COSTS, half_the_time, TODAY)
    both = next(o for o in options if o.tools == (voi.REGISTRY,))
    now, _ = voi.best_now(unsure, 84, COSTS, TODAY)
    after_good, _ = voi.best_now(good, 84, COSTS, TODAY)
    assert both.expected_after == pytest.approx(0.5 * after_good + 0.5 * now)


def test_gather_options_are_priced_on_the_same_scale_as_finishing():
    unsure = belief_over({"A": 0.5, "B": 0.5})
    certain = belief_over({"A": 0.999, "B": 0.001})
    options = voi.evaluate(unsure, 84, COSTS, lambda c: [(1.0, certain)], TODAY)
    priced = voi.as_priced_actions(options, 84)
    for action, option in zip(priced, options, strict=True):
        assert action.total == pytest.approx(option.fee + option.expected_after)
