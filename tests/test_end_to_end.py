"""The whole thing, from a drawn carton to a stock movement, and what it shows.

Everything else tests a layer. This tests the joins between them, which is where
a build that passes every unit test still produces nonsense on screen: the image
the demo shows is not the one the reader read, the probability bars do not match
the belief the decision used, the ledger says something the trace does not.

The second half is about uncertainty specifically, because that is what this
agent is for. A probability that is quietly nan, or a confidence that stays at
99% when every source has failed, would not break any test above - it would just
make every number downstream a lie.
"""

from __future__ import annotations

import copy
import math
from datetime import date
from pathlib import Path

import pytest

from agent import belief as belief_mod
from agent import loop, notes
from agent.harm import load_costs
from agent.policy import Kind
from agent.reliability import load_reliability
from demo import panels
from services.adapter import BenchServices
from services.scenarios import build_bench, load_scenarios

COSTS = load_costs()
RELIABILITY = load_reliability()
SCENARIOS = load_scenarios()
IDS = sorted(SCENARIOS)


@pytest.fixture(scope="module")
def screens() -> dict[str, panels.Screen]:
    return {sid: panels.build(sid, COSTS, RELIABILITY) for sid in IDS}


# ------------------------------------------------------- the pipeline joins up


@pytest.mark.parametrize("scenario_id", IDS)
def test_every_case_runs_end_to_end(scenario_id, screens):
    """A drawn image, read, reasoned over, decided, and posted to the ledger."""
    screen = screens[scenario_id]
    assert Path(screen.carton.image_path).exists(), "the screen points at a real image"
    assert screen.outcome, "every case reaches an outcome"
    assert screen.decisions, "every case records at least one decision"
    assert len(screen.ledger_rows) == 2, "a receipt and a placement, always"


@pytest.mark.parametrize("scenario_id", IDS)
def test_the_screen_shows_what_the_agent_actually_did(scenario_id, screens):
    """The panel data and the agent's own result must not drift apart.

    The demo is the deliverable being filmed. If it recomputed anything itself it
    could show a number the agent never produced.
    """
    screen = screens[scenario_id]
    bench = build_bench(SCENARIOS[scenario_id])
    result = loop.run(
        bench.intake,
        BenchServices(bench),
        COSTS,
        RELIABILITY,
        notes.CassetteNoteReader(),
        scenario_id=scenario_id,
    )
    assert screen.outcome == result.trace.outcome
    assert screen.spend_gbp == pytest.approx(result.spend_gbp)
    assert screen.belief.final == pytest.approx(result.belief.probability)
    assert screen.consequence.assigned_batch == result.assigned_batch


@pytest.mark.parametrize("scenario_id", IDS)
def test_the_cost_table_on_screen_proves_what_it_claims(scenario_id, screens):
    """R2 and R4, as the viewer sees them: real alternatives, cheapest wins."""
    for decision in screens[scenario_id].decisions:
        assert decision.has_a_real_choice, f"{decision.name} had nothing to choose between"
        assert decision.cheapest_is_chosen, f"{decision.name} did not take the cheapest option"
        assert sum(o.chosen for o in decision.options) == 1


@pytest.mark.parametrize("scenario_id", IDS)
def test_the_ledger_agrees_with_the_decision_on_screen(scenario_id, screens):
    screen = screens[scenario_id]
    placement = screen.ledger_rows[-1]
    assert placement["quantity"] == screen.carton.quantity
    if screen.consequence.assigned_batch:
        assert screen.consequence.assigned_batch in placement["to"]


def test_the_hero_case_shows_the_argument(screens):
    """R10, checked rather than asserted in a script.

    The label is crisp, complete, passes its check digit, and names a batch. The
    agent does not file the stock as that batch, and is right not to.
    """
    hero = screens["S4"]
    assert hero.carton.looks_trustworthy, "the label has to look convincing or there is no story"
    assert hero.carton.code_read is not None
    claimed = hero.carton.code_read.rsplit("-", 1)[0]
    assert hero.consequence.assigned_batch != claimed, "the obvious answer must be rejected"
    assert hero.consequence.assigned_batch == hero.consequence.true_batch
    assert hero.consequence.drift.clean
    assert hero.belief.final[claimed] < 0.05, "the label's batch must end up ruled out"


