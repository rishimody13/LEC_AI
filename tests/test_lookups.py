"""The paid lookups, and the fact that cracks the hero case."""

from __future__ import annotations

from datetime import date

import pytest

from agent.evidence import LookupSymptom
from services import db
from services.batch_registry import PRICE_GBP as REGISTRY_PRICE
from services.batch_registry import BatchRegistry
from services.faults import LookupFaults
from services.shipment_ledger import PRICE_GBP as LEDGER_PRICE
from services.shipment_ledger import ShipmentLedger
from world.generators import build_world


@pytest.fixture(scope="module")
def conn():
    return db.build(build_world())


def test_registry_returns_dates_and_costs_money(conn):
    e = BatchRegistry(conn).lookup(["B-2288", "B-2291"])
    assert e.available
    assert e.cost_gbp == REGISTRY_PRICE > 0
    assert {b.batch_id for b in e.batches} == {"B-2288", "B-2291"}
    assert e.record("B-2288").manufactured == date(2025, 9, 12)


def test_registry_reveals_the_temporal_impossibility(conn):
    """The whole hero case turns on these two facts."""
    e = BatchRegistry(conn).lookup(["B-2291"])
    b2291 = e.record("B-2291")

    # Released from quality hold after the June shipment left the building.
    assert b2291.qa_released == date(2026, 6, 28)
    assert b2291.qa_released > date(2026, 6, 2)

    # And it was never allocated to that customer at all.
    assert "CUST-118" not in b2291.allocated_to


def test_unavailable_registry_charges_nothing(conn):
    e = BatchRegistry(conn, LookupFaults(unavailable=True)).lookup(["B-2291"])
    assert not e.available
    assert e.batches == []
    assert e.cost_gbp == 0.0
    assert e.symptoms == {LookupSymptom.UNAVAILABLE}


def test_partial_registry_drops_the_allocation_history(conn):
    """Still answers, but without the half that settles the hero case."""
    e = BatchRegistry(conn, LookupFaults(partial=True)).lookup(["B-2291"])
    assert e.available
    assert LookupSymptom.PARTIAL in e.symptoms
    assert e.record("B-2291").qa_released == date(2026, 6, 28)
    assert e.record("B-2291").allocated_to == []


def test_registry_handles_an_empty_request(conn):
    e = BatchRegistry(conn).lookup([])
    assert e.available and e.batches == []


def test_unknown_batch_is_simply_absent(conn):
    e = BatchRegistry(conn).lookup(["B-9999"])
    assert e.available
    assert e.record("B-9999") is None


def test_ledger_reports_what_was_scanned_out(conn):
    e = ShipmentLedger(conn).scans_for("CUST-118", "SKU-4471")
    assert e.available
    assert e.cost_gbp == LEDGER_PRICE > 0
    assert [s.batch_id for s in e.scans] == ["B-2288", "B-2290"]
    assert e.scans[0].scanned_at.date() == date(2026, 6, 2)


def test_ledger_is_unaffected_by_a_stale_warehouse_replica(conn):
    """Independence is what makes it worth buying when the WMS is the suspect."""
    from services.faults import WmsFaults
    from services.wms_client import WmsClient

    stale = WmsClient(conn, WmsFaults(stale_as_of=date(2026, 7, 10)))
    record = stale.shipments_to("CUST-118", "SKU-4471", asked_on=date(2026, 8, 15))
    ledger = ShipmentLedger(conn).scans_for("CUST-118", "SKU-4471")

    assert record.batch_ids == ["B-2288"], "replica is missing the July shipment"
    assert [s.batch_id for s in ledger.scans] == ["B-2288", "B-2290"], "door scans still have it"


def test_unavailable_ledger(conn):
    e = ShipmentLedger(conn, LookupFaults(unavailable=True)).scans_for("CUST-118", "SKU-4471")
    assert not e.available and e.scans == []
