"""What was physically scanned out of the door. A paid lookup.

These are door scans, so a stale warehouse replica does not affect them. That is
why this lookup is worth buying when the warehouse system is the suspect source.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from agent.evidence import DispatchScan, LedgerEvidence, LookupSymptom

from .faults import LookupFaults

PRICE_GBP = 0.40


class ShipmentLedger:
    def __init__(self, conn: sqlite3.Connection, faults: LookupFaults | None = None) -> None:
        self._conn = conn
        self._faults = faults or LookupFaults()

    def scans_for(self, customer_id: str, sku_id: str) -> LedgerEvidence:
        if self._faults.unavailable:
            return LedgerEvidence(available=False, symptoms={LookupSymptom.UNAVAILABLE})

        rows = self._conn.execute(
            """
            SELECT d.shipment_id, d.batch_id, d.quantity, d.scanned_at
            FROM dispatch_scans d
            JOIN shipments s ON s.shipment_id = d.shipment_id
            WHERE s.customer_id = ? AND s.sku_id = ?
            ORDER BY d.scanned_at, d.shipment_id
            """,
            (customer_id, sku_id),
        ).fetchall()

        scans = [
            DispatchScan(
                shipment_id=r["shipment_id"],
                batch_id=r["batch_id"],
                quantity=r["quantity"],
                scanned_at=datetime.fromisoformat(r["scanned_at"]),
            )
            for r in rows
        ]
        return LedgerEvidence(
            available=True, scans=scans, symptoms={LookupSymptom.OK}, cost_gbp=PRICE_GBP
        )