def test_the_demo_is_quick_enough_to_film():
    """Under 45 seconds for every case, per the plan."""
    import time

    started = time.perf_counter()
    for scenario_id in IDS:
        panels.build(scenario_id, COSTS, RELIABILITY)
    elapsed = time.perf_counter() - started
    assert elapsed < 45, f"building all {len(IDS)} cases took {elapsed:.1f}s"


def test_the_demo_never_calls_a_model():
    """It has to run on a laptop with no network while being filmed."""
    anthropic = pytest.importorskip("anthropic", reason="the llm extra is not installed")

    class Boom:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the demo tried to call the API")

    original = anthropic.Anthropic
    anthropic.Anthropic = Boom  # type: ignore[misc, assignment]
    try:
        for scenario_id in IDS:
            panels.build(scenario_id, COSTS, RELIABILITY)
    finally:
        anthropic.Anthropic = original  # type: ignore[misc]


# ------------------------------------------------------------- uncertainty


@pytest.mark.parametrize("scenario_id", IDS)
def test_probabilities_are_always_real_numbers(scenario_id, screens):
    """A quiet nan or a probability of 1.0000001 would poison every cost below it."""
    screen = screens[scenario_id]
    for stage in [*[s.probability for s in screen.belief.steps], screen.belief.final]:
        assert stage, "a belief must never be empty"
        for name, p in stage.items():
            assert math.isfinite(p), f"{name} is {p}"
            assert 0.0 <= p <= 1.0, f"{name} is {p}"
        assert sum(stage.values()) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("scenario_id", IDS)
def test_nothing_is_ever_completely_ruled_out(scenario_id, screens):
    """Nothing recovers from zero.

    A candidate driven to exactly 0 can never come back however good the later
    evidence is, and the sources that rule things out can themselves be wrong.
    """
    for p in screens[scenario_id].belief.final.values():
        assert p > 0.0


@pytest.mark.parametrize("scenario_id", IDS)
def test_the_answer_might_always_be_something_we_did_not_think_of(scenario_id, screens):
    """The catch-all is what stops a confident wrong answer being unfalsifiable."""
    screen = screens[scenario_id]
    assert "other" in screen.belief.final
    assert screen.belief.catch_all > 0.0


def test_confidence_collapses_when_every_source_fails():
    """With nothing to go on the agent must not still be sure of something.

    This is the case where a badly built agent looks best: no evidence at all,
    and a prior that happens to favour one batch, reported as knowledge.
    """
    screen = panels.build("S3", COSTS, RELIABILITY)
    assert screen.belief.flat or screen.outcome.startswith("escalated"), (
        f"with no usable evidence the agent said {screen.belief.leader}"
    )


def test_more_doubt_makes_the_agent_less_willing_to_commit():
    """Turn the label reader's reliability down and it should hedge more, not less.

    A direction check on the whole chain: reliability model, likelihood, belief,
    cost, action. If this ran backwards every number would still look plausible.
    """
    trusting = RELIABILITY
    # Every label bucket is rewritten so the box is far more often a reused one.
    from agent.reliability import Bucket, LabelState, ReliabilityModel

    doubting = ReliabilityModel(
        label={
            name: Bucket(
                name=bucket.name,
                counts={
                    LabelState.OK.value: bucket.counts.get(LabelState.OK.value, 0) // 8,
                    LabelState.MISREAD.value: bucket.counts.get(LabelState.MISREAD.value, 0),
                    LabelState.WRONG_LABEL.value: bucket.counts.get(LabelState.WRONG_LABEL.value, 0)
                    * 8
                    + 50,
                },
            )
            for name, bucket in trusting.label.items()
        },
        records=copy.deepcopy(trusting.records),
    )

    committed_when_trusting = 0
    committed_when_doubting = 0
    for scenario_id in IDS:
        bench = build_bench(SCENARIOS[scenario_id])
        for model, counter in ((trusting, "t"), (doubting, "d")):
            result = loop.run(
                bench.intake,
                BenchServices(bench),
                COSTS,
                model,
                notes.CassetteNoteReader(),
            )
            filed = result.assigned_batch is not None
            if counter == "t":
                committed_when_trusting += filed
            else:
                committed_when_doubting += filed

    assert committed_when_doubting <= committed_when_trusting, (
        f"doubting the label led to MORE commits ({committed_when_doubting}) than trusting it "
        f"({committed_when_trusting})"
    )


