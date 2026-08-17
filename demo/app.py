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


def step_panes(screen: panels.Screen, index: int) -> None:
    """The belief and the cost table as they stood after one piece of evidence."""
    frame = screen.frames[index]
    previous = screen.frames[index - 1] if index else None

    st.subheader("What it might be")
    st.caption(f"**Step {frame.index} of {len(screen.frames) - 1}** — after {frame.name}")
    st.write(frame.detail)

    order = sorted(frame.probability, key=lambda k: -frame.probability[k])
    st.bar_chart(pd.DataFrame({"probability": frame.probability}).loc[order], horizontal=True)

    name, probability = frame.leader
    if previous and previous.leader[0] != name:
        st.warning(
            f"The most likely answer just changed: **{previous.leader[0]} → {name}**. "
            f"Nothing was reconsidered — one new piece of evidence moved it.",
            icon="🔄",
        )
    st.write(f"**Most likely:** `{name}` at {probability:.1%}")

    if frame.says_nothing:
        st.caption(
            "This evidence explains every candidate equally well, so it does not separate "
            "them and the probabilities above are unchanged."
        )
    elif frame.likelihood:
        order = sorted(frame.likelihood, key=lambda k: -frame.likelihood[k])
        relative = frame.relative_likelihood
        st.caption(
            "**How well each candidate explains what we just saw** — not a probability of the "
            "candidate, and deliberately not adding to 1. These are probabilities of the "
            "*evidence*, one per hypothesis, so only the ratios in the last column matter."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "candidate": name,
                        "explains it": round(frame.likelihood[name], 4),
                        "against the best": (
                            "best" if relative[name] >= 1.0 else f"{1 / relative[name]:.0f}x worse"
                        ),
                    }
                    for name in order
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    if frame.needed_a_lookup:
        st.info("This evidence had to be paid for. It is not free to know.", icon="💷")


def step_cost_pane(screen: panels.Screen, index: int) -> None:
    frame = screen.frames[index]
    previous = screen.frames[index - 1] if index else None

    st.subheader("If it had to place the stock now")
    st.caption(
        "Every way of finishing with this return, priced with only the evidence up to this "
        "step. This is not quite the question the agent asks first — that one also weighs "
        "whether to buy more evidence — but it is priced with the same code."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "action": o.action,
                    "expected cost £": o.expected_cost_gbp,
                    "chosen": "✔" if o.chosen else "",
                }
                for o in frame.ranking
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    if previous and previous.best_action != frame.best_action:
        st.success(
            f"**The best action just changed:** `{previous.best_action}` → "
            f"`{frame.best_action}`. Genuinely competing strategies, decided at runtime.",
            icon="⚡",
        )
    else:
        st.write(f"**Would choose** `{frame.best_action}` at £{frame.best_cost_gbp:.2f}")


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
    st.subheader("What this decision costs later")

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

    # Everything below is about the policy as a whole, not the case on screen.
    # It is the same for every case, and saying so is the difference between a
    # figure and a misleading one.
    st.divider()
    st.subheader("How the policy does overall")
    st.caption(
        f"**Not about this return.** These are whole-operation figures from "
        f"{harm['seeds']} simulated runs of {harm['days']} days each, and they are identical "
        f"whichever case you are looking at above."
    )
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
            "Expired units avoided per simulated run, agent vs trusting the label",
            f"{-e['mean']:.0f}",
            help=(
                f"95% interval [{-e['high']:.0f}, {-e['low']:.0f}], paired on every seed. "
                f"A whole-operation average over {harm['seeds']} runs, not this return."
            ),
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

    stepping = st.sidebar.checkbox(
        "Step through the evidence",
        value=False,
        help=(
            "Replay the case one source at a time and watch which action is ahead after each. "
            "On the hero case the most likely answer changes three times."
        ),
    )
    index = 0
    if stepping:
        index = st.sidebar.slider(
            "Evidence applied",
            0,
            len(screen.frames) - 1,
            len(screen.frames) - 1,
            format="step %d",
        )
        st.sidebar.caption(f"after **{screen.frames[index].name}**")

    top_left, top_right = st.columns(2)
    with top_left:
        carton_pane(screen.carton)
    with top_right:
        if stepping:
            step_panes(screen, index)
        else:
            belief_pane(screen.belief)

    bottom_left, bottom_right = st.columns(2)
    with bottom_left:
        if stepping:
            step_cost_pane(screen, index)
        else:
            for decision in screen.decisions:
                cost_pane(decision)
    with bottom_right:
        consequence_pane(screen.consequence)
        with st.expander("Stock movements this decision caused"):
            st.dataframe(pd.DataFrame(screen.ledger_rows), width="stretch", hide_index=True)


main()
