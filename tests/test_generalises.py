"""Does the agent still behave on cases nobody hand-built?

Six scenarios can be passed by coincidence, especially after a parameter has been
adjusted while looking at what those six produced. These tests vary the inputs and
check the properties that should hold whatever the numbers are.

Nothing here asserts a specific outcome. Asserting outcomes recorded after tuning
would only check that the code still does what it did.
"""

from __future__ import annotations

import pytest

from agent import loop, notes
from agent.harm import load_costs
from agent.policy import Kind
from agent.reliability import load_reliability
from services.adapter import BenchServices
from services.scenarios import build_bench, load_scenarios

SCENARIOS = load_scenarios()
COSTS = load_costs()
RELIABILITY = load_reliability()
NOTES = notes.CassetteNoteReader()
MAIN = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]

# Quantities well outside the ones the scenarios were written with.
QUANTITIES = [1, 5, 12, 40, 84, 150, 400]


def run_with_quantity(scenario_id: str, quantity: int):
    bench = build_bench(SCENARIOS[scenario_id])
    intake = bench.intake.model_copy(update={"quantity": quantity})
    result = loop.run(intake, BenchServices(bench), COSTS, RELIABILITY, NOTES, scenario_id)
    truth = next(
        r.true_batch_id for r in bench.world.returns if r.return_id == bench.intake.return_id
    )
    return result, truth


@pytest.mark.parametrize("scenario_id", MAIN)
@pytest.mark.parametrize("quantity", QUANTITIES)
def test_never_records_an_expiry_later_than_the_truth(scenario_id, quantity):
    """The property that has to hold everywhere, at every size.

    Recording an expiry later than the real one is the failure that matters: the
    stock waits until the recorded date and ships after it has gone off. Recording
    an earlier one only wastes shelf life.

    Note this is deliberately weaker than "always picks the right batch". At one
    or two units the agent files the wrong batch on some cases, and that is the
    correct call - a £8.53 human review is not worth spending on a single £11.40
    unit. What it must never do at any size is take the dangerous direction.
    """
    result, truth = run_with_quantity(scenario_id, quantity)
    if result.assigned_batch is None:
        return

    bench = build_bench(SCENARIOS[scenario_id])
    by_id = {b.batch_id: b for b in bench.world.batches}
    recorded = by_id[result.assigned_batch].best_before
    actual = by_id[truth].best_before
    assert recorded <= actual, (
        f"{scenario_id} at {quantity} units recorded {recorded} for stock that "
        f"really expires {actual}"
    )


@pytest.mark.parametrize("scenario_id", MAIN)
@pytest.mark.parametrize("quantity", QUANTITIES)
def test_a_wrong_commit_is_always_the_cheaper_option(scenario_id, quantity):
    """When it files the wrong batch, checking must have cost more than the risk.

    There is no fixed size above which the agent stops getting it wrong, and
    asserting one would just be writing down a number taken off the current
    output. The size varies by case because it depends on how far apart the
    candidates' expiry dates are: S7's two candidates are 49 days apart, so being
    wrong there is cheap and the agent keeps committing to larger returns than it
    does elsewhere.

    The property that does hold, at any size, is economic: a wrong commit only
    ever happens where asking a person would have cost more than the mistake.
    """
    result, truth = run_with_quantity(scenario_id, quantity)
    if result.assigned_batch is None or result.assigned_batch == truth:
        return

    placement = result.trace.decisions[-1]
    chosen = next(o for o in placement.options if o["action"] == placement.chosen)
    escalate = next(o for o in placement.options if o["action"] == "escalate")
    assert chosen["expected_cost_gbp"] <= escalate["expected_cost_gbp"], (
        f"{scenario_id} at {quantity} units filed the wrong batch even though "
        f"escalating was cheaper (£{escalate['expected_cost_gbp']} vs "
        f"£{chosen['expected_cost_gbp']})"
    )


@pytest.mark.parametrize("scenario_id", MAIN)
def test_a_bigger_return_never_makes_it_bolder_on_the_same_evidence(scenario_id):
    """More at risk should not make the agent commit more readily.

    Only holds while the evidence is unchanged. A large enough return can break
    the "you cannot return more than was sent" rule, which rules a candidate out
    and legitimately makes the agent *more* confident. Those cases are skipped
    rather than asserted against, because there the extra units are information.
    """
    committed = []
    for quantity in QUANTITIES:
        result, _ = run_with_quantity(scenario_id, quantity)
        ruled_out = any(
            e.get("rule") == "returned_more_than_shipped" for e in result.trace.evidence
        )
        committed.append((quantity, result.assigned_batch is not None, ruled_out))

    stable = [(q, c) for q, c, ruled in committed if not ruled]
    switched_back = any(stable[i][1] and not stable[i - 1][1] for i in range(1, len(stable)))
    assert not switched_back, f"{scenario_id}: {stable}"


@pytest.mark.parametrize("scenario_id", MAIN)
@pytest.mark.parametrize("quantity", QUANTITIES)
def test_a_decision_is_always_reached(scenario_id, quantity):
    """No input should leave the agent without an action."""
    result, _ = run_with_quantity(scenario_id, quantity)
    assert result.escalated or result.placement is not None
    for decision in result.trace.decisions:
        assert len(decision.options) >= 2


