"""Loads test cases and assembles the services each one needs.

A case is a YAML entry naming a return, a label damage profile, and which faults
to switch on. This is the single place where a case becomes a running set of
services, so tests and the demo always build them the same way.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agent.evidence import ReturnIntake
from world.generators import build_world
from world.labels import PROFILES, render
from world.types import World

from . import db
from .batch_registry import BatchRegistry
from .faults import LookupFaults, ServiceFaults, WmsFaults
from .label_reader import CassetteLabelReader, LabelReader, UnavailableLabelReader
from .shipment_ledger import ShipmentLedger
from .wms_client import WmsClient

SCENARIOS_PATH = Path("config/scenarios/scenarios.yaml")


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    description: str
    return_id: str
    label_damage: str
    label_reader: str
    faults: ServiceFaults
    expected_action: str


@dataclass
class Bench:
    """Everything one case needs, wired together."""

    scenario: Scenario
    world: World
    conn: sqlite3.Connection
    intake: ReturnIntake
    image_path: Path
    label_reader: LabelReader
    wms: WmsClient
    registry: BatchRegistry
    ledger: ShipmentLedger


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _faults(raw: dict[str, Any]) -> ServiceFaults:
    wms = raw.get("wms") or {}
    registry = raw.get("registry") or {}
    ledger = raw.get("ledger") or {}
    return ServiceFaults(
        wms=WmsFaults(
            timeout=bool(wms.get("timeout", False)),
            stale_as_of=_as_date(wms.get("stale_as_of")),
            duplicate_rows=bool(wms.get("duplicate_rows", False)),
            conflicting_rows=bool(wms.get("conflicting_rows", False)),
        ),
        registry=LookupFaults(
            unavailable=bool(registry.get("unavailable", False)),
            partial=bool(registry.get("partial", False)),
        ),
        ledger=LookupFaults(
            unavailable=bool(ledger.get("unavailable", False)),
            partial=bool(ledger.get("partial", False)),
        ),
    )


def load_scenarios(path: Path | str = SCENARIOS_PATH) -> dict[str, Scenario]:
    raw = yaml.safe_load(Path(path).read_text())
    out: dict[str, Scenario] = {}
    for sid, entry in raw.items():
        out[sid] = Scenario(
            scenario_id=sid,
            name=entry["name"],
            description=entry.get("description", "").strip(),
            return_id=entry["return_id"],
            label_damage=entry["label_damage"],
            label_reader=entry.get("label_reader", "cassette"),
            faults=_faults(entry),
            expected_action=entry["expected_action"],
        )
    return out


def build_bench(
    scenario: Scenario,
    *,
    world: World | None = None,
    label_dir: Path | str = "artifacts/labels",
) -> Bench:
    """Wire up the services for one case."""
    world = world or build_world()
    conn = db.build(world)

    ret = next(r for r in world.returns if r.return_id == scenario.return_id)
    customer = world.customer(ret.customer_id)

    image_path = render(ret, PROFILES[scenario.label_damage], out_dir=label_dir)

    intake = ReturnIntake(
        return_id=ret.return_id,
        customer_id=ret.customer_id,
        sku_id=ret.sku_id,
        quantity=ret.quantity,
        arrived=ret.arrived,
        condition_note=ret.condition_note,
        image_path=str(image_path),
        consignee_repacks=customer.repacks,
    )

    reader: LabelReader = (
        UnavailableLabelReader()
        if scenario.label_reader == "unavailable"
        else CassetteLabelReader()
    )

    return Bench(
        scenario=scenario,
        world=world,
        conn=conn,
        intake=intake,
        image_path=image_path,
        label_reader=reader,
        wms=WmsClient(conn, scenario.faults.wms),
        registry=BatchRegistry(conn, scenario.faults.registry),
        ledger=ShipmentLedger(conn, scenario.faults.ledger),
    )
