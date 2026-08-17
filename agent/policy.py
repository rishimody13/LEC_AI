"""Turning probabilities into an action.

For every action we work out what it would cost, summed over the candidates and
weighted by how likely each one is:

    expected cost = sum over candidates of P(candidate) x harm(action, candidate)
                    + the action's own price

Then we take the cheapest. There is no fallback branch: escalating to a human is
priced and compared like everything else, and wins when it is genuinely cheapest.

Costs are kept as linear expressions rather than plain numbers so we can also
report how wrong a cost figure would have to be to change the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from .belief import Belief
from .candidates import Candidate
from .harm import (
    DEFERRED_REVIEW,
    EXPIRED_UNIT,
    HUMAN_REVIEW,
    MISATTRIBUTION_UNIT,
    SHELF_WASTE_UNIT_DAY,
    WRONG_ZONE,
    CostModel,
    LinearCost,
    Sensitivity,
    sensitivities,
)

#: Bin used when stock is held back rather than filed.
HOLD_BIN = "H-01-01"

#: If the stock turns out to be a batch we never considered, the expiry we
#: recorded is as likely to be too late as too early. Only the "too late" half
#: causes an expired shipment.
UNKNOWN_BATCH_OVERSTATE = 0.5


class Kind(StrEnum):
    COMMIT = "commit"
    SEGREGATE = "segregate"
    GATHER = "gather"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Action:
    kind: Kind
    #: For commit: which batch we are filing the stock as.
    batch_id: str | None = None
    bin_id: str | None = None
    #: For gather: which lookups to buy.
    tools: tuple[str, ...] = ()
    #: The expiry date this action would write against the stock. For a commit
    #: that is the batch's own date; for a segregate it is the conservative date
    #: the hold is placed under. Carried here so the ledger records the date the
    #: decision actually used, rather than working it out a second time and
    #: risking the two disagreeing.
    recorded_best_before: date | None = None

    @property
    def label(self) -> str:
        if self.kind is Kind.COMMIT:
            return f"commit {self.batch_id} -> {self.bin_id}"
        if self.kind is Kind.GATHER:
            return f"gather {'+'.join(self.tools)}"
        if self.kind is Kind.SEGREGATE:
            return f"segregate -> {self.bin_id}"
        return "escalate"


@dataclass
class Priced:
    action: Action
    cost: LinearCost
    total: float
    #: What this action costs to take, before any harm it risks. Only a lookup
    #: has one. Kept separate from `cost.fixed` because a gather's LinearCost
    #: carries the fee *plus* what we expect to spend afterwards - the two are
    #: added so it sits on the same scale as committing, and reporting that sum
    #: as "the fee" showed a 30p lookup costing £28.
    fee: float = 0.0


#: A win by less than this share of the cheapest option is called fragile. At
#: that distance the answer is set by where a parameter happens to sit, not by
#: the evidence, so the log says so instead of presenting it as a clear call.
FRAGILE_MARGIN = 0.05


@dataclass
class Choice:
    """The chosen action, everything it beat, and how safe the margin is."""

    chosen: Priced
    options: list[Priced]
    sensitivity: list[Sensitivity] = field(default_factory=list)

    @property
    def runner_up(self) -> Priced | None:
        rest = [o for o in self.options if o.action != self.chosen.action]
        return min(rest, key=lambda o: o.total) if rest else None

    @property
    def margin(self) -> float:
        r = self.runner_up
        return (r.total - self.chosen.total) if r else float("inf")

    @property
    def margin_share(self) -> float:
        """Margin as a share of the cheapest option."""
        if self.chosen.total <= 0 or self.margin == float("inf"):
            return float("inf")
        return self.margin / self.chosen.total

    def _leads_elsewhere(self, other: Priced) -> bool:
        """True if picking `other` would actually change what happens.

        Two gather options that differ only in which lookups to buy both lead to
        the same next step, so a small gap between them is not a close call about
        anything. Two commits naming different batches do lead somewhere else.
        """
        if other.action.kind is not self.chosen.action.kind:
            return True
        if other.action.kind is Kind.COMMIT:
            return other.action.batch_id != self.chosen.action.batch_id
        return False

    @property
    def decisive_runner_up(self) -> Priced | None:
        """Cheapest option that would lead somewhere different."""
        rest = [o for o in self.options if self._leads_elsewhere(o)]
        return min(rest, key=lambda o: o.total) if rest else None

    @property
    def decisive_margin(self) -> float:
        r = self.decisive_runner_up
        return (r.total - self.chosen.total) if r else float("inf")

    @property
    def is_fragile(self) -> bool:
        """True when a small change would lead somewhere different.

        Measured against the cheapest option that changes the outcome, not
        against a variant of the same action. Winning by pennies over a slightly
        different set of lookups is not a close call about anything.
        """
        if self.chosen.total <= 0 or self.decisive_margin == float("inf"):
            return False
        return (self.decisive_margin / self.chosen.total) < FRAGILE_MARGIN


# --------------------------------------------------------------------------
# Harm
# --------------------------------------------------------------------------


def filing_harm(
    recorded_expiry: date | None,
    recorded_bin: str | None,
    truth: Candidate,
    quantity: int,
    today: date,
    sell_through_days: int,
    recorded_batch: str | None = None,
) -> LinearCost:
    """Cost of filing stock under one expiry when it is really another batch.

    The two directions are not symmetric.

    Recording a *later* expiry than the truth is the dangerous one. Picking runs
    first-expired-first-out, so the stock waits until the recorded date and then
    ships after it has really gone off.

    Recording an *earlier* expiry only wastes shelf life, and only if the earlier
    date actually bites. Stock normally sells within `sell_through_days`, so
    knocking 100 days off a date that is still a year out costs nothing. We only
    charge for the days by which the recorded date cuts into that window.
    """
    if recorded_expiry is None:
        # No date was written against the stock at all. Picking runs
        # first-expired-first-out, which cannot rank what it cannot date, and the
        # hold area is not picked from anyway. So these units cannot ship, and
        # cannot ship expired. They are not free - somebody has to come back and
        # identify them - but that is the deferred review cost, charged
        # separately, not an expiry risk.
        #
        # Rolling this in with the two cases below priced a hold placed with no
        # date as a coin flip on shipping expired stock, which made the safest
        # thing the agent can do when the batch list is down look like the most
        # reckless.
        return LinearCost()

    if truth.is_catch_all or truth.best_before is None:
        # We do not know what it really is, so we do not know which way the
        # expiry is wrong. Treat it as an even chance either way rather than
        # assuming the worst, which would make every commit look reckless.
        return LinearCost(exposure={EXPIRED_UNIT: float(quantity) * UNKNOWN_BATCH_OVERSTATE})

    exposure: dict[str, float] = {}
    days = (recorded_expiry - truth.best_before).days

    if days > 0:
        exposure[EXPIRED_UNIT] = float(quantity)
    elif days < 0:
        window = (recorded_expiry - today).days
        bites = max(0, sell_through_days - window)
        charged = min(bites, -days)
        if charged > 0:
            exposure[SHELF_WASTE_UNIT_DAY] = float(quantity * charged)

    # Filing under the wrong batch breaks traceability even when the expiry we
    # recorded was on the safe side.
    if recorded_batch is not None and recorded_batch != truth.batch_id:
        exposure[MISATTRIBUTION_UNIT] = float(quantity)

    if recorded_bin is not None and truth.home_bin is not None and recorded_bin != truth.home_bin:
        exposure[WRONG_ZONE] = 1.0

    return LinearCost(exposure=exposure)


def expected(action_harm: dict[str, LinearCost], belief: Belief) -> LinearCost:
    """Weight each candidate's harm by how likely that candidate is."""
    out = LinearCost()
    for name, harm in action_harm.items():
        out = out + harm.scaled(belief.of(name))
    return out


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def commit_cost(
    batch: Candidate, belief: Belief, quantity: int, today: date, costs: CostModel
) -> LinearCost:
    """Cost of filing the stock as this batch, in its home bin."""
    harms = {
        c.name: filing_harm(
            batch.best_before,
            batch.home_bin,
            c,
            quantity,
            today,
            costs.sell_through_days,
            recorded_batch=batch.batch_id,
        )
        for c in belief.candidates.candidates
    }
    return expected(harms, belief)


