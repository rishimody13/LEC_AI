"""The demo screen.

    uv run streamlit run demo/app.py

Four panes, no scrolling and no tab-switching while filming. All the arithmetic
happens in `demo/panels.py`, which has no Streamlit in it, so what appears here
is checkable without a browser - see tests/test_end_to_end.py.

Runs offline. Label readings come from the recorded cassettes and the harm
figures from artifacts/harm.json.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.harm import load_costs
from agent.reliability import load_reliability
from demo import panels

st.set_page_config(page_title="RECONCILE", layout="wide")

COSTS = load_costs()
RELIABILITY = load_reliability()


@st.cache_data(show_spinner=False)
def screen_for(scenario_id: str) -> panels.Screen:
    return panels.build(scenario_id, COSTS, RELIABILITY)


@st.cache_data(show_spinner=False)
def generated_screen(seed: int, calibrated: bool) -> panels.Screen:
    return panels.build_generated(seed, COSTS, RELIABILITY, calibrated=calibrated)


def carton_pane(carton: panels.CartonPanel) -> None:
    st.subheader("The carton")
    if carton.image_path and Path(carton.image_path).exists():
        st.image(carton.image_path, width="stretch")
    else:
        st.caption(
            "No photograph for a generated case. The reading below is constructed and put "
            "through the same validation code; perception is covered by the twelve recorded "
            "cases."
        )
    st.caption(carton.description)

    left, right = st.columns(2)
    left.metric("Units returned", carton.quantity)
    right.metric("Reader confidence", f"{carton.confidence:.0%}")

    check = {True: "valid", False: "FAILED", None: "not checked"}[carton.check_digit_ok]
    st.write(f"**Code read:** `{carton.code_read or 'nothing legible'}` — check digit {check}")
    if carton.symptoms:
        st.write("**Warning signs:** " + ", ".join(carton.symptoms))
    st.caption(carton.reader_note)

    if carton.looks_trustworthy:
        st.info(
            "This label is crisp, complete and passes its check digit. Everything about it "
            "says trust it.",
            icon="🎯",
        )


def belief_pane(belief: panels.BeliefPanel) -> None:
    st.subheader("What it might be")
    frame = pd.DataFrame([{"after": s.name, **s.probability} for s in belief.steps]).set_index(
        "after"
    )
    st.bar_chart(frame.tail(1).T, horizontal=True)

    name, probability = belief.leader
    st.write(f"**Most likely:** `{name}` at {probability:.1%}")
    if belief.flat:
        st.warning("No candidate is clearly ahead. This is real ambiguity, not a bug.", icon="⚖️")
    st.caption("Probability after each piece of evidence:")
    st.dataframe(frame.style.format("{:.3f}"), width="stretch")


def cost_pane(decision: panels.CostPanel) -> None:
    st.subheader(f"Decision: {decision.name}")
    frame = pd.DataFrame(
        [
            {
                "action": o.action,
                "expected cost £": o.expected_cost_gbp,
                "fee £": o.fee_gbp,
                "chosen": "✔" if o.chosen else "",
            }
            for o in sorted(decision.options, key=lambda o: o.expected_cost_gbp)
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)
    st.write(f"**Chose** `{decision.chosen}`, ahead by £{decision.margin_gbp:.2f}")

    if decision.fragile:
        st.warning(
            "A close call. At this margin the answer rests on a chosen cost figure rather "
            "than on the evidence, and the log says so.",
            icon="⚠️",
        )
    for note in decision.notes:
        st.caption(note)
    if decision.sensitivity:
        st.caption("How far a cost would have to move to change this:")
        st.dataframe(pd.DataFrame(decision.sensitivity), width="stretch", hide_index=True)


def consequence_pane(consequence: panels.ConsequencePanel) -> None:
    st.subheader("What it costs later")

    left, right = st.columns(2)
    left.metric("Filed as", consequence.assigned_batch or "not filed")
    right.metric("Really was", consequence.true_batch)

    if consequence.obvious_answer_was_wrong:
        st.error(
            f"Filed under the wrong batch. Recorded expiry is off by "
            f"{consequence.expiry_error_days} days.",
            icon="🚨",
        )
    elif consequence.assigned_batch:
        st.success("Correct batch, so no drift at all.", icon="✅")

    st.caption(consequence.drift.summary())

    harm = consequence.harm
    if not harm:
        st.caption("Run `uv run python -m harness.counterfactual 600` for the simulation.")
        return

    st.write(f"**Over {harm['seeds']} simulated runs of {harm['days']} days each:**")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "policy": name,
                    "expired units shipped": round(v["expired_units_shipped_per_run"], 1),
                    "£ per run": round(v["cost_gbp_per_run"]),
                }
                for name, v in harm["policies"].items()
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    against = harm["agent_against"].get("trust the label")
    if against:
        e = against["expired_units"]
        st.metric(
            "Expired units avoided against trusting the label",
            f"{-e['mean']:.0f} per run",
            help=f"95% interval [{-e['high']:.0f}, {-e['low']:.0f}], paired on every seed",
        )


def main() -> None:
    started = time.perf_counter()
    st.title("RECONCILE — a returned carton, and who to believe")

    mode = st.sidebar.radio(
        "Where the case comes from",
        ["Recorded (12 written by hand)", "Generated now (nobody wrote it)"],
        index=0,
    )
    if mode.startswith("Recorded"):
        scenario_id = st.sidebar.selectbox("Case", panels.available(), index=0)
        screen = screen_for(scenario_id)
    else:
        seed = st.sidebar.number_input("Seed", min_value=0, max_value=999_999, value=418, step=1)
        calibrated = st.sidebar.checkbox(
            "World matches the reliability model",
            value=True,
            help=(
                "Unticked, faults happen at rates the agent does not believe. Failures then "
                "mean the beliefs are wrong, not the reasoning."
            ),
        )
        screen = generated_screen(int(seed), calibrated)
        st.sidebar.caption(f"`{screen.faults}`")
        if st.sidebar.button("Another one"):
            st.rerun()

    if screen.generated:
        st.info(
            "This case was made up a moment ago from a seed: a fresh warehouse, fresh "
            "shipments, and a random fault on each source. Nobody wrote it, so nobody could "
            "have tuned the agent to it. The answer is known only to the scoring code.",
            icon="🎲",
        )

    st.sidebar.metric("Spent on lookups", f"£{screen.spend_gbp:.2f}")
    st.sidebar.write(f"**Outcome:** {screen.outcome}")
    if screen.bought:
        st.sidebar.write("**Bought:** " + ", ".join(screen.bought))
    st.sidebar.caption(f"Built in {time.perf_counter() - started:.1f}s, entirely offline.")

    top_left, top_right = st.columns(2)
    with top_left:
        carton_pane(screen.carton)
    with top_right:
        belief_pane(screen.belief)

    bottom_left, bottom_right = st.columns(2)
    with bottom_left:
        for decision in screen.decisions:
            cost_pane(decision)
    with bottom_right:
        consequence_pane(screen.consequence)
        with st.expander("Stock movements this decision caused"):
            st.dataframe(pd.DataFrame(screen.ledger_rows), width="stretch", hide_index=True)


main()
