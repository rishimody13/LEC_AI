"""Wires the real services to what the agent expects.

Keeps the agent free of any knowledge about SQLite, image paths or fault
profiles. It only ever sees evidence.
"""

from __future__ import annotations

from agent.evidence import (
    BatchSummary,
    LabelEvidence,
    LedgerEvidence,
    RecordEvidence,
    RegistryEvidence,
    ReturnIntake,
)

from .scenarios import Bench


class BenchServices:
    """The agent's view of one test case."""

    def __init__(self, bench: Bench) -> None:
        self._bench = bench

    def read_label(self, intake: ReturnIntake) -> LabelEvidence:
        return self._bench.label_reader.read(self._bench.image_path, intake)

    def query_records(self, intake: ReturnIntake) -> RecordEvidence:
        return self._bench.wms.shipments_to(
            intake.customer_id, intake.sku_id, asked_on=intake.arrived
        )

    def batch_catalogue(self, sku_id: str) -> dict[str, BatchSummary]:
        return self._bench.wms.catalogue(sku_id)

    def buy_registry(self, batch_ids: list[str]) -> RegistryEvidence:
        return self._bench.registry.lookup(batch_ids)

    def buy_ledger(self, intake: ReturnIntake) -> LedgerEvidence:
        return self._bench.ledger.scans_for(intake.customer_id, intake.sku_id)