def conservative_expiry(belief: Belief) -> date | None:
    """The date segregated stock is held under: the earliest it could possibly be.

    Holding stock back is only worth anything if this date is never later than
    the truth. An expiry no later than the truth cannot cause an expired
    shipment; an earlier one only wastes shelf life, which is charged for
    honestly and is the cheaper mistake by a wide margin.

    The obvious version of this - the earliest date among the live candidates -
    is not conservative at all. It is the earliest date among the answers we
    happened to think of, and the catch-all exists precisely to say the answer
    might not be one of them. The catch-all carries no date, so it can never pull
    the date down, and a hold could end up dated later than the stock really
    lasts. That is the failure the whole design exists to avoid, arriving through
    the action that is supposed to be the safe one. The generative sweep found
    exactly that.

    So the date is the earliest best-before of any batch of this product that
    exists. Whatever the stock turns out to be, it cannot expire before then.

    ``None`` when the batch list was unavailable: there is then no date we can
    stand behind, and we do not invent one. Stock held with no date cannot be
    picked first-expired-first-out at all, which is the safe way to be ignorant.

    The named candidates play no part. When we know enough for their dates to
    matter, committing is the cheaper action anyway, and the arithmetic picks it.
    """
    return belief.candidates.earliest_known_expiry


def segregate_cost(belief: Belief, quantity: int, today: date, costs: CostModel) -> LinearCost:
    """Hold the stock under the earliest expiry any live candidate could have.

    Safe on the dangerous side: an expiry no later than the truth can never cause
    an expired shipment. That is the one thing segregating actually buys.

    What it does *not* buy is an answer. Holding stock defers the identification
    work, it does not do it. Whoever picks it up later has the same evidence we
    have now, so they will file it under the most likely batch and be wrong just
    as often as we would have been. Segregating therefore carries the same
    misattribution risk as committing, plus the cost of handling it twice.

    Getting this wrong is what made an earlier version of this model prefer
    holding stock over deciding: it treated a deferral as if it removed the
    uncertainty.
    """
    conservative = conservative_expiry(belief)

    harms = {
        c.name: filing_harm(conservative, HOLD_BIN, c, quantity, today, costs.sell_through_days)
        for c in belief.candidates.candidates
    }
    # The eventual resolver will file it under whichever candidate looks most
    # likely, using the evidence we already have, so the chance of getting the
    # batch wrong is unchanged by waiting.
    _, best_probability = belief.best()
    still_wrong = (1.0 - best_probability) * quantity

    deferred = LinearCost(exposure={DEFERRED_REVIEW: 1.0, MISATTRIBUTION_UNIT: still_wrong})
    return expected(harms, belief) + deferred


