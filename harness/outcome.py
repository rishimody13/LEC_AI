"""What a decision actually cost, once the answer is known.

The agent picks the action with the lowest cost *in expectation*, using its own
probabilities. This works out what that action really cost, using the truth. The
two differ whenever the agent was wrong, and the gap between them is the only
honest way to ask whether the agent is deciding well rather than merely deciding
consistently with its own beliefs.

It deliberately reuses `policy.filing_harm` rather than reimplementing the
arithmetic. If the harm model is wrong, both the decision and the score are wrong
in the same way, and the comparison between policies still stands.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.candidates import Candidate
from agent.harm import (
    DEFERRED_REVIEW,
    EXPIRED_UNIT,
    HUMAN_REVIEW,
    MISATTRIBUTION_UNIT,
    CostModel,
    LinearCost,
)
from agent.loop import Result
from agent.policy import UNKNOWN_BATCH_OVERSTATE, Kind, filing_harm

from .generate import Case


@dataclass
class Outcome:
    """What one return really cost."""

    action: str
    correct: bool
    #: Harm caused, in pounds.
    harm_gbp: float
    #: What we spent looking things up.
    spend_gbp: float

    @property
    def total_gbp(self) -> float:
        return self.harm_gbp + self.spend_gbp


def truth_as_candidate(case: Case) -> Candidate:
    """The right answer, in the shape the harm model expects."""
    batch = next(b for b in case.world.batches if b.batch_id == case.truth)
    return Candidate(
        batch_id=batch.batch_id,
        best_before=batch.best_before,
        home_bin=batch.home_bin,
        source="truth",
    )


def score(case: Case, result: Result, costs: CostModel) -> Outcome:
    """Work out what this decision really cost."""
    truth = truth_as_candidate(case)
    quantity = case.intake.quantity
    today = case.intake.arrived

    if result.escalated or result.placement is None:
        # A person looks at it. We cannot simulate one, so they are charged their
        # time plus the harm on the share they still get wrong - the same figure
        # the agent used when it decided to hand it over, so escalation is not
        # scored as a free correct answer.
        slip = costs.human_error_rate * quantity * UNKNOWN_BATCH_OVERSTATE
        harm = LinearCost(exposure={HUMAN_REVIEW: 1.0, EXPIRED_UNIT: slip})
        return Outcome(
            action="escalate",
            correct=False,
            harm_gbp=harm.total(costs),
            spend_gbp=result.spend_gbp,
        )

    action = result.placement.chosen.action
    harm = filing_harm(
        action.recorded_best_before,
        action.bin_id,
        truth,
        quantity,
        today,
        costs.sell_through_days,
        recorded_batch=action.batch_id,
    )

    if action.kind is Kind.SEGREGATE:
        # Holding stock defers the identification, it does not do it. Somebody
        # comes back to it, and gets it wrong as often as we would have.
        _, best_probability = result.belief.best()
        harm = harm + LinearCost(
            exposure={
                DEFERRED_REVIEW: 1.0,
                MISATTRIBUTION_UNIT: (1.0 - best_probability) * quantity,
            }
        )

    return Outcome(
        action=action.kind.value,
        correct=result.assigned_batch == case.truth,
        harm_gbp=harm.total(costs),
        spend_gbp=result.spend_gbp,
    )
