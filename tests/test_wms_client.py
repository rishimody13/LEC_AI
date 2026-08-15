"""The warehouse system must fail in ways the agent can see."""

from __future__ import annotations

from datetime import date

import pytest

from agent.evidence import RecordSymptom
from services import db
from services.faults import WmsFaults
from services.wms_client import WmsClient
from world.generators import build_world

TODAY = date(2026, 8, 15)


@pytest.fixture(scope="module")
def conn():
    return db.build(build_world())


def query(conn, faults=None, customer="CUST-118"):
    return WmsClient(conn, faults or WmsFaults()).shipments_to(customer, "SKU-4471", asked_on=TODAY)


def test_healthy_query_returns_both_shipments(conn):
    e = query(conn)
    assert e.available
    assert e.batch_ids == ["B-2288", "B-2290"]
    assert e.lag_seconds == 0
    assert RecordSymptom.MULTIPLE_MATCHES in e.symptoms
    assert not e.warnings


def test_single_match_is_flagged_as_such(conn):
    e = query(conn, customer="CUST-455")
    assert e.batch_ids == ["B-2293"]
    assert RecordSymptom.SINGLE_MATCH in e.symptoms


def test_timeout_produces_no_evidence_at_all(conn):
    e = query(conn, WmsFaults(timeout=True))
    assert not e.available
    assert e.shipments == []
    assert e.symptoms == {RecordSymptom.TIMEOUT}


def test_stale_replica_hides_recent_shipments_without_erroring(conn):
    """The dangerous one: a plausible answer that is quietly incomplete."""
    e = query(conn, WmsFaults(stale_as_of=date(2026, 7, 10)))

    assert e.available, "a stale replica still answers - that is what makes it dangerous"
    assert e.batch_ids == ["B-2288"], "the July shipment should be invisible"
    assert RecordSymptom.REPLICA_LAG in e.symptoms
    assert e.lag_seconds > 2 * 60 * 60
    assert e.as_of is not None and e.as_of.date() == date(2026, 7, 10)
    assert any("2026-07-10" in w for w in e.warnings)


def test_lag_under_the_threshold_is_not_flagged(conn):
    e = query(conn, WmsFaults(stale_as_of=TODAY))
    assert RecordSymptom.REPLICA_LAG not in e.symptoms


def test_conflicting_rows_name_two_different_batches(conn):
    e = query(conn, WmsFaults(conflicting_rows=True), customer="CUST-455")
    assert RecordSymptom.CONFLICTING_ROWS in e.symptoms
    assert RecordSymptom.DUPLICATE_ROWS in e.symptoms
    assert len(e.batch_ids) > 1
    assert any("appears twice" in w for w in e.warnings)


def test_duplicate_rows_do_not_invent_a_second_batch(conn):
    e = query(conn, WmsFaults(duplicate_rows=True), customer="CUST-455")
    assert RecordSymptom.DUPLICATE_ROWS in e.symptoms
    assert e.batch_ids == ["B-2293"]
    assert len(e.shipments) == 2


def test_unknown_customer_reports_no_match(conn):
    e = query(conn, customer="CUST-999")
    assert e.available
    assert e.shipments == []
    assert RecordSymptom.NO_MATCH in e.symptoms
