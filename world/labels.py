"""Draws carton labels, then damages them.

The damage is real image damage, not a flag on a data structure. Anything reading
these images has to cope with actual glare, blur and missing characters.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .types import ReturnEvent

W, H = 640, 400
_PAPER = (247, 245, 240)
_INK = (18, 18, 20)

# What shows through where the label has been torn away: the kraft carton beneath.
_CARTON = (171, 138, 97)
_CARTON_DARK = (138, 108, 72)
_CARTON_LIGHT = (194, 163, 124)
#: Torn paper exposes pale fibres along the break.
_FIBRE = (253, 251, 247)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


@dataclass(frozen=True)
class Damage:
    """How badly a label is damaged, and where.

    ``target`` picks which part of the batch code the damage lands on:
    ``"none"``, ``"all"``, ``"check"`` (the trailing check digit only), or
    ``"tail"`` (the last body digit and the check digit).
    """

    name: str
    blur: float = 0.0
    glare: float = 0.0
    smear: int = 0
    occlude: float = 0.0
    target: str = "none"
    noise: float = 0.0
    #: Rip a piece of the label away, exposing the carton underneath. The rip runs
    #: from the right edge inwards, stopping at the start of the code's tail.
    tear: bool = False
    tear_top: int = 132
    tear_bottom: int = 262
    #: Index of a single character to blot with ink, leaving the rest legible.
    #: This is corruption rather than destruction: the code still reads as a
    #: code, it just reads as the wrong one.
    blot_index: int | None = None


CLEAN = Damage(name="clean")

WATER_DAMAGE = Damage(name="water_damage", blur=4.2, smear=7, occlude=0.94, target="all", noise=20)
"""Case S2 - the code is gone. Nothing recoverable."""

HEAVY_GLARE = Damage(
    name="heavy_glare", glare=1.0, blur=2.6, occlude=0.72, target="code_and_date", noise=10
)
"""Case S3 - reflected light wipes out both the batch code and the best-before date,
so the label offers nothing that identifies the batch."""

PARTIAL_GLARE = Damage(name="partial_glare", glare=0.8, blur=1.5, target="tail", noise=6)
"""Case S5 - most of the code survives, the end is doubtful."""

TORN_PIECE = Damage(name="torn_piece", tear=True, target="tail", noise=2)
"""Case S6 - a piece of the label has been ripped off, taking the last body digit, the
check digit and the best-before date with it. The code reads ``B-229``, which fits both
B-2290 and B-2291, and both the check digit and the date that would settle it are gone."""

INK_BLOT = Damage(name="ink_blot", blot_index=5, noise=3)
"""Case S8 - a blot of ink fills the hole in one digit. The code stays perfectly
legible; it just no longer says what was printed. This is the brief's *corrupted*
metadata, as opposed to the unreadable kind."""

PROFILES: dict[str, Damage] = {
    d.name: d for d in [CLEAN, WATER_DAMAGE, HEAVY_GLARE, PARTIAL_GLARE, TORN_PIECE, INK_BLOT]
}


def _code_box(
    draw: ImageDraw.ImageDraw, code: str, xy: tuple[int, int], font
) -> tuple[int, int, int, int]:
    left, top, right, bottom = draw.textbbox(xy, code, font=font)
    return int(left), int(top), int(right), int(bottom)


def _target_region(
    box: tuple[int, int, int, int], code: str, target: str
) -> tuple[int, int, int, int]:
    """Slice of the code's bounding box that the damage should cover."""
    left, top, right, bottom = box
    width = right - left
    per_char = width / max(len(code), 1)
    if target == "all":
        return left - 8, top - 8, right + 8, bottom + 8
    if target == "code_and_date":
        # Covers the batch code and the best-before line beneath it, so neither
        # can be used to identify the batch.
        return left - 8, top - 8, right + 60, bottom + 95
    if target == "check":
        # Final character only.
        return int(right - per_char * 1.15), top - 6, right + 10, bottom + 6
    if target == "tail":
        # Last body digit, the separating dash and the check digit.
        return int(right - per_char * 3.1), top - 6, right + 10, bottom + 6
    return left, top, left, top


