"""The warehouse database.

This is the store the services read from. It is built from the world, but the
services only ever hand back evidence objects, never rows, so the agent cannot
reach through to ground truth.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time
from pathlib import Path

from world.types import World

SCHEMA = """
CREATE TABLE skus (
    sku_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    unit_cost_gbp REAL NOT NULL
);
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    repacks     INTEGER NOT NULL
);
CREATE TABLE bins (
    bin_id   TEXT PRIMARY KEY,
    zone     TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    status   TEXT NOT NULL
);
CREATE TABLE batches (
    batch_id         TEXT PRIMARY KEY,
    sku_id           TEXT NOT NULL,
    manufactured     TEXT NOT NULL,
    qa_released      TEXT NOT NULL,
    best_before      TEXT NOT NULL,
    home_bin         TEXT NOT NULL,
    quantity_on_hand INTEGER NOT NULL
);
CREATE TABLE shipments (
    shipment_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    dispatched  TEXT NOT NULL
);
CREATE TABLE shipment_lines (
    shipment_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    quantity    INTEGER NOT NULL
);
-- What was physically scanned at the door. The shipment ledger reads this.
CREATE TABLE dispatch_scans (
    shipment_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    scanned_at  TEXT NOT NULL
);
-- Which customers a batch has ever been allocated to. The batch registry reads this.
CREATE TABLE batch_allocations (
    batch_id    TEXT NOT NULL,
    customer_id TEXT NOT NULL
);
CREATE TABLE returns (
    return_id      TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL,
    sku_id         TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    arrived        TEXT NOT NULL,
    condition_note TEXT NOT NULL,
    image_path     TEXT
);
CREATE INDEX idx_shipments_customer ON shipments(customer_id, sku_id);
CREATE INDEX idx_lines_shipment ON shipment_lines(shipment_id);
"""


def build(world: World, path: Path | str | None = None) -> sqlite3.Connection:
    """Create the database and load the world into it.

    Pass ``None`` for an in-memory database, which is what the tests use.
    """
    conn = sqlite3.connect(":memory:" if path is None else str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    conn.executemany(
        "INSERT INTO skus VALUES (?,?,?)",
        [(s.sku_id, s.name, s.unit_cost_gbp) for s in world.skus],
    )
    conn.executemany(
        "INSERT INTO customers VALUES (?,?,?)",
        [(c.customer_id, c.name, int(c.repacks)) for c in world.customers],
    )
    conn.executemany(
        "INSERT INTO bins VALUES (?,?,?,?)",
        [(b.bin_id, b.zone, b.capacity, str(b.status)) for b in world.bins],
    )
    conn.executemany(
        "INSERT INTO batches VALUES (?,?,?,?,?,?,?)",
        [
            (
                b.batch_id,
                b.sku_id,
                b.manufactured.isoformat(),
                b.qa_released.isoformat(),
                b.best_before.isoformat(),
                b.home_bin,
                b.quantity_on_hand,
            )
            for b in world.batches
        ],
    )
    conn.executemany(
        "INSERT INTO shipments VALUES (?,?,?,?)",
        [
            (s.shipment_id, s.customer_id, s.sku_id, s.dispatched.isoformat())
            for s in world.shipments
        ],
    )
    conn.executemany(
        "INSERT INTO shipment_lines VALUES (?,?,?)",
        [(s.shipment_id, ln.batch_id, ln.quantity) for s in world.shipments for ln in s.lines],
    )

    # Goods are scanned out mid-morning on the dispatch day.
    conn.executemany(
        "INSERT INTO dispatch_scans VALUES (?,?,?,?)",
        [
            (
                s.shipment_id,
                ln.batch_id,
                ln.quantity,
                datetime.combine(s.dispatched, time(10, 20)).isoformat(),
            )
            for s in world.shipments
            for ln in s.lines
        ],
    )

    allocations = sorted({(ln.batch_id, s.customer_id) for s in world.shipments for ln in s.lines})
    conn.executemany("INSERT INTO batch_allocations VALUES (?,?)", allocations)

    conn.executemany(
        "INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
        [
            (
                r.return_id,
                r.customer_id,
                r.sku_id,
                r.quantity,
                r.arrived.isoformat(),
                r.condition_note,
                r.image_path,
            )
            for r in world.returns
        ],
    )
    conn.commit()
    return conn
