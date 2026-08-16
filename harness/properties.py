"""What must be true of every decision, whatever the case looks like.

These are properties, not expected answers. Nothing here says which batch the
agent should pick. They say what a decision must never do, and they are checked
against cases nobody wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.harm import CostModel
from agent.loop import Result

from .generate import Case


@dataclass
class Breach:
    name: str
    detail: str


@dataclass
class Report:
    checked: int = 0
    breaches: list[Breach] = field(default_factory=list)

    def add(self, name: str, detail: str) -> None:
        self.breaches.append(Breach(name, detail))

    @property
    def ok(self) -> bool:
        return not self.breaches


def check(case: Case, result: Result, costs: CostModel) -> Report:
    report = Report(checked=1)

    _never_overstates_expiry(case, result, report)
    _always_decides(result, report)
    _a_wrong_commit_was_the_cheaper_option(result, report)
    _belief_is_a_distribution(result, report)
    _every_decision_had_alternatives(result, report)
    _chose_the_cheapest(result, report)
    _spend_within_budget(result, costs, report)
    _truth_is_reachable(case, result, report)

    return report


def _never_overstates_expiry(case: Case, result: Result, report: Report) -> None:
    """The failure that matters: stock recorded as lasting longer than it does.

    It waits until the recorded date and then ships after it has really gone off.
    Recording an earlier date only wastes shelf life.
    """
    if result.assigned_batch is None:
        return
    recorded = case.best_before(result.assigned_batch)
    actual = case.best_before(case.truth)
    if recorded > actual:
        report.add(
            "records an expiry later than the truth",
            f"filed {result.assigned_batch} (expires {recorded}) for stock that "
            f"really expires {actual}",
        )


def _always_decides(result: Result, report: Report) -> None:
    if not result.escalated and result.placement is None:
        report.add("no decision reached", "neither escalated nor placed the stock")


def _a_wrong_commit_was_the_cheaper_option(result: Result, report: Report) -> None:
    """Filing the wrong batch is allowed only where checking cost more."""
    if result.assigned_batch is None or not result.trace.decisions:
        return
    placement = result.trace.decisions[-1]
    chosen = next((o for o in placement.options if o["action"] == placement.chosen), None)
    escalate = next((o for o in placement.options if o["action"] == "escalate"), None)
    if chosen is None or escalate is None:
        return
    if chosen["expected_cost_gbp"] > escalate["expected_cost_gbp"] + 1e-6:
        report.add(
            "committed when escalating was cheaper",
            f"{placement.chosen} at £{chosen['expected_cost_gbp']} vs escalate at "
            f"£{escalate['expected_cost_gbp']}",
        )


def _belief_is_a_distribution(result: Result, report: Report) -> None:
    total = sum(result.belief.probability.values())
    if abs(total - 1.0) > 1e-6:
        report.add("belief does not sum to 1", f"sums to {total}")
    for name, p in result.belief.probability.items():
        if p < 0 or p > 1:
            report.add("probability out of range", f"{name} = {p}")


def _every_decision_had_alternatives(result: Result, report: Report) -> None:
    for decision in result.trace.decisions:
        if len(decision.options) < 2:
            report.add(
                "decision with no alternatives",
                f"{decision.name} considered {len(decision.options)} option(s)",
            )


def _chose_the_cheapest(result: Result, report: Report) -> None:
    """No default branch: the action taken is always the cheapest one listed."""
    for decision in result.trace.decisions:
        if not decision.options:
            continue
        cheapest = min(o["expected_cost_gbp"] for o in decision.options)
        chosen = next(
            (o["expected_cost_gbp"] for o in decision.options if o["action"] == decision.chosen),
            None,
        )
        if chosen is None:
            report.add("chosen action not in the options", decision.chosen)
        elif chosen > cheapest + 1e-6:
            report.add(
                "did not take the cheapest action",
                f"{decision.name}: took £{chosen}, cheapest was £{cheapest}",
            )


def _spend_within_budget(result: Result, costs: CostModel, report: Report) -> None:
    from agent.loop import LOOKUP_BUDGET_GBP

    if result.spend_gbp > LOOKUP_BUDGET_GBP + 1e-9:
        report.add("overspent on lookups", f"£{result.spend_gbp}")
    if result.spend_gbp < 0:
        report.add("negative spend", f"£{result.spend_gbp}")


def _truth_is_reachable(case: Case, result: Result, report: Report) -> None:
    """If the true batch is not a candidate, the catch-all must carry the doubt.

    A candidate list that misses the answer and is still confident is the worst
    failure available to this design, because every number after it is wrong in a
    way nothing downstream can detect.
    """
    names = {c.name for c in result.belief.candidates.candidates}
    if case.truth in names:
        return
    if result.belief.catch_all < 0.01 and result.assigned_batch is not None:
        report.add(
            "confident with the answer missing from the candidate list",
            f"truth {case.truth} not among {sorted(names)}, catch-all only "
            f"{result.belief.catch_all:.4f}, yet it filed {result.assigned_batch}",
        )