def render(
    ret: ReturnEvent,
    damage: Damage = CLEAN,
    *,
    out_dir: Path | str = "artifacts/labels",
    seed: int = 0,
) -> Path:
    """Render one carton label and return the path it was written to."""
    rng = random.Random(f"{ret.return_id}:{damage.name}:{seed}")
    img = Image.new("RGB", (W, H), _PAPER)
    draw = ImageDraw.Draw(img)

    f_small = _font(17)
    f_mid = _font(23)
    f_code = _font(46)

    draw.rectangle([12, 12, W - 12, H - 12], outline=_INK, width=3)
    draw.line([12, 74, W - 12, 74], fill=_INK, width=2)

    draw.text((30, 32), "NUTRIPLUS INFANT FORMULA 800G", font=f_mid, fill=_INK)
    draw.text((30, 92), f"SKU  {ret.sku_id}", font=f_small, fill=_INK)
    draw.text((30, 118), "LOT / BATCH", font=f_small, fill=_INK)

    code_xy = (30, 146)
    draw.text(code_xy, ret.printed_code, font=f_code, fill=_INK)
    box = _code_box(draw, ret.printed_code, code_xy, f_code)

    bb = ret.printed_best_before.strftime("%d %b %Y").upper()
    draw.text((30, 226), f"BEST BEFORE  {bb}", font=f_mid, fill=_INK)
    draw.text((30, 270), f"CONSIGNEE  {ret.customer_id}", font=f_small, fill=_INK)
    draw.text((30, 296), "STORE BELOW 25C - DO NOT FREEZE", font=f_small, fill=_INK)

    # Fake barcode so the label reads as a real label rather than a text box.
    x = 30
    while x < W - 60:
        bar = rng.choice([2, 2, 3, 5])
        draw.rectangle([x, 330, x + bar, 372], fill=_INK)
        x += bar + rng.choice([3, 4, 6])

    img = _apply_damage(img, damage, box, ret.printed_code, rng)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ret.return_id}_{damage.name}.png"
    img.save(path)
    return path


def _rip_edge(x_nominal: float, y0: int, y1: int, rng: random.Random) -> list[tuple[float, float]]:
    """The ragged line where the paper gave way.

    Two wavelengths of wobble plus per-point jitter, so the edge looks torn rather
    than cut. Clamped so it never eats into the characters we mean to keep.
    """
    pts: list[tuple[float, float]] = []
    steps = 46
    phase = rng.uniform(0, math.tau)
    for i in range(steps + 1):
        t = i / steps
        y = y0 + (y1 - y0) * t
        wobble = 15 * math.sin(t * math.pi * 1.6 + phase) + 7 * math.sin(t * math.pi * 6.1 + phase)
        x = x_nominal + wobble + rng.uniform(-3.5, 3.5)
        # Never intrude on the characters to the left of the rip.
        x = max(x_nominal - 2.0, x)
        pts.append((x, y))
    return pts


def _jittered_run(
    x_from: float, x_to: float, y: float, rng: random.Random, amp: float = 5.0
) -> list[tuple[float, float]]:
    """A roughly horizontal torn edge from one x to another."""
    pts: list[tuple[float, float]] = []
    steps = 26
    for i in range(steps + 1):
        t = i / steps
        x = x_from + (x_to - x_from) * t
        pts.append((x, y + rng.uniform(-amp, amp) + 3 * math.sin(t * math.pi * 4.3)))
    return pts


def _blot(
    img: Image.Image,
    index: int,
    box: tuple[int, int, int, int],
    code: str,
    rng: random.Random,
) -> None:
    """Drop a blot of ink into one character, closing its open shape."""
    left, top, right, bottom = box
    per_char = (right - left) / max(len(code), 1)
    x0 = left + per_char * index
    cx = x0 + per_char * 0.52
    cy = (top + bottom) / 2

    draw = ImageDraw.Draw(img)
    # Fill only the counter - the hole in the middle of the glyph - so the outer
    # strokes survive and the character still reads as a digit, just a different
    # one. Covering the whole glyph would make it unreadable, which is a
    # different failure and one the other profiles already cover.
    draw.ellipse([cx - 4.0, cy - 8, cx + 4.0, cy + 8], fill=_INK)
    for _ in range(10):
        sx = cx + rng.uniform(-5.5, 5.5)
        sy = cy + rng.uniform(-10, 10)
        r = rng.uniform(0.6, 1.5)
        draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=_INK)


