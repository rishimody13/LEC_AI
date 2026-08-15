"""The agent must not be able to see the answers.

Every harm number we report depends on the agent working only from evidence the
services hand it. If ``agent/`` could import ``world/`` it could read
``true_batch_id`` directly and the whole measurement would be meaningless.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {"world"}


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        # level > 0 means a relative import, which cannot reach world/
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_agent_package_cannot_import_ground_truth():
    offenders: list[str] = []
    for path in sorted((ROOT / "agent").rglob("*.py")):
        bad = _imported_top_level_modules(path) & FORBIDDEN
        if bad:
            offenders.append(f"{path.relative_to(ROOT)} imports {sorted(bad)}")
    assert not offenders, "agent/ must not import ground truth:\n" + "\n".join(offenders)


def test_the_check_itself_works():
    """Guard against the test silently passing because it parses nothing."""
    sample = ROOT / "world" / "generators.py"
    assert "world" not in _imported_top_level_modules(sample)  # uses relative imports
    assert _imported_top_level_modules(ROOT / "tests" / "test_world.py") & {"world"}
