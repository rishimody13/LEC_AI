"""Perception is the model's job; validation is ours.

These tests cover the validation half: given a reading, are the right warning
signs raised and the right checks applied.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.evidence import LabelSymptom, ReturnIntake
from services.label_reader import (
    CassetteLabelReader,
    LabelReading,
    UnavailableLabelReader,
    to_evidence,
)
from services.scenarios import build_bench, load_scenarios

PLAIN = ReturnIntake(
    return_id="RET-X",
    customer_id="CUST-204",
    sku_id="SKU-4471",
    quantity=10,
    arrived=date(2026, 8, 15),
)
REPACKER = PLAIN.model_copy(update={"customer_id": "CUST-118", "consignee_repacks": True})


def reading(**kw) -> LabelReading:
    base = {
        "code_text": "B-2293-2",
        "code_complete": True,
        "confidence": 0.95,
        "best_before": date(2027, 5, 20),
        "visual_condition": ["clean"],
    }
    return LabelReading(**{**base, **kw})


def test_valid_code_passes_every_check():
    e = to_evidence(reading(), PLAIN)
    assert e.well_formed and e.check_digit_ok is True
    assert e.symptoms == {LabelSymptom.CLEAN_IMAGE}
    assert e.is_usable


def test_wrong_check_digit_is_caught():
    e = to_evidence(reading(code_text="B-2293-7"), PLAIN)
    assert e.well_formed
    assert e.check_digit_ok is False
    assert LabelSymptom.CHECK_DIGIT_FAILED in e.symptoms


def test_incomplete_code_has_nothing_to_check():
    """A partial code cannot fail a check digit - there is no digit to test."""
    e = to_evidence(reading(code_text="B-229", code_complete=False), PLAIN)
    assert e.check_digit_ok is None
    assert LabelSymptom.INCOMPLETE_CODE in e.symptoms
    assert LabelSymptom.CHECK_DIGIT_FAILED not in e.symptoms
    assert e.is_usable, "partial characters are still evidence"


def test_no_code_found():
    e = to_evidence(reading(code_text=None, code_complete=False, confidence=0.0), PLAIN)
    assert LabelSymptom.NO_CODE_FOUND in e.symptoms
    assert not e.is_usable


def test_low_confidence_is_flagged():
    assert LabelSymptom.LOW_CONFIDENCE in to_evidence(reading(confidence=0.55), PLAIN).symptoms
    assert LabelSymptom.LOW_CONFIDENCE not in to_evidence(reading(confidence=0.95), PLAIN).symptoms


def test_high_confidence_on_an_incomplete_code_is_not_a_confidence_problem():
    """The torn case: the characters that survive are crisp. Different failure."""
    e = to_evidence(reading(code_text="B-229", code_complete=False, confidence=0.93), PLAIN)
    assert LabelSymptom.INCOMPLETE_CODE in e.symptoms
    assert LabelSymptom.LOW_CONFIDENCE not in e.symptoms


def test_repacking_consignee_is_flagged_even_on_a_perfect_label():
    """The hero case in one assertion: nothing wrong with the image, still suspect."""
    e = to_evidence(reading(), REPACKER)
    assert e.check_digit_ok is True
    assert e.confidence > 0.9
    assert LabelSymptom.CLEAN_IMAGE in e.symptoms
    assert LabelSymptom.REPACKING_CONSIGNEE in e.symptoms


def test_unreadable_date_is_flagged():
    assert LabelSymptom.DATE_UNREADABLE in to_evidence(reading(best_before=None), PLAIN).symptoms


def test_unavailable_reader_produces_nothing():
    e = UnavailableLabelReader().read("does/not/matter.png", PLAIN)
    assert not e.available
    assert not e.is_usable
    assert e.symptoms == {LabelSymptom.READER_UNAVAILABLE}


def test_missing_recording_fails_loudly(tmp_path):
    """A silent wrong answer would be far worse than a crash."""
    img = tmp_path / "unknown.png"
    img.write_bytes(b"not a real label")
    with pytest.raises(KeyError, match="no recorded reading"):
        CassetteLabelReader().read(img, PLAIN)


@pytest.mark.parametrize(
    ("scenario_id", "code", "usable"),
    [
        ("S1", "B-2293-2", True),
        ("S2", None, False),
        ("S3", None, False),
        ("S4", "B-2291-4", True),
        ("S5", "B-2290", True),
        ("S6", "B-229", True),
    ],
)
def test_recorded_readings_match_the_rendered_labels(scenario_id, code, usable):
    bench = build_bench(load_scenarios()[scenario_id])
    e = bench.label_reader.read(bench.image_path, bench.intake)
    assert e.code_text == code
    assert e.is_usable is usable
