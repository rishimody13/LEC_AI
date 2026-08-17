"""Is the agent as sure as it should be, and is that weight the right one?

    uv run python -m harness.calibration            # calibration table
    uv run python -m harness.calibration --sweep    # evidence weight against cost

Two questions, both answered from generated cases with known answers.

**Calibration.** When the agent says it is 96% sure, is it right 96 times in a
hundred? If not, every expected cost built on those numbers is wrong by the same
factor, and the action it picks is wrong with it.

**The evidence weight.** `agent.belief.EVIDENCE_WEIGHT` discounts each
likelihood for the fact that the sources are not independent. This runs the agent
at several weights and reports what each actually cost, on seeds that played no
part in choosing it.

This exists because the figures quoted in status.md have to be reproducible. They
were originally produced by throwaway scripts, which is not good enough for
numbers that are being used to justify a design decision.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from agent import belief as belief_mod
from agent import loop, policy
from agent.harm import CostModel, load_costs
from agent.reliability import ReliabilityModel, load_reliability
from ledger import drift as drift_mod
from ledger import posting
from ledger.ledger import Ledger

from . import generate, outcome

#: Seeds used to choose the weight, and seeds used only to check it. They must
#: not overlap: a weight that looks good on the cases it was fitted to proves
#: nothing at all.
FIT = range(1500)
HELD_OUT = range(8000, 12000)


@dataclass
class Point:
    """One case, reduced to what these questions need."""

    stated: float
    correct: bool
    committed: bool
    updates: int
    cost_gbp: float
    overstated_unit_days: int


@dataclass
class Bucket:
    label: str
    points: list[Point] = field(default_factory=list)

    @property
    def said(self) -> float:
        return statistics.mean(p.stated for p in self.points) if self.points else 0.0

    @property
    def actual(self) -> float:
        return sum(p.correct for p in self.points) / len(self.points) if self.points else 0.0

    @property
    def gap(self) -> float:
        return self.actual - self.said


def collect(seeds: Sequence[int], costs: CostModel, reliability: ReliabilityModel) -> list[Point]:
    out: list[Point] = []
    for seed in seeds:
        case = generate.build(seed, calibrated=True)
        result = loop.run(
            case.intake,
            generate.GeneratedServices(case),
            costs,
            reliability,
            generate.FixedNoteReader(case.note),
        )
        book = Ledger()
        posting.post(book, case.intake, result)
        drift = drift_mod.measure(book, case.truth_book())
        committed = (
            not result.escalated
            and result.placement is not None
            and result.placement.chosen.action.kind is policy.Kind.COMMIT
        )
        out.append(
            Point(
                stated=result.belief.best()[1],
                correct=result.assigned_batch == case.truth,
                committed=committed,
                updates=len(result.belief.steps),
                cost_gbp=outcome.score(case, result, costs).total_gbp,
                overstated_unit_days=drift.overstated_unit_days,
            )
        )
    return out


def brier(points: Sequence[Point]) -> float:
    """Mean squared gap between stated confidence and what happened. 0 is perfect."""
    if not points:
        return 0.0
    return statistics.mean((p.stated - (1.0 if p.correct else 0.0)) ** 2 for p in points)


def calibration_table(points: Sequence[Point]) -> str:
    committed = [p for p in points if p.committed]
    lines = [f"{len(committed)} commits out of {len(points)} cases", ""]

    edges = [(0.0, 0.90), (0.90, 0.99), (0.99, 0.999), (0.999, 1.001)]
    lines.append(f"{'stated confidence':>20} {'n':>6} {'said':>8} {'actual':>8} {'gap':>8}")
    for low, high in edges:
        bucket = Bucket(f"{low}-{high}", [p for p in committed if low <= p.stated < high])
        if not bucket.points:
            continue
        lines.append(
            f"{low:>9.3f}-{high:<10.3f} {len(bucket.points):>6} "
            f"{bucket.said:>8.4f} {bucket.actual:>8.4f} {bucket.gap:>+8.4f}"
        )

    lines.append("")
    lines.append("by how much evidence was applied (the telling one):")
    lines.append(f"{'updates':>20} {'n':>6} {'said':>8} {'actual':>8} {'gap':>8}")
    for count in sorted({p.updates for p in committed}):
        bucket = Bucket(str(count), [p for p in committed if p.updates == count])
        if len(bucket.points) < 20:
            continue
        lines.append(
            f"{count:>20} {len(bucket.points):>6} "
            f"{bucket.said:>8.4f} {bucket.actual:>8.4f} {bucket.gap:>+8.4f}"
        )

    lines.append("")
    lines.append(f"Brier score {brier(committed):.5f}")
    said = statistics.mean(p.stated for p in committed)
    actual = sum(p.correct for p in committed) / len(committed)
    lines.append(
        f"overall it said {said:.4f} and was right {actual:.4f} - "
        f"an error rate of {1 - said:.4f} claimed against {1 - actual:.4f} real"
    )
    return "\n".join(lines)


def weight_table(
    seeds: Sequence[int],
    costs: CostModel,
    reliability: ReliabilityModel,
    weights: Sequence[float] = (1.0, 0.97, 0.95, 0.92, 0.90),
) -> str:
    original = belief_mod.EVIDENCE_WEIGHT
    lines = [
        f"{len(seeds)} held-out cases (seeds {seeds[0]}-{seeds[-1]}), which played no part "
        f"in choosing the weight",
        "",
        f"{'weight':>7} {'£ per return':>20} {'dangerous':>12} {'overstated unit-days':>22}",
    ]
    try:
        for weight in weights:
            belief_mod.EVIDENCE_WEIGHT = weight
            points = collect(seeds, costs, reliability)
            mean = statistics.mean(p.cost_gbp for p in points)
            half = 1.96 * statistics.stdev(p.cost_gbp for p in points) / (len(points) ** 0.5)
            dangerous = sum(p.overstated_unit_days > 0 for p in points)
            lines.append(
                f"{weight:>7.2f} {mean:>13.2f} +/-{half:<5.2f} "
                f"{dangerous:>7}/{len(points)} {sum(p.overstated_unit_days for p in points):>22,}"
            )
    finally:
        belief_mod.EVIDENCE_WEIGHT = original
    return "\n".join(lines)


def main() -> int:
    costs = load_costs()
    reliability = load_reliability()
    if "--sweep" in sys.argv:
        print(weight_table(list(HELD_OUT), costs, reliability))
    else:
        print(f"evidence weight in use: {belief_mod.EVIDENCE_WEIGHT}")
        print()
        print(calibration_table(collect(range(2000), costs, reliability)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
