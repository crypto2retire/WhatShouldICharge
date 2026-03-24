#!/usr/bin/env python3
"""
WSIC Volume Calculator Test Harness
====================================
Runs test images through the exact same pipeline as production:
  Photo → Claude Vision → volume_lookup validation → price calculation

Usage:
  python tests/run_volume_tests.py                     # Run all images in tests/images/
  python tests/run_volume_tests.py --folder garage      # Run only tests/images/garage/
  python tests/run_volume_tests.py --image couch.jpg    # Run a single image
  python tests/run_volume_tests.py --report             # Save results to tests/results/

Folder structure:
  tests/images/
  ├── garage_cleanout/
  │   ├── garage1.jpg
  │   ├── garage1_expected.txt     ← optional: "4.5" (expected CY)
  │   └── garage2.jpg
  ├── single_items/
  │   ├── couch.jpg
  │   └── couch_expected.txt       ← "1.8"
  ├── truck_loads/
  │   ├── half_truck.jpg
  │   ├── half_truck_expected.txt  ← "8"
  │   └── full_truck.jpg
  └── misc/
      └── yard_waste.jpg

Each _expected.txt file is optional. If present, it should contain a single
number (the expected CY). The harness compares the AI estimate vs expected
and flags large deviations.

Environment:
  ANTHROPIC_API_KEY must be set (or in .env file in project root)
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path so we can import services
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.industry_config import get_system_prompt
from services.volume_lookup import validate_estimate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
IMAGES_DIR = PROJECT_ROOT / "tests" / "images"
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
MODEL = "claude-sonnet-4-20250514"
MAX_DIMENSION = 1600
JPEG_QUALITY = 85

# Pricing defaults (for test output — matches typical CTC setup)
DEFAULT_RATE_LOW = 35.0
DEFAULT_RATE_HIGH = 40.0
DEFAULT_RATE_PREMIUM = 55.0
DEFAULT_MIN_CHARGE = 100.0


def load_env():
    """Load .env file from project root if it exists."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val:
                    os.environ.setdefault(key, val)


def get_api_key():
    """Get Anthropic API key from environment."""
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set. Set it in .env or environment.")
        sys.exit(1)
    return key


def compress_image(raw_bytes: bytes) -> bytes:
    """Compress image to max 1600px, JPEG quality 85, under 1MB."""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw_bytes))

        # Convert RGBA/palette to RGB
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # Resize if too large
        w, h = img.size
        if max(w, h) > MAX_DIMENSION:
            ratio = MAX_DIMENSION / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        # Compress
        quality = JPEG_QUALITY
        while quality >= 30:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= 1_000_000:
                return data
            quality -= 10

        return data  # Return even if over 1MB at quality 30
    except ImportError:
        print("WARNING: Pillow not installed. Using raw image bytes (no compression).")
        return raw_bytes


def find_test_images(folder: str = None, single_image: str = None) -> list[dict]:
    """
    Discover test images. Returns list of dicts:
      {path, category, name, expected_cy}
    """
    images = []

    if single_image:
        # Single image mode
        p = Path(single_image)
        if not p.is_absolute():
            p = IMAGES_DIR / p
        if p.exists():
            expected = _read_expected(p)
            images.append({
                "path": p,
                "category": p.parent.name,
                "name": p.stem,
                "expected_cy": expected,
            })
        else:
            print(f"ERROR: Image not found: {p}")
        return images

    search_dir = IMAGES_DIR
    if folder:
        search_dir = IMAGES_DIR / folder
        if not search_dir.exists():
            print(f"ERROR: Folder not found: {search_dir}")
            return []

    if not search_dir.exists():
        print(f"ERROR: Images directory not found: {search_dir}")
        print(f"Create it and add test images:\n  mkdir -p {IMAGES_DIR}")
        return []

    # Walk directory tree
    for root, dirs, files in os.walk(search_dir):
        root_path = Path(root)
        category = root_path.name if root_path != search_dir else "uncategorized"
        for fname in sorted(files):
            fp = root_path / fname
            if fp.suffix.lower() in ALLOWED_EXTENSIONS:
                expected = _read_expected(fp)
                images.append({
                    "path": fp,
                    "category": category,
                    "name": fp.stem,
                    "expected_cy": expected,
                })

    return images


def _read_expected(image_path: Path) -> float | None:
    """Read expected CY from companion _expected.txt file."""
    expected_file = image_path.parent / f"{image_path.stem}_expected.txt"
    if expected_file.exists():
        try:
            text = expected_file.read_text().strip()
            return float(text)
        except ValueError:
            pass
    return None


