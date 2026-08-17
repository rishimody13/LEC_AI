"""What the agent is allowed to see.

Every service hands back one of these. Nothing here exposes the true batch, and
nothing here imports the world package. Without that split the harm numbers later
would be meaningless.

Each piece of evidence carries its own symptoms: the warning signs visible when it
was produced. The agent uses those to decide how much to trust it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LabelSymptom(StrEnum):
    """Warning signs from reading the carton label."""

    CLEAN_IMAGE = "clean_image"
    GLARE = "glare"
    BLUR = "blur"
    TORN = "torn"
    OCCLUDED = "occluded"
    LOW_CONFIDENCE = "low_confidence"
    CHECK_DIGIT_FAILED = "check_digit_failed"
    INCOMPLETE_CODE = "incomplete_code"
    NO_CODE_FOUND = "no_code_found"
    DATE_UNREADABLE = "date_unreadable"
    READER_UNAVAILABLE = "reader_unavailable"
    #: The consignee runs a repacking line, so a legible label may still describe
    #: the wrong contents. Visible from the paperwork, not from the image.
    REPACKING_CONSIGNEE = "repacking_consignee"


class RecordSymptom(StrEnum):
    """Warning signs from querying the warehouse system."""

    SINGLE_MATCH = "single_match"
    MULTIPLE_MATCHES = "multiple_matches"
    NO_MATCH = "no_match"
    REPLICA_LAG = "replica_lag"
    DUPLICATE_ROWS = "duplicate_rows"
    CONFLICTING_ROWS = "conflicting_rows"
    TIMEOUT = "timeout"


class LookupSymptom(StrEnum):
    """Warning signs from a paid external lookup."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class ReturnIntake(BaseModel):
    """The paperwork that arrives with the return. Given, not queried."""

    return_id: str
    customer_id: str
    sku_id: str
    #: Must be positive. A negative quantity flips the sign of every harm term,
    #: so the cheapest action becomes the most damaging one and the agent
    #: confidently picks it. Rejected here rather than at the ledger, because by
    #: then the decision has already been made.
    quantity: int = Field(gt=0)
    arrived: date
    condition_note: str = ""
    image_path: str | None = None
    #: Whether this consignee is known to repack goods.
    consignee_repacks: bool = False


class LabelEvidence(BaseModel):
    """What the reader could make out on the carton."""

    available: bool = True

    #: Characters actually legible. ``None`` if the code could not be located at
    #: all. May be shorter than a full code when part of the label is missing.
    code_text: str | None = None
    #: Confidence in the characters that were read, 0 to 1.
    confidence: float = 0.0
    #: True if ``code_text`` has the full shape of a batch code.
    well_formed: bool = False
    #: True/False if the check digit could be tested, ``None`` if the code is
    #: incomplete so there is nothing to test.
    check_digit_ok: bool | None = None
    best_before: date | None = None

    symptoms: set[LabelSymptom] = Field(default_factory=set)
    reader_note: str = ""

    @property
    def is_usable(self) -> bool:
        """True if the reader produced any character of the batch code."""
        return self.available and bool(self.code_text)


class ShipmentRecord(BaseModel):
    shipment_id: str
    dispatched: date
    batch_id: str
    quantity: int


class RecordEvidence(BaseModel):
    """What the warehouse system says it sent this customer."""

    available: bool = True
    shipments: list[ShipmentRecord] = Field(default_factory=list)

    #: When the copy we read was last synchronised. If this is well behind the
    #: return's arrival, recent shipments will simply be missing.
    as_of: datetime | None = None
    lag_seconds: int = 0

    symptoms: set[RecordSymptom] = Field(default_factory=set)
    warnings: list[str] = Field(default_factory=list)

    @property
    def batch_ids(self) -> list[str]:
        seen: list[str] = []
        for s in self.shipments:
            if s.batch_id not in seen:
                seen.append(s.batch_id)
        return seen


class BatchSummary(BaseModel):
    """Stock facts the warehouse system already holds. Free, part of the same query."""

    batch_id: str
    best_before: date
    home_bin: str
    quantity_on_hand: int


class BatchRecord(BaseModel):
    batch_id: str
    manufactured: date
    qa_released: date
    best_before: date
    #: Every customer this batch has ever been allocated to.
    allocated_to: list[str] = Field(default_factory=list)


class RegistryEvidence(BaseModel):
    """Manufacturing and quality-release facts. A paid lookup."""

    available: bool = True
    batches: list[BatchRecord] = Field(default_factory=list)
    symptoms: set[LookupSymptom] = Field(default_factory=set)
    cost_gbp: float = 0.0

    def record(self, batch_id: str) -> BatchRecord | None:
        return next((b for b in self.batches if b.batch_id == batch_id), None)


class DispatchScan(BaseModel):
    shipment_id: str
    batch_id: str
    quantity: int
    scanned_at: datetime


class LedgerEvidence(BaseModel):
    """What was physically scanned out of the door. A paid lookup."""

    available: bool = True
    scans: list[DispatchScan] = Field(default_factory=list)
    symptoms: set[LookupSymptom] = Field(default_factory=set)
    cost_gbp: float = 0.0
