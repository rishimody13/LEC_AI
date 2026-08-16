"""What must be true of every decision, whatever the case looks like.

These are properties, not expected answers. Nothing here says which batch the
agent should pick. They say what a decision must never do, and they are checked
against cases nobody wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from agent.harm import CostModel
from agent.loop import Result
from ledger import drift as drift_mod
from ledger.ledger import PLACEMENT_STEP, RECEIVING_BIN, Ledger, decision_id

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


def check(case: Case, result: Result, costs: CostModel, ledger: Ledger | None = None) -> Report:
    report = Report(checked=1)

    _never_overstates_expiry(case, result, report)
    _always_decides(result, report)
    _a_wrong_commit_was_the_cheaper_option(result, report)
    _belief_is_a_distribution(result, report)
    _every_decision_had_alternatives(result, report)
    _chose_the_cheapest(result, report)
    _spend_within_budget(result, costs, report)
    _truth_is_reachable(case, result, report)

    if ledger is not None:
        _units_in_equals_units_out(case, ledger, report)
        _stock_is_never_in_two_places(case, ledger, report)
        _ledger_records_what_was_decided(result, case, ledger, report)
        _drift_agrees_with_the_expiry_check(case, result, ledger, report)

    return report


def _recorded_expiry(result: Result) -> date | None:
    """The expiry the agent actually wrote against the stock.

    Read off the chosen action rather than looked up from the batch, so a hold
    placed under a conservative date is measured too. An earlier version only
    checked commits, which meant segregated stock could carry an expiry later
    than the truth and nothing would notice.
    """
    if result.escalated or result.placement is None:
        return None
    return result.placement.chosen.action.recorded_best_before


def _never_overstates_expiry(case: Case, result: Result, report: Report) -> None:
    """The failure that matters: stock recorded as lasting longer than it does.

    It waits until the recorded date and then ships after it has really gone off.
    Recording an earlier date only wastes shelf life.
    """
    recorded = _recorded_expiry(result)
    if recorded is None:
        return
    actual = case.best_before(case.truth)
    if recorded > actual:
        filed = result.assigned_batch or "held stock"
        report.add(
            "records an expiry later than the truth",
            f"filed {filed} under expiry {recorded} for stock that really expires {actual}",
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


def _units_in_equals_units_out(case: Case, ledger: Ledger, report: Report) -> None:
    """Nothing is created or lost between the door and the shelf."""
    try:
        ledger.check_balances()
    except Exception as exc:  # noqa: BLE001 - a broken ledger is itself a finding
        report.add("ledger totals do not match its rows", str(exc))
        return

    arrived, departed, inside = ledger.flow(case.intake.sku_id)
    if arrived - departed != inside:
        report.add(
            "units in does not equal units out",
            f"{arrived} in, {departed} out, {inside} on the shelf",
        )
    if arrived != case.intake.quantity:
        report.add(
            "the ledger did not receive what arrived",
            f"return was {case.intake.quantity} units, ledger booked in {arrived}",
        )


def _stock_is_never_in_two_places(case: Case, ledger: Ledger, report: Report) -> None:
    """Every unit ends up at exactly one position, and none of them negative.

    A return that finished should have left goods-in entirely. Stock stuck in
    two places at once is the shape data drift takes before anyone notices it.
    """
    balances = ledger.balances()
    negative = {str(p): n for p, n in balances.items() if n < 0}
    if negative:
        report.add("negative stock", f"{negative}")

    stranded = sum(n for p, n in balances.items() if p.bin_id == RECEIVING_BIN)
    if stranded:
        report.add(
            "stock left in goods-in",
            f"{stranded} units never got placed anywhere",
        )
    total = sum(balances.values())
    if total != case.intake.quantity:
        report.add(
            "stock on the shelf does not match the return",
            f"{total} units held, return was {case.intake.quantity}",
        )


def _ledger_records_what_was_decided(
    result: Result, case: Case, ledger: Ledger, report: Report
) -> None:
    """The log says what the agent chose, not something close to it.

    Without this the drift figure could look healthy while describing a
    placement that never happened.
    """
    wanted = decision_id(case.intake.return_id, PLACEMENT_STEP)
    placements = [m for m in ledger.for_return(case.intake.return_id) if m.decision == wanted]
    if len(placements) != 1:
        report.add("return was not placed exactly once", f"{len(placements)} placements")
        return
    placed = placements[0]
    if placed.destination.lot.batch_id != result.assigned_batch:
        report.add(
            "ledger batch does not match the decision",
            f"agent chose {result.assigned_batch}, ledger says {placed.destination.lot.batch_id}",
        )
    if placed.destination.lot.best_before != _recorded_expiry(result):
        report.add(
            "ledger expiry does not match the decision",
            f"agent used {_recorded_expiry(result)}, ledger says "
            f"{placed.destination.lot.best_before}",
        )


def _drift_agrees_with_the_expiry_check(
    case: Case, result: Result, ledger: Ledger, report: Report
) -> None:
    """Two ways of asking the same question must give the same answer.

    The safety property reads the chosen action. The drift measurement reads the
    ledger and compares it with ground truth. They share no code. If they ever
    disagree, one of them is lying about whether stock went out under a date
    later than the truth, and the harm numbers rest on both.
    """
    drift = drift_mod.measure(ledger, case.truth_book())
    recorded = _recorded_expiry(result)
    from_action = recorded is not None and recorded > case.best_before(case.truth)
    from_ledger = drift.overstated_unit_days > 0
    if from_action != from_ledger:
        report.add(
            "drift and the expiry check disagree",
            f"action says overstated={from_action}, ledger drift says {from_ledger} "
            f"({drift.summary()})",
        )


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
