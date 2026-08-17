"""Six months of a warehouse, so the harm can be measured rather than asserted.

The argument this exists to support is that getting a returned carton's batch
wrong is not a paperwork error. It is invisible on the day and expensive months
later, and the only way to show that honestly is to run the months.

Each day, in order:

1. **Deliveries land.** Replenishment ordered earlier arrives as a fresh batch.
2. **Returns arrive.** The policy under test decides what each one is, and that
   decision is written to the stock ledger. What the stock *really* is goes to
   the truth tracker, which the ledger cannot see.
3. **Orders ship.** Picking is first-expired-first-out on the *recorded* dates.
   Whatever comes off the shelf is whatever is physically there, so a return
   filed under a long-dated batch sits at the back of the queue until its turn
   comes - by which point it may really have expired. Nothing raises an alarm.
4. **Write-offs.** Stock whose recorded date has passed is scrapped, not shipped.
5. **Reordering.** If projected stock is below the reorder point, order more.

The measurement that matters is step 3: units that shipped after their *true*
best-before date. Every one of them traces back to a decision in step 2.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from agent.harm import CostModel
from agent.reliability import ReliabilityModel
from harness import generate, policies
from ledger.ledger import CUSTOMER, DISPATCH, SCRAP, SUPPLIER, Kind, Ledger, Lot, Position
from services import db as db_mod
from world.types import Batch, Bin, BinStatus, World

from . import demand, picking
from .truth import TruthTracker

RECEIVING = "R-00-01"


@dataclass
class Config:
    #: Eighteen months, not the six the plan assumed.
    #:
    #: This was the biggest open risk in the plan and it turned out to be real. At
    #: 180 days six of eight trial seeds reported *zero* expired units, and the
    #: total across them was 644 against 4,730 at 540 days. The horizon was
    #: truncating the harm, not measuring it, and a 180-day run would have
    #: concluded there was no difference between policies worth having.
    #:
    #: The reason is the mechanism itself. Stock filed under a long-dated batch
    #: goes to the back of a first-expired-first-out queue that is about eighty
    #: days deep and is constantly refreshed by deliveries with earlier dates. A
    #: decision made in month one is not picked until months six to twelve. Being
    #: slow is the whole point of the failure; the measurement has to outlast it.
    days: int = 540
    #: Returns have to be a realistic fraction of what goes out. An early version
    #: had more units coming back than shipping, which meant the shelf never
    #: drained far enough to reach anything misfiled - and reported zero harm for
    #: a reason that had nothing to do with the decisions.
    returns_per_week: float = 1.5
    mean_daily_units: int = 250
    #: Cover, in units. A product with a year-plus shelf life is not held at ten
    #: days of stock - it is held at two or three months. This matters more than
    #: it looks: how long stock sits on the shelf is exactly how long a wrong
    #: expiry has to become true. Hold too little and the whole queue drains in a
    #: fortnight, nothing ever ages, and the simulation reports no harm for a
    #: reason that has nothing to do with the decisions.
    reorder_point: int = 8_000
    order_up_to: int = 20_000
    lead_time_days: int = 7
    #: How long a return waits for a person once it has been handed over, and
    #: how long a hold waits before somebody comes back to identify it. Without
    #: these the simulation never resolves either, escalated stock sits in
    #: quarantine for eighteen months and gets charged as a total loss, and every
    #: policy that hands anything to a person looks ruinous for a reason that has
    #: nothing to do with its decisions.
    review_days: int = 3
    hold_review_days: int = 14
    #: Remaining life on a delivery, which varies. If every delivery arrived with
    #: exactly the same life there would be no expiry ordering among them and
    #: nothing could ever be picked in the wrong order.
    fresh_shelf_life_min: int = 120
    fresh_shelf_life_max: int = 480
    #: How many bins the replenishment stock cycles through.
    inbound_bin: str = "A-99-01"


@dataclass
class Metrics:
    """What six months of this policy produced."""

    policy: str
    seed: int
    #: The headline. Units that left the building after they had really expired.
    expired_units_shipped: int = 0
    #: Orders we could not fill from pickable stock.
    stockout_units: int = 0
    units_shipped: int = 0
    units_returned: int = 0
    returns_handled: int = 0
    escalations: int = 0
    lookup_spend_gbp: float = 0.0
    written_off_units: int = 0
    #: Units still held with no date, so nobody can ship them either way.
    stranded_units: int = 0
    #: Units in from outside, out to outside, and still in the building. The
    #: first two minus each other must equal the third over the whole run.
    units_in: int = 0
    units_out: int = 0
    units_on_hand: int = 0
    misfiled_returns: int = 0
    misfiled_units: int = 0

    @property
    def expired_rate(self) -> float:
        return self.expired_units_shipped / self.units_shipped if self.units_shipped else 0.0


@dataclass
class _Review:
    """A return sitting with a person, waiting to be identified."""

    due: date
    return_id: str
    position: Position
    quantity: int
    truth: str


def _human_answer(rng: random.Random, truth: str, world: World, costs: CostModel) -> str:
    """What the person concludes.

    Good but not perfect, at the rate the cost model already assumes. Making a
    human an oracle here would price escalation as a free correct answer, and the
    agent would be measured against a rival that cannot be beaten.
    """
    if rng.random() >= costs.human_error_rate:
        return truth
    others = [b.batch_id for b in world.batches if b.batch_id != truth]
    return rng.choice(others) if others else truth


def _fresh_batch(index: int, made: date, config: Config, bin_id: str, rng: random.Random) -> Batch:
    life = rng.randrange(config.fresh_shelf_life_min, config.fresh_shelf_life_max)
    return Batch(
        batch_id=f"B-9{index:03d}",
        sku_id=generate.SKU_ID,
        manufactured=made,
        qa_released=made,
        best_before=made + timedelta(days=life),
        home_bin=bin_id,
        quantity_on_hand=0,
    )


def run(
    seed: int,
    policy: policies.Policy,
    costs: CostModel,
    reliability: ReliabilityModel,
    config: Config | None = None,
    calibrated: bool = True,
    verify: bool = False,
) -> Metrics:
    """Run one policy through one seeded eighteen months.

    `verify` checks, at the end of every simulated day, that the ledger and the
    truth tracker agree on *how many* units sit at each position. They disagree
    about what those units are - that is the whole point - but a disagreement
    about the count would mean the two halves of the simulation had drifted apart
    and every figure it produces would be meaningless. It is off by default only
    because it is O(positions) a day.
    """
    config = config or Config()
    sku = generate.SKU_ID

    # Two independent random streams, and keeping them apart is essential rather
    # than tidy. `stream` produces the returns and the days they arrive. Every
    # policy must see exactly the same ones, or the comparison stops being
    # paired and the difference between policies gets buried in the difference
    # between the cases they happened to be given.
    #
    # `ops` covers everything whose timing depends on what the policy did -
    # when replenishment is triggered, what a reviewer concludes. Those draws
    # legitimately differ between policies. Sharing one stream let them shift
    # the return sequence, so a policy that escalated more quietly got different
    # returns from one that escalated less.
    stream = random.Random(seed)
    ops = random.Random(seed ^ 0x5BF03)

    world = generate.build(seed).world
    # The generated world names its bins after its batches, so only add the ones
    # it does not already have.
    have = {b.bin_id for b in world.bins}
    extra = [
        Bin(bin_id=RECEIVING, zone="R-00", capacity=100_000),
        Bin(bin_id=config.inbound_bin, zone="A-99", capacity=100_000),
        Bin(bin_id="Q-01-01", zone="Q-01", capacity=100_000, status=BinStatus.QUARANTINE),
    ]
    world.bins = list(world.bins) + [b for b in extra if b.bin_id not in have]
    conn = db_mod.build(world)

    book = Ledger()
    real = TruthTracker()
    metrics = Metrics(policy=policy.name, seed=seed)
    start = world.today

    # Fixed for the whole run: what a return could be. Replenishment batches are
    # added to the world below but must never join this pool - see one_return.
    returnable = list(world.batches)
    expiry_of: dict[str, date] = {b.batch_id: b.best_before for b in world.batches}
    home_of: dict[str, str] = {b.batch_id: b.home_bin for b in world.batches}

    # Opening stock, so the warehouse is not empty on day one.
    for batch in world.batches:
        if batch.quantity_on_hand <= 0:
            continue
        where = Position(sku, batch.home_bin, Lot(batch.batch_id, batch.best_before))
        book.append(
            at=start,
            kind=Kind.RECEIPT,
            quantity=batch.quantity_on_hand,
            source=Position(sku, SUPPLIER),
            destination=where,
            reason="opening stock",
        )
        real.add(where, batch.batch_id, batch.quantity_on_hand)

    orders = demand.generate(seed, sku, start, config.days, config.mean_daily_units)
    orders_by_day: dict[date, list[demand.Order]] = {}
    for order in orders:
        orders_by_day.setdefault(order.on, []).append(order)

    return_days = _return_days(stream, start, config)
    incoming: list[tuple[date, str, int]] = []
    pending: list[_Review] = []
    fresh_index = 0

    for offset in range(config.days):
        today = start + timedelta(days=offset)

        # 1. deliveries -------------------------------------------------------
        for _when, batch_id, units in [x for x in incoming if x[0] == today]:
            where = Position(sku, config.inbound_bin, Lot(batch_id, expiry_of[batch_id]))
            book.append(
                at=today,
                kind=Kind.RECEIPT,
                quantity=units,
                source=Position(sku, SUPPLIER),
                destination=where,
                reason="replenishment delivery",
            )
            real.add(where, batch_id, units)
        incoming = [x for x in incoming if x[0] != today]

        # 1b. people finish the reviews they were given ------------------------
        for review in [r for r in pending if r.due <= today]:
            # The stock may not be there any more. A hold is dated at the
            # earliest expiry any batch has, which is sometimes already in the
            # past, so some held stock is written off before anyone gets to it.
            # That is a real cost of holding, not an error to paper over.
            still_there = min(review.quantity, book.quantity_at(review.position))
            if still_there <= 0:
                continue
            answer = _human_answer(ops, review.truth, world, costs)
            landing = Position(
                sku,
                home_of.get(answer, config.inbound_bin),
                Lot(answer, expiry_of[answer]),
            )
            if landing == review.position:
                continue
            book.append(
                at=today,
                kind=Kind.RECLASSIFY,
                quantity=still_there,
                source=review.position,
                destination=landing,
                return_id=review.return_id,
                decision=f"{review.return_id}:review",
                reason="identified by a person",
            )
            real.move(review.position, landing, still_there)
            if answer != review.truth:
                metrics.misfiled_returns += 1
                metrics.misfiled_units += still_there
        pending = [r for r in pending if r.due > today]

        # 2. returns ----------------------------------------------------------
        for n in range(return_days.get(today, 0)):
            return_id = f"RET-{offset:03d}-{n}"
            case = generate.one_return(
                world,
                conn,
                stream,
                return_id,
                today,
                calibrated=calibrated,
                batches=returnable,
            )
            services = generate.StreamServices(case)
            if isinstance(policy, policies.Oracle):
                policy.truth[return_id] = case.truth
                policy.facts = {b.batch_id: (b.best_before, b.home_bin) for b in world.batches}

            decision = policy.decide(case.intake, services, costs, reliability)
            metrics.returns_handled += 1
            metrics.units_returned += case.intake.quantity
            metrics.lookup_spend_gbp += decision.spend_gbp

            dock = Position(sku, RECEIVING)
            book.append(
                at=today,
                kind=Kind.RECEIPT,
                quantity=case.intake.quantity,
                source=Position(sku, CUSTOMER),
                destination=dock,
                return_id=return_id,
                decision=f"{return_id}:intake",
                reason="return received",
            )
            real.add(dock, case.truth, case.intake.quantity)

            if decision.escalated:
                metrics.escalations += 1
                landing = Position(sku, "Q-01-01")
            else:
                # The fallback bin must NOT come from `home_of`, which is ground
                # truth. A policy that names a batch but cannot name a bin -
                # because the warehouse system was down and it had no catalogue -
                # would otherwise be handed the correct home bin for free, and
                # the comparison would be measuring a hint the policy never had.
                landing = Position(
                    sku,
                    decision.bin_id or config.inbound_bin,
                    Lot(decision.batch_id, decision.best_before),
                )
            kind = Kind.RECLASSIFY if landing.lot != dock.lot else Kind.PUTAWAY
            book.append(
                at=today,
                kind=kind,
                quantity=case.intake.quantity,
                source=dock,
                destination=landing,
                return_id=return_id,
                decision=f"{return_id}:placement",
                reason=f"{policy.name} decision",
            )
            real.move(dock, landing, case.intake.quantity)

            # Anything not filed under a batch is waiting for a person.
            if decision.batch_id is None:
                pending.append(
                    _Review(
                        due=today
                        + timedelta(
                            days=config.review_days
                            if decision.escalated
                            else config.hold_review_days
                        ),
                        return_id=return_id,
                        position=landing,
                        quantity=case.intake.quantity,
                        truth=case.truth,
                    )
                )
            # Only a decision that actually names a batch can be misfiled. An
            # escalation names none, and counting those here made a third of the
            # "misfiled" returns things that were never filed at all.
            if decision.batch_id is not None and decision.batch_id != case.truth:
                metrics.misfiled_returns += 1
                metrics.misfiled_units += case.intake.quantity

        # 3. orders ship ------------------------------------------------------
        for order in orders_by_day.get(today, []):
            gone = picking.plan(book, world.bins, sku, order.quantity, today)
            metrics.stockout_units += gone.shortfall
            for take in gone.takes:
                really = real.take(take.position, take.quantity)
                book.append(
                    at=today,
                    kind=Kind.PICK,
                    quantity=take.quantity,
                    source=take.position,
                    destination=Position(sku, DISPATCH, take.position.lot),
                    reason=f"order {order.quantity}",
                )
                metrics.units_shipped += take.quantity
                for batch_id, units in really.items():
                    if expiry_of.get(batch_id, today) < today:
                        metrics.expired_units_shipped += units

        # 4. write off what the record says has gone off ----------------------
        for position, units in list(book.balances().items()):
            if position.sku_id != sku or units <= 0:
                continue
            recorded = position.lot.best_before
            if recorded is not None and recorded < today and not position.bin_id.startswith("Q"):
                real.take(position, units)
                book.append(
                    at=today,
                    kind=Kind.WRITE_OFF,
                    quantity=units,
                    source=position,
                    destination=Position(sku, SCRAP, position.lot),
                    reason="past its recorded best-before",
                )
                metrics.written_off_units += units

        # 5. reorder ----------------------------------------------------------
        on_hand = sum(
            units
            for p, units in book.balances().items()
            if p.sku_id == sku and p.bin_id in picking.pickable_bins(world.bins)
        )
        on_order = sum(units for _, _, units in incoming)
        if on_hand + on_order < config.reorder_point:
            fresh_index += 1
            batch = _fresh_batch(fresh_index, today, config, config.inbound_bin, ops)
            expiry_of[batch.batch_id] = batch.best_before
            home_of[batch.batch_id] = batch.home_bin
            world.batches.append(batch)
            incoming.append(
                (
                    today + timedelta(days=config.lead_time_days),
                    batch.batch_id,
                    config.order_up_to - on_hand - on_order,
                )
            )

        if verify:
            _agree(book, real, sku, today)

    metrics.stranded_units = sum(
        units
        for p, units in book.balances().items()
        if p.sku_id == sku and p.lot.best_before is None
    )

    # Eighteen months of movements, and nothing invented or lost anywhere in
    # them. Cheap to check and the kind of thing that would otherwise go wrong
    # quietly and make every number above meaningless.
    book.check_balances()
    metrics.units_in, metrics.units_out, metrics.units_on_hand = book.flow(sku)
    return metrics


def _agree(book: Ledger, real: TruthTracker, sku_id: str, today: date) -> None:
    """The ledger and the shelf must hold the same number of units everywhere."""
    believed = {p: n for p, n in book.balances().items() if p.sku_id == sku_id and n}
    actual = {p: real.units_at(p) for p in set(believed) | set(real.positions())}
    actual = {p: n for p, n in actual.items() if p.sku_id == sku_id and n}
    if believed != actual:
        wrong = {
            str(p): (believed.get(p, 0), actual.get(p, 0))
            for p in set(believed) | set(actual)
            if believed.get(p, 0) != actual.get(p, 0)
        }
        raise AssertionError(f"on {today} the ledger and the shelf disagree: {wrong}")


def _return_days(rng: random.Random, start: date, config: Config) -> dict[date, int]:
    """How many returns arrive on each day."""
    out: dict[date, int] = {}
    per_day = config.returns_per_week / 7.0
    for offset in range(config.days):
        day = start + timedelta(days=offset)
        if day.weekday() in demand.WEEKEND:
            continue
        count = 0
        while rng.random() < per_day:
            count += 1
            if count > 4:
                break
        if count:
            out[day] = count
    return out
