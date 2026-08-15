"""Each test case wires up, and each evidence source can fail on its own.

The independent-failure tests are the proof for requirement R3: one component
breaks while the other is untouched, and the break leaves a named symptom.
"""

from __future__ import annotations

import pytest

from agent.evidence import LabelSymptom, RecordSymptom
from services.scenarios import build_bench, load_scenarios

SCENARIOS = load_scenarios()
MAIN = ["S1", "S2", "S3", "S4", "S5", "S6"]


def evidence(scenario_id):
    bench = build_bench(SCENARIOS[scenario_id])
    label = bench.label_reader.read(bench.image_path, bench.intake)
    record = bench.wms.shipments_to(
        bench.intake.customer_id, bench.intake.sku_id, asked_on=bench.intake.arrived
    )
    return bench, label, record


@pytest.mark.parametrize("scenario_id", MAIN)
def test_every_case_produces_both_kinds_of_evidence(scenario_id):
    bench, label, record = evidence(scenario_id)
    assert bench.intake.return_id == SCENARIOS[scenario_id].return_id
    assert bench.image_path.exists()
    assert label is not None and record is not None


def test_only_the_label_fails():
    """R3: the reader is down; the warehouse system is untouched."""
    _, label, record = evidence("X-label-down")
    assert not label.available
    assert LabelSymptom.READER_UNAVAILABLE in label.symptoms
    assert record.available and record.batch_ids == ["B-2293"]


def test_only_the_warehouse_system_fails():
    """R3: the query times out; the label reads perfectly."""
    _, label, record = evidence("X-wms-down")
    assert not record.available
    assert RecordSymptom.TIMEOUT in record.symptoms
    assert label.available and label.check_digit_ok is True


def test_warehouse_rows_can_contradict_each_other():
    _, label, record = evidence("X-wms-conflicting")
    assert record.available
    assert RecordSymptom.CONFLICTING_ROWS in record.symptoms
    assert label.check_digit_ok is True


def test_clean_case_has_no_conflict():
    _, label, record = evidence("S1")
    from common.coding import batch_id_from_label

    assert batch_id_from_label(label.code_text) in record.batch_ids
    assert RecordSymptom.SINGLE_MATCH in record.symptoms


def test_hero_case_label_is_perfect_and_contradicts_the_records():
    """R5b: internally consistent metadata, conflicting with plausible records."""
    from common.coding import batch_id_from_label

    _, label, record = evidence("S4")

    assert label.check_digit_ok is True
    assert label.confidence > 0.9
    assert LabelSymptom.CLEAN_IMAGE in label.symptoms
    assert LabelSymptom.REPACKING_CONSIGNEE in label.symptoms

    claimed = batch_id_from_label(label.code_text)
    assert claimed == "B-2291"
    assert claimed not in record.batch_ids, "the label names a batch never sent here"
    assert len(record.batch_ids) == 2, "and two plausible batches were"


def test_unreadable_label_case_still_has_one_clear_record():
    """R5a: metadata destroyed, but the records settle it on their own."""
    _, label, record = evidence("S2")
    assert not label.is_usable
    assert record.batch_ids == ["B-2293"]


def test_fog_of_war_case_has_nothing_to_go_on():
    _, label, record = evidence("S3")
    assert not label.is_usable
    assert label.best_before is None, "the date would otherwise identify the batch"
    assert len(record.batch_ids) == 3


def test_both_sources_degraded_at_once():
    """R4: two failing components that interact, each with its own symptoms."""
    _, label, record = evidence("S5")

    assert label.is_usable and LabelSymptom.LOW_CONFIDENCE in label.symptoms
    assert LabelSymptom.INCOMPLETE_CODE in label.symptoms

    assert record.available and RecordSymptom.REPLICA_LAG in record.symptoms
    assert record.lag_seconds > 2 * 60 * 60

    # And they disagree: the label points at a batch the stale replica cannot see.
    assert label.code_text.startswith("B-2290")
    assert record.batch_ids == ["B-2288"]


def test_near_miss_fragment_fits_both_recorded_batches():
    _, label, record = evidence("S6")
    assert label.code_text == "B-229"
    assert record.batch_ids == ["B-2290", "B-2291"]
    assert all(b.startswith(label.code_text) for b in record.batch_ids)
    assert label.best_before is None, "the date would otherwise settle it"


def test_paid_lookup_can_be_unavailable():
    bench = build_bench(SCENARIOS["X-registry-down"])
    e = bench.registry.lookup(["B-2291"])
    assert not e.available and e.cost_gbp == 0.0


def test_scenarios_declare_an_expected_action():
    for scenario in SCENARIOS.values():
        assert scenario.expected_action in {"commit", "gather", "segregate", "escalate"}
