"""
Post-AI volume reconciliation: apply precise lookup values for standard items,
clamp AI estimates against a reference table of known CY ranges, remove phantom
misc items, and sync totals to the final bottom-up item sum.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Precise per-unit CY values for standardized items ──
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
    (lambda n: "railroad tie" in n or "railroad ties" in n, 0.17),
    (
        lambda n: "landscape timber" in n or "landscape timbers" in n,
        0.11,
    ),
]

# ── Synonyms for item matching (AI may use different names) ──
_ITEM_SYNONYMS: dict[str, str] = {
    "couch": "sofa",
    "fridge": "refrigerator",
    "freezer": "refrigerator",
    "clothes": "clothing",
    "wardrobe": "wardrobe box",
    "television": "tv",
    "flat screen": "tv",
    "ottoman": "footstool",
    "bookcase": "bookshelf",
    "washing machine": "washer",
    "stove": "range",
    "oven": "range",
    "bed frame": "bedframe",
    "treadmill": "exercise equipment",
    "elliptical": "exercise equipment",
    "bike": "bicycle",
    "bicycle": "bicycle",
    "entertainment center": "tv stand",
    "armoire": "wardrobe box",
    "recliner": "armchair",
}

# ── Per-item CY reference bounds (from industry_config.py prompt reference table) ──
# Each entry: (predicate_fn, min_cy, max_cy).  AI estimate is clamped into [min, max].
def _match_any(*keywords: str) -> Callable[[str], bool]:
    return lambda n: any(k in n for k in keywords)


_ITEM_BOUNDS: list[tuple[Callable[[str], bool], float, float]] = [
    # ── Bags & Soft Goods ──
    (_match_any("contractor bag"), 0.3, 0.5),
    (_match_any("trash bag", "garbage bag", "kitchen bag", "plastic bag", "black bag"), 0.05, 0.4),
    (_match_any("yard bag", "leaf bag"), 0.2, 0.35),
    (_match_any("duffel bag", "gym bag"), 0.1, 0.25),
    (_match_any("suitcase"), 0.1, 0.35),
    (_match_any("pillow"), 0.05, 0.15),
    (_match_any("sleeping bag"), 0.1, 0.2),
    # ── Boxes & Containers ──
    (_match_any("wardrobe box"), 0.35, 0.6),
    (_match_any("book box"), 0.04, 0.08),
    (_match_any("small box"), 0.04, 0.08),
    (_match_any("medium box"), 0.08, 0.13),
    (_match_any("large box"), 0.13, 0.25),
    (_match_any("cardboard box", "card board box"), 0.05, 0.25),
    (_match_any("plastic tote", "storage tote", "plastic bin"), 0.05, 0.2),
    # ── Furniture — Seating ──
    (_match_any("office chair", "desk chair"), 0.3, 0.5),
    (_match_any("dining chair"), 0.1, 0.2),
    (_match_any("folding chair", "lawn chair"), 0.05, 0.15),
    (_match_any("armchair", "recliner"), 0.8, 1.5),
    (_match_any("loveseat"), 0.8, 1.5),
    (_match_any("sofa", "couch"), 1.2, 2.0),
    (_match_any("sectional"), 0.8, 1.2),
    (_match_any("footstool", "ottoman"), 0.15, 0.4),
    # ── Furniture — Tables & Surfaces ──
    (_match_any("end table", "side table"), 0.15, 0.4),
    (_match_any("coffee table"), 0.3, 0.6),
    (_match_any("dining table"), 0.8, 1.5),
    (_match_any("desk"), 0.5, 1.5),
    (_match_any("tv stand", "media console", "entertainment center"), 0.4, 0.8),
    (_match_any("dresser"), 0.5, 1.0),
    (_match_any("nightstand", "night stand"), 0.15, 0.4),
    (_match_any("bookshelf", "bookcase", "shelv"), 0.3, 1.0),
    (_match_any("filing cabinet"), 0.3, 0.6),
    # ── Beds & Bedding ──
    (_match_any("twin mattress"), 0.4, 0.6),
    (_match_any("full mattress", "double mattress"), 0.55, 0.75),
    (_match_any("queen mattress"), 0.65, 0.85),
    (_match_any("king mattress"), 0.75, 1.0),
    (_match_any("mattress"), 0.4, 1.0),
    (_match_any("box spring"), 0.4, 1.0),
    (_match_any("bedframe", "bed frame"), 0.3, 0.8),
    (_match_any("headboard"), 0.2, 0.5),
    (_match_any("bunk bed"), 1.2, 1.8),
    # ── Appliances ──
    (_match_any("microwave"), 0.15, 0.4),
    (_match_any("toaster oven", "toaster"), 0.1, 0.2),
    (_match_any("mini fridge"), 0.35, 0.6),
    (_match_any("refrigerator", "fridge", "freezer"), 0.8, 1.2),
    (_match_any("washing machine", "washer"), 0.6, 1.0),
    (_match_any("dryer"), 0.6, 1.0),
    (_match_any("dishwasher"), 0.4, 0.6),
    (_match_any("stove", "range", "oven"), 0.6, 1.0),
    (_match_any("window ac", "air conditioner"), 0.2, 0.5),
    (_match_any("dehumidifier"), 0.2, 0.4),
    (_match_any("water heater"), 0.6, 1.0),
    (_match_any("vacuum cleaner", "vacuum"), 0.1, 0.2),
    # ── Electronics ──
    (_match_any("tv", "television", "flat screen"), 0.15, 0.5),
    (_match_any("computer monitor", "monitor"), 0.1, 0.2),
    (_match_any("desktop computer", "computer tower"), 0.15, 0.3),
    (_match_any("laptop"), 0.02, 0.05),
    (_match_any("printer"), 0.1, 0.3),
    (_match_any("speaker", "stereo"), 0.05, 0.3),
    # ── Misc Household ──
    (_match_any("bicycle", "bike"), 0.35, 0.6),
    (_match_any("treadmill", "elliptical"), 0.8, 1.2),
    (_match_any("exercise equipment"), 0.2, 0.5),
    (_match_any("lawn mower"), 0.4, 0.6),
    (_match_any("riding mower", "riding lawn mower"), 1.5, 2.5),
    (_match_any("grill"), 0.4, 0.8),
    (_match_any("patio chair"), 0.2, 0.4),
    (_match_any("patio table"), 0.4, 0.7),
    (_match_any("stroller"), 0.2, 0.4),
    (_match_any("car seat"), 0.15, 0.25),
    (_match_any("high chair"), 0.2, 0.4),
    (_match_any("playpen", "play pen"), 0.3, 0.5),
    (_match_any("christmas tree"), 0.3, 0.5),
    (_match_any("floor lamp", "standing lamp"), 0.1, 0.2),
    (_match_any("table lamp"), 0.03, 0.08),
    (_match_any("area rug", "rug rolled"), 0.2, 0.5),
    (_match_any("mirror"), 0.05, 0.15),
    (_match_any("picture", "frame"), 0.03, 0.08),
    (_match_any("pet crate", "dog crate", "cat crate"), 0.15, 0.35),
    # ── Paper / Clothing / Books ──
    (_match_any("papers", "documents", "stack of paper", "paper pile"), 0.03, 0.5),
    (_match_any("clothing", "clothes", "textile", "fabric"), 0.1, 1.0),
    (_match_any("bag of clothing", "clothing bag"), 0.2, 0.35),
    (_match_any("box of books", "book box"), 0.04, 0.08),
    (_match_any("books", "magazines"), 0.05, 1.0),
    (_match_any("shoes"), 0.01, 0.03),
    # ── Construction / Outdoor ──
    (_match_any("lumber", "wood pile", "wood bundle"), 0.05, 0.3),
    (_match_any("plywood sheet"), 0.05, 0.12),
    (_match_any("drywall sheet"), 0.05, 0.12),
    (_match_any("bag of concrete", "concrete bag"), 0.03, 0.06),
    (_match_any("potted plant"), 0.03, 0.2),
    (_match_any("tire"), 0.15, 0.35),
    (_match_any("propane tank"), 0.1, 0.2),
    (_match_any("paint can"), 0.02, 0.12),
    (_match_any("wooden board", "plank", "wood board", "wood piece", "scrap wood"), 0.03, 0.15),
    # ── Broken / Damaged Items ──
    (_match_any("broken chair"), 0.1, 0.25),
    (_match_any("broken furniture", "broken wooden", "furniture fragment", "furniture piece"), 0.1, 0.35),
    (_match_any("broken table leg"), 0.03, 0.1),
    (_match_any("broken appliance", "appliance part"), 0.1, 0.3),
    (_match_any("metal framework", "metal piece"), 0.1, 0.3),
    (_match_any("small debris", "loose debris"), 0.05, 0.4),
    # ── Miscellaneous ──
    (_match_any("misc", "miscellaneous", "assorted", "mixed items", "household items", "small items"), 0.1, 0.5),
    # ── Per-item hard caps (anything not matched above but commonly overestimated) ──
    (_match_any("trash", "garbage", "waste", "junk"), 0.05, 0.5),
    (_match_any("bag"), 0.02, 0.5),
    (_match_any("box"), 0.02, 0.6),
    (_match_any("chair"), 0.05, 2.0),
    (_match_any("table"), 0.1, 2.0),
    (_match_any("cabinet"), 0.2, 1.0),
    (_match_any("fan"), 0.05, 0.15),
    (_match_any("curtain", "drape", "blind"), 0.02, 0.08),
    (_match_any("carpet", "rug"), 0.1, 0.6),
    (_match_any("foam", "cushion", "pad"), 0.05, 0.3),
    (_match_any("blanket", "comforter", "quilt"), 0.05, 0.2),
    (_match_any("towel"), 0.01, 0.05),
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


def _normalize_item_name(name: str) -> str:
    """Apply synonyms to normalize AI item names for matching against reference tables."""
    name = _norm(name)
    for syn, canonical in _ITEM_SYNONYMS.items():
        if syn in name:
            name = name.replace(syn, canonical, 1)
            break
    return name


def _lookup_item_bounds(norm_name: str) -> Optional[tuple[float, float]]:
    """Return (min_cy, max_cy) for a normalized item name, or None if no bounds defined."""
    normalized = _normalize_item_name(norm_name)
    for pred, min_cy, max_cy in _ITEM_BOUNDS:
        try:
            if pred(normalized):
                return (min_cy, max_cy)
        except Exception:
            continue
    return None


def _apply_item_bounds(items: list) -> tuple[int, int]:
    """Apply reference table bounds — floor lifts underestimates, ceiling caps overestimates above 3x max.

    Returns (lifted_count, capped_count).
    """
    lifted = 0
    capped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _norm(str(item.get("name", "")))
        bounds = _lookup_item_bounds(name)
        if bounds is None:
            continue
        min_cy, max_cy = bounds
        ai_cy = float(item.get("cubic_yards", 0) or 0)
        if ai_cy < min_cy:
            logger.info("[item_bounds] Lifting '%s' from %.3f to min %.3f CY", name, ai_cy, min_cy)
            item["cubic_yards"] = round(min_cy, 4)
            item["volume_reference_clamped"] = True
            lifted += 1
        elif ai_cy > max_cy * 3.0:
            logger.info("[item_bounds] Capping '%s' from %.3f to ceiling %.3f CY (3x max)", name, ai_cy, max_cy * 3.0)
            item["cubic_yards"] = round(max_cy * 3.0, 4)
            item["volume_reference_clamped"] = True
            capped += 1
    return lifted, capped


def _compute_item_bounds_sum(items: list) -> tuple[float, float, int]:
    """Sum per-item min/max CY from lookup tables. Returns (min_sum, max_sum, item_count)."""
    min_sum = 0.0
    max_sum = 0.0
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _norm(str(item.get("name", "")))
        bounds = _lookup_item_bounds(name)
        if bounds:
            qty = max(1, int(item.get("quantity", 1) or 1))
            min_sum += bounds[0] * qty
            max_sum += bounds[1] * qty
            count += 1
        else:
            # Unrecognized items use a small default range
            qty = max(1, int(item.get("quantity", 1) or 1))
            min_sum += 0.1 * qty
            max_sum += 3.0 * qty
            count += 1
    return min_sum, max_sum, count


def _cleanup_phantom_misc(items: list) -> None:
    """Remove misc items when they dominate the item list by count."""
    non_misc = [it for it in items if isinstance(it, dict)
                and "misc" not in _norm(str(it.get("name", "")))]
    misc = [it for it in items if isinstance(it, dict)
            and "misc" in _norm(str(it.get("name", "")))]
    if len(misc) > len(non_misc) and len(non_misc) > 0:
        for mi in misc:
            if mi in items:
                items.remove(mi)


def _parse_spatial_total_cy(notes: str) -> Optional[float]:
    if not notes or not str(notes).strip():
        return None
    text = str(notes)
    # Prefer the last "= … CY" in chains like: … = 7.1 CY or … = 4.4 CY × 1.25 packing = 5.6 CY
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

    # ── Spatial-first branch: use area_measurements directly ──
    areas = out.get("area_measurements")
    if isinstance(areas, list) and areas:
        total_cy = 0.0
        for area in areas:
            if isinstance(area, dict):
                total_cy += float(area.get("estimated_cy", 0) or 0)
        if total_cy > 0:
            _sync_totals_from_target(out, round(total_cy, 2))
            items = out.get("items", [])
            if isinstance(items, list):
                _cleanup_phantom_misc(items)

                # Cross-validate: build per-item CY from lookup tables + bounds
                item_min_sum, item_max_sum, item_count = _compute_item_bounds_sum(items)
                if item_count > 0 and item_min_sum > 0:
                    # If spatial is wildly outside item bounds, flag it
                    if total_cy < item_min_sum * 0.5:
                        confidence_notes = out.setdefault("notes", "")
                        out["notes"] = (str(confidence_notes) + "\n" if confidence_notes else "") + \
                            f"Warning: spatial estimate ({total_cy:.1f} CY) is below item-based minimum ({item_min_sum:.1f} CY). Items may have been undercounted."
                    elif total_cy > item_max_sum * 2.0 and item_max_sum > 0:
                        confidence_notes = out.setdefault("notes", "")
                        out["notes"] = (str(confidence_notes) + "\n" if confidence_notes else "") + \
                            f"Warning: spatial estimate ({total_cy:.1f} CY) is above item-based maximum ({item_max_sum:.1f} CY). Bounding boxes may be over-generous."

                # Run per-item bounds on spatial items (injects CY for validation)
                for item in items:
                    if isinstance(item, dict) and not item.get("cubic_yards"):
                        name = _norm(str(item.get("name", "")))
                        bounds = _lookup_item_bounds(name)
                        if bounds:
                            item["cubic_yards"] = round(bounds[1], 4)  # use max as placeholder

            return out

    # ── Legacy branch: item-sum driven (backward compatibility) ──
    items = out.get("items")
    if not isinstance(items, list) or not items:
        return out

    target = _target_total_cy(out)
    if target <= 0:
        return out

    # NOTE: Sparse-scene cap REMOVED (2026-03-25). Was capping target to
    # item_sum * 1.5 when ratio > 2x, but this destroyed legitimate estimates
    # where AI's per-item CY values were low but spatial math was correct
    # (e.g., railroad ties, lumber piles). Trust spatial math from notes.

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

    # Apply lookup volumes (precise per-unit values)
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

    # ── Bottom-up approach: trust item volumes, don't inflate to spatial total ──
    # The new prompt generates bottom-up estimates where item sum IS the total.
    # We only apply lookup corrections (above), then sync totals to item sum.
    # We do NOT redistribute remaining spatial volume onto debris items,
    # as this was the primary cause of massive overestimation.

    # Recalculate item sum after lookup corrections
    corrected_sum = sum(line_volume(it) for it in items if isinstance(it, dict))

    # Remove phantom misc items that exceed all real items combined
    non_misc = [it for it in items if isinstance(it, dict)
                and "misc" not in _norm(str(it.get("name", "")))]
    misc = [it for it in items if isinstance(it, dict)
            and "misc" in _norm(str(it.get("name", "")))]
    non_misc_vol = sum(line_volume(it) for it in non_misc)

    items_to_remove = []
    for mi in misc:
        mv = line_volume(mi)
        if mv > non_misc_vol and non_misc_vol > 0:
            logger.info(
                "[validate_estimate] Removing phantom misc: %s (%.2f CY) > all real items (%.2f CY)",
                mi.get("name"), mv, non_misc_vol,
            )
            items_to_remove.append(mi)

    for mi in items_to_remove:
        if mi in items:
            items.remove(mi)

    # Apply reference table clamping (from AI prompt's reference table)
    _apply_item_bounds(items)

    # Final total = sum of remaining items (bottom-up)
    final_sum = sum(line_volume(it) for it in items if isinstance(it, dict))
    if final_sum > 0:
        target = round(final_sum, 2)

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


# ---------------------------------------------------------------------------
# Pile / mound depth adjustment
# ---------------------------------------------------------------------------

# Minimum ratio of pile_volume to item_sum before we boost.
# If pile is 30%+ bigger than what items sum to, hidden items are likely.
_PILE_BOOST_THRESHOLD = 1.30

# Hard cap: never boost by more than this factor (prevents wild AI pile guesses).
_PILE_BOOST_MAX_FACTOR = 2.5

# Confidence penalty per 10% gap between pile and items.
_PILE_CONFIDENCE_PENALTY_PER_10PCT = 3  # points


def apply_pile_adjustment(result_data: dict) -> tuple[dict, list[str]]:
    """Compare pile geometry estimate against item sum.

    When the AI reports a pile_estimate with estimated_cy significantly larger
    than the bottom-up item sum, hidden items likely exist behind/beneath the
    front-facing layer.  Boost the total upward and return explanatory notes.

    Does NOT invent phantom items — only adjusts volume totals.

    Returns (updated_result_data, notes_list).
    """
    pile = result_data.get("pile_estimate")
    if not isinstance(pile, dict):
        return result_data, []
    if not pile.get("is_pile", False):
        return result_data, []

    pile_cy = float(pile.get("estimated_cy", 0) or 0)
    if pile_cy <= 0:
        return result_data, []

    totals = result_data.get("totals") or {}
    item_sum = 0.0
    for it in (result_data.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            item_sum += max(0.0, float(it.get("cubic_yards") or 0)) * max(1, int(it.get("quantity") or 1))
        except (TypeError, ValueError):
            continue

    if item_sum <= 0:
        # No items parsed — use pile estimate directly.
        _sync_totals_from_target(result_data, pile_cy)
        return result_data, [
            "Pile geometry estimate used as total — no individual items identified."
        ]

    ratio = pile_cy / item_sum
    if ratio < _PILE_BOOST_THRESHOLD:
        return result_data, []

    # Items undercount the pile.  Blend: use 60% pile + 40% item sum.
    # This preserves the item-level detail while accounting for hidden depth.
    blended = pile_cy * 0.6 + item_sum * 0.4

    # Cap the boost.
    boost_factor = blended / item_sum
    if boost_factor > _PILE_BOOST_MAX_FACTOR:
        blended = item_sum * _PILE_BOOST_MAX_FACTOR
        boost_factor = _PILE_BOOST_MAX_FACTOR

    _sync_totals_from_target(result_data, round(blended, 2))

    notes: list[str] = []
    pile_dims = pile.get("width_in", 0), pile.get("depth_in", 0), pile.get("height_in", 0)
    pf = float(pile.get("packing_factor", 0.65) or 0.65)
    notes.append(
        f"Pile geometry ({int(pile_dims[0])}x{int(pile_dims[1])}x{int(pile_dims[2])} in, "
        f"packing {int(pf * 100)}%) suggests ~{pile_cy:.1f} CY, but only {item_sum:.1f} CY "
        f"of items are visible. Volume adjusted to {blended:.1f} CY to account for hidden "
        f"items behind the front layer."
    )

    # Confidence penalty.
    current_conf = int(result_data.get("confidence", 75) or 75)
    gap_pct = (ratio - 1.0) * 100  # e.g. ratio 1.8 → gap 80%
    penalty = int(gap_pct / 10) * _PILE_CONFIDENCE_PENALTY_PER_10PCT
    penalty = min(penalty, 20)  # hard cap at -20
    if penalty > 0:
        result_data["confidence"] = max(50, current_conf - penalty)
        notes.append(
            f"Confidence reduced by {penalty} points due to hidden-depth uncertainty."
        )

    result_data["pile_adjustment_applied"] = True
    result_data["pile_adjustment_original_item_sum"] = round(item_sum, 2)
    result_data["pile_adjustment_pile_estimate"] = round(pile_cy, 2)

    logger.info(
        "[apply_pile_adjustment] pile=%.2f CY, items=%.2f CY, ratio=%.2f, "
        "blended=%.2f CY, conf_penalty=%d",
        pile_cy, item_sum, ratio, blended, penalty,
    )

    return result_data, notes


# ---------------------------------------------------------------------------
# Spatial-first estimation (new primary method)
# ---------------------------------------------------------------------------

def apply_spatial_estimate(result_data: dict) -> tuple[dict, list[str]]:
    """Compute total CY from area_measurements (spatial-first approach).

    Each area has width_in, depth_in, height_in, packing_factor, estimated_cy.
    The total is the sum of all area estimated_cy values.

    If area_measurements is absent or empty, falls back to item-sum behavior
    by returning the result unchanged.

    Returns (updated_result_data, notes_list).
    """
    areas = result_data.get("area_measurements")
    if not isinstance(areas, list) or not areas:
        return result_data, []

    total_cy = 0.0
    notes: list[str] = []
    area_details: list[str] = []

    for area in areas:
        if not isinstance(area, dict):
            continue
        cy = float(area.get("estimated_cy", 0) or 0)
        if cy <= 0:
            continue
        total_cy += cy
        name = area.get("area_name", "unknown area")
        w = area.get("width_in", 0)
        d = area.get("depth_in", 0)
        h = area.get("height_in", 0)
        pf = float(area.get("packing_factor", 0.65) or 0.65)
        area_details.append(
            f"{name}: {int(w)}x{int(d)}x{int(h)} in @ {int(pf*100)}% = {cy:.2f} CY"
        )

    if total_cy <= 0:
        return result_data, []

    _sync_totals_from_target(result_data, round(total_cy, 2))

    notes.append(
        f"Spatial estimate from {len(area_details)} area(s): {total_cy:.2f} CY total."
    )
    notes.extend(area_details)

    logger.info(
        "[apply_spatial_estimate] %d areas, %.2f CY total",
        len(area_details), total_cy
    )

    return result_data, notes


# ---------------------------------------------------------------------------
# Heavy material detection
# ---------------------------------------------------------------------------

_HEAVY_MATERIAL_KEYWORDS = (
    "shingle", "shingles", "roofing", "asphalt shingle",
    "concrete", "concrete chunks", "masonry", "brick", "bricks",
    "stone", "stones", "gravel", "dirt", "soil", "sand",
    "tile", "tiles", "ceramic tile",
)


def detect_heavy_materials(result_data: dict) -> list[str]:
    """Scan items for heavy materials that should trigger premium pricing.

    Returns list of conditions to append (e.g. ["heavy_items"]).
    Also promotes job_type to "premium" when heavy materials are found.
    """
    items = result_data.get("items") or []
    if not isinstance(items, list):
        return []

    item_text = " ".join(
        str(it.get("name", "")).lower()
        for it in items
        if isinstance(it, dict)
    )
    if not item_text:
        return []

    found: list[str] = []
    for kw in _HEAVY_MATERIAL_KEYWORDS:
        if kw in item_text:
            found.append(kw)
            break  # one hit is enough

    if not found:
        return []

    conditions = result_data.get("conditions") or []
    if not isinstance(conditions, list):
        conditions = []
    if "heavy_items" not in conditions:
        conditions.append("heavy_items")
        result_data["conditions"] = conditions

    job_type = str(result_data.get("job_type", "standard") or "standard").lower()
    if job_type not in ("premium", "hoarder", "truck_load"):
        result_data["job_type"] = "premium"
        logger.info(
            "[detect_heavy_materials] Promoted job_type to premium — "
            "heavy materials detected: %s", found
        )

    return found
