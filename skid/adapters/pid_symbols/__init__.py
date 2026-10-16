"""Original P&ID symbol geometry for the pump-skid prototype.

Every symbol here was drawn for this project from plain SVG primitives. None is
copied or traced from ISA-5.1, ISO 10628, or another symbol library, and no
conformance with a drafting standard is claimed.

Coordinates are local drawing units with +x right and +y down. An occurrence
places them with ``translate(x y) rotate(angle)``, as ``skid.templates`` does
for diagram anchors. Port anchors are not defined here: validation, commands,
and drawing all take them from the templates. Each outline passes through its
template's anchors, which lie on the boundary of ``bbox``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FALLBACK_SYMBOL", "SYMBOLS", "SymbolSpec"]

_INK = "#111111"
_PAPER = "#ffffff"


@dataclass(frozen=True)
class SymbolSpec:
    """Drawing primitives and layout hints for one equipment kind."""

    primitives: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    bbox: tuple[float, float, float, float]  # local (x0, y0, x1, y1)
    attach: str  # "circle" (radius about the origin) or "box" (the placed bbox)
    radius: float = 0.0
    inner_text_width: float = 0.0  # room for the tag inside the outline; 0 keeps it outside
    label_sides: tuple[str, ...] = ("below", "above", "right", "left")


def _outline(d: str, fill: str = _PAPER) -> tuple[str, tuple[tuple[str, str], ...]]:
    return ("path", (("d", d), ("fill", fill), ("stroke", _INK), ("stroke-width", "1.4")))


def _stroke(d: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    return ("path", (("d", d), ("fill", "none"), ("stroke", _INK), ("stroke-width", "1.4")))


def _circle(r: float, fill: str = _PAPER) -> tuple[str, tuple[tuple[str, str], ...]]:
    return (
        "circle",
        (("cx", "0"), ("cy", "0"), ("r", f"{r:g}"), ("fill", fill), ("stroke", _INK), ("stroke-width", "1.4")),
    )


SYMBOLS = {
    # Vertical vessel with dished heads; outlet anchor (40, 20) on the right wall.
    "tank": SymbolSpec(
        primitives=(_outline("M -40 -40 A 40 12 0 0 1 40 -40 L 40 48 A 40 12 0 0 1 -40 48 Z"),),
        bbox=(-40, -52, 40, 60),
        attach="box",
        inner_text_width=70,
        label_sides=("below", "left", "right", "above"),
    ),
    # Casing circle on a base; the inner wedge points to the top discharge.
    # Anchors: inlet (-28, 0) and outlet (0, -28) on the casing.
    "centrifugal_pump": SymbolSpec(
        primitives=(
            _stroke("M -16 23 L -24 36 L 24 36 L 16 23"),
            _circle(28),
            _stroke("M -11 9 L 0 -13 L 11 9 Z"),
        ),
        bbox=(-28, -28, 28, 36),
        attach="circle",
        radius=28,
        label_sides=("below", "right", "left", "above"),
    ),
    # Bow tie with a hand-operated stem; anchors (-16, 0) and (16, 0).
    "isolation_valve": SymbolSpec(
        primitives=(
            _outline("M -16 -9 L 16 9 L 16 -9 L -16 9 Z"),
            _stroke("M 0 0 L 0 -15 M -7 -15 L 7 -15"),
        ),
        bbox=(-16, -15, 16, 9),
        attach="box",
    ),
    # Bow tie with the outlet half filled and a flow arrow; anchors (-16, 0) and (16, 0).
    "check_valve": SymbolSpec(
        primitives=(
            _outline("M -16 -9 L 0 0 L -16 9 Z"),
            _outline("M 16 -9 L 0 0 L 16 9 Z", fill=_INK),
            _stroke("M -9 -15 L 5 -15"),
            _outline("M 4 -18 L 9 -15 L 4 -12 Z", fill=_INK),
        ),
        bbox=(-16, -18, 16, 9),
        attach="box",
    ),
    # Plain bubble for a conceptual indication; no ports.
    "indicator": SymbolSpec(
        primitives=(_circle(18),),
        bbox=(-18, -18, 18, 18),
        attach="circle",
        radius=18,
        inner_text_width=30,
        label_sides=("right", "left", "above", "below"),
    ),
    # Flag marking where the modeled scope ends; inlet anchor (-18, 0).
    "boundary": SymbolSpec(
        primitives=(_outline("M -18 -10 L 8 -10 L 18 0 L 8 10 L -18 10 Z"),),
        bbox=(-18, -10, 18, 10),
        attach="box",
    ),
}

# Drawn for a kind that has a template but no symbol yet; the adapter reports it.
FALLBACK_SYMBOL = SymbolSpec(
    primitives=(
        (
            "rect",
            (
                ("x", "-20"),
                ("y", "-15"),
                ("width", "40"),
                ("height", "30"),
                ("fill", _PAPER),
                ("stroke", _INK),
                ("stroke-width", "1.4"),
                ("stroke-dasharray", "4 2"),
            ),
        ),
        _stroke("M -6 -6 L 6 6 M 6 -6 L -6 6"),
    ),
    bbox=(-20, -15, 20, 15),
    attach="box",
)
