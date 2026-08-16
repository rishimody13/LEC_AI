"""Manufacturing and quality-release facts for a batch. A paid lookup.

It knows when a batch was released from quality hold and which customers it has
been allocated to. Together those two facts can show that a batch could not have
been in a given shipment. That is what settles the hero case.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from agent.evidence import BatchRecord, LookupSymptom, RegistryEvidence

from .faults import LookupFaults

PRICE_GBP = 0.30


class BatchRegistry:
    def __init__(self, conn: sqlite3.Connection, faults: LookupFaults | None = None) -> None:
        self._conn = conn
        self._faults = faults or LookupFaults()

    def lookup(self, batch_ids: list[str]) -> RegistryEvidence:
        if self._faults.unavailable:
            return RegistryEvidence(
                available=False,
                symptoms={LookupSymptom.UNAVAILABLE},
                cost_gbp=0.0,
            )

        if not batch_ids:
            return RegistryEvidence(batches=[], symptoms={LookupSymptom.OK}, cost_gbp=PRICE_GBP)

        placeholders = ",".join("?" * len(batch_ids))
        rows = self._conn.execute(
            f"""
            SELECT batch_id, manufactured, qa_released, best_before
            FROM batches WHERE batch_id IN ({placeholders})
            ORDER BY batch_id
            """,
            batch_ids,
        ).fetchall()

        records: list[BatchRecord] = []
        for r in rows:
            allocated: list[str] = []
            if not self._faults.partial:
                allocated = [
                    str(a["customer_id"])
                    for a in self._conn.execute(
                        "SELECT customer_id FROM batch_allocations WHERE batch_id = ?"
                        " ORDER BY customer_id",
                        (r["batch_id"],),
                    ).fetchall()
                ]
            records.append(
                BatchRecord(
                    batch_id=r["batch_id"],
                    manufactured=date.fromisoformat(r["manufactured"]),
                    qa_released=date.fromisoformat(r["qa_released"]),
                    best_before=date.fromisoformat(r["best_before"]),
                    allocated_to=allocated,
                )
            )

        symptoms = {LookupSymptom.PARTIAL} if self._faults.partial else {LookupSymptom.OK}
        return RegistryEvidence(
            available=True, batches=records, symptoms=symptoms, cost_gbp=PRICE_GBP
        )