def test_a_confident_reading_beats_a_doubtful_one():
    """Same code, more confidence, no less belief in it."""
    from agent.candidates import Candidate, CandidateSet
    from agent.evidence import LabelEvidence

    candidates = CandidateSet(
        candidates=[Candidate("B-2288"), Candidate("B-2291"), Candidate(None)],
        prior={"B-2288": 0.4, "B-2291": 0.4, "other": 0.2},
    )
    sure = LabelEvidence(code_text="B-2288-0", confidence=0.99, check_digit_ok=True)
    unsure = LabelEvidence(code_text="B-2288-0", confidence=0.55, check_digit_ok=True)

    strong = belief_mod.label_likelihood(sure, candidates, RELIABILITY)
    weak = belief_mod.label_likelihood(unsure, candidates, RELIABILITY)
    assert strong["B-2288"] >= weak["B-2288"]


def test_a_belief_survives_evidence_that_explains_nothing():
    """Every candidate scoring near zero must not produce nan or a flat crash."""
    from agent.candidates import Candidate, CandidateSet

    candidates = CandidateSet(
        candidates=[Candidate("B-1"), Candidate("B-2"), Candidate(None)],
        prior={"B-1": 0.5, "B-2": 0.3, "other": 0.2},
    )
    belief = belief_mod.start(candidates)
    for _ in range(50):
        belief = belief_mod.update(
            belief,
            "hopeless",
            "nothing explains this",
            {"B-1": 1e-12, "B-2": 1e-12, "other": 1e-12},
        )
    assert sum(belief.probability.values()) == pytest.approx(1.0)
    assert all(math.isfinite(p) for p in belief.probability.values())


@pytest.mark.parametrize("quantity", [1, 7, 84, 500, 5000])
def test_the_agent_stays_sane_at_any_size(quantity):
    """The quantity scales the harm but must not break the arithmetic.

    A single unit is not worth an analyst; five thousand is worth almost any
    amount of checking. Both have to produce a real decision.
    """
    bench = build_bench(SCENARIOS["S4"])
    bench.intake.quantity = quantity
    result = loop.run(
        bench.intake, BenchServices(bench), COSTS, RELIABILITY, notes.CassetteNoteReader()
    )
    assert result.escalated or result.placement is not None
    assert sum(result.belief.probability.values()) == pytest.approx(1.0)
    for decision in result.trace.decisions:
        for option in decision.options:
            assert math.isfinite(option["expected_cost_gbp"])
            assert option["expected_cost_gbp"] >= 0.0


def test_it_never_pays_for_information_and_then_gives_up():
    """Buying a lookup and escalating anyway is money for nothing.

    It is the obvious way for a value-of-information calculation to be wrong, and
    it would be invisible in the outcome - the return still reaches a person, and
    the only trace is 30p that bought nothing.
    """
    from harness import generate

    wasted = 0.0
    bought = 0
    for seed in range(400):
        case = generate.build(seed)
        result = loop.run(
            case.intake,
            generate.GeneratedServices(case),
            COSTS,
            RELIABILITY,
            generate.FixedNoteReader(case.note),
        )
        if result.spend_gbp > 0:
            bought += 1
            if result.escalated:
                wasted += result.spend_gbp
    assert bought > 0, "no case bought a lookup, so this proves nothing"
    assert wasted == 0.0, f"£{wasted:.2f} spent on lookups that changed nothing"


@pytest.mark.parametrize("quantity", [200, 400, 1000])
def test_a_large_return_is_never_filed_without_checking(quantity):
    """More at stake must never mean less care.

    Above about a hundred units on the hero case the agent stops paying for the
    lookup - not because it has become careless, but because a person now costs
    less than the risk left over *after* the lookup, so buying it would change
    nothing. That is defensible. Committing without checking would not be, and
    this pins the difference.
    """
    bench = build_bench(SCENARIOS["S4"])
    bench.intake.quantity = quantity
    result = loop.run(
        bench.intake, BenchServices(bench), COSTS, RELIABILITY, notes.CassetteNoteReader()
    )
    if result.spend_gbp == 0.0:
        assert (
            result.escalated
            or result.placement is None
            or (result.placement.chosen.action.kind is not Kind.COMMIT)
        ), "filed a large return under a batch without buying the evidence to justify it"


