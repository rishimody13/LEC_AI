"""Cost model, and how sensitive a decision is to it.

Two parts:

1. Costs. Every figure is worked out from the parameters in config/harm.yaml
   instead of being written down directly, so changing a wage or a margin
   updates everything that depends on it.

2. Linear cost expressions. Expected cost is a straight line in each cost
   parameter, so we can solve for the value where a decision flips instead of
   trying lots of values. That is what break_even does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

HARM_PATH = Path("config/harm.yaml")

# Names of the cost parameters. Used as dictionary keys so an expected cost can
# carry its own breakdown and be solved against any one of them.
EXPIRED_UNIT = "expired_unit"
MISATTRIBUTION_UNIT = "misattribution_unit"
SCRAP_UNIT = "scrap_unit"
SHELF_WASTE_UNIT_DAY = "shelf_waste_unit_day"
WRONG_ZONE = "wrong_zone"
HUMAN_REVIEW = "human_review"
DEFERRED_REVIEW = "deferred_review"
SEGREGATION_UNIT_DAY = "segregation_unit_day"


class CostModelError(ValueError):
    """The cost file does not add up."""


@dataclass(frozen=True)
class CostModel:
    """Costs worked out from the basis file."""

    unit_cost: float
    expired_unit: float
    scrap_unit: float
    shelf_waste_unit_day: float
    wrong_zone: float
    human_review: float
    # Cost per unit of the batch record being wrong, whatever the expiry says.
    misattribution_unit: float
    # Cost of identifying held stock later, in a batch, rather than now.
    deferred_review: float
    holding_unit_day: float
    segregation_unit_day: float

    # How often a human gets it wrong after investigating.
    human_error_rate: float
    # How long stock normally sits before it is sold.
    sell_through_days: int

    # Used by the downstream simulator, not by the agent's own decision.
    stockout_unit: float
    backorder_unit_day: float

    prices: dict[str, float] = field(default_factory=dict)

    @property
    def value(self) -> dict[str, float]:
        """Cost parameters by name."""
        return {
            EXPIRED_UNIT: self.expired_unit,
            MISATTRIBUTION_UNIT: self.misattribution_unit,
            SCRAP_UNIT: self.scrap_unit,
            SHELF_WASTE_UNIT_DAY: self.shelf_waste_unit_day,
            WRONG_ZONE: self.wrong_zone,
            HUMAN_REVIEW: self.human_review,
            DEFERRED_REVIEW: self.deferred_review,
            SEGREGATION_UNIT_DAY: self.segregation_unit_day,
        }

    def days_before_scrap_beats_backorder(self) -> float:
        """How long we would keep a unit on backorder before scrapping is cheaper."""
        if self.backorder_unit_day <= 0:
            return float("inf")
        return (self.scrap_unit - self.stockout_unit) / self.backorder_unit_day


def load_costs(path: Path | str = HARM_PATH) -> CostModel:
    raw = yaml.safe_load(Path(path).read_text())
    b = raw["basis"]
    unit = float(b["unit_cost_gbp"])

    selling_price = unit / (1.0 - float(b["gross_margin_fraction"]))
    lost_margin = selling_price - unit
    penalty = selling_price * float(b["non_supply_penalty_fraction"])
    holding_per_day = unit * float(b["annual_holding_rate"]) / 365.0

    model = CostModel(
        unit_cost=unit,
        expired_unit=float(raw["judged"]["expired_unit_gbp"]),
        # Scrapping destroys the goods, so we lose what we paid for them.
        scrap_unit=unit,
        # Recording an expiry earlier than the truth throws away part of the
        # window in which the unit could have been sold.
        shelf_waste_unit_day=unit / float(b["shelf_life_days"]),
        wrong_zone=float(b["analyst_hourly_gbp"]) * float(b["pick_detour_minutes"]) / 60.0,
        # If the batch record is wrong, a recall of the real batch will not find
        # these units and a recall of the recorded one will pull units that were
        # never affected. Charged whether or not the expiry was safe.
        misattribution_unit=float(b["batch_trace_probability"])
        * float(raw["judged"]["expired_unit_gbp"]),
        human_review=float(b["analyst_hourly_gbp"]) * float(b["review_minutes"]) / 60.0,
        deferred_review=float(b["analyst_hourly_gbp"]) * float(b["deferred_review_minutes"]) / 60.0,
        human_error_rate=float(b["human_error_rate"]),
        sell_through_days=int(b["sell_through_days"]),
        holding_unit_day=holding_per_day,
        # A hold bin costs normal holding plus the cost of blocking that location.
        segregation_unit_day=holding_per_day * 2.0,
        # Failing to supply one unit: the margin we did not earn, plus the
        # penalty the customer charges us.
        stockout_unit=lost_margin + penalty,
        backorder_unit_day=float(b["backorder_admin_per_unit_day_gbp"]),
        prices={
            "batch_registry": float(raw["prices"]["batch_registry_gbp"]),
            "shipment_ledger": float(raw["prices"]["shipment_ledger_gbp"]),
        },
    )
    _check_invariants(model, raw.get("invariants", {}))
    return model


def _check_invariants(m: CostModel, want: dict[str, object]) -> None:
    """Stop a bad edit from quietly changing how the agent behaves."""
    if want.get("expired_worse_than_scrap") and not m.expired_unit > m.scrap_unit:
        raise CostModelError(
            f"shipping an expired unit (£{m.expired_unit:.2f}) must cost more than "
            f"scrapping it (£{m.scrap_unit:.2f})"
        )
    if want.get("stockout_cheaper_than_scrap") and not m.stockout_unit < m.scrap_unit:
        raise CostModelError(
            f"failing to supply a unit (£{m.stockout_unit:.2f}) must cost less than "
            f"scrapping it (£{m.scrap_unit:.2f}), or the agent would scrap stock "
            f"rather than backorder it"
        )
    floor = want.get("min_backorder_days_before_scrap_is_better")
    if floor is not None:
        days = m.days_before_scrap_beats_backorder()
        if days < float(floor):  # type: ignore[arg-type]
            raise CostModelError(
                f"scrapping becomes cheaper than backordering after only {days:.1f} "
                f"days; the file asks for at least {floor}"
            )


# --------------------------------------------------------------------------
# Linear cost expressions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LinearCost:
    """An expected cost, kept as cost parameters times how much we are exposed to them.

    Keeping it in this form instead of a single number lets us solve for the
    value where two actions swap places.
    """

    # cost parameter name -> how many units/days of it this action risks
    exposure: dict[str, float] = field(default_factory=dict)
    # anything that does not scale with a cost parameter, such as a lookup fee
    fixed: float = 0.0

    def total(self, costs: CostModel) -> float:
        v = costs.value
        return self.fixed + sum(v[k] * x for k, x in self.exposure.items())

    def __add__(self, other: LinearCost) -> LinearCost:
        merged = dict(self.exposure)
        for k, x in other.exposure.items():
            merged[k] = merged.get(k, 0.0) + x
        return LinearCost(exposure=merged, fixed=self.fixed + other.fixed)

    def scaled(self, factor: float) -> LinearCost:
        return LinearCost(
            exposure={k: x * factor for k, x in self.exposure.items()},
            fixed=self.fixed * factor,
        )


def break_even(
    chosen: LinearCost, rival: LinearCost, parameter: str, costs: CostModel
) -> float | None:
    """Value of one cost parameter at which two actions cost the same.

    The difference between two actions is a straight line in any single cost
    parameter c:

        EC(chosen) - EC(rival) = alpha * c + beta

    so the crossing point is c* = -beta / alpha.

    Returns None when both actions have the same exposure to this parameter,
    which means the decision does not depend on it.
    """
    alpha = chosen.exposure.get(parameter, 0.0) - rival.exposure.get(parameter, 0.0)
    if abs(alpha) < 1e-12:
        return None

    v = costs.value
    beta = (chosen.fixed - rival.fixed) + sum(
        v[k] * (chosen.exposure.get(k, 0.0) - rival.exposure.get(k, 0.0))
        for k in set(chosen.exposure) | set(rival.exposure)
        if k != parameter
    )
    return -beta / alpha


@dataclass(frozen=True)
class Sensitivity:
    """How wrong one cost figure would have to be to change the decision."""

    parameter: str
    current: float
    flips_at: float
    # How many times the current value would have to change. Big means the
    # decision does not really depend on this figure.
    slack: float

    def describe(self) -> str:
        side = "above" if self.current > self.flips_at else "below"
        return (
            f"{self.parameter}: choice holds while {side} £{self.flips_at:.2f} "
            f"(now £{self.current:.2f}, {self.slack:.0f}x slack)"
        )


def sensitivities(chosen: LinearCost, rival: LinearCost, costs: CostModel) -> list[Sensitivity]:
    """Break-even for every cost parameter, tightest first.

    The first entry is the figure the decision actually rests on. If its slack is
    large, the decision is safe whatever the cost table says.

    An empty list is a result, not a gap: it means no single cost figure can be
    moved to a sensible value that would change the answer. Callers should say so
    rather than showing nothing.
    """
    out: list[Sensitivity] = []
    for parameter, current in costs.value.items():
        flips_at = break_even(chosen, rival, parameter, costs)
        # A negative crossing point means the parameter would have to go below
        # zero to flip the decision, so it cannot.
        if flips_at is None or flips_at < 0 or current <= 0:
            continue
        slack = current / flips_at if flips_at > 0 else float("inf")
        if slack < 1:
            slack = 1 / slack if slack > 0 else float("inf")
        out.append(
            Sensitivity(parameter=parameter, current=current, flips_at=flips_at, slack=slack)
        )
    out.sort(key=lambda s: s.slack)
    return out
