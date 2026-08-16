"""Queries the warehouse system for what was sent to a customer.

This is one of the two components that can fail on its own. When it does, it
leaves a symptom the agent can see: a stale synchronisation timestamp, duplicate
rows, contradictory rows, or nothing at all.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time

from agent.evidence import BatchSummary, RecordEvidence, RecordSymptom, ShipmentRecord

from .faults import WmsFaults

#: A copy more than this far behind is flagged as lagging.
LAG_THRESHOLD_SECONDS = 2 * 60 * 60


class WmsClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        faults: WmsFaults | None = None,
        *,
        now: date | None = None,
    ) -> None:
        self._conn = conn
        self._faults = faults or WmsFaults()
        self._now = now

    def shipments_to(self, customer_id: str, sku_id: str, *, asked_on: date) -> RecordEvidence:
        """What this customer was sent. Returns evidence, never rows."""
        if self._faults.timeout:
            return RecordEvidence(
                available=False,
                symptoms={RecordSymptom.TIMEOUT},
                warnings=["warehouse system did not respond"],
            )

        rows = self._conn.execute(
            """
            SELECT s.shipment_id, s.dispatched, l.batch_id, l.quantity
            FROM shipments s
            JOIN shipment_lines l ON l.shipment_id = s.shipment_id
            WHERE s.customer_id = ? AND s.sku_id = ?
            ORDER BY s.dispatched, s.shipment_id
            """,
            (customer_id, sku_id),
        ).fetchall()

        records = [
            ShipmentRecord(
                shipment_id=r["shipment_id"],
                dispatched=date.fromisoformat(r["dispatched"]),
                batch_id=r["batch_id"],
                quantity=r["quantity"],
            )
            for r in rows
        ]

        symptoms: set[RecordSymptom] = set()
        warnings: list[str] = []

        as_of = datetime.combine(asked_on, time(6, 0))
        lag = 0

        stale = self._faults.stale_as_of
        if stale is not None:
            hidden = [r for r in records if r.dispatched > stale]
            records = [r for r in records if r.dispatched <= stale]
            as_of = datetime.combine(stale, time(23, 0))
            lag = int((datetime.combine(asked_on, time(6, 0)) - as_of).total_seconds())
            warnings.append(
                f"read from a replica last synchronised {stale.isoformat()}; "
                f"anything dispatched after that date is not visible here"
            )
            if hidden:
                # The service does not know what it is missing, only that it is behind.
                warnings.append("replica may be missing recent shipments")

        if lag > LAG_THRESHOLD_SECONDS:
            symptoms.add(RecordSymptom.REPLICA_LAG)

        if self._faults.conflicting_rows and records:
            first = records[0]
            other = self._other_batch(first.batch_id, sku_id)
            if other is not None:
                records.append(
                    ShipmentRecord(
                        shipment_id=first.shipment_id,
                        dispatched=first.dispatched,
                        batch_id=other,
                        quantity=first.quantity,
                    )
                )
                symptoms.add(RecordSymptom.DUPLICATE_ROWS)
                symptoms.add(RecordSymptom.CONFLICTING_ROWS)
                warnings.append(
                    f"shipment {first.shipment_id} appears twice naming different batches"
                )
        elif self._faults.duplicate_rows and records:
            records.append(records[0].model_copy())
            symptoms.add(RecordSymptom.DUPLICATE_ROWS)
            warnings.append("duplicate rows returned")

        distinct = {r.batch_id for r in records}
        if not records:
            symptoms.add(RecordSymptom.NO_MATCH)
        elif len(distinct) == 1:
            symptoms.add(RecordSymptom.SINGLE_MATCH)
        else:
            symptoms.add(RecordSymptom.MULTIPLE_MATCHES)

        return RecordEvidence(
            available=True,
            shipments=records,
            as_of=as_of,
            lag_seconds=lag,
            symptoms=symptoms,
            warnings=warnings,
        )

    def batch_summaries(self, batch_ids: list[str]) -> dict[str, BatchSummary]:
        """Expiry and home bin for batches we hold.

        Free: the warehouse system already knows this about its own stock. It is
        the manufacturing and allocation history that costs money, and that lives
        in the batch registry.
        """
        if not batch_ids or self._faults.timeout:
            return {}
        placeholders = ",".join("?" * len(batch_ids))
        rows = self._conn.execute(
            f"SELECT batch_id, best_before, home_bin, quantity_on_hand "
            f"FROM batches WHERE batch_id IN ({placeholders})",
            batch_ids,
        ).fetchall()
        return {
            r["batch_id"]: BatchSummary(
                batch_id=r["batch_id"],
                best_before=date.fromisoformat(r["best_before"]),
                home_bin=r["home_bin"],
                quantity_on_hand=r["quantity_on_hand"],
            )
            for r in rows
        }

    def catalogue(self, sku_id: str) -> dict[str, BatchSummary]:
        """Every batch of this product the warehouse knows about.

        Free, same as batch_summaries. The agent needs it to work out which
        batches a partial code could belong to, otherwise a batch missing from a
        stale shipment record could never become a candidate at all.
        """
        if self._faults.timeout:
            return {}
        rows = self._conn.execute(
            "SELECT batch_id, best_before, home_bin, quantity_on_hand "
            "FROM batches WHERE sku_id = ? ORDER BY batch_id",
            (sku_id,),
        ).fetchall()
        return {
            r["batch_id"]: BatchSummary(
                batch_id=r["batch_id"],
                best_before=date.fromisoformat(r["best_before"]),
                home_bin=r["home_bin"],
                quantity_on_hand=r["quantity_on_hand"],
            )
            for r in rows
        }

    def _other_batch(self, batch_id: str, sku_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT batch_id FROM batches WHERE sku_id = ? AND batch_id != ? ORDER BY batch_id",
            (sku_id, batch_id),
        ).fetchone()
        return str(row["batch_id"]) if row else None
