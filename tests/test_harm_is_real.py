"""The R8 proof: the decision matters, and here is the measured difference.

An anecdote is not proof. This runs every policy through the same eighteen
months, on the same warehouses, with the same demand and the same returns, and
asks whether the difference between them survives a confidence interval.

Three things make it a proof rather than a demonstration:

- **It is paired.** Every policy sees identical returns on a given seed. The
  variation between seeds is far bigger than the difference between policies, so
  an unpaired comparison would bury the effect. There is a test below that this
  pairing actually holds, because it was broken once and the results looked
  plausible anyway.
- **The rivals are real.** "Trust the label" is what warehouses actually do and
  it is right most of the time. Beating a straw man would prove nothing.
- **The horizon is long enough.** Six months was the plan's assumption and it was
  wrong: at 180 days most seeds report zero harm because misfiled stock has not
  reached the front of the queue yet. See downstream/simulate.py.

If the harm were hypothetical, these would fail.
"""

from __future__ import annotations

import pytest

from agent.harm import load_costs
from agent.reliability import load_reliability
from downstream import simulate
from harness import counterfactual, policies

COSTS = load_costs()
RELIABILITY = load_reliability()

#: Enough for the intervals to separate without making the suite unbearable.
#: The committed headline figures come from a 600-seed run - see artifacts/.
SEEDS = 200

#: What the committed 600-seed figures say. Checked loosely, because the point is
#: to notice a result that has moved, not to pin the number.
HEADLINE_EXPIRED_REDUCTION = 0.10


@pytest.fixture(scope="module")
def runs() -> dict[str, list[simulate.Metrics]]:
    return counterfactual.run(range(SEEDS), costs=COSTS, reliability=RELIABILITY)


def test_every_policy_saw_exactly_the_same_returns(runs):
    """Without this the comparison is not paired and none of it means anything.

    This broke once. Human review and replenishment were drawing from the same
    random stream as the return generator, so a policy that escalated more got
    different returns from one that escalated less, and the numbers still looked
    reasonable.
    """
    reference = [(m.returns_handled, m.units_returned) for m in runs["agent"]]
    for name, metrics in runs.items():
        assert [(m.returns_handled, m.units_returned) for m in metrics] == reference, (
            f"{name} did not see the same returns as the agent"
        )


def test_the_agent_ships_less_expired_stock_than_trusting_the_label(runs):
    """The headline. This is the claim the whole project is making."""
    result = counterfactual.paired(
        runs, "agent", "trust the label", lambda m: float(m.expired_units_shipped)
    )
    assert result.high < 0, (
        f"expected fewer expired units than trusting the label, got "
        f"{result.mean_difference:.1f} [{result.low:.1f}, {result.high:.1f}]"
    )


@pytest.mark.parametrize("rival", ["trust the records", "always escalate"])
def test_the_agent_beats_the_other_runnable_policies_on_both_counts(runs, rival):
    expired = counterfactual.paired(runs, "agent", rival, lambda m: float(m.expired_units_shipped))
    money = counterfactual.paired(runs, "agent", rival, lambda m: counterfactual.cost_of(m, COSTS))
    assert expired.high < 0, f"expired vs {rival}: {expired}"
    assert money.high < 0, f"cost vs {rival}: {money}"


def test_escalating_everything_is_not_safe(runs):
    """A person is good, not perfect, and it compounds over every return.

    Handing all of it to a human ships *more* expired stock than the agent does,
    because a 1% error rate applied to every single return beats a larger error
    rate applied only to the ones the agent could not resolve. If the simulation
    treated review as a free correct answer this would be unbeatable, and the
    comparison would be rigged.
    """
    worse = counterfactual.paired(
        runs, "agent", "always escalate", lambda m: float(m.expired_units_shipped)
    )
    assert worse.high < 0, (
        f"expected escalating everything to ship MORE expired stock than the agent, got "
        f"{worse.mean_difference:.1f} [{worse.low:.1f}, {worse.high:.1f}]"
    )
    assert sum(m.expired_units_shipped for m in runs["always escalate"]) > 0


def test_holding_everything_is_safe_and_useless(runs):
    """Safety on its own is trivially satisfiable, so it is not the target.

    Segregating every return ships very little expired stock and costs far more
    than anything else, because the stock is never on a pick face when it is
    needed. Quoting the expiry number alone would make this look like the best
    policy here.
    """
    expired = counterfactual.paired(
        runs, "agent", "always segregate", lambda m: float(m.expired_units_shipped)
    )
    money = counterfactual.paired(
        runs, "agent", "always segregate", lambda m: counterfactual.cost_of(m, COSTS)
    )
    assert expired.low > 0, "expected segregating everything to ship less expired stock"
    assert money.high < 0, "expected segregating everything to cost more"


def test_the_oracle_is_the_floor_and_nobody_reaches_it(runs):
    """Knowing the answer ships no expired stock. Nothing runnable manages that."""
    assert all(m.expired_units_shipped == 0 for m in runs["oracle"])
    gap = counterfactual.paired(runs, "agent", "oracle", lambda m: float(m.expired_units_shipped))
    assert gap.low > 0, "the agent cannot be as good as knowing the answer"


def test_the_harm_needs_a_long_horizon_to_appear():
    """Six months would have shown nothing, and the plan assumed six months.

    Kept as a test because it is the assumption most likely to be reintroduced by
    someone speeding the suite up.
    """
    short = simulate.Config(days=180)
    long = simulate.Config(days=540)
    seeds = [6, 7, 9, 17]
    label = policies.TrustTheLabel()
    early = sum(
        simulate.run(s, label, COSTS, RELIABILITY, short).expired_units_shipped for s in seeds
    )
    late = sum(
        simulate.run(s, label, COSTS, RELIABILITY, long).expired_units_shipped for s in seeds
    )
    assert late > early * 2, f"180 days showed {early}, 540 days showed {late}"


def test_the_reduction_is_worth_having(runs):
    """A statistically real difference can still be trivially small.

    The committed figure is a 22% reduction in expired units against trusting the
    label. This checks the effect has not quietly shrunk to something that
    survives a confidence interval but would not persuade anybody.
    """
    agent = sum(m.expired_units_shipped for m in runs["agent"])
    label = sum(m.expired_units_shipped for m in runs["trust the label"])
    assert label > 0
    assert (label - agent) / label > HEADLINE_EXPIRED_REDUCTION, (
        f"agent {agent}, trusting the label {label} - only {(label - agent) / label:.1%} better"
    )


@pytest.mark.parametrize("seed", [3, 6, 17])
def test_stock_is_conserved_across_the_whole_run(seed):
    """Eighteen months of movements, and nothing invented or lost.

    `simulate.run` re-derives the ledger balances from its rows at the end, so a
    running total that had drifted from the log would raise rather than quietly
    change every figure in this file.
    """
    metrics = simulate.run(seed, policies.TrustTheLabel(), COSTS, RELIABILITY)
    assert metrics.units_shipped > 0
    assert metrics.returns_handled > 0
    assert metrics.units_in - metrics.units_out == metrics.units_on_hand
    assert metrics.units_in > metrics.units_returned