def test_a_person_is_charged_for_the_same_mistake_as_the_agent():
    """A wrong batch breaks traceability whoever filed it.

    Leaving the misattribution charge off escalation made the identical mistake
    cheaper when a person made it, which tilted every close call towards handing
    the work over.
    """
    from agent.harm import MISATTRIBUTION_UNIT
    from agent.policy import escalate_cost

    cost = escalate_cost(COSTS, 400)
    assert cost.exposure.get(MISATTRIBUTION_UNIT, 0.0) > 0.0


# --------------------------------------------------------------- the screen


def test_the_demo_screen_renders_every_pane():
    """The page itself runs, not just the data behind it.

    `panels.py` is tested above, but a Streamlit script can still fail on the
    rendering: a chart handed a frame of the wrong shape, a column that does not
    exist. That failure would only appear while filming.
    """
    pytest.importorskip("streamlit", reason="the demo extra is not installed")
    from streamlit.testing.v1 import AppTest

    app_path = Path(__file__).resolve().parent.parent / "demo" / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=120).run()

    assert not app.exception, [str(e) for e in app.exception]
    assert not app.error, [e.value for e in app.error]

    panes = {s.value for s in app.subheader}
    assert {"The carton", "What it might be", "What this decision costs later"} <= panes
    assert any(s.startswith("Decision:") for s in panes), "no cost table on screen"
    assert "How the policy does overall" in panes, (
        "the whole-operation figures must be a separate, labelled section - next to a single "
        "return they read as a claim about that return"
    )

    labels = {m.label for m in app.metric}
    assert {"Units returned", "Reader confidence", "Filed as", "Really was"} <= labels


def test_the_screen_opens_on_the_case_the_video_is_about():
    """The hero case has to be first, so the film does not open on a menu."""
    assert panels.available()[0] == "S4"


# ------------------------------------------------- cases nobody wrote


@pytest.mark.parametrize("seed", [7, 52, 418, 1001])
def test_a_case_can_be_generated_on_the_spot(seed):
    """The demo must be able to show a case nobody wrote.

    The twelve recorded cases can only ever show that the agent handles what it
    was built while looking at. A case drawn from a seed has a fresh warehouse,
    fresh shipments and a random fault on each source.
    """
    screen = panels.build_generated(seed, COSTS, RELIABILITY)
    assert screen.generated
    assert screen.faults, "the screen must say how the case was built"
    assert screen.image_path_is_absent()
    assert screen.outcome
    assert len(screen.ledger_rows) == 2
    assert sum(screen.belief.final.values()) == pytest.approx(1.0)


def test_a_generated_case_is_allowed_to_show_the_agent_failing():
    """Seed 418 is the residual class: a reused label and a stale replica, so
    nothing anywhere names the true batch. The demo showing this is the point -
    an interface that could only display successes would be worth nothing."""
    screen = panels.build_generated(418, COSTS, RELIABILITY)
    assert screen.consequence.obvious_answer_was_wrong
    assert screen.consequence.expiry_error_days is not None


def test_generated_cases_are_reproducible():
    """Same seed, same case, so a demo can be rehearsed."""
    a = panels.build_generated(99, COSTS, RELIABILITY)
    b = panels.build_generated(99, COSTS, RELIABILITY)
    assert a.outcome == b.outcome
    assert a.belief.final == b.belief.final
    assert a.faults == b.faults


def test_the_screen_offers_both_kinds_of_case():
    pytest.importorskip("streamlit", reason="the demo extra is not installed")
    from streamlit.testing.v1 import AppTest

    app_path = Path(__file__).resolve().parent.parent / "demo" / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=180).run()
    options = app.sidebar.radio[0].options
    assert len(options) == 2

    app.sidebar.radio[0].set_value(options[1]).run()
    assert not app.exception, [str(e) for e in app.exception]
    panes = {s.value for s in app.subheader}
    assert {"The carton", "What it might be", "What this decision costs later"} <= panes
    assert any("made up a moment ago" in i.value for i in app.info)


# ------------------------------------------------------- the documentation