@pytest.mark.parametrize("quantity", QUANTITIES)
def test_the_hero_case_never_falls_for_the_label(quantity):
    """Whatever the size of the return, B-2291 must not win.

    This is the property the case exists to test, and unlike the outcome it does
    not depend on where any parameter happens to sit.
    """
    result, _ = run_with_quantity("S4", quantity)
    assert result.assigned_batch != "B-2291"
    if result.bought:
        assert result.belief.of("B-2291") < 0.05


def test_making_a_human_free_makes_the_agent_lazy():
    """A sanity check on direction, not on any particular outcome."""
    import tempfile
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    raw["basis"]["analyst_hourly_gbp"] = 0.01
    raw["basis"]["human_error_rate"] = 0.0

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harm.yaml"
        path.write_text(yaml.safe_dump(raw))
        cheap = load_costs(path)

        escalations = 0
        for scenario_id in MAIN:
            bench = build_bench(SCENARIOS[scenario_id])
            result = loop.run(
                bench.intake, BenchServices(bench), cheap, RELIABILITY, NOTES, scenario_id
            )
            escalations += result.escalated

    assert escalations == len(MAIN), "a free, perfect human should win every time"


def test_fragile_decisions_are_flagged():
    """A decision that only just won should say so rather than look confident."""
    seen_fragile = False
    for scenario_id in MAIN:
        bench = build_bench(SCENARIOS[scenario_id])
        result = loop.run(
            bench.intake, BenchServices(bench), COSTS, RELIABILITY, NOTES, scenario_id
        )
        for decision in result.trace.decisions:
            if decision.fragile:
                seen_fragile = True
                assert decision.margin_share < 0.05
                assert any("rests on a chosen parameter" in n for n in result.trace.notes)
    assert seen_fragile, "expected at least one close call to be flagged"


@pytest.mark.parametrize("scenario_id", MAIN)
def test_segregating_is_never_a_free_way_to_dodge_the_problem(scenario_id):
    """Holding stock defers the identification work, it does not do it.

    An earlier version treated a deferral as if it removed the uncertainty, which
    made the agent prefer parking stock over deciding. Segregating must always
    cost at least what the deferred handling costs.
    """
    bench = build_bench(SCENARIOS[scenario_id])
    result = loop.run(bench.intake, BenchServices(bench), COSTS, RELIABILITY, NOTES, scenario_id)
    for decision in result.trace.decisions:
        held = [o for o in decision.options if o["action"].startswith("segregate")]
        for option in held:
            assert option["expected_cost_gbp"] >= COSTS.deferred_review * 0.99


def test_segregation_carries_the_same_batch_risk_as_committing():
    """Waiting does not make the uncertainty go away."""
    from agent import belief as belief_mod
    from agent import candidates as candidates_mod
    from agent.policy import segregate_cost

    bench = build_bench(SCENARIOS["S6"])
    label = bench.label_reader.read(bench.image_path, bench.intake)
    records = bench.wms.shipments_to(
        bench.intake.customer_id, bench.intake.sku_id, asked_on=bench.intake.arrived
    )
    catalogue = bench.wms.catalogue(bench.intake.sku_id)
    cs = candidates_mod.build(bench.intake, records, label, catalogue)
    b = belief_mod.start(cs)

    cost = segregate_cost(b, 100, bench.intake.arrived, COSTS)
    from agent.harm import MISATTRIBUTION_UNIT

    _, best_p = b.best()
    assert cost.exposure.get(MISATTRIBUTION_UNIT, 0.0) == pytest.approx((1 - best_p) * 100)


@pytest.mark.parametrize("scenario_id", MAIN)
def test_outcomes_do_not_flip_on_a_tiny_cost_change(scenario_id):
    """A one percent nudge should not change what the agent does.

    Where it does, the trace has to have called that decision fragile. Silently
    flipping on a rounding-sized change would mean the answer was set by the
    parameter rather than the evidence.
    """
    import tempfile
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    raw["judged"]["expired_unit_gbp"] *= 1.01

    base_result, _ = run_with_quantity(scenario_id, 84)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harm.yaml"
        path.write_text(yaml.safe_dump(raw))
        nudged_costs = load_costs(path)
        bench = build_bench(SCENARIOS[scenario_id])
        intake = bench.intake.model_copy(update={"quantity": 84})
        nudged = loop.run(
            intake, BenchServices(bench), nudged_costs, RELIABILITY, NOTES, scenario_id
        )

    if nudged.trace.outcome != base_result.trace.outcome:
        assert any(d.fragile for d in base_result.trace.decisions), (
            f"{scenario_id} flipped on a 1% cost change without being flagged fragile"
        )


def test_all_actions_still_win_somewhere():
    seen = set()
    for scenario_id in SCENARIOS:
        bench = build_bench(SCENARIOS[scenario_id])
        result = loop.run(
            bench.intake, BenchServices(bench), COSTS, RELIABILITY, NOTES, scenario_id
        )
        if result.bought:
            seen.add(Kind.GATHER.value)
        if result.escalated:
            seen.add(Kind.ESCALATE.value)
        elif result.placement is not None:
            seen.add(result.placement.chosen.action.kind.value)
    assert {Kind.COMMIT.value, Kind.SEGREGATE.value, Kind.GATHER.value, Kind.ESCALATE.value} <= seen
