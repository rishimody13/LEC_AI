"""Builds the warehouse ground truth used by every test case.

The numbers here are fixed rather than random so the demo is reproducible and so
the worked example in PLAN.md section 6.5 can be checked by hand. Randomness
enters later, in the demand simulator.
"""

from __future__ import annotations

from datetime import date

from common.coding import label_code

from .types import (
    Batch,
    Bin,
    BinStatus,
    Customer,
    ReturnEvent,
    Shipment,
    ShipmentLine,
    Sku,
    World,
)

TODAY = date(2026, 8, 15)
SKU_ID = "SKU-4471"


def _bins() -> list[Bin]:
    bins = [
        Bin(bin_id="A-07-02", zone="A-07", capacity=2000),
        Bin(bin_id="A-07-05", zone="A-07", capacity=2000),
        Bin(bin_id="A-07-08", zone="A-07", capacity=2000),
        Bin(bin_id="C-04-01", zone="C-04", capacity=3000),
        Bin(bin_id="C-04-03", zone="C-04", capacity=3000),
        # Holding area used when the agent decides to segregate.
        Bin(bin_id="H-01-01", zone="H-01", capacity=500, status=BinStatus.HOLD),
        Bin(bin_id="Q-01-01", zone="Q-01", capacity=500, status=BinStatus.QUARANTINE),
    ]
    return bins


def _batches() -> list[Batch]:
    return [
        Batch(
            batch_id="B-2288",
            sku_id=SKU_ID,
            manufactured=date(2025, 9, 12),
            qa_released=date(2025, 9, 19),
            best_before=date(2026, 9, 30),
            home_bin="A-07-02",
            quantity_on_hand=1120,
        ),
        Batch(
            batch_id="B-2290",
            sku_id=SKU_ID,
            manufactured=date(2025, 11, 3),
            qa_released=date(2025, 11, 10),
            best_before=date(2026, 11, 30),
            home_bin="A-07-05",
            quantity_on_hand=780,
        ),
        Batch(
            batch_id="B-2291",
            sku_id=SKU_ID,
            manufactured=date(2026, 1, 20),
            # Released from quality hold long after it was made. This is the fact
            # that makes the hero case solvable.
            qa_released=date(2026, 6, 28),
            best_before=date(2027, 3, 15),
            home_bin="C-04-01",
            quantity_on_hand=2400,
        ),
        Batch(
            batch_id="B-2293",
            sku_id=SKU_ID,
            manufactured=date(2026, 3, 4),
            qa_released=date(2026, 3, 11),
            best_before=date(2027, 5, 20),
            home_bin="C-04-03",
            quantity_on_hand=1650,
        ),
        Batch(
            batch_id="B-2296",
            sku_id=SKU_ID,
            manufactured=date(2026, 4, 22),
            qa_released=date(2026, 4, 29),
            best_before=date(2027, 7, 8),
            home_bin="A-07-08",
            quantity_on_hand=940,
        ),
    ]


def _customers() -> list[Customer]:
    return [
        # Runs its own repacking line, which is why its labels sometimes describe
        # the wrong contents.
        Customer(customer_id="CUST-118", name="Northgate Distribution", repacks=True),
        Customer(customer_id="CUST-204", name="Halden Grocers", repacks=False),
        Customer(customer_id="CUST-337", name="Peak Foods", repacks=False),
        Customer(customer_id="CUST-455", name="Ridgeway Retail", repacks=False),
    ]


def _shipments() -> list[Shipment]:
    return [
        # Hero case: two shipments to the same customer, different batches.
        Shipment(
            shipment_id="SH-77120",
            customer_id="CUST-118",
            sku_id=SKU_ID,
            dispatched=date(2026, 6, 2),
            lines=[ShipmentLine(batch_id="B-2288", quantity=240)],
        ),
        Shipment(
            shipment_id="SH-77455",
            customer_id="CUST-118",
            sku_id=SKU_ID,
            dispatched=date(2026, 7, 19),
            lines=[ShipmentLine(batch_id="B-2290", quantity=120)],
        ),
        # Clean case: one shipment, one batch. Kept on its own customer so the
        # near-miss case below is left with exactly two candidates.
        Shipment(
            shipment_id="SH-77501",
            customer_id="CUST-455",
            sku_id=SKU_ID,
            dispatched=date(2026, 7, 2),
            lines=[ShipmentLine(batch_id="B-2293", quantity=180)],
        ),
        # Near-miss case: two batches whose codes differ by one digit.
        Shipment(
            shipment_id="SH-77610",
            customer_id="CUST-204",
            sku_id=SKU_ID,
            dispatched=date(2026, 7, 25),
            lines=[ShipmentLine(batch_id="B-2290", quantity=90)],
        ),
        Shipment(
            shipment_id="SH-77655",
            customer_id="CUST-204",
            sku_id=SKU_ID,
            dispatched=date(2026, 8, 1),
            lines=[ShipmentLine(batch_id="B-2291", quantity=90)],
        ),
        # Fog-of-war case: three open orders across three batches.
        Shipment(
            shipment_id="SH-77700",
            customer_id="CUST-337",
            sku_id=SKU_ID,
            dispatched=date(2026, 6, 15),
            lines=[ShipmentLine(batch_id="B-2288", quantity=100)],
        ),
        Shipment(
            shipment_id="SH-77733",
            customer_id="CUST-337",
            sku_id=SKU_ID,
            dispatched=date(2026, 7, 6),
            lines=[ShipmentLine(batch_id="B-2293", quantity=100)],
        ),
        Shipment(
            shipment_id="SH-77780",
            customer_id="CUST-337",
            sku_id=SKU_ID,
            dispatched=date(2026, 7, 28),
            lines=[ShipmentLine(batch_id="B-2296", quantity=100)],
        ),
    ]


