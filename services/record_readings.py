"""Re-records the label readings used by the offline reader.

Run this only when the label images change. It needs an Anthropic API key and
network access; everything else in the project runs from the recorded file.

    uv run python -m services.record_readings
"""

from __future__ import annotations

import sys
from pathlib import Path

from services.label_reader import CASSETTE_PATH, ClaudeLabelReader, image_key
from world.generators import build_world
from world.labels import SCENARIO_DAMAGE, render_all


def main() -> int:
    world = build_world()
    paths = render_all(world.returns, SCENARIO_DAMAGE)

    if CASSETTE_PATH.exists():
        CASSETTE_PATH.rename(CASSETTE_PATH.with_suffix(".json.bak"))
        print(f"previous recordings moved to {CASSETTE_PATH.with_suffix('.json.bak')}")

    reader = ClaudeLabelReader(record_to=CASSETTE_PATH)
    for return_id, path in sorted(paths.items()):
        reading = reader.perceive(path)
        code = reading.code_text or "<nothing legible>"
        print(
            f"{return_id}  {Path(path).name:32} key={image_key(path)}  "
            f"read={code!r} complete={reading.code_complete} conf={reading.confidence:.2f}"
        )

    print(f"\nwrote {CASSETTE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