def escalate_cost(costs: CostModel, quantity: int) -> LinearCost:
    """Cost of handing the return to a person.

    Their time, plus the harm they cause on the returns they still get wrong. A
    person who investigates is good but not perfect, and leaving that out makes
    escalation look free - the agent then hands over everything rather than
    deciding anything.

    The harm they cause is priced the same way as the agent's own. A batch filed
    under the wrong number breaks traceability whoever filed it, so the
    misattribution charge applies here too. Leaving it off - which it was -
    made the identical mistake cheaper when a person made it, and quietly tilted
    every close call towards handing the work over.
    """
    wrong = costs.human_error_rate * quantity
    return LinearCost(
        exposure={
            HUMAN_REVIEW: 1.0,
            MISATTRIBUTION_UNIT: wrong,
            EXPIRED_UNIT: wrong * UNKNOWN_BATCH_OVERSTATE,
        }
    )


def terminal_actions(belief: Belief, quantity: int, costs: CostModel, today: date) -> list[Priced]:
    """Every way of finishing with this return, priced."""
    out: list[Priced] = []
    for c in belief.candidates.candidates:
        if c.is_catch_all or c.batch_id is None or c.home_bin is None:
            continue
        cost = commit_cost(c, belief, quantity, today, costs)
        out.append(
            Priced(
                action=Action(
                    kind=Kind.COMMIT,
                    batch_id=c.batch_id,
                    bin_id=c.home_bin,
                    recorded_best_before=c.best_before,
                ),
                cost=cost,
                total=0.0,
            )
        )
    out.append(
        Priced(
            action=Action(
                kind=Kind.SEGREGATE,
                bin_id=HOLD_BIN,
                recorded_best_before=conservative_expiry(belief),
            ),
            cost=segregate_cost(belief, quantity, today, costs),
            total=0.0,
        )
    )
    out.append(
        Priced(
            action=Action(kind=Kind.ESCALATE),
            cost=escalate_cost(costs, quantity),
            total=0.0,
        )
    )
    return out


def choose(options: list[Priced], costs: CostModel) -> Choice:
    """Pick the cheapest action.

    Ties break on the action's own price, so an action that risks nothing but
    costs money never beats one that costs nothing.
    """
    if not options:
        raise ValueError("no actions to choose between")

    for o in options:
        o.total = o.cost.total(costs)

    ranked = sorted(options, key=lambda o: (o.total, o.fee))
    chosen = ranked[0]
    choice = Choice(chosen=chosen, options=options)

    runner_up = choice.runner_up
    if runner_up is not None:
        choice.sensitivity = sensitivities(chosen.cost, runner_up.cost, costs)
    return choice