def call_claude_vision(api_key: str, image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """
    Call Claude Vision with the production system prompt.
    Returns the parsed JSON response.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    system_prompt = get_system_prompt("junk_removal")

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    image_content = [
        {"type": "text", "text": "Photo 1:"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        },
    ]

    message = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        temperature=0,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": image_content
                + [
                    {
                        "type": "text",
                        "text": "Analyze these junk removal photos and provide your estimate as JSON.",
                    }
                ],
            }
        ],
    )

    # Extract tokens
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens

    # Parse response
    raw_text = message.content[0].text
    result = _parse_ai_json(raw_text)

    return {
        "result": result,
        "raw_text": raw_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": message.model,
    }


def _parse_ai_json(text: str) -> dict:
    """Parse JSON from Claude response, stripping markdown if present."""
    text = text.strip()
    # Strip markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last line if they're fence markers
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    return json.loads(text)


def calculate_price_simple(result_data: dict) -> dict:
    """
    Simplified price calculation matching production logic.
    Returns {price_low, price_high, cy_mid, job_type, is_premium}.
    """
    totals = result_data.get("totals", {})
    cy_mid = float(totals.get("cubic_yards_mid", 0))
    cy_low = float(totals.get("cubic_yards_low", cy_mid * 0.85))
    cy_high = float(totals.get("cubic_yards_high", cy_mid * 1.15))

    job_type = result_data.get("job_type", "standard")
    conditions = result_data.get("conditions", [])

    is_premium = (
        job_type in ("premium", "hoarder", "truck_load")
        or "stairs" in conditions
        or "heavy_items" in conditions
        or cy_mid > 10
    )

    if is_premium:
        rate_low = DEFAULT_RATE_PREMIUM
        rate_high = DEFAULT_RATE_PREMIUM
    else:
        rate_low = DEFAULT_RATE_LOW
        rate_high = DEFAULT_RATE_HIGH

    price_low = round(cy_low * rate_low, 2)
    price_high = round(cy_high * rate_high, 2)

    # Min charge
    min_applied = False
    if price_low < DEFAULT_MIN_CHARGE:
        price_low = DEFAULT_MIN_CHARGE
        min_applied = True
    if price_high < DEFAULT_MIN_CHARGE:
        price_high = DEFAULT_MIN_CHARGE
        min_applied = True

    # Ensure range spread
    if price_high <= price_low:
        price_high = round(price_low * 1.5, 2)
    elif price_high < price_low * 1.15:
        price_high = round(price_low * 1.5, 2)

    return {
        "price_low": price_low,
        "price_high": price_high,
        "cy_mid": cy_mid,
        "cy_low": cy_low,
        "cy_high": cy_high,
        "job_type": job_type,
        "is_premium": is_premium,
        "min_charge_applied": min_applied,
    }


def run_single_test(api_key: str, image_info: dict) -> dict:
    """
    Run a single image through the full pipeline.
    Returns a result dict with all details.
    """
    path = image_info["path"]
    start = time.time()

    # Read and compress
    raw_bytes = path.read_bytes()
    compressed = compress_image(raw_bytes)
    media_type = "image/jpeg"  # After compression, always JPEG

    # Call Claude Vision
    try:
        vision_result = call_claude_vision(api_key, compressed, media_type)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error": f"JSON parse error: {e}",
            "image": str(path.name),
            "category": image_info["category"],
            "elapsed_seconds": round(time.time() - start, 1),
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "image": str(path.name),
            "category": image_info["category"],
            "elapsed_seconds": round(time.time() - start, 1),
        }

    ai_result = vision_result["result"]

    # Run volume_lookup validation (same as production)
    validated = validate_estimate(ai_result)

    # Calculate price
    pricing = calculate_price_simple(validated)

    # Compare vs expected
    expected_cy = image_info.get("expected_cy")
    deviation = None
    deviation_pct = None
    if expected_cy is not None and expected_cy > 0:
        deviation = pricing["cy_mid"] - expected_cy
        deviation_pct = round((deviation / expected_cy) * 100, 1)

    elapsed = round(time.time() - start, 1)

    return {
        "status": "ok",
        "image": str(path.name),
        "category": image_info["category"],
        "elapsed_seconds": elapsed,
        # Volume
        "cy_low": pricing["cy_low"],
        "cy_mid": pricing["cy_mid"],
        "cy_high": pricing["cy_high"],
        "expected_cy": expected_cy,
        "deviation_cy": round(deviation, 2) if deviation is not None else None,
        "deviation_pct": deviation_pct,
        # Pricing
        "price_low": pricing["price_low"],
        "price_high": pricing["price_high"],
        "job_type": pricing["job_type"],
        "is_premium": pricing["is_premium"],
        "min_charge_applied": pricing["min_charge_applied"],
        # Items
        "items": validated.get("items", []),
        "reference_points": validated.get("reference_points", []),
        "conditions": validated.get("conditions", []),
        "confidence": validated.get("confidence"),
        "notes": validated.get("notes", ""),
        # Tokens
        "input_tokens": vision_result["input_tokens"],
        "output_tokens": vision_result["output_tokens"],
        "model": vision_result["model"],
        # Raw AI text (for debugging)
        "raw_ai_text": vision_result["raw_text"],
    }


def print_result(r: dict, verbose: bool = False):
    """Pretty-print a single test result."""
    if r["status"] == "error":
        print(f"\n{'='*60}")
        print(f"  ERROR: {r['image']} ({r['category']})")
        print(f"  {r['error']}")
        print(f"  Time: {r['elapsed_seconds']}s")
        return

    print(f"\n{'='*60}")
    print(f"  {r['image']}  ({r['category']})")
    print(f"{'='*60}")

    # Volume
    expected_str = ""
    if r["expected_cy"] is not None:
        flag = ""
        if abs(r["deviation_pct"]) > 30:
            flag = " ⚠️  HIGH DEVIATION"
        elif abs(r["deviation_pct"]) > 15:
            flag = " ⚡ moderate deviation"
        expected_str = (
            f"  Expected:   {r['expected_cy']} CY\n"
            f"  Deviation:  {r['deviation_cy']:+.2f} CY ({r['deviation_pct']:+.1f}%){flag}"
        )

    print(f"  Volume:     {r['cy_mid']} CY  (range: {r['cy_low']}–{r['cy_high']})")
    if expected_str:
        print(expected_str)
    print(f"  Price:      ${r['price_low']:.0f}–${r['price_high']:.0f}")
    print(f"  Job type:   {r['job_type']}{'  (PREMIUM)' if r['is_premium'] else ''}")
    print(f"  Confidence: {r['confidence']}%")
    if r["min_charge_applied"]:
        print(f"  ℹ️  Min charge applied (${DEFAULT_MIN_CHARGE})")

    # Items table
    items = r.get("items", [])
    if items:
        print(f"\n  {'Item':<35} {'Qty':>4} {'CY':>7} {'Category':<12} {'Special':>7}")
        print(f"  {'─'*35} {'─'*4} {'─'*7} {'─'*12} {'─'*7}")
        for item in items:
            name = (item.get("name", "???"))[:35]
            qty = item.get("quantity", 1)
            cy = item.get("cubic_yards", 0)
            cat = (item.get("category", ""))[:12]
            special = "YES" if item.get("is_special") else ""
            flags = ""
            if item.get("volume_lookup_applied"):
                flags += " [lookup]"
            if item.get("volume_redistributed"):
                flags += " [redist]"
            print(f"  {name:<35} {qty:>4} {cy:>7.3f} {cat:<12} {special:>7}{flags}")

    # Reference points
    refs = r.get("reference_points", [])
    if refs and verbose:
        print(f"\n  Reference points:")
        for ref in refs:
            print(f"    - {ref.get('name')}: {ref.get('known_dimensions')}")

    # Notes (spatial math)
    if r.get("notes"):
        notes = r["notes"]
        # Just show the spatial math part
        print(f"\n  Notes: {notes[:200]}")

    # Tokens/cost
    inp = r.get("input_tokens", 0)
    out = r.get("output_tokens", 0)
    cost = (inp / 1_000_000) * 3.0 + (out / 1_000_000) * 15.0
    print(f"\n  Tokens: {inp:,} in / {out:,} out  (≈${cost:.3f})")
    print(f"  Time:   {r['elapsed_seconds']}s")


def print_summary(results: list[dict]):
    """Print aggregate summary of all test results."""
    ok = [r for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"] == "error"]
    with_expected = [r for r in ok if r["expected_cy"] is not None]

    print(f"\n\n{'='*60}")
    print(f"  SUMMARY — {len(results)} images tested")
    print(f"{'='*60}")
    print(f"  Passed: {len(ok)}  |  Errors: {len(errors)}")

    if ok:
        total_tokens_in = sum(r.get("input_tokens", 0) for r in ok)
        total_tokens_out = sum(r.get("output_tokens", 0) for r in ok)
        total_cost = (total_tokens_in / 1_000_000) * 3.0 + (total_tokens_out / 1_000_000) * 15.0
        total_time = sum(r["elapsed_seconds"] for r in ok)
        avg_time = total_time / len(ok)
        print(f"  Total cost: ${total_cost:.3f}  |  Avg time: {avg_time:.1f}s/image")

        # Volume stats
        cy_values = [r["cy_mid"] for r in ok]
        print(f"  Volume range: {min(cy_values):.1f}–{max(cy_values):.1f} CY across all tests")

        # Job type breakdown
        types = {}
        for r in ok:
            jt = r["job_type"]
            types[jt] = types.get(jt, 0) + 1
        type_str = ", ".join(f"{k}: {v}" for k, v in sorted(types.items()))
        print(f"  Job types: {type_str}")

    if with_expected:
        print(f"\n  ACCURACY ({len(with_expected)} images with expected values):")
        deviations = [abs(r["deviation_pct"]) for r in with_expected]
        avg_dev = sum(deviations) / len(deviations)
        within_15 = sum(1 for d in deviations if d <= 15)
        within_30 = sum(1 for d in deviations if d <= 30)
        over_30 = sum(1 for d in deviations if d > 30)

        print(f"  Avg absolute deviation: {avg_dev:.1f}%")
        print(f"  Within 15%: {within_15}/{len(with_expected)}  ({within_15/len(with_expected)*100:.0f}%)")
        print(f"  Within 30%: {within_30}/{len(with_expected)}  ({within_30/len(with_expected)*100:.0f}%)")
        print(f"  Over 30%:   {over_30}/{len(with_expected)}  ({over_30/len(with_expected)*100:.0f}%) ⚠️" if over_30 else "")

        # Show worst offenders
        sorted_by_dev = sorted(with_expected, key=lambda r: abs(r["deviation_pct"]), reverse=True)
        if sorted_by_dev and abs(sorted_by_dev[0]["deviation_pct"]) > 15:
            print(f"\n  Largest deviations:")
            for r in sorted_by_dev[:5]:
                print(f"    {r['image']}: {r['cy_mid']} CY vs expected {r['expected_cy']} CY ({r['deviation_pct']:+.1f}%)")

    if errors:
        print(f"\n  ERRORS:")
        for r in errors:
            print(f"    {r['image']}: {r['error'][:80]}")

    print()


def save_results(results: list[dict], report: bool = False):
    """Save results to JSON and optional human-readable report."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON (full data, for programmatic comparison later)
    json_path = RESULTS_DIR / f"run_{timestamp}.json"
    # Strip raw_ai_text to save space
    save_data = []
    for r in results:
        d = dict(r)
        d.pop("raw_ai_text", None)
        save_data.append(d)

    json_path.write_text(json.dumps(save_data, indent=2, default=str))
    print(f"  Results saved: {json_path}")

    return json_path


def main():
    parser = argparse.ArgumentParser(description="WSIC Volume Calculator Test Harness")
    parser.add_argument("--folder", "-f", help="Only test images in this subfolder of tests/images/")
    parser.add_argument("--image", "-i", help="Test a single image file")
    parser.add_argument("--report", "-r", action="store_true", help="Save results to tests/results/")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show reference points and full notes")
    args = parser.parse_args()

    api_key = get_api_key()

    # Find images
    images = find_test_images(folder=args.folder, single_image=args.image)
    if not images:
        print("No test images found.")
        print(f"\nTo get started:")
        print(f"  1. Create folder:  mkdir -p {IMAGES_DIR}")
        print(f"  2. Add subfolders by category:")
        print(f"     mkdir -p {IMAGES_DIR}/garage_cleanout")
        print(f"     mkdir -p {IMAGES_DIR}/single_items")
        print(f"     mkdir -p {IMAGES_DIR}/truck_loads")
        print(f"     mkdir -p {IMAGES_DIR}/yard_waste")
        print(f"     mkdir -p {IMAGES_DIR}/construction")
        print(f"  3. Drop photos into the folders")
        print(f"  4. (Optional) Create expected files:")
        print(f"     echo '4.5' > {IMAGES_DIR}/garage_cleanout/photo1_expected.txt")
        print(f"  5. Run:  python tests/run_volume_tests.py")
        sys.exit(0)

    print(f"WSIC Volume Calculator Test Harness")
    print(f"{'='*60}")
    print(f"Images found: {len(images)}")
    print(f"Model: {MODEL}")
    print(f"Rates: ${DEFAULT_RATE_LOW}–${DEFAULT_RATE_HIGH}/CY (premium ${DEFAULT_RATE_PREMIUM})")
    print(f"Min charge: ${DEFAULT_MIN_CHARGE}")

    with_expected = sum(1 for img in images if img["expected_cy"] is not None)
    if with_expected:
        print(f"Expected values: {with_expected}/{len(images)} images")

    # Run tests
    results = []
    for idx, img in enumerate(images):
        print(f"\n[{idx+1}/{len(images)}] Processing {img['path'].name}...", end="", flush=True)
        result = run_single_test(api_key, img)
        results.append(result)
        if result["status"] == "ok":
            print(f" {result['cy_mid']} CY (${result['price_low']:.0f}–${result['price_high']:.0f})")
        else:
            print(f" ERROR: {result['error'][:60]}")

    # Print detailed results
    for r in results:
        print_result(r, verbose=args.verbose)

    # Summary
    print_summary(results)

    # Save if requested
    if args.report:
        save_results(results, report=True)


if __name__ == "__main__":
    main()
