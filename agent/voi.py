"""Is a lookup worth buying?

We work out what each set of lookups would probably tell us, what we would do
next in each case, and how much harm that avoids. If the saving beats the fee, we
buy.

Every combination of lookups is evaluated, not just one at a time. Looking only
one step ahead can reject two lookups that are weak on their own but decisive
together, and then escalate at a much higher cost. With three tools there are
only eight combinations, so we can just check them all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import combinations

from .belief import Belief
from .harm import CostModel, LinearCost
from .policy import Priced, terminal_actions

#: The lookups the agent can buy.
REGISTRY = "batch_registry"
LEDGER = "shipment_ledger"
ALL_TOOLS = (REGISTRY, LEDGER)

#: What one call to each lookup would tell us. Passed in as a function so the
#: real services and the lookahead share the same code path.
Simulate = Callable[[tuple[str, ...]], list[tuple[float, Belief]]]


@dataclass(frozen=True)
class Option:
    """One set of lookups, and what buying it is worth."""

    tools: tuple[str, ...]
    fee: float
    #: Best expected cost once we have paid and acted on what we learned.
    expected_after: float
    #: Expected cost if we act now without buying anything.
    expected_now: float

    @property
    def saving(self) -> float:
        return self.expected_now - self.expected_after

    @property
    def worth_it(self) -> bool:
        return self.saving > self.fee


def best_now(belief: Belief, quantity: int, costs: CostModel, today: date) -> tuple[float, Priced]:
    """Cheapest way to finish right now, without buying anything."""
    options = terminal_actions(belief, quantity, costs, today)
    for o in options:
        o.total = o.cost.total(costs)
    best = min(options, key=lambda o: (o.total, o.cost.fixed))
    return best.total, best


def tool_subsets(
    available: tuple[str, ...], budget: float, prices: dict[str, float]
) -> list[tuple[str, ...]]:
    """Every affordable combination of lookups, cheapest first."""
    out: list[tuple[str, ...]] = []
    for size in range(1, len(available) + 1):
        for combo in combinations(available, size):
            if sum(prices[t] for t in combo) <= budget:
                out.append(combo)
    out.sort(key=lambda c: sum(prices[t] for t in c))
    return out


def evaluate(
    belief: Belief,
    quantity: int,
    costs: CostModel,
    simulate: Simulate,
    today: date,
    available: tuple[str, ...] = ALL_TOOLS,
    budget: float = 2.50,
) -> list[Option]:
    """Price every combination of lookups.

    `simulate` returns the outcomes a set of lookups could produce, as
    (probability, resulting belief) pairs. We take the best action in each
    outcome and average.
    """
    now, _ = best_now(belief, quantity, costs, today)
    out: list[Option] = []

    for combo in tool_subsets(available, budget, costs.prices):
        fee = sum(costs.prices[t] for t in combo)
        outcomes = simulate(combo)
        if not outcomes:
            continue
        after = sum(p * best_now(b, quantity, costs, today)[0] for p, b in outcomes)
        out.append(Option(tools=combo, fee=fee, expected_after=after, expected_now=now))

    out.sort(key=lambda o: o.expected_after + o.fee)
    return out


def as_priced_actions(options: list[Option], quantity: int) -> list[Priced]:
    """Turn lookup options into priced actions the chooser can compare.

    A gather action's cost is the fee plus what we expect to spend afterwards, so
    it sits on the same scale as committing or escalating.
    """
    from .policy import Action, Kind

    out: list[Priced] = []
    for o in options:
        out.append(
            Priced(
                action=Action(kind=Kind.GATHER, tools=o.tools),
                cost=LinearCost(fixed=o.fee + o.expected_after),
                total=o.fee + o.expected_after,
                fee=o.fee,
            )
        )
    return out
