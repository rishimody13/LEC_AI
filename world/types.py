"""Ground truth entities.

Everything in this module describes what is *actually* true in the warehouse.
The agent package must never import it — see tests/test_isolation.py.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class BinStatus(StrEnum):
    ACTIVE = "active"
    HOLD = "hold"
    QUARANTINE = "quarantine"


class Sku(BaseModel):
    sku_id: str
    name: str
    unit_cost_gbp: float


class Batch(BaseModel):
    batch_id: str
    sku_id: str
    manufactured: date
    qa_released: date
    best_before: date
    home_bin: str
    quantity_on_hand: int


class Bin(BaseModel):
    bin_id: str
    zone: str
    capacity: int
    status: BinStatus = BinStatus.ACTIVE


class ShipmentLine(BaseModel):
    batch_id: str
    quantity: int


class Shipment(BaseModel):
    shipment_id: str
    customer_id: str
    sku_id: str
    dispatched: date
    lines: list[ShipmentLine]

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)


class Customer(BaseModel):
    customer_id: str
    name: str
    #: True if this customer repacks goods, which is what causes labels to be
    #: physically correct but attached to the wrong contents.
    repacks: bool = False


class ReturnEvent(BaseModel):
    """A partial return arriving at goods-in.

    ``true_batch_id`` is the answer. It is never exposed to the agent; only the
    scoring code and the simulator may read it.
    """

    return_id: str
    customer_id: str
    sku_id: str
    quantity: int
    arrived: date

    true_batch_id: str

    #: What is physically printed on the carton. May belong to a different batch
    #: entirely if the customer reused an outer box.
    printed_code: str
    printed_best_before: date
    condition_note: str = ""

    #: Filled in once the label image has been rendered.
    image_path: str | None = None


class World(BaseModel):
    """One complete, self-consistent warehouse state."""

    today: date
    skus: list[Sku]
    customers: list[Customer]
    bins: list[Bin]
    batches: list[Batch]
    shipments: list[Shipment]
    returns: list[ReturnEvent] = Field(default_factory=list)

    def batch(self, batch_id: str) -> Batch:
        for b in self.batches:
            if b.batch_id == batch_id:
                return b
        raise KeyError(batch_id)

    def customer(self, customer_id: str) -> Customer:
        for c in self.customers:
            if c.customer_id == customer_id:
                return c
        raise KeyError(customer_id)

    def shipments_to(self, customer_id: str, sku_id: str) -> list[Shipment]:
        return [s for s in self.shipments if s.customer_id == customer_id and s.sku_id == sku_id]
