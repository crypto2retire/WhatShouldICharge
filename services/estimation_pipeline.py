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

    # Conflicting specific sizes → different items (5-gallon vs 1-gallon).
    sz_a = _extract_size_marker(name_a)
    sz_b = _extract_size_marker(name_b)
    if sz_a and sz_b and sz_a != sz_b:
        return False

    words_a = _normalize_for_match(name_a)
    words_b = _normalize_for_match(name_b)
    if not words_a or not words_b:
        return False

    # If core words are identical but one has a substantive parenthetical qualifier
    # (e.g. "cardboard boxes (drawer kits)" vs "large cardboard boxes"),
    # the qualifier makes it a different item. Color/material tags don't count.
    # Also block when both have different substantive qualifiers
    # (e.g. "bed frame (headboard)" vs "bed frame (side rails)").
    if words_a == words_b:
        sub_a = _has_substantive_parenthetical(name_a)
        sub_b = _has_substantive_parenthetical(name_b)
        if sub_a != sub_b:
            return False
        if sub_a and sub_b:
            # Both substantive — merge only if same qualifier
            qual_a = re.search(r"\((.*?)\)", name_a)
            qual_b = re.search(r"\((.*?)\)", name_b)
            if qual_a and qual_b and qual_a.group(1).strip().lower() != qual_b.group(1).strip().lower():
                return False

    intersection = words_a & words_b
    union = words_a | words_b
    jaccard = len(intersection) / len(union) if union else 0.0

    # 1. High Jaccard overlap → duplicate.
    if jaccard >= 0.5:
        return True

    # 2. One name's normalized words are a subset of the other + share ≥2 words.
    shorter = words_a if len(words_a) <= len(words_b) else words_b
    longer = words_b if len(words_a) <= len(words_b) else words_a
    if shorter.issubset(longer) and len(intersection) >= 2:
        return True

    # 3. Containment in raw text + material overlap.
    norm_a = " ".join(sorted(words_a))
    norm_b = " ".join(sorted(words_b))
    if (norm_a in norm_b or norm_b in norm_a) and len(intersection) >= 2:
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


async def run_parallel_estimate(images: list, prompt: str) -> tuple[dict, dict]:
    providers = await _build_providers()
    if not providers:
        raise RuntimeError("No vision providers configured. Set GEMINI_API_KEY, VENICE_API_KEY, or OPENROUTER_API_KEY.")

    max_photos = 4
    img_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "image"]
    if len(img_blocks) > max_photos:
        text_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "text"]
        images = text_blocks[:4] + img_blocks[:max_photos]

    tasks = [_run_single(p, images, prompt) for p in providers]
    provider_results = await asyncio.gather(*tasks)

    merged = merge_results(list(provider_results))
    merged, fuzzy_dedup_count = deduplicate_merged_items(merged)
    meta = merged.pop("_meta", {})
    if fuzzy_dedup_count > 0:
        meta["fuzzy_dedup_count"] = fuzzy_dedup_count
    return merged, meta
