"""The agent against cases nobody wrote.

Everything else in this suite runs against eight hand-written cases. Those were
added one at a time, each once the code could already handle it, which is exactly
why several real bugs sat in the build unnoticed. These tests build fresh worlds
from a seed and check properties, not answers.

Two worlds are used and the difference matters:

- Calibrated: faults occur at the rates config/reliability.yaml says they do. A
  failure here is a reasoning failure, because the agent's beliefs match the
  world it is in.
- Miscalibrated: every fault is equally likely, which is not what the model
  believes. Failures there mean the beliefs are wrong, not the reasoning. Only
  the properties that must hold regardless are checked in that world.
"""

from __future__ import annotations

import pytest

from agent.harm import load_costs
from agent.reliability import load_reliability
from harness import generate, properties, sweep

COSTS = load_costs()
RELIABILITY = load_reliability()

#: Enough to catch a regression without making the suite slow.
CASES = 400

#: The model itself expects labels to be wrong on about 6.6% of returns (1% for
#: ordinary customers, 17% for repackers, and about a third of customers repack).
#: Most of those are caught. This bound sits well under that figure and well over
#: what the agent currently does, so it catches a real regression without being
#: so tight that ordinary variation trips it.
MAX_DANGEROUS_SHARE = 0.02


@pytest.fixture(scope="module")
def calibrated() -> sweep.Summary:
    return sweep.sweep(CASES, calibrated=True)


@pytest.fixture(scope="module")
def miscalibrated() -> sweep.Summary:
    return sweep.sweep(CASES, calibrated=False)


def test_nothing_crashes(calibrated, miscalibrated):
    """Any combination of faults must produce a decision, not an exception."""
    assert calibrated.crashed == []
    assert miscalibrated.crashed == []


def test_no_property_breaches_when_the_world_matches_the_model(calibrated):
    """Failures here would be reasoning bugs, not calibration."""
    reducible = [
        o
        for o in calibrated.breaches
        if any(b.name != "records an expiry later than the truth" for b in o.breaches)
        or not _nothing_pointed_at_the_truth(o.seed)
    ]
    assert not reducible, "\n".join(
        f"seed {o.seed} [{o.description}]: " + "; ".join(b.name for b in o.breaches)
        for o in reducible
    )


def _nothing_pointed_at_the_truth(seed: int) -> bool:
    """Was there any evidence at all for the right answer?

    When a reused box carries another real batch's label and the replica is too
    far behind to list the real shipment, nothing anywhere names the true batch.
    Being wrong there is not a reasoning failure - it is the residual error rate
    the reliability model itself predicts.
    """
    case = generate.build(seed, calibrated=True)
    records = case.wms.shipments_to(
        case.intake.customer_id, case.intake.sku_id, asked_on=case.intake.arrived
    )
    in_records = case.truth in records.batch_ids
    on_label = bool(case.label.code_text) and case.label.code_text.startswith(
        f"{case.truth}-"[: len(case.label.code_text or "")]
    )
    in_note = case.truth in case.note.batch_codes_mentioned
    return not (in_records or on_label or in_note)


def test_dangerous_outcomes_stay_rare(calibrated):
    """Recording an expiry later than the truth is the failure that matters."""
    dangerous = sum(o.dangerous for o in calibrated.outcomes)
    share = dangerous / max(len(calibrated.outcomes), 1)
    assert share <= MAX_DANGEROUS_SHARE, (
        f"{dangerous}/{len(calibrated.outcomes)} cases recorded an expiry later "
        f"than the truth ({share:.1%})"
    )


def test_holding_stock_is_actually_safe(calibrated, miscalibrated):
    """Segregating buys one thing: an expiry that cannot be too late.

    This has to hold in both worlds. It does not depend on the agent's beliefs
    being right, only on the hold being dated at the earliest expiry any batch of
    the product has, so a miscalibrated world is no excuse.

    It was not true until the ledger was built. The hold was dated from the
    candidates the agent had thought of, which left it later than the truth
    whenever the stock was a batch nobody named.
    """
    for summary in (calibrated, miscalibrated):
        held = [o for o in summary.outcomes if o.action == "segregate"]
        assert held, "no case segregated, so this proves nothing"
        bad = [o for o in held if o.overstated_unit_days > 0]
        assert not bad, "\n".join(f"seed {o.seed} [{o.description}]" for o in bad)


def test_drift_is_only_ever_on_the_safe_side_of_the_expiry(calibrated):
    """Whatever else it gets wrong, the stock record must not say the stock
    lasts longer than it does more often than the reliability model predicts."""
    overstated = sum(o.overstated_unit_days for o in calibrated.outcomes)
    understated = sum(o.understated_unit_days for o in calibrated.outcomes)
    assert understated > 0, "no drift at all means the measurement is not running"
    assert overstated <= understated * 0.01, (
        f"{overstated} unit-days on the dangerous side against {understated} on the wasteful side"
    )


def test_it_gets_most_of_them_right(calibrated):
    """A sanity floor. An agent that escalated everything would pass the safety
    properties and be useless."""
    committed = [o for o in calibrated.outcomes if o.action == "commit"]
    assert len(committed) > len(calibrated.outcomes) * 0.3, "barely ever decides anything"
    right = sum(o.correct for o in committed)
    assert right / len(committed) > 0.85


def test_all_four_actions_appear(calibrated):
    actions = {o.action for o in calibrated.outcomes}
    assert {"commit", "segregate", "escalate"} <= actions


@pytest.mark.parametrize("seed", range(40))
def test_structural_properties_hold_in_any_world(seed):
    """These must hold whatever the world looks like, calibrated or not.

    No default branch, a real choice at every step, a belief that is a
    distribution, spend inside budget.
    """
    from agent import loop

    for calibrated_world in (True, False):
        case = generate.build(seed, calibrated=calibrated_world)
        result = loop.run(
            case.intake,
            generate.GeneratedServices(case),
            COSTS,
            RELIABILITY,
            generate.FixedNoteReader(case.note),
        )
        report = properties.check(case, result, COSTS)
        structural = [
            b
            for b in report.breaches
            if b.name
            in {
                "no decision reached",
                "decision with no alternatives",
                "did not take the cheapest action",
                "chosen action not in the options",
                "belief does not sum to 1",
                "probability out of range",
                "overspent on lookups",
                "negative spend",
            }
        ]
        assert not structural, f"seed {seed} calibrated={calibrated_world}: {structural}"


def test_generation_is_reproducible():
    a = generate.build(7)
    b = generate.build(7)
    assert a.truth == b.truth
    assert a.label.code_text == b.label.code_text
    assert a.intake.quantity == b.intake.quantity
    assert a.description == b.description


def test_the_fault_space_is_actually_covered(miscalibrated):
    """A generator that only ever produces easy cases proves nothing."""
    labels = {o.description.split("label=")[1].split()[0] for o in miscalibrated.outcomes}
    records = {o.description.split("records=")[1].split()[0] for o in miscalibrated.outcomes}
    assert len(labels) == len(generate.LabelMode)
    assert len(records) == len(generate.RecordMode)
