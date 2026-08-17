"""The no-default-branch demonstration, for the video.

    uv run python -m demo.flip

Same case, same evidence, one cost figure changed. The chosen action changes with
it. That is the whole claim behind "there is no `else` branch": the action is not
written into the code, it falls out of the arithmetic over a priced list.

`test_agent.py::test_no_default_branch` asserts the same thing. This exists so it
can be *shown* rather than asserted, in one command, on camera.
"""

from __future__ import annotations

from agent import loop, notes
from agent.harm import CostModel, load_costs
from agent.reliability import load_reliability
from services.adapter import BenchServices
from services.scenarios import build_bench, load_scenarios

CASE = "S4"


def outcome(costs: CostModel) -> str:
    bench = build_bench(load_scenarios()[CASE])
    result = loop.run(
        bench.intake,
        BenchServices(bench),
        costs,
        load_reliability(),
        notes.CassetteNoteReader(),
    )
    return result.trace.outcome


def main() -> int:
    base = load_costs()
    print(f"case {CASE}: identical evidence every time, one cost figure moved\n")

    rows = [
        ("as shipped: an analyst costs £8.53", base),
        (
            "an analyst costs £4.00",
            CostModel(**{**base.__dict__, "human_review": 4.0}),
        ),
        (
            "a wrong batch costs £20/unit, not £0.96",
            CostModel(**{**base.__dict__, "misattribution_unit": 20.0}),
        ),
    ]
    for label, costs in rows:
        print(f"  {label:42} -> {outcome(costs)}")

    print(
        "\nMake a person cheaper, or being wrong dearer, and it stops deciding and hands the\n"
        "return over. Nothing about the evidence changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
