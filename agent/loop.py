"""The agent: two decisions about one return.

Decision 1 - who do we believe?
    Read the label, query the warehouse records, then choose between trusting
    what we have, paying for a lookup, or handing it to a person.

Decision 2 - where does the stock go?
    Using the probabilities decision 1 produced, choose between filing it under a
    batch, holding it back under a conservative expiry, or handing it to a person.

Both decisions are the cheapest option out of a priced list. Neither has a
fallback branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from common.coding import label_code

from . import belief as belief_mod
from . import candidates as candidates_mod
from . import constraints, notes, policy, voi
from .evidence import (
    BatchSummary,
    LabelEvidence,
    LedgerEvidence,
    RecordEvidence,
    RegistryEvidence,
    ReturnIntake,
)
from .harm import CostModel
from .policy import Choice, Kind, Priced
from .reliability import ReliabilityModel
from .trace import Trace

#: Most we will spend on lookups for one return.
LOOKUP_BUDGET_GBP = 2.50


class Services(Protocol):
    """What the agent needs from the outside world."""

    def read_label(self, intake: ReturnIntake) -> LabelEvidence: ...
    def query_records(self, intake: ReturnIntake) -> RecordEvidence: ...
    def batch_catalogue(self, sku_id: str) -> dict[str, BatchSummary]: ...
    def buy_registry(self, batch_ids: list[str]) -> RegistryEvidence: ...
    def buy_ledger(self, intake: ReturnIntake) -> LedgerEvidence: ...


@dataclass
class Result:
    trace: Trace
    belief: belief_mod.Belief
    stance: Choice
    placement: Choice | None = None
    assigned_batch: str | None = None
    assigned_bin: str | None = None
    escalated: bool = False
    spend_gbp: float = 0.0
    bought: list[str] = field(default_factory=list)


def run(
    intake: ReturnIntake,
    services: Services,
    costs: CostModel,
    reliability: ReliabilityModel,
    note_reader: notes.NoteReader,
    scenario_id: str = "",
) -> Result:
    trace = Trace(return_id=intake.return_id, scenario_id=scenario_id)

    # ---------------------------------------------------------------- evidence
    label = services.read_label(intake)
    records = services.query_records(intake)
    catalogue = services.batch_catalogue(intake.sku_id)

    trace.add_evidence(
        "label",
        label.reader_note,
        code=label.code_text,
        confidence=round(label.confidence, 2),
        check_digit_ok=label.check_digit_ok,
        symptoms=sorted(s.value for s in label.symptoms),
        bucket=reliability.label_bucket_name(label),
    )
    trace.add_evidence(
        "records",
        "; ".join(records.warnings),
        batches=records.batch_ids,
        lag_hours=round(records.lag_seconds / 3600, 1),
        symptoms=sorted(s.value for s in records.symptoms),
        bucket=reliability.record_bucket_name(records),
    )

    # The note is read before the candidate list is built, because it can name a
    # batch that neither the records nor the label mention.
    note_facts = note_reader.read(intake.condition_note)
    if note_facts.is_informative:
        trace.add_evidence(
            "note",
            note_facts.summary,
            print_dates=[d.isoformat() for d in note_facts.print_dates],
            batch_codes_mentioned=note_facts.batch_codes_mentioned,
            repacked=note_facts.repacked,
            mixed_pallet=note_facts.mixed_pallet,
            off_site_origin=note_facts.off_site_origin,
        )

    # May be empty, when the warehouse system did not answer. `label_likelihood`
    # treats an empty list as "nothing to check against" rather than as a no.
    known_codes = {label_code(b) for b in catalogue}
    off_record = note_facts.suggests_off_record_stock
    candidate_set = candidates_mod.build(intake, records, label, catalogue, note_facts)
    from_note = [c.name for c in candidate_set.candidates if c.source == "note"]
    if from_note:
        trace.notes.append(
            f"the note named {from_note}, which neither the records nor the label "
            f"mention; added as candidates after checking they are real batches"
        )
    if candidate_set.rejected:
        trace.notes.append(
            f"label named {candidate_set.rejected}, which is not a known batch; "
            f"its weight went to the catch-all"
        )

    b = belief_mod.start(candidate_set)
    b = belief_mod.update(
        b,
        "records",
        "shipment records",
        belief_mod.record_likelihood(records, candidate_set, reliability, off_record),
    )
    b = belief_mod.update(
        b,
        "label",
        "carton label",
        belief_mod.label_likelihood(label, candidate_set, reliability, known_codes),
    )
    # A lot code written out in the note needs nothing bought, so it is applied
    # here with everything else. Only the print date has to wait for the registry.
    b = belief_mod.update(
        b,
        "note",
        "lot code written in the note",
        notes.note_code_likelihood(note_facts, candidate_set),
    )

    # ------------------------------------------------- decision 1: who to believe
    registry: RegistryEvidence | None = None
    ledger: LedgerEvidence | None = None
    spend = 0.0
    bought: list[str] = []

    def simulate(combo: tuple[str, ...]) -> list[tuple[float, belief_mod.Belief]]:
        """What we would believe after buying these lookups.

        The services are deterministic here, so there is a single outcome. The
        shape still allows several, which is what a probabilistic source would
        need.
        """
        trial = belief_mod.Belief(
            candidates=b.candidates, probability=dict(b.probability), steps=[]
        )
        reg = (
            services.buy_registry([c.batch_id for c in candidate_set.candidates if c.batch_id])
            if voi.REGISTRY in combo
            else None
        )
        led = services.buy_ledger(intake) if voi.LEDGER in combo else None

        trial = belief_mod.apply_constraints(
            trial,
            constraints.check(candidate_set.candidates, intake, records, reg, off_record),
        )
        trial = belief_mod.update(
            trial,
            "dispatch",
            "paid lookups",
            belief_mod.dispatch_likelihood(reg, led, candidate_set, intake, off_record),
        )
        trial = belief_mod.update(
            trial,
            "note dates",
            "print date on the goods",
            notes.note_date_likelihood(note_facts, candidate_set, reg),
        )
        return [(1.0, trial)]

    options = voi.evaluate(
        b, intake.quantity, costs, simulate, intake.arrived, budget=LOOKUP_BUDGET_GBP
    )
    stance_options: list[Priced] = policy.terminal_actions(
        b, intake.quantity, costs, intake.arrived
    )
    stance_options += voi.as_priced_actions(options, intake.quantity)
    stance = policy.choose(stance_options, costs)
    trace.add_decision("who to believe", b, stance)

    # ------------------------------------------------------- act on decision 1
    if stance.chosen.action.kind is Kind.GATHER:
        combo = stance.chosen.action.tools
        if voi.REGISTRY in combo:
            registry = services.buy_registry(
                [c.batch_id for c in candidate_set.candidates if c.batch_id]
            )
            spend += registry.cost_gbp
            bought.append(voi.REGISTRY)
        if voi.LEDGER in combo:
            ledger = services.buy_ledger(intake)
            spend += ledger.cost_gbp
            bought.append(voi.LEDGER)

        violations = constraints.check(
            candidate_set.candidates, intake, records, registry, off_record
        )
        for v in violations:
            trace.add_evidence("constraint", v.detail, rule=v.rule)
        b = belief_mod.apply_constraints(b, violations)
        b = belief_mod.update(
            b,
            "dispatch",
            "paid lookups",
            belief_mod.dispatch_likelihood(registry, ledger, candidate_set, intake, off_record),
        )

        b = belief_mod.update(
            b,
            "note dates",
            "print date on the goods",
            notes.note_date_likelihood(note_facts, candidate_set, registry),
        )

    elif stance.chosen.action.kind is Kind.ESCALATE:
        trace.spend_gbp = spend
        trace.outcome = "escalated at decision 1"
        return Result(
            trace=trace, belief=b, stance=stance, escalated=True, spend_gbp=spend, bought=bought
        )

    # ------------------------------------------------ decision 2: where it goes
    placement = policy.choose(
        policy.terminal_actions(b, intake.quantity, costs, intake.arrived), costs
    )
    trace.add_decision("where it goes", b, placement)
    trace.spend_gbp = spend

    action = placement.chosen.action
    if action.kind is Kind.ESCALATE:
        trace.outcome = "escalated at decision 2"
        return Result(
            trace=trace,
            belief=b,
            stance=stance,
            placement=placement,
            escalated=True,
            spend_gbp=spend,
            bought=bought,
        )

    trace.outcome = action.label
    return Result(
        trace=trace,
        belief=b,
        stance=stance,
        placement=placement,
        assigned_batch=action.batch_id,
        assigned_bin=action.bin_id,
        spend_gbp=spend,
        bought=bought,
    )
