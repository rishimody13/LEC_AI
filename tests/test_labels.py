"""The damage has to be real damage, not a flag.

These tests measure ink left in the batch-code region of the rendered image, so a
profile that claims to destroy the code has to actually destroy it.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from world.generators import build_world
from world.labels import PROFILES, SCENARIO_DAMAGE, render

WORLD = build_world()
RETURNS = {r.return_id: r for r in WORLD.returns}

# Where the batch code sits on the rendered label.
CODE_REGION = (24, 140, 420, 205)


def _ink_mask(path) -> np.ndarray:
    """Pixels that are printed ink.

    Ink is dark *and* neutral in colour. The kraft carton exposed by a tear is also
    fairly dark but strongly warm, so testing brightness alone would count torn-away
    carton as legible text.
    """
    rgb = np.asarray(Image.open(path).convert("RGB")).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dark = rgb.mean(axis=2) < 110
    neutral = (np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)) < 40
    return dark & neutral


def _ink(path) -> float:
    """Fraction of ink pixels in the batch-code region. High means legible."""
    x0, y0, x1, y1 = CODE_REGION
    return float(_ink_mask(path)[y0:y1, x0:x1].mean())


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("labels")
    return {
        rid: render(RETURNS[rid], PROFILES[name], out_dir=out)
        for rid, name in SCENARIO_DAMAGE.items()
    }


def test_clean_labels_are_clearly_legible(rendered):
    for rid in ["RET-S1", "RET-S4"]:
        assert _ink(rendered[rid]) > 0.05, f"{rid} should be crisp"


def test_unreadable_cases_really_are_unreadable(rendered):
    for rid in ["RET-S2", "RET-S3"]:
        assert _ink(rendered[rid]) < 0.005, f"{rid} still has legible ink in the code region"


def test_torn_piece_keeps_the_start_and_loses_the_end(rendered):
    ink = _ink_mask(rendered["RET-S6"])
    start = ink[140:205, 24:200].mean()
    end = ink[140:205, 210:400].mean()
    assert start > 0.05, "the first characters should survive"
    assert end < 0.002, "the final digit and check digit should be gone"


def test_torn_piece_also_destroys_the_best_before_date(rendered):
    """The date alone would identify the batch, so the rip has to take it too."""
    date_value = _ink_mask(rendered["RET-S6"])[222:255, 215:400].mean()
    assert date_value < 0.002, "the best-before date is still legible, so S6 is not ambiguous"


def test_torn_piece_exposes_carton_underneath(rendered):
    """A tear shows the box beneath. A pale patch would just look like an erasure."""
    rgb = np.asarray(Image.open(rendered["RET-S6"]).convert("RGB")).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    # Kraft brown: clearly warm, and darker than the label stock.
    carton = (r > g + 18) & (g > b + 18) & (r > 120) & (r < 215)
    torn_band = carton[130:265, 170:640]
    assert torn_band.mean() > 0.5, "the torn area should be mostly exposed carton"
    # And it must not bleed outside the rip.
    assert carton[:120, :].mean() < 0.01
    assert carton[300:, :].mean() < 0.01


def test_torn_piece_has_a_ragged_edge(rendered):
    """A straight edge reads as a cut or a drawn box, not a tear."""
    rgb = np.asarray(Image.open(rendered["RET-S6"]).convert("RGB")).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    carton = (r > g + 18) & (g > b + 18) & (r > 120) & (r < 215)

    # Leftmost carton pixel on each row of the tear: should wander, not line up.
    boundaries = []
    for row in range(140, 255):
        cols = np.flatnonzero(carton[row])
        if cols.size:
            boundaries.append(int(cols[0]))
    assert len(boundaries) > 80
    assert np.std(boundaries) > 4.0, "the rip boundary is too straight to be a tear"


def test_partial_glare_degrades_without_erasing(rendered):
    clean = _ink(render(RETURNS["RET-S5"], PROFILES["clean"], out_dir=rendered["RET-S5"].parent))
    damaged = _ink(rendered["RET-S5"])
    assert damaged < clean, "glare should reduce legibility"
    assert damaged > 0.005, "S5 should stay partly readable"


def test_rendering_is_deterministic(tmp_path):
    a = render(RETURNS["RET-S3"], PROFILES["heavy_glare"], out_dir=tmp_path / "a")
    b = render(RETURNS["RET-S3"], PROFILES["heavy_glare"], out_dir=tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
