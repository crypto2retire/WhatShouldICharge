"""
Parallel dual-estimation pipeline for WSIC.
Runs two vision providers simultaneously and merges results.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from services.vision_providers import VisionProvider, VisionResult, VisionProviderError, GeminiProvider, VeniceProvider, OpenRouterProvider
from database import AsyncSessionLocal
from models import ProviderHealthEvent

logger = logging.getLogger("wsic.pipeline")

VARIANCE_FLAG_THRESHOLD = 0.20


def _build_providers() -> list[VisionProvider]:
    providers = []
    gemini_key = os.environ.get("GEMINI_API_KEY")
    venice_key = os.environ.get("VENICE_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if gemini_key:
        providers.append(GeminiProvider())
    if venice_key:
        providers.append(VeniceProvider())
    if openrouter_key:
        providers.append(OpenRouterProvider())
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
    providers = _build_providers()
    if not providers:
        raise RuntimeError("No vision providers configured. Set GEMINI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY.")

    max_photos = 4
    img_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "image"]
    if len(img_blocks) > max_photos:
        text_blocks = [b for b in images if isinstance(b, dict) and b.get("type") == "text"]
        images = text_blocks[:4] + img_blocks[:max_photos]

    tasks = [_run_single(p, images, prompt) for p in providers]
    provider_results = await asyncio.gather(*tasks)

    merged = merge_results(list(provider_results))
    meta = merged.pop("_meta", {})
    return merged, meta