def _tear(
    img: Image.Image,
    damage: Damage,
    box: tuple[int, int, int, int],
    code: str,
    rng: random.Random,
) -> Image.Image:
    """Rip away the right-hand part of the label and show the carton beneath."""
    left, _, right, _ = box
    per_char = (right - left) / max(len(code), 1)
    # Start the rip where the code's tail begins, so "B-229" survives.
    x_nominal = right - per_char * 3.0 + 6
    y0, y1 = damage.tear_top, damage.tear_bottom

    edge = _rip_edge(x_nominal, y0, y1, rng)
    # Walk the hole anticlockwise: down the rip, right along the bottom, up the
    # right-hand side (off-image), then back left along the top.
    bottom = _jittered_run(edge[-1][0], W + 10, y1, rng)
    top = _jittered_run(edge[0][0], W + 10, y0, rng)
    polygon = [*edge, *bottom, *reversed(top)]

    # Build the carton as its own layer and stencil it through the hole, so the
    # texture cannot leak outside the torn area.
    carton = Image.new("RGB", (W, H), _CARTON)
    cdraw = ImageDraw.Draw(carton)
    # A few faint creases in the kraft. Wavy and low contrast, so the exposed
    # carton reads as board rather than printed stripes.
    for _ in range(4):
        fy = rng.randrange(y0, y1)
        drift = rng.uniform(-0.02, 0.02)
        crease = [(x, fy + 3 * math.sin(x * 0.013 + fy) + x * drift) for x in range(0, W, 8)]
        cdraw.line(crease, fill=_CARTON_DARK, width=1)
    cpx = carton.load()
    assert cpx is not None
    for _ in range(W * H // 4):
        x, y = rng.randrange(W), rng.randrange(H)
        r, g, b = cpx[x, y]
        d = rng.randint(-14, 14)
        cpx[x, y] = (
            max(0, min(255, r + d)),
            max(0, min(255, g + d)),
            max(0, min(255, b + d)),
        )

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(polygon, fill=255)
    img.paste(carton, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # Shadow of the label's own thickness, falling onto the carton just inside the rip.
    shadow = [(x + 3, y + 2) for x, y in edge]
    draw.line(shadow, fill=_CARTON_DARK, width=5)
    draw.line([(x + 2, y + 2) for x, y in top], fill=_CARTON_DARK, width=4)
    draw.line([(x + 2, y - 2) for x, y in bottom], fill=_CARTON_DARK, width=4)

    # Pale fibres exposed along the break, on the paper side of the line.
    draw.line(edge, fill=_FIBRE, width=2)
    draw.line(top, fill=_FIBRE, width=2)
    draw.line(bottom, fill=_FIBRE, width=2)

    # Individual fibres straddling the edge.
    for _ in range(90):
        x, y = rng.choice(edge + top + bottom)
        length = rng.uniform(2, 7)
        angle = rng.uniform(-0.9, 0.9)
        draw.line(
            [(x, y), (x + length * math.cos(angle), y + length * math.sin(angle))],
            fill=rng.choice([_FIBRE, _CARTON_LIGHT]),
            width=1,
        )

    return img


def _apply_damage(
    img: Image.Image,
    damage: Damage,
    box: tuple[int, int, int, int],
    code: str,
    rng: random.Random,
) -> Image.Image:
    if damage.name == "clean":
        return img

    if damage.blot_index is not None:
        _blot(img, damage.blot_index, box, code, rng)

    if damage.tear:
        img = _tear(img, damage, box, code, rng)

    region = _target_region(box, code, damage.target)
    draw = ImageDraw.Draw(img, "RGBA")

    if damage.occlude > 0:
        # Torn or soaked paper: irregular pale patch that removes the ink.
        x0, y0, x1, y1 = region
        alpha = int(255 * min(1.0, damage.occlude))
        pts = []
        steps = 14
        for i in range(steps):
            t = i / (steps - 1)
            pts.append((x0 + (x1 - x0) * t, y0 - rng.randint(0, 7)))
        for i in range(steps):
            t = i / (steps - 1)
            pts.append((x1 - (x1 - x0) * t, y1 + rng.randint(0, 7)))
        draw.polygon(pts, fill=(250, 248, 243, alpha))

    if damage.glare > 0:
        x0, y0, x1, y1 = region
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = (x1 - x0) * 0.75, (y1 - y0) * 1.5
        layers = 9
        for i in range(layers):
            t = (i + 1) / layers
            # Bright in the middle, falling away at the edges.
            a = int(255 * damage.glare * (1 - t**2))
            draw.ellipse(
                [cx - rx * t, cy - ry * t, cx + rx * t, cy + ry * t],
                fill=(255, 255, 255, a),
            )

    if damage.smear:
        img = img.filter(ImageFilter.BoxBlur(damage.smear))

    if damage.blur:
        img = img.filter(ImageFilter.GaussianBlur(damage.blur))

    if damage.noise:
        px = img.load()
        assert px is not None
        n = int(damage.noise)
        for _ in range(W * H // 12):
            x, y = rng.randrange(W), rng.randrange(H)
            r, g, b = px[x, y]
            d = rng.randint(-n, n)
            px[x, y] = (
                max(0, min(255, r + d)),
                max(0, min(255, g + d)),
                max(0, min(255, b + d)),
            )

    return img


def render_all(
    returns: list[ReturnEvent],
    assignment: dict[str, str],
    *,
    out_dir: Path | str = "artifacts/labels",
) -> dict[str, Path]:
    """Render one label per return, using ``assignment`` to pick a damage profile."""
    paths: dict[str, Path] = {}
    for ret in returns:
        profile = PROFILES[assignment.get(ret.return_id, "clean")]
        paths[ret.return_id] = render(ret, profile, out_dir=out_dir)
    return paths


#: Which damage profile each test case uses.
SCENARIO_DAMAGE: dict[str, str] = {
    "RET-S1": "clean",
    "RET-S2": "water_damage",
    "RET-S3": "heavy_glare",
    "RET-S4": "clean",
    "RET-S5": "partial_glare",
    "RET-S6": "torn_piece",
    "RET-S7": "water_damage",
    "RET-S8": "ink_blot",
}


def date_str(d: date) -> str:
    return d.strftime("%d %b %Y").upper()
