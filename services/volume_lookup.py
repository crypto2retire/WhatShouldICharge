"""
Post-AI volume reconciliation: correct known small items from a lookup table,
then distribute remaining cubic yards (from spatial math in notes) across
bulk/debris line items so totals stay honest.
"""

from __future__ import annotations

import copy
import re
from typing import Callable, Optional

# Per-unit cubic yards for standardized items (calibrated to field / lookup values).
def _is_five_gallon_bucket(n: str) -> bool:
    if "bucket" not in n:
        return False
    return (
        "5-gallon" in n
        or "5 gallon" in n
        or "five gallon" in n
        or "5gal" in n.replace(" ", "")
    )


_LOOKUP_RULES: list[tuple[Callable[[str], bool], float]] = [
    (_is_five_gallon_bucket, 0.025),
    (lambda n: "pallet" in n, 0.15),
    (
        lambda n: ("cardboard" in n or "card board" in n) and "box" in n,
        0.15,
    ),
    (
        lambda n: "tarp" in n
        or "sheeting" in n
        or ("plastic" in n and "sheet" in n),
        0.03,
    ),
]

# Do not reallocate volume onto named furniture / fixtures.
_REDIST_EXCLUDE_SUBSTRINGS = (
    "couch",
    "sofa",
    "sectional",
    "loveseat",
    "mattress",
    "bed frame",
    "bedframe",
    "dresser",
    "nightstand",
    "headboard",
    "refrigerator",
    "fridge",
    "freezer",
    "washer",
    "washing machine",
    "dryer",
    "dishwasher",
    "stove",
    "oven",
    "range",
    "microwave",
    "television",
    " tv",
    "flat screen",
    "desk",
    "chair",
    "table",
    "bookshelf",
    "bookcase",
    "cabinet",
    "armoire",
    "ottoman",
    "recliner",
    "entertainment center",
    "treadmill",
    "elliptical",
    "grill",
    "lawn mower",
    "bike",
    "bicycle",
)