def test_the_readme_describes_the_world_that_exists():
    """Documentation drifts silently. The countable claims are checked."""
    from world import generators

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    world = generators.build_world()
    assert f"{len(world.batches)} batches" in readme
    assert f"{len(world.bins)} bins" in readme
    assert f"{len(world.customers)} customers" in readme
    assert f"{len(world.shipments)} shipments and {len(world.returns)} return events" in readme
    assert f"{len(SCENARIOS)} cases" in readme


def test_the_guide_lists_seeds_that_do_what_it_says():
    """The demo guide points at particular seeds. They have to still behave."""
    failing = panels.build_generated(418, COSTS, RELIABILITY)
    assert failing.consequence.obvious_answer_was_wrong, "seed 418 is the honest failure"

    held = panels.build_generated(52, COSTS, RELIABILITY)
    assert held.outcome.startswith("segregate"), "seed 52 is the hold-it-back case"

    escalated = panels.build_generated(24, COSTS, RELIABILITY)
    assert escalated.outcome.startswith("escalated"), "seed 24 is the large-return escalation"
    assert escalated.carton.quantity > 400

    decided = panels.build_generated(7, COSTS, RELIABILITY)
    assert decided.outcome.startswith("commit"), "seed 7 is the large return it does decide"
    assert decided.carton.quantity > 500
    assert not decided.consequence.obvious_answer_was_wrong


def test_every_case_the_guide_describes_exists():
    guide = (Path(__file__).resolve().parent.parent / "demo" / "GUIDE.md").read_text()
    for scenario_id in ("S1", "S4", "S8"):
        assert scenario_id in guide
    assert set(SCENARIOS) == set(IDS)


# ----------------------------------------- the model's contribution is real


def test_a_lot_code_written_in_prose_changes_what_the_agent_believes():
    """The one thing here a database query cannot do.

    A note saying "inner cases stamped B-2296" is prose. No `SELECT` will find
    it. The model reads it out, code checks the batch is real, and it becomes a
    candidate with evidence behind it.

    S7 is the case built around this, and the true batch is named *nowhere else*.
    """
    from agent.notes import NoteFacts

    bench = build_bench(SCENARIOS["S7"])
    event = next(r for r in bench.world.returns if r.return_id == bench.intake.return_id)
    facts = notes.CassetteNoteReader().read(bench.intake.condition_note)
    assert event.true_batch_id in facts.batch_codes_mentioned, "the note must name the truth"

    class Blind:
        """The same note, with anything the model extracted taken away."""

        def read(self, note: str) -> NoteFacts:
            return facts.model_copy(update={"batch_codes_mentioned": []})

    with_note = loop.run(
        bench.intake, BenchServices(bench), COSTS, RELIABILITY, notes.CassetteNoteReader()
    )
    without = loop.run(bench.intake, BenchServices(bench), COSTS, RELIABILITY, Blind())

    truth = event.true_batch_id
    assert with_note.belief.of(truth) > without.belief.of(truth) * 3, (
        f"the extracted lot code barely moved the belief: "
        f"{without.belief.of(truth):.3f} -> {with_note.belief.of(truth):.3f}"
    )
    assert with_note.belief.best()[0] == truth, "the note should make the truth the leader"


def test_the_model_earns_its_place_on_cases_nobody_wrote():
    """Guards against this feature going inert again.

    It has been dead twice. First the code did not exist at all. Then it existed,
    ran, produced a candidate - and changed no decision anywhere, because nothing
    ever treated the extracted code as *evidence* for the batch it named, and the
    one function that would have was gated behind a paid lookup.

    A test that only checks the candidate appears would have passed throughout.
    This one checks it changes what the agent does.
    """
    from harness import generate

    class Blind:
        def __init__(self, facts):
            self.facts = facts.model_copy(update={"batch_codes_mentioned": []})

        def read(self, note):
            return self.facts

    had = changed = rescued = broke = 0
    for seed in range(600):
        case = generate.build(seed)
        if not case.note.batch_codes_mentioned:
            continue
        had += 1
        services = generate.GeneratedServices(case)
        with_note = loop.run(
            case.intake, services, COSTS, RELIABILITY, generate.FixedNoteReader(case.note)
        )
        without = loop.run(case.intake, services, COSTS, RELIABILITY, Blind(case.note))
        if with_note.assigned_batch != without.assigned_batch:
            changed += 1
            if with_note.assigned_batch == case.truth:
                rescued += 1
            elif without.assigned_batch == case.truth:
                broke += 1

    assert had >= 5, f"only {had} generated cases had a lot code in the note"
    assert changed > 0, "the extracted lot code changed no decision anywhere: it is dead weight"
    assert rescued > broke, f"it got {rescued} right and {broke} wrong"


