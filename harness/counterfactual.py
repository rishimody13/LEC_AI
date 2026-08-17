"""Every policy, through the same months, on many seeds.

    uv run python -m harness.counterfactual 200

The comparison is **paired**. For a given seed every policy gets the same
warehouse, the same demand, the same returns and the same faults, so the only
thing that differs is what was decided. That matters more than the sample size:
the variation between seeds is far larger than the difference between policies,
and an unpaired comparison would drown the effect in it.

Confidence intervals are bootstrapped over seeds rather than assumed normal. The
per-seed harm is heavily skewed - most seeds cost nothing and a few cost a great
deal, which is exactly the shape of the problem - and a normal interval on that
would be wrong in the direction that flatters the result.
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent import notes
from agent.harm import CostModel, load_costs
from agent.reliability import ReliabilityModel, load_reliability
from downstream import simulate
from harness import policies


def cost_of(metrics: simulate.Metrics, costs: CostModel) -> float:
    """What six months under this policy cost, in pounds.

    Everything here is a consequence somebody pays for: stock that shipped after
    it had really expired, orders that could not be filled, analyst time, lookup
    fees, stock thrown away, and stock left in a state where nobody can ship it.
    """
    return (
        metrics.expired_units_shipped * costs.expired_unit
        + metrics.stockout_units * costs.stockout_unit
        + metrics.escalations * costs.human_review
        + metrics.lookup_spend_gbp
        + metrics.written_off_units * costs.scrap_unit
        + metrics.stranded_units * costs.scrap_unit
    )


@dataclass
class Paired:
    """One policy against another, seed by seed."""

    name: str
    against: str
    mean_difference: float
    low: float
    high: float
    seeds: int

    @property
    def beats(self) -> bool:
        """True when the whole interval is on the better side of zero."""
        return self.high < 0


def bootstrap(
    values: Sequence[float], draws: int = 5000, seed: int = 0, level: float = 0.95
) -> tuple[float, float]:
    """A confidence interval that does not assume the shape of the distribution."""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(draws):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((1 - level) / 2 * draws)]
    hi = means[int((1 + level) / 2 * draws) - 1]
    return lo, hi


def paired(
    runs: dict[str, list[simulate.Metrics]],
    subject: str,
    against: str,
    measure: Callable[[simulate.Metrics], float],
) -> Paired:
    """Difference per seed, subject minus rival. Negative means subject is better."""
    mine = runs[subject]
    theirs = runs[against]
    differences = [measure(a) - measure(b) for a, b in zip(mine, theirs, strict=True)]
    lo, hi = bootstrap(differences)
    return Paired(
        name=subject,
        against=against,
        mean_difference=sum(differences) / len(differences),
        low=lo,
        high=hi,
        seeds=len(differences),
    )


def all_policies(note_reader: notes.NoteReader | None = None) -> list[policies.Policy]:
    reader = note_reader or notes.CassetteNoteReader()
    return [
        policies.Agent(reader),
        policies.TrustTheLabel(),
        policies.TrustTheRecords(),
        policies.AlwaysEscalate(),
        policies.AlwaysSegregate(),
        policies.Oracle({}),
    ]


def run(
    seeds: Sequence[int],
    costs: CostModel | None = None,
    reliability: ReliabilityModel | None = None,
    config: simulate.Config | None = None,
) -> dict[str, list[simulate.Metrics]]:
    """Every policy through every seed."""
    costs = costs or load_costs()
    reliability = reliability or load_reliability()
    out: dict[str, list[simulate.Metrics]] = {}
    for policy in all_policies():
        # Fresh oracle per run so its answers do not leak between seeds.
        if isinstance(policy, policies.Oracle):
            policy.truth = {}
            policy.facts = {}
        out[policy.name] = [
            simulate.run(seed, policy, costs, reliability, config) for seed in seeds
        ]
    return out


def report(runs: dict[str, list[simulate.Metrics]], costs: CostModel) -> str:
    lines: list[str] = []
    seeds = len(next(iter(runs.values())))
    lines.append(f"{seeds} seeds, {simulate.Config().days} days each, paired on every seed")
    lines.append("")
    floor = sum(cost_of(m, costs) for m in runs["oracle"]) / seeds if "oracle" in runs else 0.0
    lines.append(
        f"{'policy':18} {'expired units':>14} {'stock-out':>10} {'escalations':>12} "
        f"{'£ per run':>11} {'£ above floor':>14}"
    )
    for name, metrics in runs.items():
        expired = sum(m.expired_units_shipped for m in metrics) / seeds
        stockout = sum(m.stockout_units for m in metrics) / seeds
        escalations = sum(m.escalations for m in metrics) / seeds
        money = sum(cost_of(m, costs) for m in metrics) / seeds
        lines.append(
            f"{name:18} {expired:>14.1f} {stockout:>10.1f} {escalations:>12.1f} "
            f"{money:>11,.0f} {money - floor:>14,.0f}"
        )
    lines.append("")
    lines.append(
        "The absolute £ column is dominated by stock written off at its recorded best-before, "
        "which comes from the replenishment rule rather than from any decision about a return: "
        "a fixed order-up-to level with no forecasting leaves long-dated stock at the back of "
        "the queue until some of it ages out. Every policy that files stock pays this within "
        "1%. Only the last column and the paired differences below say anything about the "
        "decisions."
    )

    lines.append("")
    lines.append("agent against each alternative, paired by seed (negative = agent better)")
    lines.append(f"{'against':18} {'expired units':>26} {'total £':>28}")
    for rival in runs:
        if rival == "agent":
            continue
        e = paired(runs, "agent", rival, lambda m: float(m.expired_units_shipped))
        c = paired(runs, "agent", rival, lambda m: cost_of(m, costs))
        lines.append(
            f"{rival:18} {e.mean_difference:>10.1f} [{e.low:>7.1f},{e.high:>7.1f}] "
            f"{c.mean_difference:>10,.0f} [{c.low:>8,.0f},{c.high:>8,.0f}]"
        )
    return "\n".join(lines)


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    costs = load_costs()
    runs = run(range(count), costs=costs)
    print(report(runs, costs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
