import pytest

from common.coding import batch_id_from_label, check_digit_ok
from world.generators import build_world

WORLD = build_world()
BATCHES = {b.batch_id: b for b in WORLD.batches}
BINS = {b.bin_id for b in WORLD.bins}


def test_world_is_internally_consistent():
    for b in WORLD.batches:
        assert b.manufactured <= b.qa_released < b.best_before
        assert b.home_bin in BINS
    for s in WORLD.shipments:
        for line in s.lines:
            assert line.batch_id in BATCHES
            # Nothing can ship before it clears quality control.
            assert BATCHES[line.batch_id].qa_released <= s.dispatched


def test_every_return_is_a_partial_return():
    for r in WORLD.returns:
        shipped = sum(s.total_quantity for s in WORLD.shipments_to(r.customer_id, r.sku_id))
        assert 0 < r.quantity < shipped, f"{r.return_id} is not a partial return"


def test_true_batch_was_actually_shipped_to_that_customer():
    for r in WORLD.returns:
        sent = {
            line.batch_id for s in WORLD.shipments_to(r.customer_id, r.sku_id) for line in s.lines
        }
        assert r.true_batch_id in sent, f"{r.return_id} claims a batch never sent there"


def test_printed_codes_are_valid_even_when_they_describe_the_wrong_batch():
    for r in WORLD.returns:
        assert check_digit_ok(r.printed_code), r.return_id


def test_hero_case_is_actually_misleading():
    """The whole demo rests on this: a valid label naming the wrong batch."""
    s4 = next(r for r in WORLD.returns if r.return_id == "RET-S4")
    printed_batch = batch_id_from_label(s4.printed_code)

    assert printed_batch != s4.true_batch_id
    assert check_digit_ok(s4.printed_code)
    assert WORLD.customer(s4.customer_id).repacks

    printed = BATCHES[printed_batch]
    truth = BATCHES[s4.true_batch_id]

    # Believing the label overstates shelf life, which is the dangerous direction.
    assert printed.best_before > truth.best_before
    assert (printed.best_before - truth.best_before).days == 166

    # And the label's batch could not physically have been in that shipment.
    june = next(s for s in WORLD.shipments if s.shipment_id == "SH-77120")
    assert printed.qa_released > june.dispatched


def test_near_miss_case_has_exactly_two_candidates():
    """The torn label leaves 'B-229' visible, which matches four batch codes.

    Only the shipping records narrow it down, and they must narrow it to two -
    otherwise this case is just a repeat of the fog-of-war case.
    """
    s6 = next(r for r in WORLD.returns if r.return_id == "RET-S6")
    sent = sorted(
        {line.batch_id for s in WORLD.shipments_to(s6.customer_id, s6.sku_id) for line in s.lines}
    )
    assert sent == ["B-2290", "B-2291"]

    # Both really do match the surviving fragment of the code.
    fragment = "B-229"
    matching = sorted(b.batch_id for b in WORLD.batches if b.batch_id.startswith(fragment))
    assert set(sent) <= set(matching)

    # And their best-before dates differ, so losing the date to the tear matters.
    assert BATCHES["B-2290"].best_before != BATCHES["B-2291"].best_before


def test_fog_of_war_case_has_three_candidate_batches():
    s3 = next(r for r in WORLD.returns if r.return_id == "RET-S3")
    sent = {
        line.batch_id for s in WORLD.shipments_to(s3.customer_id, s3.sku_id) for line in s.lines
    }
    assert len(sent) == 3


def test_build_world_is_deterministic():
    assert build_world().model_dump() == build_world().model_dump()


@pytest.mark.parametrize("return_id", ["RET-S1", "RET-S2", "RET-S3", "RET-S4", "RET-S5", "RET-S6"])
def test_all_six_cases_exist(return_id):
    assert any(r.return_id == return_id for r in WORLD.returns)