def test_a_lookup_fee_is_the_fee_and_not_the_whole_cost():
    """The screen shows both, and they mean different things.

    A gather action's LinearCost carries the fee *plus* what we expect to spend
    afterwards, so the two are on the same scale as committing. Reporting that
    sum as "the fee" showed a 30p lookup costing £28.
    """
    from agent.harm import load_costs

    prices = load_costs().prices
    screen = panels.build("S4", COSTS, RELIABILITY)
    gathers = [o for d in screen.decisions for o in d.options if o.action.startswith("gather")]
    assert gathers, "S4 must offer at least one lookup"
    for option in gathers:
        tools = option.action.removeprefix("gather ").split("+")
        assert option.fee_gbp == pytest.approx(sum(prices[t] for t in tools))
        assert option.fee_gbp < option.expected_cost_gbp

    terminal = [o for d in screen.decisions for o in d.options if not o.action.startswith("gather")]
    assert all(o.fee_gbp == 0.0 for o in terminal), "only a lookup costs anything to take"


@pytest.mark.parametrize("quantity", [0, -1, -84])
def test_a_return_of_nothing_or_less_is_refused(quantity):
    """A negative quantity flips the sign of every harm term.

    The cheapest action then becomes the most damaging one, and the agent picks
    it confidently. Found by probing edge cases: at −5 units the hero case filed
    the stock as the batch the label claimed — the answer the whole project
    exists to reject — because the arithmetic had been turned upside down.

    Rejected at the intake boundary rather than at the ledger, because by the
    time the ledger sees it the decision has already been made.
    """
    from pydantic import ValidationError

    from agent.evidence import ReturnIntake

    with pytest.raises(ValidationError):
        ReturnIntake(
            return_id="RET-X",
            customer_id="CUST-118",
            sku_id="SKU-4471",
            quantity=quantity,
            arrived=date(2026, 8, 15),
        )


# ------------------------------------------- stepping through the evidence


@pytest.mark.parametrize("scenario_id", IDS)
def test_every_case_can_be_replayed_one_source_at_a_time(scenario_id, screens):
    """The brief asks for competing strategies chosen at runtime.

    A final screenshot cannot show that - it shows one winner and no contest.
    These frames are the contest: the belief and the priced options after each
    piece of evidence.
    """
    screen = screens[scenario_id]
    assert len(screen.frames) == len(screen.belief.steps) + 1, "a frame per step, plus the prior"

    first, last = screen.frames[0], screen.frames[-1]
    assert first.name == "before any evidence"
    assert not first.likelihood, "the prior is not evidence"
    assert last.probability == pytest.approx(screen.belief.final)

    for frame in screen.frames:
        assert sum(frame.probability.values()) == pytest.approx(1.0)
        assert frame.ranking, "every step must offer something to choose between"
        cheapest = min(o.expected_cost_gbp for o in frame.ranking)
        picked = next(o for o in frame.ranking if o.chosen)
        assert picked.expected_cost_gbp <= cheapest + 1e-6
        assert sum(o.chosen for o in frame.ranking) == 1


def test_the_hero_case_shows_the_answer_changing_as_evidence_arrives():
    """The single most useful thing the step-through does.

    On S4 the leader changes three times: the prior favours the batch the label
    claims, the records overturn it, the clean label pulls it *back*, and only
    the paid lookup settles it. That is what "genuinely competing strategies"
    looks like, and a static view of the final state hides all of it.
    """
    screen = panels.build("S4", COSTS, RELIABILITY)
    leaders = [f.leader[0] for f in screen.frames]
    changes = sum(1 for a, b in zip(leaders, leaders[1:], strict=False) if a != b)
    assert changes >= 3, f"expected the leader to change repeatedly, got {leaders}"

    actions = [f.best_action for f in screen.frames]
    assert actions[0] != actions[-1], "the best action must change as evidence arrives"
    assert actions[-1].startswith("commit"), "it should end up willing to file the stock"


