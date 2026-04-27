"""
Parallel dual-estimation pipeline for WSIC.
Runs two vision providers simultaneously and merges results.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional
from sqlalchemy import select

from services.vision_providers import VisionProvider, VisionResult, VisionProviderError, GeminiProvider, VeniceProvider, OpenRouterProvider
from database import AsyncSessionLocal
from models import ProviderHealthEvent, SiteConfig

logger = logging.getLogger("wsic.pipeline")

VARIANCE_FLAG_THRESHOLD = 0.20


async def _load_provider_runtime_config() -> tuple[list[str], dict[str, str]]:
    cfg = {}
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(SiteConfig))).scalars().all()
            cfg = {str(r.config_key or ""): str(r.config_value or "") for r in rows}
    except Exception as e:
        logger.warning(f"[pipeline] Could not load site config for provider routing: {e}")

    allowed = {"gemini", "venice", "openrouter", "none"}
    configured_order = [
        (cfg.get("estimate_provider_primary", "") or "").strip().lower(),
        (cfg.get("estimate_provider_fallback_1", "") or "").strip().lower(),
        (cfg.get("estimate_provider_fallback_2", "") or "").strip().lower(),
    ]
    order = []
    for p in configured_order:
        if p in allowed and p not in {"", "none"} and p not in order:
            order.append(p)
    for default_p in ["gemini", "venice", "openrouter"]:
        if default_p not in order:
            order.append(default_p)

    model_overrides = {
        "gemini": (cfg.get("estimate_model_gemini", "") or "").strip(),
        "venice": (cfg.get("estimate_model_venice", "") or "").strip(),
        "openrouter": (cfg.get("estimate_model_openrouter", "") or "").strip(),
    }
    return order, model_overrides


async def _build_providers() -> list[VisionProvider]:
    providers = []
    provider_order, model_overrides = await _load_provider_runtime_config()

    key_present = {
        "gemini": bool((os.environ.get("GEMINI_API_KEY") or "").strip()),
        "venice": bool((os.environ.get("VENICE_API_KEY") or "").strip()),
        "openrouter": bool((os.environ.get("OPENROUTER_API_KEY") or "").strip()),
    }

    for provider_name in provider_order:
        if not key_present.get(provider_name):
            continue
        model = model_overrides.get(provider_name) or None
        if provider_name == "gemini":
            providers.append(GeminiProvider(model=model))
        elif provider_name == "venice":
            providers.append(VeniceProvider(model=model))
        elif provider_name == "openrouter":
            providers.append(OpenRouterProvider(model=model))
    return providers


async def _run_single(provider: VisionProvider, images: list, prompt: str) -> Optional[VisionResult]:
    try:
        result = await asyncio.wait_for(provider.estimate(images, prompt), timeout=120.0)
        logger.info(f"[pipeline] {provider.name} succeeded: {len(result.data.get('items', []))} items, "
                     f"{result.input_tokens}+{result.output_tokens} tokens")
        await _record_provider_event(
            provider_name=provider.name,
            model_name=result.model_used or provider.model_name,
            status="success",
            error_type="",
            error_message="",
            photos_count=len([b for b in images if isinstance(b, dict) and b.get('type') == 'image']),
            latency_ms=getattr(result, 'latency_ms', 0),
        )
        return result
    except VisionProviderError as e:
        logger.warning(f"[pipeline] {e.provider_name} failed: {e.message}")
        await _record_provider_event(
            provider_name=e.provider_name,
            model_name=getattr(e, 'model_name', '') or provider.model_name,
            status="failure",
            error_type=type(e.__cause__).__name__ if e.__cause__ else type(e).__name__,
            error_message=e.message[:1000],
            photos_count=len([b for b in images if isinstance(b, dict) and b.get('type') == 'image']),
            latency_ms=getattr(e, 'latency_ms', 0),
        )
        return None
    except Exception as e:
        logger.warning(f"[pipeline] {provider.name} failed: {type(e).__name__}: {e}")
        await _record_provider_event(
            provider_name=provider.name,
            model_name=provider.model_name,
            status="failure",
            error_type=type(e).__name__,
            error_message=str(e)[:1000],
            photos_count=len([b for b in images if isinstance(b, dict) and b.get('type') == 'image']),
            latency_ms=0,
        )
        return None


async def _record_provider_event(provider_name: str, model_name: str, status: str, error_type: str, error_message: str, photos_count: int, latency_ms: int):
    try:
        async with AsyncSessionLocal() as db:
            db.add(ProviderHealthEvent(
                provider_name=provider_name,
                model_name=model_name,
                status=status,
                error_type=error_type,
                error_message=error_message,
                photos_count=photos_count,
                latency_ms=latency_ms,
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"[pipeline] Failed to record provider health event: {e}")


# ---------------------------------------------------------------------------
# Fuzzy item deduplication
# ---------------------------------------------------------------------------

# Synonym groups — canonical word chosen (first alphabetically) for matching.
_SYNONYM_GROUPS: list[set[str]] = [
    {"pile", "stack", "group", "collection", "bunch", "bundle"},
    {"debris", "junk", "rubbish", "trash", "garbage", "waste", "scrap", "refuse"},
    {"pieces", "parts", "fragments", "bits", "scraps", "chunks"},
    {"boxes", "cartons", "crates"},
    {"miscellaneous", "misc", "assorted", "mixed", "various"},
    {"items", "things", "stuff", "goods", "materials", "contents"},
    {"bucket", "buckets", "pail"},
    {"wood", "wooden", "lumber", "boards", "board", "plywood"},
    {"paint", "paints", "stain", "stains"},
    {"plastic", "plastics", "vinyl"},
]

# Words to strip before comparison (noise, not signal).
_MATCH_STOP_WORDS = frozenset({
    "and", "or", "the", "a", "an", "with", "of", "in", "from", "to",
    "for", "by", "on", "at", "some", "few", "several", "multiple",
    "other", "etc", "including", "small", "large", "medium", "big",
    "little", "tiny", "detected", "noted",
})


def _has_substantive_parenthetical(name: str) -> bool:
    """True when the parenthetical is a substantive qualifier (product type,
    contents, brand) rather than just a color or material.

    (drawer kits) → True  — differentiates item type
    (wire/cable) → True
    (Heater) → True
    (black) → False — just a color, same item
    (white) → False
    (wooden) → False
    """
    m = re.search(r"\((.*?)\)", str(name or ""))
    if not m:
        return False
    content = m.group(1).strip().lower()
    # Colors and materials — not substantive enough to differentiate items
    non_substantive = {
        "black", "white", "brown", "gray", "grey", "blue", "red", "green",
        "beige", "tan", "navy", "silver", "gold", "orange", "yellow",
        "wooden", "metal", "plastic", "fabric", "cloth", "leather",
        "full", "empty", "large", "small", "medium", "big", "old", "new",
    }
    return content not in non_substantive


def _normalize_for_match(name: str) -> set[str]:
    """Lowercase, strip parentheticals for core matching, split compound tokens
    (lumber/wood), apply synonyms, drop stop words."""
    name = re.sub(r"\(.*?\)", "", str(name or ""), flags=re.IGNORECASE).strip()
    # Split compound tokens on / and - (e.g. "lumber/wood" → "lumber" "wood")
    expanded: list[str] = []
    for tok in name.lower().split():
        expanded.extend(re.findall(r"[a-zA-Z0-9]+", tok))

    canonical: list[str] = []
    for word in expanded:
        cw = word
        for group in _SYNONYM_GROUPS:
            if word in group:
                cw = min(group)
                break
        if cw not in _MATCH_STOP_WORDS:
            canonical.append(cw)
    return set(canonical)


def _extract_size_marker(name: str) -> Optional[str]:
    """Return a size token like '5-gallon', '1-gallon' or None."""
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*[- ]?(?:gallon|gal|in|inch|ft|foot|cy|yard|lb|pound|oz|quart)",
        str(name).lower(),
    )
    return m.group(0) if m else None


def _is_fuzzy_duplicate(item_a: dict, item_b: dict) -> bool:
    """True when two items are likely the same physical object, differently named."""
    name_a = str(item_a.get("name", ""))
    name_b = str(item_b.get("name", ""))
    if not name_a or not name_b:
        return False

    words_a = _normalize_for_match(name_a)
    words_b = _normalize_for_match(name_b)
    if not words_a or not words_b:
        return False

    intersection = words_a & words_b
    union = words_a | words_b
    jaccard = len(intersection) / len(union) if union else 0.0

    # Size markers: only block if both present AND different AND neither is a superset of the other.
    # "5-gallon" vs "20-gallon" → different items. "5-gallon" vs None → may be same item.
    sz_a = _extract_size_marker(name_a)
    sz_b = _extract_size_marker(name_b)
    size_conflict = False
    if sz_a and sz_b and sz_a != sz_b:
        sz_a_val = re.sub(r"[^\d.]", "", sz_a)
        sz_b_val = re.sub(r"[^\d.]", "", sz_b)
        try:
            if float(sz_a_val) != float(sz_b_val):
                size_conflict = True
        except ValueError:
            size_conflict = True

    # High Jaccard overlap → duplicate (unless clear size conflict like "5-gallon" vs "20-gallon").
    if jaccard >= 0.45 and not size_conflict:
        return True

    # Identical core words → duplicate even if one has a parenthetical qualifier,
    # unless there's a direct size conflict ("5-gallon" vs "20-gallon").
    if words_a == words_b and not size_conflict:
        return True

    # Near-identical with size qualifiers that are compatible (one has size, other doesn't).
    if jaccard >= 0.5 and (not sz_a or not sz_b) and not size_conflict:
        return True

    # One name's normalized words are a subset of the other + share ≥3 words.
    shorter = words_a if len(words_a) <= len(words_b) else words_b
    longer = words_b if len(words_a) <= len(words_b) else words_a
    if shorter.issubset(longer) and len(intersection) >= 3:
        return True

    return False


def _merge_two_items(item_a: dict, item_b: dict) -> dict:
    """Average CY, keep the more descriptive name, union flags."""
    merged = dict(item_a)

    cy_a = float(item_a.get("cubic_yards", 0) or 0)
    cy_b = float(item_b.get("cubic_yards", 0) or 0)
    merged["cubic_yards"] = round((cy_a + cy_b) / 2.0, 3)

    # Keep longer / more descriptive name.
    if len(str(item_b.get("name", ""))) > len(str(item_a.get("name", ""))):
        merged["name"] = item_b["name"]

    # Union of boolean flags.
    for flag in ("is_special", "is_uncertain"):
        if item_b.get(flag):
            merged[flag] = True

    merged["quantity"] = 1
    merged["fuzzy_deduped"] = True
    return merged


def deduplicate_merged_items(result_data: dict) -> tuple[dict, int]:
    """Fuzzy-deduplicate items after model merge.

    Uses first-match-wins: each item merges with at most one partner.
    This prevents cascade merging where A+B merged result accidentally
    matches C (which the original A would not have matched).

    Returns (updated_result, dedup_count).
    """
    items = result_data.get("items", [])
    if not isinstance(items, list) or len(items) <= 1:
        return result_data, 0

    consumed: set[int] = set()
    final_items: list[dict] = []
    dedup_count = 0

    for i in range(len(items)):
        if i in consumed:
            continue

        merged_partner: Optional[int] = None
        for j in range(i + 1, len(items)):
            if j in consumed:
                continue
            if _is_fuzzy_duplicate(items[i], items[j]):
                merged_partner = j
                break  # first-match-wins

        if merged_partner is not None:
            final_items.append(_merge_two_items(items[i], items[merged_partner]))
            consumed.add(merged_partner)
            dedup_count += 1
        else:
            final_items.append(items[i])

    if dedup_count > 0:
        result_data = dict(result_data)
        result_data["items"] = final_items
        logger.info(
            f"[pipeline] Fuzzy dedup: {len(items)} → {len(final_items)} items "
            f"({dedup_count} merged)"
        )

    return result_data, dedup_count


# ---------------------------------------------------------------------------
# Model merge
# ---------------------------------------------------------------------------


def merge_results(results: list[VisionResult]) -> dict:
    valid = [r for r in results if r is not None]
    if not valid:
        raise RuntimeError("All vision providers failed to return results")
    if len(valid) == 1:
        merged = valid[0].data
        merged["_meta"] = {
            "providers_used": [valid[0].provider_name],
            "provider_models": [valid[0].model_used],
            "single_provider": True,
            "variance_flagged": False,
        }
        return _finalize_merged(merged, valid)

    primary = valid[0].data
    secondary = valid[1].data
    primary_items = {item_key(it): it for it in primary.get("items", []) if item_key(it)}
    secondary_items = {item_key(it): it for it in secondary.get("items", []) if item_key(it)}
    all_keys = list(dict.fromkeys(list(primary_items.keys()) + list(secondary_items.keys())))

    merged_items = []
    variance_flags = []
    for key in all_keys:
        p_item = primary_items.get(key)
        s_item = secondary_items.get(key)
        if p_item and s_item:
            p_cy = float(p_item.get("cubic_yards", 0))
            s_cy = float(s_item.get("cubic_yards", 0))
            avg_cy = (p_cy + s_cy) / 2.0
            if p_cy > 0 and s_cy > 0:
                variance = abs(p_cy - s_cy) / max(p_cy, s_cy)
                if variance > VARIANCE_FLAG_THRESHOLD:
                    variance_flags.append({
                        "item": key, "primary_cy": round(p_cy, 2),
                        "secondary_cy": round(s_cy, 2), "variance": round(variance, 2),
                    })
            merged = dict(p_item)
            merged["cubic_yards"] = round(avg_cy, 2)
            if s_item.get("is_special") or p_item.get("is_special"):
                merged["is_special"] = True
            if s_item.get("is_uncertain") or p_item.get("is_uncertain"):
                merged["is_uncertain"] = True
            merged_items.append(merged)
        elif p_item:
            merged_items.append(dict(p_item))
        elif s_item:
            merged_items.append(dict(s_item))

    if not merged_items:
        merged_items = primary.get("items", [])

    result = dict(primary)
    result["items"] = merged_items

    p_total = sum(it.get("cubic_yards", 0) * it.get("quantity", 1) for it in primary.get("items", []))
    s_total = sum(it.get("cubic_yards", 0) * it.get("quantity", 1) for it in secondary.get("items", []))
    merged_total = sum(it.get("cubic_yards", 0) * it.get("quantity", 1) for it in merged_items)

    totals = result.get("totals", {})
    totals["cubic_yards_mid"] = round(merged_total, 1)
    totals["cubic_yards_low"] = round(merged_total * 0.85, 1)
    totals["cubic_yards_high"] = round(merged_total * 1.15, 1)
    result["totals"] = totals
    result["total_cubic_yards"] = round(merged_total, 1)

    p_refs = primary.get("reference_points", [])
    s_refs = secondary.get("reference_points", [])
    seen = set()
    result["reference_points"] = []
    for ref in p_refs + s_refs:
        rk = ref.get("name", "")
        if rk not in seen:
            seen.add(rk)
            result["reference_points"].append(ref)

    p_conf = int(primary.get("confidence", 75) or 75)
    s_conf = int(secondary.get("confidence", 75) or 75)
    result["confidence"] = min(p_conf, s_conf)

    p_scene = primary.get("scene_description", "")
    s_scene = secondary.get("scene_description", "")
    result["scene_description"] = p_scene or s_scene

    result["_meta"] = {
        "providers_used": [r.provider_name for r in valid],
        "provider_models": [r.model_used for r in valid],
        "single_provider": False,
        "variance_flagged": len(variance_flags) > 0,
        "variance_details": variance_flags,
        "primary_total": round(p_total, 1),
        "secondary_total": round(s_total, 1),
        "merged_total": round(merged_total, 1),
    }

    return _finalize_merged(result, valid)


def item_key(item: dict) -> str:
    if not item:
        return ""
    return str(item.get("name", "")).lower().strip()


def _finalize_merged(result: dict, results: list[VisionResult]) -> dict:
    result["_meta"]["input_tokens"] = sum(r.input_tokens for r in results)
    result["_meta"]["output_tokens"] = sum(r.output_tokens for r in results)
    result["_meta"]["cost_cents"] = sum(r.cost_cents for r in results)
    return result


def _compute_item_cy_from_dimensions(items: list) -> int:
    """For each item with width_in, height_in, depth_in, compute cubic_yards from dimensions.

    CY = (height_in * width_in * depth_in) / 46656 (cubic inches → cubic yards).

    If computed CY differs from AI-guessed CY by >30%, use the computed value and set
    is_uncertain=True.  If dimensions are missing, leave AI guess as-is.

    Returns count of items where computed CY replaced AI guess.
    """
    replaced = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            h = float(item.get("height_in", 0) or 0)
            w = float(item.get("width_in", 0) or 0)
            d = float(item.get("depth_in", 0) or 0)
        except (TypeError, ValueError):
            continue

        if h <= 0 or w <= 0 or d <= 0:
            # If only 2 of 3 dimensions present, try to infer the third from item type
            present_count = (1 if h > 0 else 0) + (1 if w > 0 else 0) + (1 if d > 0 else 0)
            if present_count < 2:
                continue
            # Rough default for missing dimension based on typical item depth/height ratios
            if h <= 0 and w > 0 and d > 0:
                h = max(w, d) * 0.6
            elif w <= 0 and h > 0 and d > 0:
                w = max(h, d) * 0.6
            elif d <= 0 and h > 0 and w > 0:
                d = min(h, w) * 0.5

        computed_cy = (h * w * d) / 46656.0
        if computed_cy <= 0.001:
            continue

        ai_cy = float(item.get("cubic_yards", 0) or 0)
        if ai_cy <= 0:
            item["cubic_yards"] = round(computed_cy, 4)
            item["cy_computed_from_dimensions"] = True
            replaced += 1
        else:
            divergence = abs(computed_cy - ai_cy) / ai_cy if ai_cy > 0 else 0
            if divergence > 0.30:
                logger.info(
                    "[dimension_cy] '%s': computed %.3f CY (%sx%sx%s in) vs AI guess %.3f CY — "
                    "divergence %.0f%%. Using computed CY.",
                    item.get("name", "?"), computed_cy, int(w), int(h), int(d),
                    ai_cy, divergence * 100,
                )
                item["cubic_yards"] = round(computed_cy, 4)
                item["cy_computed_from_dimensions"] = True
                item["is_uncertain"] = True
                replaced += 1

    return replaced


async def run_verification_pass(images: list, verification_prompt: str) -> Optional[dict]:
    """Run a focused spatial verification pass on the primary estimate's item list.

    Uses a SINGLE provider (first available) to save cost.  The verifier receives
    the item list from the primary pass and focuses exclusively on verifying/correcting
    dimensions and totals.

    Returns parsed result dict on success, None on failure.
    """
    providers = await _build_providers()
    if not providers:
        logger.warning("[verification] No providers available for verification pass")
        return None

    verifier = providers[0]
    logger.info("[verification] Running verification pass with %s", verifier.name)
    result = await _run_single(verifier, images, verification_prompt)
    if result is None or not isinstance(result.data, dict):
        logger.warning("[verification] Verification pass returned no usable data")
        return None

    items = result.data.get("items", [])
    totals = result.data.get("totals", {})
    mid = totals.get("cubic_yards_mid", 0) if isinstance(totals, dict) else 0
    logger.info("[verification] Verifier found %d items, %.1f CY total", len(items), mid)
    return result.data


async def _process_single_batch(providers: list, images: list, prompt: str) -> Optional[dict]:
    """Run a single batch through all providers, merge, dedup, and compute dimensions."""
    tasks = [_run_single(p, images, prompt) for p in providers]
    provider_results = await asyncio.gather(*tasks)
    valid = list(provider_results)
    if not valid:
        return None

    merged = merge_results(valid)

    dimension_replaced = _compute_item_cy_from_dimensions(merged.get("items") or [])
    if dimension_replaced > 0:
        merged.setdefault("_meta", {})["dimension_cy_replacements"] = dimension_replaced

    merged, _ = deduplicate_merged_items(merged)
    merged.pop("_meta", None)
    return merged


def _cross_batch_deduplicate(items: list[dict]) -> list[dict]:
    """Deduplicate items from different batches using fuzzy name matching + dimension check.

    When two items from different photo batches share a fuzzy-matched name AND similar
    dimensions (within 40% volume), they're likely the same object.  Merge by averaging CY
    and summing quantities.
    """
    if len(items) <= 1:
        return items

    consumed: set[int] = set()
    result: list[dict] = []

    for i in range(len(items)):
        if i in consumed:
            continue
        partner = None
        for j in range(i + 1, len(items)):
            if j in consumed:
                continue
            if not _is_fuzzy_duplicate(items[i], items[j]):
                continue

            # Cross-batch extra check: dimension compatibility
            if _dimensions_compatible(items[i], items[j]):
                partner = j
                break

        if partner is not None:
            merged = _merge_two_items(items[i], items[partner])
            merged["cross_batch_merged"] = True
            result.append(merged)
            consumed.add(partner)
        else:
            result.append(items[i])

    return result


def _dimensions_compatible(item_a: dict, item_b: dict) -> bool:
    """True if two items have compatible dimensions (within 40% volume), or one lacks dims."""
    try:
        h_a = float(item_a.get("height_in", 0) or 0)
        w_a = float(item_a.get("width_in", 0) or 0)
        d_a = float(item_a.get("depth_in", 0) or 0)
        h_b = float(item_b.get("height_in", 0) or 0)
        w_b = float(item_b.get("width_in", 0) or 0)
        d_b = float(item_b.get("depth_in", 0) or 0)
    except (TypeError, ValueError):
        return True  # can't compare, trust fuzzy name match

    dims_a = h_a > 0 and w_a > 0 and d_a > 0
    dims_b = h_b > 0 and w_b > 0 and d_b > 0

    if dims_a and dims_b:
        vol_a = h_a * w_a * d_a
        vol_b = h_b * w_b * d_b
        if vol_a > 0 and vol_b > 0:
            ratio = max(vol_a, vol_b) / min(vol_a, vol_b)
            return ratio <= 1.4
    return True


def _split_image_content_into_batches(image_content: list, max_per_batch: int = 8) -> list[list]:
    """Split interleaved text+image blocks into batches by room markers.

    Room markers are text blocks starting with '\n--- ROOM:'.  Each batch gets its own
    room context header.  If a single room has more than max_per_batch photos, it's
    split further.
    """
    batches: list[list] = []
    current: list = []

    for block in image_content:
        if not isinstance(block, dict):
            continue
        is_room_header = (
            block.get("type") == "text"
            and isinstance(block.get("text", ""), str)
            and block["text"].startswith("\n--- ROOM:")
        )
        if is_room_header and current:
            batches.append(current)
            current = []
        current.append(block)

    if current:
        batches.append(current)

    # Tighten batches that exceed max_per_batch photos
    final_batches: list[list] = []
    for batch in batches:
        img_count = sum(1 for b in batch if isinstance(b, dict) and b.get("type") == "image")
        if img_count <= max_per_batch:
            final_batches.append(batch)
        else:
            # Split oversized batch into sub-batches
            sub: list = []
            sub_img = 0
            room_header = None
            for block in batch:
                is_header = (
                    block.get("type") == "text"
                    and isinstance(block.get("text", ""), str)
                    and block["text"].startswith("\n--- ROOM:")
                )
                if is_header:
                    room_header = block
                    sub.append(block)
                    continue
                if block.get("type") == "image":
                    if sub_img >= max_per_batch and sub:
                        final_batches.append(list(sub))
                        sub = [room_header] if room_header else []
                        sub_img = 0
                    sub_img += 1
                sub.append(block)
            if sub:
                final_batches.append(sub)

    return final_batches if final_batches else [image_content]


async def run_batched_estimate(image_content: list, extraction_prompt: str) -> tuple[dict, dict]:
    """Process photos in batches when there are more than 8 across multiple rooms.

    Each room batch gets its own independent estimate.  Results are merged across
    batches with cross-batch dedup to avoid double-counting items visible in multiple
    rooms/angles.
    """
    providers = await _build_providers()
    if not providers:
        raise RuntimeError("No vision providers configured.")

    img_blocks = [b for b in image_content if isinstance(b, dict) and b.get("type") == "image"]
    if len(img_blocks) <= 8:
        # Single batch — pass through to original pipeline
        tasks = [_run_single(p, image_content, extraction_prompt) for p in providers]
        provider_results = await asyncio.gather(*tasks)
        merged = merge_results(list(provider_results))
        dimension_replaced = _compute_item_cy_from_dimensions(merged.get("items") or [])
        if dimension_replaced > 0:
            merged.setdefault("_meta", {})["dimension_cy_replacements"] = dimension_replaced
        merged, dedup_count = deduplicate_merged_items(merged)
        meta = merged.pop("_meta", {})
        if dedup_count > 0:
            meta["fuzzy_dedup_count"] = dedup_count
        meta["batched"] = False
        return merged, meta

    batches = _split_image_content_into_batches(image_content)
    logger.info("[batched] Processing %d batches (%d total photos)", len(batches), len(img_blocks))

    batch_results = await asyncio.gather(
        *[_process_single_batch(providers, batch, extraction_prompt) for batch in batches],
        return_exceptions=True,
    )

    all_items: list[dict] = []
    batch_count = 0
    for res in batch_results:
        if isinstance(res, Exception):
            logger.warning("[batched] Batch %d failed: %s", batch_count, res)
            continue
        if isinstance(res, dict):
            items = res.get("items", [])
            if isinstance(items, list):
                all_items.extend(items)
        batch_count += 1

    if not all_items:
        return {"items": [], "totals": {"cubic_yards_mid": 0, "cubic_yards_low": 0, "cubic_yards_high": 0}}, {"batched": True, "batches_succeeded": 0}

    # Cross-batch dedup
    final_items = _cross_batch_deduplicate(all_items)
    cross_dedup = len(all_items) - len(final_items)

    # Sum totals
    total_cy = sum(
        float(it.get("cubic_yards", 0) or 0) * max(1, int(it.get("quantity", 1) or 1))
        for it in final_items
    )
    merged = {
        "items": final_items,
        "totals": {
            "cubic_yards_mid": round(total_cy, 2),
            "cubic_yards_low": round(total_cy * 0.85, 2),
            "cubic_yards_high": round(total_cy * 1.15, 2),
        },
        "job_type": "standard",
        "conditions": [],
        "confidence": 70,
        "notes": f"Batched estimate from {len(batches)} rooms, {cross_dedup} cross-batch duplicates merged.",
    }
    meta = {"batched": True, "batches_used": len(batches), "cross_batch_dedup_count": cross_dedup}
    return merged, meta


async def run_parallel_estimate(images: list, prompt: str) -> tuple[dict, dict]:
    providers = await _build_providers()
    if not providers:
        raise RuntimeError("No vision providers configured. Set GEMINI_API_KEY, VENICE_API_KEY, or OPENROUTER_API_KEY.")

    max_photos = 8
    img_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "image"]
    if len(img_blocks) > max_photos:
        text_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "text"]
        images = text_blocks[:8] + img_blocks[:max_photos]

    tasks = [_run_single(p, images, prompt) for p in providers]
    provider_results = await asyncio.gather(*tasks)

    merged = merge_results(list(provider_results))

    # Compute CY from AI-provided dimensions where available
    dimension_replaced = _compute_item_cy_from_dimensions(merged.get("items") or [])
    if dimension_replaced > 0:
        merged.setdefault("_meta", {})["dimension_cy_replacements"] = dimension_replaced

    merged, fuzzy_dedup_count = deduplicate_merged_items(merged)
    meta = merged.pop("_meta", {})
    if fuzzy_dedup_count > 0:
        meta["fuzzy_dedup_count"] = fuzzy_dedup_count
    return merged, meta
