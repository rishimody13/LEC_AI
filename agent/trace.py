"""The decision log.

Everything the agent weighed, in a form a reviewer can check without rerunning
anything. Each decision records every option it considered, what each would cost,
which one won, by how much, and how wrong a cost figure would have to be to
change the answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .belief import Belief
from .policy import Choice


@dataclass
class DecisionRecord:
    name: str
    belief: dict[str, float]
    options: list[dict[str, Any]]
    chosen: str
    margin_gbp: float
    margin_share: float
    fragile: bool
    sensitivity: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Trace:
    return_id: str
    scenario_id: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    spend_gbp: float = 0.0
    outcome: str = ""
    notes: list[str] = field(default_factory=list)

    def add_evidence(self, name: str, detail: str, **fields: Any) -> None:
        self.evidence.append({"source": name, "detail": detail, **fields})

    def add_decision(self, name: str, belief: Belief, choice: Choice) -> None:
        if not choice.sensitivity and choice.runner_up is not None:
            self.notes.append(
                f"{name}: no single cost figure can be moved to a sensible value "
                f"that would change this. The decision does not rest on the cost table."
            )
        if choice.is_fragile:
            share = choice.decisive_margin / choice.chosen.total * 100
            rival = choice.decisive_runner_up
            self.notes.append(
                f"{name}: only £{choice.decisive_margin:.2f} ({share:.1f}%) ahead of "
                f"'{rival.action.label if rival else 'nothing'}', which would lead "
                f"somewhere different. A decision this close rests on a chosen "
                f"parameter rather than on the evidence."
            )
        self.decisions.append(
            DecisionRecord(
                name=name,
                belief={k: round(v, 4) for k, v in belief.probability.items()},
                options=[
                    {
                        "action": o.action.label,
                        "expected_cost_gbp": round(o.total, 2),
                        "exposure": {k: round(v, 3) for k, v in o.cost.exposure.items()},
                        "fee_gbp": round(o.fee, 2),
                    }
                    for o in sorted(choice.options, key=lambda o: o.total)
                ],
                chosen=choice.chosen.action.label,
                margin_gbp=round(choice.margin, 2) if choice.margin != float("inf") else -1.0,
                margin_share=round(choice.margin_share, 4)
                if choice.margin_share != float("inf")
                else -1.0,
                fragile=choice.is_fragile,
                sensitivity=[
                    {
                        "parameter": s.parameter,
                        "now_gbp": round(s.current, 2),
                        "flips_at_gbp": round(s.flips_at, 2),
                        "slack_x": round(s.slack, 1),
                    }
                    for s in choice.sensitivity[:3]
                ],
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "return_id": self.return_id,
            "scenario_id": self.scenario_id,
            "evidence": self.evidence,
            "decisions": [
                {
                    "name": d.name,
                    "belief": d.belief,
                    "options": d.options,
                    "chosen": d.chosen,
                    "margin_gbp": d.margin_gbp,
                    "margin_share": d.margin_share,
                    "fragile": d.fragile,
                    "sensitivity": d.sensitivity,
                }
                for d in self.decisions
            ],
            "spend_gbp": round(self.spend_gbp, 2),
            "outcome": self.outcome,
            "notes": self.notes,
        }

    def write(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return p