def test_paid_evidence_is_marked_as_paid():
    """Which steps cost money is part of the argument, not decoration."""
    screen = panels.build("S4", COSTS, RELIABILITY)
    free = [f.name for f in screen.frames if not f.needed_a_lookup]
    paid = [f.name for f in screen.frames if f.needed_a_lookup]
    assert "records" in free and "label" in free
    assert paid, "the hero case buys a lookup, so some steps must be marked paid"


def test_the_screen_can_step_through_evidence():
    pytest.importorskip("streamlit", reason="the demo extra is not installed")
    from streamlit.testing.v1 import AppTest

    app_path = Path(__file__).resolve().parent.parent / "demo" / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=180).run()
    app.sidebar.checkbox[0].set_value(True).run()
    assert not app.exception, [str(e) for e in app.exception]

    slider = app.sidebar.slider[0]
    assert slider.max == len(panels.build("S4", COSTS, RELIABILITY).frames) - 1

    seen_leader_change = False
    seen_action_change = False
    for step in range(int(slider.max) + 1):
        app.sidebar.slider[0].set_value(step).run()
        assert not app.exception, [str(e) for e in app.exception]
        seen_leader_change |= any("most likely answer just changed" in w.value for w in app.warning)
        seen_action_change |= any("best action just changed" in s.value for s in app.success)
    assert seen_leader_change, "the hero case must visibly change its mind on screen"
    assert seen_action_change, "and must visibly change what it would do"


def test_a_print_date_never_favours_the_batch_nobody_named():
    """Found by looking at the step-through on screen.

    The catch-all used to be left at "no information", which is 1.0 — higher than
    the 0.55 a batch whose manufacture date actually matches gets. So a print
    date came out as evidence *for* "some batch nobody named" over the very batch
    it identified, and the screen showed `other` as the best explanation of it.
    """
    from agent.notes import P_DATE_MATCHES, NoteFacts, note_date_likelihood
    from services.scenarios import build_bench

    bench = build_bench(SCENARIOS["S4"])
    facts = notes.CassetteNoteReader().read(bench.intake.condition_note)
    assert facts.print_dates, "S4's note must carry a print date"

    result = loop.run(
        bench.intake, BenchServices(bench), COSTS, RELIABILITY, notes.CassetteNoteReader()
    )
    registry = BenchServices(bench).buy_registry(
        [c.batch_id for c in result.belief.candidates.candidates if c.batch_id]
    )
    likelihood = note_date_likelihood(facts, result.belief.candidates, registry)

    best = max(likelihood, key=lambda k: likelihood[k])
    assert best != "other", (
        f"the catch-all explains the print date best, which is backwards: {likelihood}"
    )
    assert likelihood[best] == pytest.approx(P_DATE_MATCHES)

    # And when nothing known matches, the date *is* evidence the answer is unlisted.
    nothing_matches = note_date_likelihood(
        NoteFacts(print_dates=[date(1999, 1, 1)]), result.belief.candidates, registry
    )
    assert nothing_matches["other"] == pytest.approx(P_DATE_MATCHES)
    assert nothing_matches["other"] > nothing_matches[best]


# ------------------------------------------------------- the instructions


def test_every_command_the_readme_gives_actually_exists():
    """Instructions rot faster than code. These are checked, not trusted."""
    import importlib
    import re

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    modules = set(re.findall(r"uv run python -m ([a-z_.]+)", readme))
    assert modules, "the README should tell somebody how to run this"
    for name in sorted(modules):
        importlib.import_module(name)


def test_the_single_case_runner_works_both_ways():
    """`demo.run` is the answer to "how do I run the agent?", so it has to work."""
    from demo import run as runner

    recorded = panels.build("S4", COSTS, RELIABILITY)
    generated = panels.build_generated(418, COSTS, RELIABILITY)
    for screen in (recorded, generated):
        runner.show(screen)  # must not raise on either kind of case
        assert screen.frames


def test_the_readme_names_the_only_command_that_needs_a_key():
    """Everything else is offline, and somebody should be able to tell which is which."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "record_readings" in readme
    section = readme[readme.index("## 8. Running it") :]
    assert "NEEDS AN API KEY" in section
