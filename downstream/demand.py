"""Orders arriving over time.

Nothing here knows about returns, batches or the agent. It is the ordinary
business the warehouse would be doing anyway, and it is what turns a wrong batch
record into a real consequence: stock only hurts you once somebody tries to ship
it.

Demand is seeded, so a policy comparison can put every policy through exactly the
same orders and attribute the difference to the decisions rather than the luck.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Order:
    on: date
    sku_id: str
    quantity: int


#: Orders per weekday, and the spread around it. A warehouse does not ship a flat
#: number every day, and a flat number would hide stock-outs that only happen on
#: a busy day. The simulation passes its own figure; this is only a default, and
#: it is kept equal to `simulate.Config.mean_daily_units` so the two cannot tell
#: different stories.
MEAN_DAILY_UNITS = 250
SPREAD = 0.35

#: Saturday and Sunday. Nothing ships.
WEEKEND = (5, 6)


def generate(
    seed: int,
    sku_id: str,
    start: date,
    days: int,
    mean_daily_units: int = MEAN_DAILY_UNITS,
) -> list[Order]:
    """One order per working day, sized around the mean."""
    rng = random.Random(seed)
    orders: list[Order] = []
    for offset in range(days):
        on = start + timedelta(days=offset)
        if on.weekday() in WEEKEND:
            continue
        size = int(rng.gauss(mean_daily_units, mean_daily_units * SPREAD))
        if size <= 0:
            continue
        orders.append(Order(on=on, sku_id=sku_id, quantity=size))
    return orders
