"""How often each evidence source is wrong, given the warning signs we can see.

An overall accuracy figure is no use here. What matters is: given that this image
is clean, the check digit passed, and the customer repacks goods, how often is the
label still wrong? So counts are kept per symptom bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from .evidence import LabelEvidence, LabelSymptom, RecordEvidence, RecordSymptom

RELIABILITY_PATH = Path("config/reliability.yaml")


class LabelState(StrEnum):
    """What the label channel is doing."""

    OK = "ok"
    MISREAD = "misread"
    WRONG_LABEL = "wrong_label"


class RecordState(StrEnum):
    """What the warehouse records are doing."""

    OK = "ok"
    INCOMPLETE = "incomplete"
    CORRUPTED = "corrupted"


@dataclass(frozen=True)
class Bucket:
    """Counts of how a source turned out, for one set of warning signs."""

    name: str
    counts: dict[str, int]

    def rates(self) -> dict[str, float]:
        """Chance of each state. Smoothed so a thin bucket cannot claim certainty."""
        total = sum(self.counts.values()) + len(self.counts)
        return {state: (n + 1) / total for state, n in self.counts.items()}

    @property
    def observations(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class ReliabilityModel:
    label: dict[str, Bucket]
    records: dict[str, Bucket]

    def label_states(self, evidence: LabelEvidence) -> dict[LabelState, float]:
        bucket = self.label[label_bucket(evidence)]
        return {LabelState(k): v for k, v in bucket.rates().items()}

    def record_states(self, evidence: RecordEvidence) -> dict[RecordState, float]:
        bucket = self.records[record_bucket(evidence)]
        return {RecordState(k): v for k, v in bucket.rates().items()}

    def label_bucket_name(self, evidence: LabelEvidence) -> str:
        return label_bucket(evidence)

    def record_bucket_name(self, evidence: RecordEvidence) -> str:
        return record_bucket(evidence)


def label_bucket(e: LabelEvidence) -> str:
    """Pick the bucket that matches what we can see on this label.

    Order matters: the most specific failure wins.
    """
    if not e.available or LabelSymptom.NO_CODE_FOUND in e.symptoms:
        return "no_code_found"
    if LabelSymptom.CHECK_DIGIT_FAILED in e.symptoms:
        return "check_digit_failed"
    if LabelSymptom.INCOMPLETE_CODE in e.symptoms:
        return "incomplete_code"
    if LabelSymptom.LOW_CONFIDENCE in e.symptoms:
        return "low_confidence"
    # Clean and valid. The only thing left that predicts trouble is who sent it.
    if LabelSymptom.REPACKING_CONSIGNEE in e.symptoms:
        return "clean_valid_repacker"
    return "clean_valid_standard"


def record_bucket(e: RecordEvidence) -> str:
    if RecordSymptom.CONFLICTING_ROWS in e.symptoms:
        return "conflicting_rows"
    if RecordSymptom.REPLICA_LAG in e.symptoms:
        return "replica_lag"
    if RecordSymptom.NO_MATCH in e.symptoms:
        return "no_match"
    if RecordSymptom.MULTIPLE_MATCHES in e.symptoms:
        return "fresh_multiple_matches"
    return "fresh_single_match"


def load_reliability(path: Path | str = RELIABILITY_PATH) -> ReliabilityModel:
    raw = yaml.safe_load(Path(path).read_text())
    return ReliabilityModel(
        label={name: Bucket(name, counts) for name, counts in raw["label"].items()},
        records={name: Bucket(name, counts) for name, counts in raw["records"].items()},
    )
