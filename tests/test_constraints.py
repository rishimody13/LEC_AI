"""Hard rules, and the difference between two kinds of them.

`quality_release_after_shipment` is a fact about the physical world. The other two
only hold if our records of what went where are complete, and cross-docked stock
breaks that. Treating them all the same was a real bug: it threw away the answer
in the case where the note was the only source.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent import constraints
from agent.candidates import Candidate
from agent.evidence import (
    BatchRecord,
    RecordEvidence,
    RegistryEvidence,
    ReturnIntake,
    ShipmentRecord,
)

INTAKE = ReturnIntake(
    return_id="RET-X",
    customer_id="CUST-118",
    sku_id="SKU-4471",
    quantity=84,
    arrived=date(2026, 8, 15),
)

RECORDS = RecordEvidence(
    shipments=[
        ShipmentRecord(
            shipment_id="SH-1", dispatched=date(2026, 6, 2), batch_id="B-2288", quantity=240
        )
    ]
)

CANDIDATES = [
    Candidate(batch_id="B-2288", source="records", shipped_quantity=240),
    Candidate(batch_id="B-2291", source="label"),
    Candidate(batch_id=None, source="catch_all"),
]


def registry(qa_released: date, allocated: list[str]) -> RegistryEvidence:
    return RegistryEvidence(
        batches=[
            BatchRecord(
                batch_id="B-2288",
                manufactured=date(2025, 9, 12),
                qa_released=date(2025, 9, 19),
                best_before=date(2026, 9, 30),
                allocated_to=["CUST-118"],
            ),
            BatchRecord(
                batch_id="B-2291",
                manufactured=date(2026, 1, 20),
                qa_released=qa_released,
                best_before=date(2027, 3, 15),
                allocated_to=allocated,
            ),
        ]
    )


def rules(violations):
    return {v.rule for v in violations}


def test_a_batch_cannot_be_in_a_shipment_that_left_before_it_existed():
    found = constraints.check(
        CANDIDATES, INTAKE, RECORDS, registry(date(2026, 6, 28), ["CUST-204"])
    )
    assert "quality_release_after_shipment" in rules(found)
    breach = next(v for v in found if v.rule == "quality_release_after_shipment")
    assert breach.candidate == "B-2291"


def test_a_batch_released_before_the_shipment_is_fine():
    found = constraints.check(
        CANDIDATES, INTAKE, RECORDS, registry(date(2026, 1, 25), ["CUST-118"])
    )
    assert "quality_release_after_shipment" not in rules(found)


def test_never_allocated_to_this_customer():
    found = constraints.check(
        CANDIDATES, INTAKE, RECORDS, registry(date(2026, 1, 25), ["CUST-204"])
    )
    assert "never_allocated_to_customer" in rules(found)


def test_cannot_return_more_than_was_sent():
    big = INTAKE.model_copy(update={"quantity": 500})
    found = constraints.check(CANDIDATES, big, RECORDS, registry(date(2026, 1, 25), ["CUST-118"]))
    assert "returned_more_than_shipped" in rules(found)


def test_the_physical_rule_still_applies_to_off_record_stock():
    """A batch that had not been made yet cannot have been in a lorry.

    Nothing about how the goods reached us changes that.
    """
    found = constraints.check(
        CANDIDATES,
        INTAKE,
        RECORDS,
        registry(date(2026, 6, 28), ["CUST-204"]),
        off_record=True,
    )
    assert "quality_release_after_shipment" in rules(found)


@pytest.mark.parametrize("rule", ["never_allocated_to_customer", "returned_more_than_shipped"])
def test_the_record_completeness_rules_stop_applying_to_off_record_stock(rule):
    """Cross-docked stock was never going to appear in the allocation history.

    Applying these anyway threw away the only candidate the note had supplied.
    """
    big = INTAKE.model_copy(update={"quantity": 500})
    without = constraints.check(CANDIDATES, big, RECORDS, registry(date(2026, 1, 25), ["CUST-204"]))
    with_flag = constraints.check(
        CANDIDATES, big, RECORDS, registry(date(2026, 1, 25), ["CUST-204"]), off_record=True
    )
    assert rule in rules(without)
    assert rule not in rules(with_flag)


def test_a_repacker_may_return_more_of_a_batch_than_we_sent_them():
    """Found by the generative sweep, on a case nobody wrote.

    A customer who repacks splits and recombines pallets, so one we sent 66 units
    of a batch can perfectly well send back 72 of it. The rule describes a
    customer who keeps consignments intact. Applying it anyway ruled out the true
    batch, and it did so hardest on exactly the customers whose habits make these
    cases difficult in the first place.
    """
    big = INTAKE.model_copy(update={"quantity": 500})
    repacker = big.model_copy(update={"consignee_repacks": True})
    reg = registry(date(2026, 1, 25), ["CUST-118"])

    assert "returned_more_than_shipped" in rules(constraints.check(CANDIDATES, big, RECORDS, reg))
    assert "returned_more_than_shipped" not in rules(
        constraints.check(CANDIDATES, repacker, RECORDS, reg)
    )


def test_repacking_does_not_excuse_a_batch_we_never_sent_them():
    """The other half of the rule above, and the one that matters more.

    Repacking rearranges stock the customer already has; it cannot produce a
    batch we never sent them. A batch arriving from somewhere else is off-record
    stock, which is a separate flag. This is the rule the hero case turns on, and
    an earlier version of the fix above switched it off for repackers too - which
    would have removed the evidence that solves S4.
    """
    repacker = INTAKE.model_copy(update={"consignee_repacks": True})
    found = constraints.check(
        CANDIDATES, repacker, RECORDS, registry(date(2026, 1, 25), ["CUST-204"])
    )
    assert "never_allocated_to_customer" in rules(found)


def test_nothing_is_ever_driven_to_zero():
    """Nothing recovers from zero, and the source reporting the rule can be wrong."""
    found = constraints.check(
        CANDIDATES, INTAKE, RECORDS, registry(date(2026, 6, 28), ["CUST-204"])
    )
    for v in found:
        assert 0.0 < v.survives_with < 1.0
    for value in constraints.multipliers(found).values():
        assert value > 0.0


def test_no_registry_means_no_rules_can_be_checked():
    assert constraints.check(CANDIDATES, INTAKE, RECORDS, None) == []


def test_a_partial_lookup_does_not_look_like_never_allocated():
    """An empty allocation list means the lookup came back short, not that the
    batch went nowhere."""
    partial = RegistryEvidence(
        batches=[
            BatchRecord(
                batch_id="B-2291",
                manufactured=date(2026, 1, 20),
                qa_released=date(2026, 1, 25),
                best_before=date(2027, 3, 15),
                allocated_to=[],
            )
        ]
    )
    found = constraints.check(CANDIDATES, INTAKE, RECORDS, partial)
    assert "never_allocated_to_customer" not in rules(found)


def test_multipliers_combine_across_rules():
    found = constraints.check(
        CANDIDATES, INTAKE, RECORDS, registry(date(2026, 6, 28), ["CUST-204"])
    )
    combined = constraints.multipliers(found)
    assert combined["B-2291"] == pytest.approx(constraints.REGISTRY_ERROR_RATE**2), (
        "two separate breaches should both count"
    )
