"""Run the agent on one return and show its working, in the terminal.

    uv run python -m demo.run S4          # one of the twelve recorded cases
    uv run python -m demo.run --seed 418  # a case generated on the spot
    uv run python -m demo.run --list

The Streamlit screen shows the same thing with pictures. This needs no extra
dependencies, fits in a terminal recording, and is the quickest way to see what
the agent actually did on a case without reading a JSON trace.

Everything runs offline.
"""

from __future__ import annotations

import argparse

from agent.harm import load_costs
from agent.reliability import load_reliability
from demo import panels

BAR = "#"


def _bars(probability: dict[str, float], width: int = 32) -> list[str]:
    out = []
    for name in sorted(probability, key=lambda k: -probability[k]):
        share = probability[name]
        out.append(f"    {name:<10} {BAR * round(share * width):<{width}} {share:6.1%}")
    return out


def show(screen: panels.Screen) -> None:
    carton = screen.carton
    print(f"\n{carton.title}  ({carton.scenario_id})")
    print("=" * 72)
    print(carton.description)
    print()
    print(f"  {carton.quantity} units back from {carton.customer_id}")
    check = {True: "valid", False: "FAILED", None: "not checked"}[carton.check_digit_ok]
    print(
        f"  label read : {carton.code_read or 'nothing legible'}  "
        f"({carton.confidence:.0%} confident, check digit {check})"
    )
    if carton.symptoms:
        print(f"  warnings   : {', '.join(carton.symptoms)}")
    if carton.condition_note:
        print(f"  note       : {carton.condition_note}")

    print("\nWhat it might be, one piece of evidence at a time")
    print("-" * 72)
    for frame in screen.frames:
        paid = "  (this one had to be paid for)" if frame.needed_a_lookup else ""
        print(f"\n  step {frame.index}: after {frame.name}{paid}")
        print(f"    {frame.detail}")
        for line in _bars(frame.probability):
            print(line)
        if frame.says_nothing:
            print("    (explains every candidate equally well, so it separates nothing)")
        print(f"    would place it: {frame.best_action}  at £{frame.best_cost_gbp:.2f}")

    print("\nDecisions")
    print("-" * 72)
    for decision in screen.decisions:
        print(f"\n  {decision.name}")
        for option in sorted(decision.options, key=lambda o: o.expected_cost_gbp):
            mark = "->" if option.chosen else "  "
            fee = f"  (fee £{option.fee_gbp:.2f})" if option.fee_gbp else ""
            print(f"   {mark} {option.action:<40} £{option.expected_cost_gbp:>9,.2f}{fee}")
        print(
            f"      chosen by £{decision.margin_gbp:.2f}"
            + ("  [close call]" if decision.fragile else "")
        )

    result = screen.consequence
    print("\nOutcome")
    print("-" * 72)
    print(f"  {screen.outcome}")
    print(f"  spent on lookups: £{screen.spend_gbp:.2f}")
    print(f"  filed as {result.assigned_batch or 'not filed'}, really {result.true_batch}")
    if result.obvious_answer_was_wrong:
        print(f"  WRONG: recorded expiry is off by {result.expiry_error_days} days")
    print(f"  {result.drift.summary()}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?", default="S4", help="a recorded case id, e.g. S4")
    parser.add_argument("--seed", type=int, help="generate a case nobody wrote instead")
    parser.add_argument(
        "--miscalibrated",
        action="store_true",
        help="with --seed: draw faults at rates the agent does not believe",
    )
    parser.add_argument("--list", action="store_true", help="list the recorded cases")
    args = parser.parse_args()

    if args.list:
        for name in panels.available():
            print(name)
        return 0

    costs, reliability = load_costs(), load_reliability()
    if args.seed is not None:
        screen = panels.build_generated(
            args.seed, costs, reliability, calibrated=not args.miscalibrated
        )
        print(f"\ngenerated from seed {args.seed}: {screen.faults}")
    else:
        if args.case not in panels.available():
            parser.error(f"unknown case {args.case!r}; try --list")
        screen = panels.build(args.case, costs, reliability)
    show(screen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
