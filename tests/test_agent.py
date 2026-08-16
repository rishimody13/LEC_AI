"""The agent end to end.

The important test here is test_no_default_branch: editing the cost file changes
which action wins, on identical evidence. That is what shows the choice is made
at runtime rather than written into the code.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

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


def run(scenario_id: str, costs=None):
    bench = build_bench(SCENARIOS[scenario_id])
    result = loop.run(
        bench.intake,
        BenchServices(bench),
        costs or COSTS,
        RELIABILITY,
        NOTES,
        scenario_id=scenario_id,
    )
    truth = next(
        r.true_batch_id for r in bench.world.returns if r.return_id == bench.intake.return_id
    )
    return result, truth


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_behaviour_has_not_changed(scenario_id):
    """Change detector, not a correctness check.

    The values in scenarios.yaml were recorded after running the code, so passing
    this proves only that behaviour is stable. The properties that are actually
    asserted live in tests/test_generalises.py.
    """
    scenario = SCENARIOS[scenario_id]
    result, _ = run(scenario_id)

    if scenario.observed_outcome == "escalate":
        assert result.escalated
    elif scenario.observed_outcome == "segregate":
        assert result.placement is not None
        assert result.placement.chosen.action.kind is Kind.SEGREGATE
    else:
        assert result.assigned_batch == scenario.observed_batch

    assert bool(result.bought) == scenario.observed_gathers


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_the_agent_never_files_stock_under_the_wrong_batch(scenario_id):
    """Escalating or segregating is fine. Confidently filing it wrong is not."""
    result, truth = run(scenario_id)
    if result.assigned_batch is not None:
        assert result.assigned_batch == truth


def test_hero_case_avoids_the_obvious_answer():
    """The label is crisp, valid and names B-2291. The stock is B-2288."""
    result, truth = run("S4")

    stance = result.trace.decisions[0]
    assert stance.belief["B-2291"] > 0.5, "the obvious answer should lead at first"

    assert result.assigned_batch == truth == "B-2288"
    assert result.belief.of("B-2291") < 0.01, "and be gone by the end"

    # The trap is priced in the log, so the cost of getting it wrong is visible.
    placement = result.trace.decisions[1]
    trap = next(o for o in placement.options if "B-2291" in o["action"])
    chosen = next(o for o in placement.options if o["action"] == placement.chosen)
    assert trap["expected_cost_gbp"] > 1000
    assert trap["expected_cost_gbp"] > chosen["expected_cost_gbp"] * 100


def test_hero_case_reasoning_is_recorded():
    result, _ = run("S4")
    rules = {e.get("rule") for e in result.trace.evidence}
    assert "quality_release_after_shipment" in rules
    assert "never_allocated_to_customer" in rules


def test_stale_records_are_distrusted_in_favour_of_the_label():
    """S5: the label is damaged but the warehouse copy is 35 days behind."""
    result, truth = run("S5")
    assert result.belief.of(truth) > result.belief.of("B-2288")


def test_no_default_branch():
    """Editing the cost file flips the action on identical evidence.

    If any action were hardcoded this could not happen. Escalating is made very
    expensive, which should push a case that escalates into deciding for itself.
    """
    before, _ = run("S5")
    assert before.escalated

    raw = yaml.safe_load(Path("config/harm.yaml").read_text())
    raw["basis"]["analyst_hourly_gbp"] = 4000.0  # a very expensive human

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harm.yaml"
        path.write_text(yaml.safe_dump(raw))
        after, _ = run("S5", costs=load_costs(path))

    assert not after.escalated, "the action should change when escalating costs more"
    assert after.trace.outcome != before.trace.outcome


def test_cheap_case_does_not_escalate():
    """Over-escalation would be a failure even though it is always safe."""
    result, _ = run("S1")
    assert not result.escalated
    assert result.spend_gbp == 0.0


def test_every_decision_lists_what_it_beat():
    """A decision with no alternatives is not a decision."""
    for scenario_id in MAIN:
        result, _ = run(scenario_id)
        for decision in result.trace.decisions:
            assert len(decision.options) >= 2, scenario_id
            assert decision.chosen in [o["action"] for o in decision.options]


def test_all_four_actions_are_used_somewhere():
    """If an action never wins, it is not a real alternative."""
    seen = set()
    for scenario_id in SCENARIOS:
        result, _ = run(scenario_id)
        if result.bought:
            seen.add("gather")
        if result.escalated:
            seen.add("escalate")
        elif result.placement is not None:
            seen.add(result.placement.chosen.action.kind.value)
    assert {"commit", "segregate", "gather", "escalate"} <= seen, seen


def test_decisions_report_how_wrong_the_costs_could_be():
    """Every decision either lists its break-even figures or says none can flip it.

    An empty list is a real answer - it means no cost figure could be moved to a
    sensible value and change the outcome - so the trace has to say that rather
    than showing nothing.
    """
    for scenario_id in MAIN:
        result, _ = run(scenario_id)
        for decision in result.trace.decisions:
            if decision.sensitivity:
                for entry in decision.sensitivity:
                    assert entry["flips_at_gbp"] >= 0
                    assert entry["slack_x"] >= 1.0
            else:
                assert any("does not rest on the cost table" in n for n in result.trace.notes), (
                    f"{scenario_id}/{decision.name} reported no break-even and did not say why"
                )


def test_the_agent_is_reproducible():
    a, _ = run("S4")
    b, _ = run("S4")
    assert a.trace.to_dict() == b.trace.to_dict()
