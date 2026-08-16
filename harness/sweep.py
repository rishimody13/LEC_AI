"""Runs the agent over many generated cases and reports what happened.

    uv run python -m harness.sweep 2000

Prints how the agent behaved across the fault space, which combinations were
actually exercised, and any property breach with the seed that produced it so it
can be reproduced on its own.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field

from agent import loop
from agent.harm import CostModel, load_costs
from agent.reliability import ReliabilityModel, load_reliability
from ledger import drift as drift_mod
from ledger import posting
from ledger.ledger import Ledger

from . import generate, properties


@dataclass
class Outcome:
    seed: int
    description: str
    action: str
    correct: bool
    #: True when the stock record says the stock lasts longer than it really does.
    dangerous: bool
    overstated_unit_days: int = 0
    understated_unit_days: int = 0
    misattributed_units: int = 0
    undated_units: int = 0
    breaches: list[properties.Breach] = field(default_factory=list)


@dataclass
class Summary:
    outcomes: list[Outcome] = field(default_factory=list)
    crashed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def breaches(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.breaches]


def run_one(
    seed: int, costs: CostModel, reliability: ReliabilityModel, calibrated: bool = True
) -> Outcome:
    case = generate.build(seed, calibrated=calibrated)
    result = loop.run(
        case.intake,
        generate.GeneratedServices(case),
        costs,
        reliability,
        generate.FixedNoteReader(case.note),
        scenario_id=f"gen-{seed}",
    )

    # Every decision is written to a ledger, exactly as it would be in the real
    # system, and the drift is measured off that. The safety figure below is
    # therefore what the stock record actually says, not a separate calculation
    # that could quietly drift away from it.
    book = Ledger()
    posting.post(book, case.intake, result)
    drift = drift_mod.measure(book, case.truth_book())

    report = properties.check(case, result, costs, ledger=book)

    if result.escalated:
        action = "escalate"
    elif result.placement is not None:
        action = result.placement.chosen.action.kind.value
    else:
        action = "none"

    return Outcome(
        seed=seed,
        description=case.description,
        action=action,
        correct=result.assigned_batch == case.truth,
        dangerous=drift.overstated_unit_days > 0,
        overstated_unit_days=drift.overstated_unit_days,
        understated_unit_days=drift.understated_unit_days,
        misattributed_units=drift.units_misattributed,
        undated_units=drift.units_without_a_date,
        breaches=report.breaches,
    )


def sweep(count: int, start: int = 0, calibrated: bool = True) -> Summary:
    costs = load_costs()
    reliability = load_reliability()
    summary = Summary()
    for seed in range(start, start + count):
        try:
            summary.outcomes.append(run_one(seed, costs, reliability, calibrated))
        except Exception as exc:  # noqa: BLE001 - a crash is itself a finding
            summary.crashed.append((seed, f"{type(exc).__name__}: {exc}"))
    return summary


def report(summary: Summary) -> str:
    lines: list[str] = []
    n = len(summary.outcomes)
    lines.append(f"ran {n} generated cases, {len(summary.crashed)} crashed")

    actions = Counter(o.action for o in summary.outcomes)
    lines.append("")
    lines.append("what it did:")
    for action, k in actions.most_common():
        lines.append(f"  {action:12} {k:5}  {k / max(n, 1) * 100:5.1f}%")

    committed = [o for o in summary.outcomes if o.action == "commit"]
    right = sum(o.correct for o in committed)
    lines.append("")
    lines.append("when it filed the stock under a batch:")
    lines.append(f"  right batch      {right:5} / {len(committed)}")
    lines.append(f"  wrong batch      {len(committed) - right:5} / {len(committed)}")
    lines.append(
        f"  expiry too late  {sum(o.dangerous for o in summary.outcomes):5}   "
        f"<- the failure that ships expired stock"
    )

    over = sum(o.overstated_unit_days for o in summary.outcomes)
    under = sum(o.understated_unit_days for o in summary.outcomes)
    lines.append("")
    lines.append("drift left in the stock record:")
    lines.append(f"  expiry on the dangerous side  {over:8} unit-days")
    lines.append(f"  expiry on the wasteful side   {under:8} unit-days")
    lines.append(
        f"  units under the wrong batch   {sum(o.misattributed_units for o in summary.outcomes):8}"
    )
    lines.append(
        f"  units with no date at all     {sum(o.undated_units for o in summary.outcomes):8}"
    )

    lines.append("")
    lines.append("fault combinations exercised:")
    combos = Counter(
        tuple(part for part in o.description.split() if part.startswith(("label=", "records=")))
        for o in summary.outcomes
    )
    lines.append(f"  {len(combos)} distinct label x records pairs")

    if summary.crashed:
        lines.append("")
        lines.append("crashes:")
        for seed, err in summary.crashed[:10]:
            lines.append(f"  seed {seed}: {err}")

    breaches = summary.breaches
    lines.append("")
    if breaches:
        counts = Counter(b.name for o in breaches for b in o.breaches)
        lines.append(f"PROPERTY BREACHES: {len(breaches)} case(s)")
        for name, k in counts.most_common():
            lines.append(f"  {k:5}  {name}")
        lines.append("")
        for o in breaches[:10]:
            lines.append(f"  seed {o.seed} [{o.description}]")
            for b in o.breaches:
                lines.append(f"      {b.name}: {b.detail}")
    else:
        lines.append("no property breaches")
    return "\n".join(lines)


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    calibrated = "--miscalibrated" not in sys.argv
    summary = sweep(count, calibrated=calibrated)
    print(
        "world matches the reliability model"
        if calibrated
        else "world does NOT match the reliability model"
    )
    print(report(summary))
    return 1 if (summary.breaches or summary.crashed) else 0


if __name__ == "__main__":
    sys.exit(main())
