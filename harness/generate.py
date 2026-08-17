"""Builds random cases instead of hand-written ones.

Every bug found in this project so far hid behind the same thing: eight cases
written by hand, and code that happened to pass them. A case was only added once
the code could already handle it, so anything the code got wrong was invisible.

This module builds a fresh warehouse and a fresh return from a seed, picks the
faults at random, and hands the result to the agent. Nothing here knows what the
agent is supposed to answer.

What is real and what is synthetic:

- The warehouse, the database, the shipment records, the batch registry and the
  shipment ledger are all real. The generated world is loaded into the same
  SQLite schema and read through the same service classes with the same fault
  switches as the hand-written cases.
- The label reading and the note extraction are synthetic. Rendering and reading
  thousands of images is not practical, so instead a reading is constructed and
  put through the real validation code in `services.label_reader.to_evidence`.
  Perception itself is covered by the image tests and the recorded readings.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from agent.evidence import (
    BatchSummary,
    LabelEvidence,
    LedgerEvidence,
    RecordEvidence,
    RegistryEvidence,
    ReturnIntake,
)
from agent.notes import NoteFacts
from common.coding import label_code
from ledger.drift import TruthBook
from services import db
from services.batch_registry import BatchRegistry
from services.faults import LookupFaults, WmsFaults
from services.label_reader import LabelReading, to_evidence
from services.shipment_ledger import ShipmentLedger
from services.wms_client import WmsClient
from world.types import (
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

SKU_ID = "SKU-9000"
TODAY = date(2026, 8, 15)


class LabelMode(StrEnum):
    """How the carton label relates to what is in the box."""

    CORRECT = "correct"
    #: A reused outer box carrying another real batch's label.
    WRONG_BATCH = "wrong_batch"
    #: Ink damage turned one digit into a lookalike, so the code fails its check digit.
    CORRUPTED = "corrupted"
    #: A tear or glare removed the end of the code.
    PARTIAL = "partial"
    #: Nothing legible at all.
    DESTROYED = "destroyed"
    #: The reader itself is down.
    UNAVAILABLE = "unavailable"


class RecordMode(StrEnum):
    HEALTHY = "healthy"
    #: A replica that has not caught up, hiding the most recent shipment.
    STALE = "stale"
    CONFLICTING = "conflicting"
    TIMEOUT = "timeout"


class TruthMode(StrEnum):
    #: The stock came from a shipment we have on record.
    ON_RECORD = "on_record"
    #: Cross-docked or transferred, so no shipment line matches.
    OFF_RECORD = "off_record"


@dataclass
class Case:
    """One generated case, plus the answer nobody tells the agent."""

    seed: int
    world: World
    intake: ReturnIntake
    truth: str
    label: LabelEvidence
    note: NoteFacts
    wms: WmsClient
    registry: BatchRegistry
    ledger: ShipmentLedger
    catalogue: dict[str, BatchSummary]

    calibrated: bool = True
    label_mode: LabelMode = LabelMode.CORRECT
    record_mode: RecordMode = RecordMode.HEALTHY
    truth_mode: TruthMode = TruthMode.ON_RECORD
    registry_down: bool = False
    ledger_down: bool = False

    @property
    def description(self) -> str:
        bits = [
            f"seed={self.seed}",
            f"label={self.label_mode}",
            f"records={self.record_mode}",
            f"truth={self.truth_mode}",
            f"qty={self.intake.quantity}",
        ]
        if self.registry_down:
            bits.append("registry_down")
        if self.ledger_down:
            bits.append("ledger_down")
        return " ".join(bits)

    def best_before(self, batch_id: str) -> date:
        return next(b.best_before for b in self.world.batches if b.batch_id == batch_id)

    def truth_book(self) -> TruthBook:
        """The answers, in the form the drift measurement wants them."""
        return TruthBook(
            true_batch={self.intake.return_id: self.truth},
            best_before={b.batch_id: b.best_before for b in self.world.batches},
            home_bin={b.batch_id: b.home_bin for b in self.world.batches},
        )


@dataclass
class GeneratedServices:
    """What the agent sees. Real services, synthetic perception."""

    case: Case

    def read_label(self, intake: ReturnIntake) -> LabelEvidence:
        return self.case.label

    def query_records(self, intake: ReturnIntake) -> RecordEvidence:
        return self.case.wms.shipments_to(
            intake.customer_id, intake.sku_id, asked_on=intake.arrived
        )

    def batch_catalogue(self, sku_id: str) -> dict[str, BatchSummary]:
        return self.case.wms.catalogue(sku_id)

    def buy_registry(self, batch_ids: list[str]) -> RegistryEvidence:
        return self.case.registry.lookup(batch_ids)

    def buy_ledger(self, intake: ReturnIntake) -> LedgerEvidence:
        return self.case.ledger.scans_for(intake.customer_id, intake.sku_id)


class FixedNoteReader:
    """Serves the note facts this case was generated with."""

    def __init__(self, facts: NoteFacts) -> None:
        self._facts = facts

    def read(self, note: str) -> NoteFacts:
        return self._facts


# --------------------------------------------------------------------------
# Building a world
# --------------------------------------------------------------------------


def _batches(rng: random.Random, how_many: int) -> list[Batch]:
    """Batches with plausible, ordered dates and a spread of expiries."""
    out: list[Batch] = []
    used: set[int] = set()
    for i in range(how_many):
        while True:
            body = rng.randrange(1000, 9999)
            if body not in used:
                used.add(body)
                break
        made = TODAY - timedelta(days=rng.randrange(200, 700))
        released = made + timedelta(days=rng.randrange(3, 40))
        # Expiries deliberately range from nearly gone to a long way out, because
        # how far apart two candidates' expiries are decides how much being wrong
        # about them costs.
        best_before = TODAY + timedelta(days=rng.randrange(-10, 500))
        out.append(
            Batch(
                batch_id=f"B-{body}",
                sku_id=SKU_ID,
                manufactured=made,
                qa_released=released,
                best_before=best_before,
                home_bin=f"A-{i:02d}-01",
                quantity_on_hand=rng.randrange(50, 3000),
            )
        )
    return out


def _world(rng: random.Random, batches: list[Batch], customer: Customer) -> World:
    bins = [Bin(bin_id=b.home_bin, zone=b.home_bin[:4], capacity=3000) for b in batches]
    bins.append(Bin(bin_id="H-01-01", zone="H-01", capacity=500, status=BinStatus.HOLD))
    return World(
        today=TODAY,
        skus=[Sku(sku_id=SKU_ID, name="Generated product", unit_cost_gbp=11.40)],
        customers=[customer],
        bins=bins,
        batches=batches,
        shipments=[],
    )


def _shipments(
    rng: random.Random, batches: list[Batch], customer: Customer, how_many: int
) -> list[Shipment]:
    """Shipments that could really have happened: never before quality release."""
    out: list[Shipment] = []
    for i, batch in enumerate(rng.sample(batches, how_many)):
        earliest = max(batch.qa_released, TODAY - timedelta(days=180))
        if earliest >= TODAY:
            continue
        span = (TODAY - earliest).days
        dispatched = earliest + timedelta(days=rng.randrange(0, max(span, 1)))
        out.append(
            Shipment(
                shipment_id=f"SH-{9000 + i}",
                customer_id=customer.customer_id,
                sku_id=SKU_ID,
                dispatched=dispatched,
                lines=[ShipmentLine(batch_id=batch.batch_id, quantity=rng.randrange(60, 600))],
            )
        )
    return out


# --------------------------------------------------------------------------
# Building the evidence
# --------------------------------------------------------------------------


def _corrupt(code: str, rng: random.Random) -> str:
    """Swap one digit for a lookalike, the way ink damage does."""
    from agent.confusion import CONFUSABLE_GROUPS

    positions = [i for i, ch in enumerate(code) if ch.isdigit()]
    rng.shuffle(positions)
    for i in positions:
        swaps = [
            c for g in CONFUSABLE_GROUPS if code[i] in g for c in g if c != code[i] and c.isdigit()
        ]
        if swaps:
            return code[:i] + rng.choice(swaps) + code[i + 1 :]
    return code


def _label(
    mode: LabelMode,
    truth: Batch,
    others: list[Batch],
    intake: ReturnIntake,
    rng: random.Random,
) -> LabelEvidence:
    true_code = label_code(truth.batch_id)

    if mode is LabelMode.UNAVAILABLE:
        from agent.evidence import LabelSymptom

        return LabelEvidence(
            available=False,
            symptoms={LabelSymptom.READER_UNAVAILABLE},
            reader_note="reader did not respond",
        )

    if mode is LabelMode.DESTROYED:
        reading = LabelReading(
            code_text=None,
            code_complete=False,
            confidence=0.0,
            best_before=None,
            visual_condition=["water_damage", "blur"],
            note="the code area is washed out",
        )
    elif mode is LabelMode.PARTIAL:
        keep = rng.randrange(3, len(true_code) - 1)
        reading = LabelReading(
            code_text=true_code[:keep],
            code_complete=False,
            confidence=round(rng.uniform(0.55, 0.95), 2),
            best_before=None,
            visual_condition=["torn"],
            note="part of the code is missing",
        )
    elif mode is LabelMode.CORRUPTED:
        reading = LabelReading(
            code_text=_corrupt(true_code, rng),
            code_complete=True,
            confidence=round(rng.uniform(0.45, 0.7), 2),
            best_before=truth.best_before,
            visual_condition=["smudge"],
            note="a blot sits over one digit",
        )
    elif mode is LabelMode.WRONG_BATCH and others:
        impostor = rng.choice(others)
        reading = LabelReading(
            code_text=label_code(impostor.batch_id),
            code_complete=True,
            confidence=round(rng.uniform(0.9, 0.99), 2),
            best_before=impostor.best_before,
            visual_condition=["clean"],
            note="label is clean and crisp",
        )
    else:
        reading = LabelReading(
            code_text=true_code,
            code_complete=True,
            confidence=round(rng.uniform(0.9, 0.99), 2),
            best_before=truth.best_before,
            visual_condition=["clean"],
            note="label is clean and crisp",
        )

    return to_evidence(reading, intake)


def _note(
    rng: random.Random, truth: Batch, truth_mode: TruthMode, label_mode: LabelMode
) -> NoteFacts:
    facts = NoteFacts(summary="generated note")
    if truth_mode is TruthMode.OFF_RECORD:
        facts.off_site_origin = True
        # Cross-docked stock often arrives with the lot written on the paperwork.
        if rng.random() < 0.6:
            facts.batch_codes_mentioned = [truth.batch_id]
    if label_mode is LabelMode.WRONG_BATCH and rng.random() < 0.5:
        facts.repacked = True
    if rng.random() < 0.25:
        facts.print_dates = [truth.manufactured]
    if rng.random() < 0.1:
        facts.mixed_pallet = True
    return facts


def _label_mode(rng: random.Random, repacks: bool, calibrated: bool) -> LabelMode:
    """Pick how the label went wrong.

    Two modes, and the difference matters when reading the results.

    `calibrated` draws at rates that match what config/reliability.yaml says the
    world looks like: a clean label from an ordinary customer is almost always
    right, and a repacking customer's is wrong far more often. A failure here is
    a reasoning failure, because the agent's beliefs match the world it is in.

    `calibrated=False` draws every mode equally. A failure there is a different
    thing: it means the agent's beliefs do not match the world, which is worth
    knowing but is not the same bug.
    """
    if not calibrated:
        return rng.choice(list(LabelMode))

    wrong_label = 0.17 if repacks else 0.01
    roll = rng.random()
    if roll < wrong_label:
        return LabelMode.WRONG_BATCH
    if roll < wrong_label + 0.10:
        return LabelMode.CORRUPTED
    if roll < wrong_label + 0.22:
        return LabelMode.PARTIAL
    if roll < wrong_label + 0.30:
        return LabelMode.DESTROYED
    if roll < wrong_label + 0.33:
        return LabelMode.UNAVAILABLE
    return LabelMode.CORRECT


def _record_mode(rng: random.Random, calibrated: bool) -> RecordMode:
    if not calibrated:
        return rng.choice(list(RecordMode))
    roll = rng.random()
    if roll < 0.10:
        return RecordMode.STALE
    if roll < 0.14:
        return RecordMode.CONFLICTING
    if roll < 0.18:
        return RecordMode.TIMEOUT
    return RecordMode.HEALTHY


def build(seed: int, calibrated: bool = True) -> Case:
    """One complete case: a world, a return, evidence, and the answer."""
    rng = random.Random(seed)

    batches = _batches(rng, rng.randrange(2, 7))
    customer = Customer(
        customer_id="CUST-900",
        name="Generated Customer",
        repacks=rng.random() < 0.35,
    )
    label_mode = _label_mode(rng, customer.repacks, calibrated)
    record_mode = _record_mode(rng, calibrated)
    truth_mode = (
        TruthMode.OFF_RECORD
        if rng.random() < (0.15 if not calibrated else 0.05)
        else TruthMode.ON_RECORD
    )
    registry_down = rng.random() < (0.15 if not calibrated else 0.03)
    ledger_down = rng.random() < (0.15 if not calibrated else 0.03)
    world = _world(rng, batches, customer)
    shipped = _shipments(rng, batches, customer, rng.randrange(1, min(4, len(batches)) + 1))
    world.shipments = shipped

    sent_ids = {ln.batch_id for s in shipped for ln in s.lines}
    on_record = [b for b in batches if b.batch_id in sent_ids]
    off_record = [b for b in batches if b not in on_record]

    if truth_mode is TruthMode.OFF_RECORD and off_record:
        truth = rng.choice(off_record)
    elif on_record:
        truth = rng.choice(on_record)
        truth_mode = TruthMode.ON_RECORD
    else:
        truth = rng.choice(batches)
        truth_mode = TruthMode.OFF_RECORD

    shipped_units = sum(ln.quantity for s in shipped for ln in s.lines) or 100
    quantity = rng.randrange(1, max(2, shipped_units))

    world.returns = [
        ReturnEvent(
            return_id=f"GEN-{seed}",
            customer_id=customer.customer_id,
            sku_id=SKU_ID,
            quantity=quantity,
            arrived=TODAY,
            true_batch_id=truth.batch_id,
            off_record=truth_mode is TruthMode.OFF_RECORD,
            printed_code=label_code(truth.batch_id),
            printed_best_before=truth.best_before,
            condition_note="generated note",
        )
    ]

    intake = ReturnIntake(
        return_id=f"GEN-{seed}",
        customer_id=customer.customer_id,
        sku_id=SKU_ID,
        quantity=quantity,
        arrived=TODAY,
        condition_note="generated note",
        consignee_repacks=customer.repacks,
    )

    conn = db.build(world)

    wms_faults = WmsFaults()
    if record_mode is RecordMode.TIMEOUT:
        wms_faults = WmsFaults(timeout=True)
    elif record_mode is RecordMode.CONFLICTING:
        wms_faults = WmsFaults(conflicting_rows=True)
    elif record_mode is RecordMode.STALE and shipped:
        newest = max(s.dispatched for s in shipped)
        wms_faults = WmsFaults(stale_as_of=newest - timedelta(days=1))

    others = [b for b in batches if b.batch_id != truth.batch_id]
    label = _label(label_mode, truth, others, intake, rng)

    wms = WmsClient(conn, wms_faults)
    return Case(
        seed=seed,
        world=world,
        intake=intake,
        truth=truth.batch_id,
        label=label,
        note=_note(rng, truth, truth_mode, label_mode),
        wms=wms,
        registry=BatchRegistry(conn, LookupFaults(unavailable=registry_down)),
        ledger=ShipmentLedger(conn, LookupFaults(unavailable=ledger_down)),
        catalogue=wms.catalogue(SKU_ID),
        calibrated=calibrated,
        label_mode=label_mode,
        record_mode=record_mode,
        truth_mode=truth_mode,
        registry_down=registry_down,
        ledger_down=ledger_down,
    )


@dataclass
class StreamCase:
    """One return arriving into a warehouse that already exists.

    `build` above makes a fresh world per case, which is what a single-decision
    sweep wants. The simulation needs the opposite: many returns arriving into
    one shared inventory over months, so that a batch filed wrongly in week two
    is still on the shelf in week nine when somebody tries to ship it.

    Everything about how the evidence goes wrong is the same code either way.
    """

    intake: ReturnIntake
    truth: str
    label: LabelEvidence
    note: NoteFacts
    wms: WmsClient
    registry: BatchRegistry
    ledger: ShipmentLedger
    catalogue: dict[str, BatchSummary]
    label_mode: LabelMode
    record_mode: RecordMode


@dataclass
class StreamServices:
    """What a policy sees for one return in the stream."""

    case: StreamCase

    def read_label(self, intake: ReturnIntake) -> LabelEvidence:
        return self.case.label

    def query_records(self, intake: ReturnIntake) -> RecordEvidence:
        return self.case.wms.shipments_to(
            intake.customer_id, intake.sku_id, asked_on=intake.arrived
        )

    def batch_catalogue(self, sku_id: str) -> dict[str, BatchSummary]:
        return self.case.catalogue

    def buy_registry(self, batch_ids: list[str]) -> RegistryEvidence:
        return self.case.registry.lookup(batch_ids)

    def buy_ledger(self, intake: ReturnIntake) -> LedgerEvidence:
        return self.case.ledger.scans_for(intake.customer_id, intake.sku_id)

    @property
    def note_reader(self) -> FixedNoteReader:
        """The note facts this return was generated with.

        The recorded cassettes only cover the eight hand-written notes, so a
        policy running against a generated stream has to take its note facts
        from here instead.
        """
        return FixedNoteReader(self.case.note)


def one_return(
    world: World,
    conn: object,
    rng: random.Random,
    return_id: str,
    arrived: date,
    calibrated: bool = True,
    batches: list[Batch] | None = None,
) -> StreamCase:
    """Generate a single return against a world that already exists.

    `batches` is the pool a return can really be drawn from, and it must be held
    fixed. A simulation appends fresh batches as replenishment arrives, and if
    those were allowed in here the pool would depend on when each policy happened
    to reorder - which changes what is drawn, and quietly gives each policy a
    different set of returns. The comparison is paired, so that would invalidate
    it. It also would not be true: a return is stock we shipped, and we shipped
    from the batches that existed at the time.
    """
    customer = world.customers[0]
    batches = batches if batches is not None else world.batches
    label_mode = _label_mode(rng, customer.repacks, calibrated)
    record_mode = _record_mode(rng, calibrated)

    sent_ids = {ln.batch_id for s in world.shipments for ln in s.lines}
    on_record = [b for b in batches if b.batch_id in sent_ids]
    off_record_batches = [b for b in batches if b.batch_id not in sent_ids]
    wants_off_record = rng.random() < (0.15 if not calibrated else 0.05)

    if wants_off_record and off_record_batches:
        truth = rng.choice(off_record_batches)
        truth_mode = TruthMode.OFF_RECORD
    elif on_record:
        truth = rng.choice(on_record)
        truth_mode = TruthMode.ON_RECORD
    else:
        truth = rng.choice(batches)
        truth_mode = TruthMode.OFF_RECORD

    quantity = rng.randrange(20, 400)
    intake = ReturnIntake(
        return_id=return_id,
        customer_id=customer.customer_id,
        sku_id=world.skus[0].sku_id,
        quantity=quantity,
        arrived=arrived,
        condition_note="generated note",
        consignee_repacks=customer.repacks,
    )

    wms_faults = WmsFaults()
    if record_mode is RecordMode.TIMEOUT:
        wms_faults = WmsFaults(timeout=True)
    elif record_mode is RecordMode.CONFLICTING:
        wms_faults = WmsFaults(conflicting_rows=True)
    elif record_mode is RecordMode.STALE and world.shipments:
        newest = max(s.dispatched for s in world.shipments)
        wms_faults = WmsFaults(stale_as_of=newest - timedelta(days=1))

    others = [b for b in batches if b.batch_id != truth.batch_id]
    wms = WmsClient(conn, wms_faults)  # type: ignore[arg-type]
    return StreamCase(
        intake=intake,
        truth=truth.batch_id,
        label=_label(label_mode, truth, others, intake, rng),
        note=_note(rng, truth, truth_mode, label_mode),
        wms=wms,
        registry=BatchRegistry(
            conn,  # type: ignore[arg-type]
            LookupFaults(unavailable=rng.random() < (0.15 if not calibrated else 0.03)),
        ),
        ledger=ShipmentLedger(
            conn,  # type: ignore[arg-type]
            LookupFaults(unavailable=rng.random() < (0.15 if not calibrated else 0.03)),
        ),
        catalogue=wms.catalogue(world.skus[0].sku_id),
        label_mode=label_mode,
        record_mode=record_mode,
    )
