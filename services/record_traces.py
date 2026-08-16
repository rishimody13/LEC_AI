"""Write the committed decision logs.

    uv run python -m services.record_traces

One JSON file per case in ``artifacts/traces/``, plus the stock movements each
decision produced. These files are the evidence for several of the requirements,
so they have to be regenerable rather than something written once by hand and
left to go stale.

Runs offline. The label readings come from the recorded cassettes.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import loop, notes
from agent.harm import load_costs
from agent.reliability import load_reliability
from ledger import posting
from ledger.ledger import Ledger
from services.adapter import BenchServices
from services.scenarios import build_bench, load_scenarios

OUT = Path("artifacts/traces")


def main() -> int:
    scenarios = load_scenarios()
    costs = load_costs()
    reliability = load_reliability()
    note_reader = notes.CassetteNoteReader()
    OUT.mkdir(parents=True, exist_ok=True)

    for scenario_id in sorted(scenarios):
        bench = build_bench(scenarios[scenario_id])
        result = loop.run(
            bench.intake, BenchServices(bench), costs, reliability, note_reader, scenario_id
        )

        book = Ledger()
        posting.post(book, bench.intake, result)

        payload = result.trace.to_dict()
        payload["stock_movements"] = [
            {
                "seq": m.seq,
                "at": m.at.isoformat(),
                "kind": str(m.kind),
                "quantity": m.quantity,
                "from": str(m.source),
                "to": str(m.destination),
                "caused_by": m.decision,
                "reason": m.reason,
            }
            for m in book.movements()
        ]
        path = OUT / f"{scenario_id}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"{scenario_id:18} {result.trace.outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