_REDIST_KEYWORDS = (
    "debris",
    "rubble",
    "scrap",
    "miscellaneous",
    "misc.",
    "misc ",
    "construction debris",
    "demolition",
    "pile",
    "bulk",
    "mixed",
    "assorted",
    "leftover",
    "left over",
    "junk pile",
    "trash pile",
    "garbage pile",
    "lumber pile",
    "wood pile",
    "remnants",
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _lookup_cy_per_unit(norm_name: str) -> Optional[float]:
    for pred, cy in _LOOKUP_RULES:
        try:
            if pred(norm_name):
                return cy
        except Exception:
            continue
    return None


def _parse_spatial_total_cy(notes: str) -> Optional[float]:
    if not notes or not str(notes).strip():
        return None
    text = str(notes)
    # Prefer the last "= … CY" in chains like: … = 11.9 CY × 0.7 packing = 8.3 CY
    eq_matches = re.findall(
        r"=\s*(\d+(?:\.\d+)?)\s*(?:CY|cubic\s*yards?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if eq_matches:
        return float(eq_matches[-1])
    tail_matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:CY|cubic\s*yards?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if tail_matches:
        return float(tail_matches[-1])
    return None


def _target_total_cy(data: dict) -> float:
    notes = data.get("notes", "")
    parsed = _parse_spatial_total_cy(notes if isinstance(notes, str) else "")
    if parsed is not None and parsed > 0:
        return parsed
    if isinstance(data.get("total_cubic_yards"), (int, float)):
        v = float(data["total_cubic_yards"])
        if v > 0:
            return v
    totals = data.get("totals") or {}
    if isinstance(totals, dict):
        mid = totals.get("cubic_yards_mid")
        if isinstance(mid, (int, float)) and mid > 0:
            return float(mid)
    return 0.0


def _is_redistributable(item: dict, norm_name: str) -> bool:
    if any(ex in norm_name for ex in _REDIST_EXCLUDE_SUBSTRINGS):
        return False
    if item.get("category") == "debris":
        return True
    return any(k in norm_name for k in _REDIST_KEYWORDS)


def validate_estimate(result_data: dict) -> dict:
    """
    Reconcile per-item CY against a trusted total (spatial math in notes, or
    total_cubic_yards, or totals.cubic_yards_mid).

    Expected keys (WSIC shape):
      - items: [{ name, cubic_yards, quantity, category?, ... }]
      - totals: { cubic_yards_low, cubic_yards_mid, cubic_yards_high }
      - notes: str (spatial reasoning; last '= X CY' wins when present)

    Also accepts optional legacy keys:
      - total_cubic_yards
      - price_range: { low, high } — passed through unchanged
    """
    if not isinstance(result_data, dict):
        return result_data

    out = copy.deepcopy(result_data)
    items = out.get("items")
    if not isinstance(items, list) or not items:
        return out

    target = _target_total_cy(out)
    if target <= 0:
        return out

    # Classify rows and apply lookup CY per unit
    lookup_flags: list[bool] = []
    redist_flags: list[bool] = []
    for raw in items:
        if not isinstance(raw, dict):
            lookup_flags.append(False)
            redist_flags.append(False)
            continue
        name = _norm(str(raw.get("name", "")))
        lookup_flags.append(_lookup_cy_per_unit(name) is not None)
        redist_flags.append(_is_redistributable(raw, name))

    def line_volume(item: dict) -> float:
        try:
            cy = float(item.get("cubic_yards") or 0)
            qty = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, cy) * max(1, qty)

    # Apply lookup volumes
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if not lookup_flags[i]:
            continue
        name = _norm(str(item.get("name", "")))
        cy_unit = _lookup_cy_per_unit(name)
        if cy_unit is not None:
            item["cubic_yards"] = round(cy_unit, 4)
            item["volume_lookup_applied"] = True

    known_lookup_vol = 0.0
    for i, item in enumerate(items):
        if isinstance(item, dict) and lookup_flags[i]:
            known_lookup_vol += line_volume(item)

    other_vol = 0.0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if lookup_flags[i]:
            continue
        if redist_flags[i]:
            continue
        other_vol += line_volume(item)

    remaining = target - known_lookup_vol - other_vol

    redist_indices = [
        i
        for i in range(len(items))
        if isinstance(items[i], dict) and redist_flags[i] and not lookup_flags[i]
    ]

    if remaining < 0 or not redist_indices:
        # No bulk rows to absorb remainder, or overshoot — keep lookup fixes only
        _sync_totals_from_target(out, target)
        return out

    weights = []
    for i in redist_indices:
        w = line_volume(items[i])
        weights.append(max(w, 1e-6))

    total_w = sum(weights)
    for j, i in enumerate(redist_indices):
        item = items[i]
        qty = int(item.get("quantity") or 1)
        qty = max(1, qty)
        share = (weights[j] / total_w) * remaining
        item["cubic_yards"] = round(share / qty, 4)
        item["volume_redistributed"] = True

    _sync_totals_from_target(out, target)
    return out


def _sync_totals_from_target(out: dict, target: float) -> None:
    totals = out.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    low = totals.get("cubic_yards_low")
    high = totals.get("cubic_yards_high")
    mid = totals.get("cubic_yards_mid")
    try:
        mid_f = float(mid) if mid is not None else target
    except (TypeError, ValueError):
        mid_f = target
    if mid_f and mid_f > 0:
        ratio_lo = float(low) / mid_f if low is not None else 0.85
        ratio_hi = float(high) / mid_f if high is not None else 1.15
        ratio_lo = max(0.5, min(ratio_lo, 1.0))
        ratio_hi = max(1.0, min(ratio_hi, 1.5))
    else:
        ratio_lo, ratio_hi = 0.85, 1.15
    totals["cubic_yards_mid"] = round(target, 2)
    totals["cubic_yards_low"] = round(target * ratio_lo, 2)
    totals["cubic_yards_high"] = round(target * ratio_hi, 2)
    out["totals"] = totals
    out["total_cubic_yards"] = round(target, 2)
