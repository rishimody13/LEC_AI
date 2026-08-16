"""Reads the batch code off a carton photo.

This is the second component that can fail on its own. Perception is done by a
vision model; every judgement that has to be checkable - is the code well formed,
does the check digit agree, which warning signs apply - is done here in plain code.

The reader can run from recorded readings (the default) so the demo and the tests
work offline and give the same answer every time.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from agent.evidence import LabelEvidence, LabelSymptom, ReturnIntake
from common.coding import check_digit_ok, is_well_formed

#: Below this, we treat the reading as doubtful.
LOW_CONFIDENCE = 0.70

CASSETTE_PATH = Path("tests/cassettes/label_readings.json")

PROMPT = """\
You are reading a carton label at a warehouse goods-in desk.

Report only what is actually legible in the image. Do not guess at characters you
cannot see, and do not infer a plausible code from context. If part of the batch
code is destroyed, missing or unreadable, report only the characters you can
genuinely make out and set code_complete to false.

- code_text: the characters of the LOT / BATCH code that are legible, in order.
  Use null if none of it can be read.
- code_complete: true only if you can read the entire code including the final
  digit after the last dash.
- confidence: 0 to 1, how sure you are of the characters you did report.
- best_before: the best-before date if legible, otherwise null.
- visual_condition: short tags for what you can see, from: clean, glare, blur,
  smudge, torn, water_damage, occluded, low_contrast.
- note: one sentence on the physical state of the label.
"""


class LabelReading(BaseModel):
    """Raw perception output. No judgements, just what was seen."""

    code_text: str | None = None
    code_complete: bool = False
    confidence: float = 0.0
    best_before: date | None = None
    visual_condition: list[str] = Field(default_factory=list)
    note: str = ""


class LabelReader(Protocol):
    def read(self, image_path: Path | str, intake: ReturnIntake) -> LabelEvidence: ...


def image_key(image_path: Path | str) -> str:
    """Content hash of the image. Changing the image invalidates its recording."""
    return hashlib.sha256(Path(image_path).read_bytes()).hexdigest()[:16]


def _condition_symptoms(tags: list[str]) -> set[LabelSymptom]:
    mapping = {
        "clean": LabelSymptom.CLEAN_IMAGE,
        "glare": LabelSymptom.GLARE,
        "blur": LabelSymptom.BLUR,
        "smudge": LabelSymptom.BLUR,
        "water_damage": LabelSymptom.BLUR,
        "torn": LabelSymptom.TORN,
        "occluded": LabelSymptom.OCCLUDED,
        "low_contrast": LabelSymptom.BLUR,
    }
    return {mapping[t] for t in tags if t in mapping}


def to_evidence(reading: LabelReading, intake: ReturnIntake) -> LabelEvidence:
    """Turn a raw reading into evidence, adding the checks the agent can rely on."""
    symptoms = _condition_symptoms(reading.visual_condition)

    code = reading.code_text or None
    well_formed = bool(code) and is_well_formed(code or "")

    if not code:
        symptoms.add(LabelSymptom.NO_CODE_FOUND)
        digit_ok: bool | None = None
    elif not reading.code_complete or not well_formed:
        # Nothing to test the check digit against.
        symptoms.add(LabelSymptom.INCOMPLETE_CODE)
        digit_ok = None
    else:
        digit_ok = check_digit_ok(code)
        if not digit_ok:
            symptoms.add(LabelSymptom.CHECK_DIGIT_FAILED)

    if reading.confidence < LOW_CONFIDENCE:
        symptoms.add(LabelSymptom.LOW_CONFIDENCE)

    if reading.best_before is None:
        symptoms.add(LabelSymptom.DATE_UNREADABLE)

    # Not visible in the image, but it is the single strongest reason a perfectly
    # legible label might still describe the wrong contents.
    if intake.consignee_repacks:
        symptoms.add(LabelSymptom.REPACKING_CONSIGNEE)

    return LabelEvidence(
        available=True,
        code_text=code,
        confidence=reading.confidence,
        well_formed=well_formed,
        check_digit_ok=digit_ok,
        best_before=reading.best_before,
        symptoms=symptoms,
        reader_note=reading.note,
    )


class CassetteLabelReader:
    """Replays readings recorded earlier. The default, so nothing needs a network."""

    def __init__(self, path: Path | str = CASSETTE_PATH) -> None:
        self._path = Path(path)
        self._readings: dict[str, LabelReading] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            self._readings = {k: LabelReading(**v) for k, v in raw.items()}

    def read(self, image_path: Path | str, intake: ReturnIntake) -> LabelEvidence:
        key = image_key(image_path)
        if key not in self._readings:
            raise KeyError(
                f"no recorded reading for {Path(image_path).name} (key {key}). "
                f"Re-record with: uv run python -m services.record_readings"
            )
        return to_evidence(self._readings[key], intake)


class UnavailableLabelReader:
    """The camera or the reader is down. Produces no evidence at all."""

    def read(self, image_path: Path | str, intake: ReturnIntake) -> LabelEvidence:
        return LabelEvidence(
            available=False,
            symptoms={LabelSymptom.READER_UNAVAILABLE},
            reader_note="label reader did not respond",
        )


#: Reading a damaged label is a narrow perception job, and the hard part is
#: restraint - not guessing at characters that are not there. Sonnet is the
#: default because it holds that line more reliably than Haiku on the degraded
#: images; swap in "claude-haiku-4-5" if recording cost matters more.
DEFAULT_VISION_MODEL = "claude-sonnet-5"


class ClaudeLabelReader:
    """Reads the image with Claude. Used to record cassettes; needs an API key."""

    def __init__(
        self, model: str = DEFAULT_VISION_MODEL, record_to: Path | str | None = None
    ) -> None:
        self._model = model
        self._record_to = Path(record_to) if record_to else None

    def read(self, image_path: Path | str, intake: ReturnIntake) -> LabelEvidence:
        return to_evidence(self.perceive(image_path), intake)

    def perceive(self, image_path: Path | str) -> LabelReading:
        import anthropic

        path = Path(image_path)
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")

        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=self._model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": data,
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
            output_format=LabelReading,
        )
        reading = response.parsed_output
        assert reading is not None
        if self._record_to is not None:
            self._append(path, reading)
        return reading

    def _append(self, path: Path, reading: LabelReading) -> None:
        assert self._record_to is not None
        existing: dict[str, object] = {}
        if self._record_to.exists():
            existing = json.loads(self._record_to.read_text())
        existing[image_key(path)] = json.loads(reading.model_dump_json())
        self._record_to.parent.mkdir(parents=True, exist_ok=True)
        self._record_to.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
