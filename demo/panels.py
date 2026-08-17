"""Everything the demo screen shows, as plain data.

Deliberately free of any Streamlit import. The screen is the thing being filmed,
so what it displays has to be checkable without a browser: if the numbers on
camera came from code that only runs inside a web server, nothing can assert they
are the same numbers the agent actually produced.

`demo/app.py` renders these and does no arithmetic of its own.

Four panels, matching the shot list in PLAN.md section 13:

- `carton`      the photo, what the reader made of it, and how sure it was
- `belief`      probability per candidate, after each piece of evidence
- `costs`       every action, what it would cost, and which won by how much
- `consequence` what this decision costs downstream, against the alternatives
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from agent import loop, notes
from agent.harm import CostModel, load_costs
from agent.policy import Kind
from agent.reliability import ReliabilityModel, load_reliability
from ledger import posting
from ledger.drift import Drift, TruthBook, measure
from ledger.ledger import Ledger
from services.adapter import BenchServices
from services.scenarios import Bench, build_bench, load_scenarios

HARM_ARTIFACT = Path("artifacts/harm.json")


@dataclass
class CartonPanel:
    scenario_id: str
    title: str
    description: str
    #: None for a case generated on the spot: there is no photo, because the
    #: reading is constructed rather than read off an image.
    image_path: str | None
    quantity: int
    customer_id: str
    #: What the reader made out, and how much of it.
    code_read: str | None
    confidence: float
    check_digit_ok: bool | None
    symptoms: list[str]
    reader_note: str
    condition_note: str

    @property
    def looks_trustworthy(self) -> bool:
        """The trap the hero case is built around.

        A label can be crisp, complete and pass its check digit and still be on
        the wrong box. When this is True and the agent does *not* file the stock
        as the code says, that is the whole argument on screen at once.
        """
        return bool(self.code_read) and self.check_digit_ok is True and self.confidence >= 0.85


@dataclass
class BeliefStep:
    name: str
    detail: str
    probability: dict[str, float]


@dataclass
class BeliefPanel:
    candidates: list[str]
    #: Probability after each piece of evidence, in the order it arrived.
    steps: list[BeliefStep]
    final: dict[str, float]
    catch_all: float

    @property
    def leader(self) -> tuple[str, float]:
        name = max(self.final, key=lambda k: self.final[k])
        return name, self.final[name]

    @property
    def flat(self) -> bool:
        """No candidate is clearly ahead. This is what real ambiguity looks like."""
        return self.leader[1] < 0.6


@dataclass
class Frame:
    """The agent's state after one piece of evidence, and what it would do now.

    The brief asks for genuinely competing strategies chosen at runtime. A final
    screenshot cannot show that: it shows one winner and no contest. Stepping
    through shows the contest - which action is ahead after the records, after
    the label, after the lookup - and, in the hero case, the winner changing.

    `best_action` answers a real question: *if you had to put this stock on a
    shelf right now, with only the evidence so far, what would you do?* It is
    priced with the same code as the real decision. It is not the same question
    the agent asks at decision one, which also weighs whether to buy more
    evidence, and the screen says so rather than blurring the two.
    """

    index: int
    name: str
    detail: str
    #: How well each candidate explains what was just seen: P(evidence | candidate).
    #:
    #: These are **not** probabilities of the candidates and do not add to 1. They
    #: are probabilities of the *evidence*, one per hypothesis, so there is no
    #: reason for them to normalise across hypotheses - and with four candidates
    #: they routinely sum to about four. Bayes only ever uses their ratios.
    #: Empty for the prior, which is not evidence.
    likelihood: dict[str, float]
    probability: dict[str, float]
    #: Cheapest way of placing the stock, given only the evidence up to here.
    best_action: str
    best_cost_gbp: float
    ranking: list[Option]
    #: True when reaching this step meant paying for a lookup.
    needed_a_lookup: bool = False

    @property
    def leader(self) -> tuple[str, float]:
        name = max(self.probability, key=lambda k: self.probability[k])
        return name, self.probability[name]

    @property
    def says_nothing(self) -> bool:
        """True when this evidence does not separate the candidates at all.

        Every candidate explains it equally well, so the ratios are all 1 and the
        probabilities come out unchanged. Worth saying on screen rather than
        showing a column of identical numbers and letting someone wonder.
        """
        if not self.likelihood:
            return False
        values = list(self.likelihood.values())
        return max(values) - min(values) < 1e-9

    @property
    def relative_likelihood(self) -> dict[str, float]:
        """Each candidate against the best-explained one.

        This is the form that actually does the work. The update multiplies by
        the likelihood, so only ratios matter: a candidate at 0.05 against a best
        of 0.95 is nineteen times worse at explaining what we just saw, and the
        raw numbers 0.05 and 0.95 mean nothing on their own.
        """
        if not self.likelihood:
            return {}
        best = max(self.likelihood.values()) or 1.0
        return {name: value / best for name, value in self.likelihood.items()}


@dataclass
class Option:
    action: str
    expected_cost_gbp: float
    fee_gbp: float
    exposure: dict[str, float]
    chosen: bool


@dataclass
class CostPanel:
    """The pane that carries the argument: every action, priced, and the winner."""

    name: str
    options: list[Option]
    chosen: str
    margin_gbp: float
    fragile: bool
    #: Which cost figure would have to move, and how far, to change the answer.
    sensitivity: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    @property
    def has_a_real_choice(self) -> bool:
        """R2 on screen: more than one option, and the winner is not a default."""
        return len(self.options) > 1

    @property
    def cheapest_is_chosen(self) -> bool:
        """R4 on screen: no `else` branch anywhere in the decision."""
        if not self.options:
            return False
        cheapest = min(o.expected_cost_gbp for o in self.options)
        picked = next((o for o in self.options if o.chosen), None)
        return picked is not None and picked.expected_cost_gbp <= cheapest + 1e-6


@dataclass
class ConsequencePanel:
    """What the decision costs later, and what the alternatives would have cost."""

    assigned_batch: str | None
    recorded_expiry: date | None
    true_batch: str
    true_expiry: date
    expiry_error_days: int | None
    drift: Drift
    #: Committed simulation results, if they have been generated.
    harm: dict[str, Any] | None = None

    @property
    def obvious_answer_was_wrong(self) -> bool:
        """R10: the label said one thing and the stock was another."""
        return self.assigned_batch is not None and self.assigned_batch != self.true_batch

    @property
    def days_of_shelf_life_at_stake(self) -> int:
        return abs((self.recorded_expiry - self.true_expiry).days) if self.recorded_expiry else 0


@dataclass
class Screen:
    """One complete state of the demo."""

    carton: CartonPanel
    belief: BeliefPanel
    decisions: list[CostPanel]
    consequence: ConsequencePanel
    spend_gbp: float
    bought: list[str]
    outcome: str
    ledger_rows: list[dict[str, Any]]
    #: True when this case was made up on the spot rather than being one of the
    #: twelve recorded ones. Worth saying on screen: nobody wrote it, so nobody
    #: could have tuned the agent to it.
    generated: bool = False
    #: How the case was built, for a generated one.
    faults: str = ""
    #: One entry per piece of evidence, in the order it arrived.
    frames: list[Frame] = field(default_factory=list)

    def image_path_is_absent(self) -> bool:
        """Generated cases have no photograph, and the screen says so."""
        return self.carton.image_path is None


#: Evidence that can only be had by paying for a lookup.
PAID_STEPS = {"constraints", "dispatch", "note dates"}


def _frames(result: Any, quantity: int, today: date, costs: CostModel) -> list[Frame]:
    """Replay the belief one step at a time, pricing the actions at each point."""
    from agent import belief as belief_mod
    from agent import policy as policy_mod

    def priced(probability: dict[str, float]) -> tuple[str, float, list[Option]]:
        state = belief_mod.Belief(
            candidates=result.belief.candidates, probability=dict(probability)
        )
        choice = policy_mod.choose(
            policy_mod.terminal_actions(state, quantity, costs, today), costs
        )
        ranking = [
            Option(
                action=o.action.label,
                expected_cost_gbp=round(o.total, 2),
                fee_gbp=round(o.fee, 2),
                exposure={k: round(v, 3) for k, v in o.cost.exposure.items()},
                chosen=o.action == choice.chosen.action,
            )
            for o in sorted(choice.options, key=lambda o: o.total)
        ]
        return choice.chosen.action.label, round(choice.chosen.total, 2), ranking

    prior = dict(result.belief.candidates.prior)
    action, cost, ranking = priced(prior)
    frames = [
        Frame(
            index=0,
            name="before any evidence",
            detail="prior, from how much of each batch is in the building",
            likelihood={},
            probability=prior,
            best_action=action,
            best_cost_gbp=cost,
            ranking=ranking,
        )
    ]
    for i, step in enumerate(result.belief.steps, start=1):
        action, cost, ranking = priced(step.posterior)
        frames.append(
            Frame(
                index=i,
                name=step.name,
                detail=step.detail,
                likelihood={k: round(v, 4) for k, v in step.likelihood.items()},
                probability=dict(step.posterior),
                best_action=action,
                best_cost_gbp=cost,
                ranking=ranking,
                needed_a_lookup=step.name in PAID_STEPS,
            )
        )
    return frames


def _options(record: Any) -> list[Option]:
    return [
        Option(
            action=o["action"],
            expected_cost_gbp=o["expected_cost_gbp"],
            fee_gbp=o["fee_gbp"],
            exposure=o["exposure"],
            chosen=o["action"] == record.chosen,
        )
        for o in record.options
    ]


def load_harm() -> dict[str, Any] | None:
    """The committed simulation results, if they are there."""
    if not HARM_ARTIFACT.exists():
        return None
    loaded: dict[str, Any] = json.loads(HARM_ARTIFACT.read_text())
    return loaded


def _assemble(
    *,
    costs: CostModel,
    scenario_id: str,
    title: str,
    description: str,
    image_path: str | None,
    intake: Any,
    label: Any,
    result: Any,
    truth: TruthBook,
    true_batch: str,
    generated: bool = False,
    faults: str = "",
) -> Screen:
    """Lay out one finished run, whichever kind of case it came from."""
    book = Ledger()
    posting.post(book, intake, result)
    drift = measure(book, truth)

    carton = CartonPanel(
        scenario_id=scenario_id,
        title=title,
        description=description,
        image_path=image_path,
        quantity=intake.quantity,
        customer_id=intake.customer_id,
        code_read=label.code_text,
        confidence=label.confidence,
        check_digit_ok=label.check_digit_ok,
        symptoms=sorted(s.value for s in label.symptoms),
        reader_note=label.reader_note,
        condition_note=intake.condition_note,
    )

    belief = BeliefPanel(
        candidates=result.belief.candidates.names,
        steps=[
            BeliefStep(name=s.name, detail=s.detail, probability=dict(s.posterior))
            for s in result.belief.steps
        ],
        final=dict(result.belief.probability),
        catch_all=result.belief.catch_all,
    )

    action = result.placement.chosen.action if result.placement else None
    recorded = action.recorded_best_before if action else None
    true_expiry = truth.best_before[true_batch]
    consequence = ConsequencePanel(
        assigned_batch=result.assigned_batch,
        recorded_expiry=recorded,
        true_batch=true_batch,
        true_expiry=true_expiry,
        expiry_error_days=(recorded - true_expiry).days if recorded else None,
        drift=drift,
        harm=load_harm(),
    )

    return Screen(
        carton=carton,
        belief=belief,
        frames=_frames(result, intake.quantity, intake.arrived, costs),
        decisions=[
            CostPanel(
                name=d.name,
                options=_options(d),
                chosen=d.chosen,
                margin_gbp=d.margin_gbp,
                fragile=d.fragile,
                sensitivity=d.sensitivity,
                notes=[n for n in result.trace.notes if n.startswith(d.name)],
            )
            for d in result.trace.decisions
        ],
        consequence=consequence,
        spend_gbp=result.spend_gbp,
        bought=list(result.bought),
        outcome=result.trace.outcome,
        ledger_rows=[
            {
                "seq": m.seq,
                "kind": str(m.kind),
                "quantity": m.quantity,
                "from": str(m.source),
                "to": str(m.destination),
                "caused_by": m.decision,
                "reason": m.reason,
            }
            for m in book.movements()
        ],
        generated=generated,
        faults=faults,
    )


def build_generated(
    seed: int,
    costs: CostModel | None = None,
    reliability: ReliabilityModel | None = None,
    calibrated: bool = True,
) -> Screen:
    """A case nobody wrote, built on the spot from a seed.

    This is the honest demonstration of uncertainty. The twelve recorded cases
    were written by hand and the agent was built while looking at them, so they
    can only ever show that it handles what it was shown. A case drawn from a
    seed has a fresh warehouse, fresh shipments, a fresh fault on each source and
    an answer nothing in the agent has seen.

    What is real here: the warehouse, the SQLite database, the shipment records,
    the batch registry and the paid lookups, all read through the same service
    classes with the same fault switches. What is not: there is no photograph.
    The label reading is constructed and put through the same validation code, so
    the screen shows no carton for these. Perception is covered by the twelve
    recorded cases instead.
    """
    from harness import generate as gen

    costs = costs or load_costs()
    reliability = reliability or load_reliability()
    case = gen.build(seed, calibrated=calibrated)
    result = loop.run(
        case.intake,
        gen.GeneratedServices(case),
        costs,
        reliability,
        gen.FixedNoteReader(case.note),
        scenario_id=f"gen-{seed}",
    )
    return _assemble(
        costs=costs,
        scenario_id=f"gen-{seed}",
        title=f"Generated case #{seed}",
        description=(
            "Nobody wrote this case. A fresh warehouse, a fresh return and a random fault on "
            "each source, drawn from the seed. The answer is known only to the scorer."
        ),
        image_path=None,
        intake=case.intake,
        label=case.label,
        result=result,
        truth=case.truth_book(),
        true_batch=case.truth,
        generated=True,
        faults=case.description,
    )


def build(
    scenario_id: str,
    costs: CostModel | None = None,
    reliability: ReliabilityModel | None = None,
    bench: Bench | None = None,
) -> Screen:
    """Run one case and lay out everything the screen needs.

    Offline: label readings come from the recorded cassettes, and the committed
    simulation figures from `artifacts/harm.json`. Nothing here calls a model or
    a network.
    """
    costs = costs or load_costs()
    reliability = reliability or load_reliability()
    scenarios = load_scenarios()
    scenario = scenarios[scenario_id]
    bench = bench or build_bench(scenario)

    services = BenchServices(bench)
    label = services.read_label(bench.intake)

    result = loop.run(
        bench.intake,
        services,
        costs,
        reliability,
        notes.CassetteNoteReader(),
        scenario_id=scenario_id,
    )

    event = next(r for r in bench.world.returns if r.return_id == bench.intake.return_id)
    truth = TruthBook(
        true_batch={event.return_id: event.true_batch_id},
        best_before={b.batch_id: b.best_before for b in bench.world.batches},
        home_bin={b.batch_id: b.home_bin for b in bench.world.batches},
    )
    return _assemble(
        costs=costs,
        scenario_id=scenario_id,
        title=scenario.name,
        description=scenario.description.strip(),
        image_path=str(bench.image_path),
        intake=bench.intake,
        label=label,
        result=result,
        truth=truth,
        true_batch=event.true_batch_id,
    )


def available() -> list[str]:
    """Scenario ids the screen can show, hero case first."""
    ids = sorted(load_scenarios())
    return sorted(ids, key=lambda s: (s != "S4", s))


def kind_of(screen: Screen) -> Kind | None:
    """What the agent finally did, for the headline line on screen."""
    if not screen.decisions:
        return None
    if screen.outcome.startswith("escalated"):
        return Kind.ESCALATE
    if screen.outcome.startswith("segregate"):
        return Kind.SEGREGATE
    if screen.outcome.startswith("commit"):
        return Kind.COMMIT
    return None
