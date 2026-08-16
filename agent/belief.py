"""Working out how likely each candidate is.

Standard Bayes with one addition: whether a source is broken is part of the sum,
not a filter applied first. That is what lets the agent say "the label is
perfectly legible, but the process that produced it is probably compromised".

    P(evidence | candidate) = sum over source states of
        P(state | symptoms) x P(evidence | candidate, state)

All arithmetic is in log space so a chain of small likelihoods cannot underflow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .candidates import CandidateSet
from .confusion import misread_prob
from .constraints import Violation, multipliers
from .evidence import (
    LabelEvidence,
    LedgerEvidence,
    RecordEvidence,
    RegistryEvidence,
    ReturnIntake,
)
from .reliability import LabelState, RecordState, ReliabilityModel

# The catch-all explains any evidence equally badly. Giving it a flat likelihood
# means it wins mass on its own whenever no named candidate fits, which is the
# signature of a candidate list that is missing something.
CATCH_ALL_LIKELIHOOD = 0.05

# Floor for a likelihood, so nothing is ever driven to exactly zero.
FLOOR = 1e-9


@dataclass
class Step:
    """One evidence update, kept for the decision log."""

    name: str
    detail: str
    likelihood: dict[str, float] = field(default_factory=dict)
    posterior: dict[str, float] = field(default_factory=dict)
    #: Total weight of the evidence across all candidates. A low best likelihood
    #: means nothing we thought of explains what we saw.
    best_likelihood: float = 0.0


@dataclass
class Belief:
    """Probabilities over candidates, plus how they got there."""

    candidates: CandidateSet
    probability: dict[str, float]
    steps: list[Step] = field(default_factory=list)

    def best(self) -> tuple[str, float]:
        name = max(self.probability, key=lambda k: self.probability[k])
        return name, self.probability[name]

    def of(self, name: str) -> float:
        return self.probability.get(name, 0.0)

    @property
    def catch_all(self) -> float:
        from .candidates import CATCH_ALL

        return self.probability.get(CATCH_ALL, 0.0)


def start(candidates: CandidateSet) -> Belief:
    return Belief(candidates=candidates, probability=dict(candidates.prior))


def _normalise(log_weights: dict[str, float]) -> dict[str, float]:
    biggest = max(log_weights.values())
    weights = {k: math.exp(v - biggest) for k, v in log_weights.items()}
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def update(belief: Belief, name: str, detail: str, likelihood: dict[str, float]) -> Belief:
    """Multiply the current probabilities by a set of likelihoods."""
    log_weights = {
        candidate: math.log(max(belief.probability[candidate], FLOOR))
        + math.log(max(likelihood.get(candidate, FLOOR), FLOOR))
        for candidate in belief.probability
    }
    posterior = _normalise(log_weights)
    named = [v for k, v in likelihood.items() if k != "other"]
    belief.steps.append(
        Step(
            name=name,
            detail=detail,
            likelihood=dict(likelihood),
            posterior=dict(posterior),
            best_likelihood=max(named) if named else 0.0,
        )
    )
    belief.probability = posterior
    return belief


# --------------------------------------------------------------------------
# Likelihoods
# --------------------------------------------------------------------------


#: A reused box carries a real label from somewhere. If the code we read matches
#: no batch we know of, the wrong-label story needs that label to have come from
#: stock we do not hold at all - possible, but a long shot compared with simply
#: having misread a character.
P_WRONG_LABEL_UNKNOWN_CODE = 0.02


def label_likelihood(
    label: LabelEvidence,
    candidates: CandidateSet,
    reliability: ReliabilityModel,
    known_codes: set[str] | None = None,
) -> dict[str, float]:
    """How likely this label reading is, for each candidate.

    Three ways a reading can come about:
      ok          - the label shows the true batch and we read it right
      misread     - the label shows the true batch and we read it wrong
      wrong_label - we read it right, but it is not this box's batch

    The third one only makes sense if what we read is a code that some real batch
    actually carries. A reused outer box has a genuine label on it. When the
    reading matches no batch we know of, "we misread it" is left as very nearly
    the only explanation, and the character confusion table decides which code it
    was. Without that check the wrong-label route is a flat term across every
    candidate, and it drowns out the one piece of evidence that discriminates.
    """
    from common.coding import label_code

    if not label.is_usable or not label.code_text:
        # No characters at all, so the reading tells us nothing about which batch
        # it is. Flat across candidates.
        return {c.name: 1.0 for c in candidates.candidates}

    states = reliability.label_states(label)
    read = label.code_text
    out: dict[str, float] = {}

    # How much of the wrong-label mass any one batch attracts. A reused box could
    # carry any batch's label; bigger batches are seen more often.
    named = [c for c in candidates.candidates if not c.is_catch_all]
    share = 1.0 / max(len(named), 1)

    # Does what we read correspond to a label a real batch carries?
    if known_codes is not None and label.check_digit_ok is not None and read not in known_codes:
        share *= P_WRONG_LABEL_UNKNOWN_CODE

    for c in candidates.candidates:
        if c.is_catch_all:
            out[c.name] = CATCH_ALL_LIKELIHOOD
            continue

        true_code = label_code(c.batch_id) if c.batch_id else ""

        # Read correctly: does the true code match what we saw?
        exact = 1.0 if true_code.startswith(read) else 0.0
        p_ok = states[LabelState.OK] * exact

        # Misread: how easily does the true code garble into what we saw?
        p_misread = states[LabelState.MISREAD] * misread_prob(true_code, read)

        # Wrong label: the box carries some other batch's label, which we then
        # read correctly. Does not depend on this candidate's own code.
        p_wrong = states[LabelState.WRONG_LABEL] * share

        out[c.name] = max(p_ok + p_misread + p_wrong, FLOOR)

    return out


#: State of the shipment records when the paperwork itself says the stock arrived
#: outside the normal flow - cross-docked, transferred, no delivery note. That is
#: the definition of the records being incomplete for this stock, so a batch being
#: absent from them stops being evidence against it.
OFF_RECORD_STATES = {
    RecordState.OK: 0.15,
    RecordState.INCOMPLETE: 0.80,
    RecordState.CORRUPTED: 0.05,
}


def record_likelihood(
    records: RecordEvidence,
    candidates: CandidateSet,
    reliability: ReliabilityModel,
    off_record: bool = False,
) -> dict[str, float]:
    """How likely this set of shipment records is, for each candidate.

    `off_record` comes from the condition note. If the goods came in without
    paperwork, the shipment records were never going to list them, so their
    silence about a batch says almost nothing. Without this the records veto a
    candidate that the note itself put on the list.
    """
    if not records.available:
        return {c.name: 1.0 for c in candidates.candidates}

    states = OFF_RECORD_STATES if off_record else reliability.record_states(records)
    on_record = set(records.batch_ids)
    out: dict[str, float] = {}

    for c in candidates.candidates:
        if c.is_catch_all:
            out[c.name] = CATCH_ALL_LIKELIHOOD
            continue

        present = c.batch_id in on_record

        # Records complete and correct: the true batch has to be in the set.
        p_ok = states[RecordState.OK] * (1.0 if present else 0.02)
        # Records incomplete: a batch missing from the set is exactly what we
        # would expect, so absence is only weak evidence against it.
        p_incomplete = states[RecordState.INCOMPLETE] * (0.6 if present else 0.4)
        # Records corrupted: they barely constrain anything.
        p_corrupted = states[RecordState.CORRUPTED] * 0.5

        out[c.name] = max(p_ok + p_incomplete + p_corrupted, FLOOR)

    return out


def dispatch_likelihood(
    registry: RegistryEvidence | None,
    ledger: LedgerEvidence | None,
    candidates: CandidateSet,
    intake: ReturnIntake,
    off_record: bool = False,
) -> dict[str, float]:
    """One update for both paid lookups.

    The registry's allocation history and the ledger's door scans both come from
    the same dispatch events. Treating them as two independent pieces of evidence
    would count the same fact twice and overstate confidence, so they are fused
    into a single channel here.

    Hard impossibilities are handled by agent/constraints.py, not here. This
    covers the softer part: was this batch seen going out to this customer.

    When the paperwork says the goods arrived off-record, none of this applies -
    stock that was cross-docked was never going to appear in the door scans for
    this customer, so its absence says nothing.
    """
    if off_record:
        return {c.name: 1.0 for c in candidates.candidates}

    seen: set[str] = set()
    have_evidence = False

    if ledger is not None and ledger.available:
        have_evidence = True
        seen |= {s.batch_id for s in ledger.scans}

    if registry is not None and registry.available:
        for record in registry.batches:
            if record.allocated_to:
                have_evidence = True
                if intake.customer_id in record.allocated_to:
                    seen.add(record.batch_id)

    if not have_evidence:
        return {c.name: 1.0 for c in candidates.candidates}

    out: dict[str, float] = {}
    for c in candidates.candidates:
        if c.is_catch_all:
            out[c.name] = CATCH_ALL_LIKELIHOOD
            continue
        out[c.name] = 0.95 if c.batch_id in seen else 0.10
    return out


def apply_constraints(belief: Belief, violations: list[Violation]) -> Belief:
    """Fold hard-rule breaches into the probabilities."""
    if not violations:
        return belief
    factors = multipliers(violations)
    detail = "; ".join(v.detail for v in violations)
    likelihood = {c.name: factors.get(c.name, 1.0) for c in belief.candidates.candidates}
    return update(belief, "constraints", detail, likelihood)


def describe(belief: Belief) -> str:
    """One-line summary for the log."""
    parts = [
        f"{name} {p * 100:.1f}%"
        for name, p in sorted(belief.probability.items(), key=lambda kv: -kv[1])
    ]
    return "  ".join(parts)