def _returns(batches: dict[str, Batch]) -> list[ReturnEvent]:
    def printed(batch_id: str) -> tuple[str, date]:
        b = batches[batch_id]
        return label_code(batch_id), b.best_before

    out: list[ReturnEvent] = []

    # S1 - clean. Label agrees with the only shipment on record.
    code, bb = printed("B-2293")
    out.append(
        ReturnEvent(
            return_id="RET-S1",
            customer_id="CUST-455",
            sku_id=SKU_ID,
            quantity=40,
            arrived=TODAY,
            true_batch_id="B-2293",
            printed_code=code,
            printed_best_before=bb,
            condition_note="Two outers crushed in transit, contents intact.",
        )
    )

    # S2 - label damaged beyond reading, but only one batch was ever shipped here.
    code, bb = printed("B-2293")
    out.append(
        ReturnEvent(
            return_id="RET-S2",
            customer_id="CUST-455",
            sku_id=SKU_ID,
            quantity=35,
            arrived=TODAY,
            true_batch_id="B-2293",
            printed_code=code,
            printed_best_before=bb,
            condition_note="Pallet wrap failed, label face water damaged.",
        )
    )

    # S3 - label unreadable and three batches were shipped to this customer.
    code, bb = printed("B-2296")
    out.append(
        ReturnEvent(
            return_id="RET-S3",
            customer_id="CUST-337",
            sku_id=SKU_ID,
            quantity=60,
            arrived=TODAY,
            true_batch_id="B-2296",
            printed_code=code,
            printed_best_before=bb,
            condition_note="Mixed pallet, paperwork missing.",
        )
    )

    # S4 - the hero case. Contents are B-2288. The carton is a reused outer box
    # carrying a perfectly legible B-2291 label.
    b2291 = batches["B-2291"]
    out.append(
        ReturnEvent(
            return_id="RET-S4",
            customer_id="CUST-118",
            sku_id=SKU_ID,
            quantity=84,
            arrived=TODAY,
            true_batch_id="B-2288",
            printed_code=label_code("B-2291"),
            printed_best_before=b2291.best_before,
            condition_note=(
                "Outer sleeve re-taped, inner cases show print date 12SEP25. "
                "Customer cites over-ordering, goods unopened."
            ),
        )
    )

    # S5 - both sources degraded: partly readable label, stale warehouse replica.
    code, bb = printed("B-2290")
    out.append(
        ReturnEvent(
            return_id="RET-S5",
            customer_id="CUST-118",
            sku_id=SKU_ID,
            quantity=48,
            arrived=TODAY,
            true_batch_id="B-2290",
            printed_code=code,
            printed_best_before=bb,
            condition_note="Chiller door left open overnight, temperature log attached.",
        )
    )

    # S6 - near miss. Check digit destroyed; the visible characters fit both
    # B-2290 and B-2291, and both were shipped to this customer.
    code, bb = printed("B-2291")
    out.append(
        ReturnEvent(
            return_id="RET-S6",
            customer_id="CUST-204",
            sku_id=SKU_ID,
            quantity=30,
            arrived=TODAY,
            true_batch_id="B-2291",
            printed_code=code,
            printed_best_before=bb,
            condition_note="Piece of label torn away during de-palletising.",
        )
    )

    return out


def build_world() -> World:
    """The single canonical warehouse state. Deterministic - no seed needed."""
    batches = _batches()
    by_id = {b.batch_id: b for b in batches}
    return World(
        today=TODAY,
        skus=[Sku(sku_id=SKU_ID, name="NutriPlus Infant Formula 800g", unit_cost_gbp=11.40)],
        customers=_customers(),
        bins=_bins(),
        batches=batches,
        shipments=_shipments(),
        returns=_returns(by_id),
    )
