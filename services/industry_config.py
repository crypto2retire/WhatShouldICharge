"""
Industry configuration module for WSIC visual estimation platform.
Each industry defines its own prompt template, calibration data, business rules,
and intake form questions. The core estimation engine is industry-agnostic.
"""

INDUSTRIES = {
    "junk_removal": {
        "id": "junk_removal",
        "display_name": "Junk Removal",
        "slug": "junk-removal",
        "unit": "cubic yard",
        "unit_abbrev": "CY",
        "description": "AI-powered junk removal volume estimation from photos",

        "combined_prompt": """You are an expert junk removal estimator. Analyze the photos to estimate total volume by measuring the space occupied by ALL staged items, NOT by individually sizing every object. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

1 CUBIC YARD = 3ft x 3ft x 3ft = 27 cubic feet. A standard washing machine is ~1 CY. A pickup truck bed is ~2 CY. A standard junk truck holds 12-16 CY.

YOUR PRIMARY TASK: SPATIAL MEASUREMENT
For EACH distinct area or room shown in the photos (garage, living room, driveway, etc.), estimate the overall dimensions of ALL staged items in that area combined.

STEP 1 — FIND SCALE REFERENCES
Look for 2-3 objects with known real-world dimensions to establish scale:
- Standard interior door frame: 80inH x 36inW
- Standard kitchen counter: 36inH
- Standard washing machine: ~27inW x 27inD x 39inH
- Standard refrigerator: ~36inW x 30inD x 70inH
- Standard 5-gallon bucket: 14.5inH x 12in diameter
- Adult human: ~66inH
- Wooden pallet: 48in x 40in x 6in
- Cinder block: 16x8x8 in
- Chain-link fence: 48in (4ft) or 72in (6ft) tall

Use these to determine pixels-to-inches conversion. A staged appliance or piece of furniture CAN be used as a scale reference AND tagged as an item.

STEP 2 — MEASURE EACH AREA
For each area/room, estimate the overall bounding box of ALL staged items:
- width_in:  left-to-right span of all items
- depth_in:  front-to-back depth (how far items extend from the wall/door)
- height_in: tallest point of any item in the area

If items are spread across the floor in a single layer, depth might be 12-24in (just the item height). If items are stacked or piled, include the full stack height.

STEP 3 — APPLY PACKING FACTOR
Convert to cubic yards: (width_in × depth_in × height_in) / 46656
Then multiply by packing factor:
- 0.45: Very loose/soft (clothing, pillows, empty boxes — lots of air)
- 0.55: Loose mixed (bags, soft goods, light cardboard)
- 0.65: General mixed junk (furniture, boxes, some air gaps)
- 0.75: Medium density (appliances, tools, moderate stacking)
- 0.85: Dense/flat (shingles, plywood sheets, tightly packed boxes)
- 0.90: Near-solid (dirt, gravel, concrete, construction debris)

The packing factor accounts for air gaps between items when loaded into a truck.

STEP 4 — TAG KEY ITEMS (for pricing, NOT for volume)
Identify only items that matter for pricing rules and customer display:
- is_special: true for regulated disposal items (TVs, monitors, mattresses, box springs, tires, propane tanks, refrigerators/freezers, AC units, paint, chemicals, e-waste, batteries, fluorescent bulbs)
- Also tag any large furniture or appliances for customer visibility

Items in the items list do NOT have cubic_yards — volume comes from area_measurements.
Items only need: name, quantity, is_special, photo_numbers.

MULTI-PHOTO HANDLING:
- You will receive up to 8 photos of the same job from different angles or rooms.
- Group photos by area (garage, living room, driveway, etc.).
- The SAME area in multiple photos = one area_measurement. Do NOT double-count.

CONSOLIDATING DUPLICATE ITEMS:
- If the same type of item appears in multiple photos (e.g., "TV" in photo 2 and "CRT television" in photo 3), they are the SAME item. Create ONE entry with quantity = total count across all photos.
- Never create separate entries for the same item type (e.g., "TV" and "television" or "tire" and "tires" should be one entry with quantity > 1).
- Group similar items: all televisions → "television" (qty = count), all tires → "tires" (qty = count), all boxes → "boxes" (qty = count).
- The items list is for PRICING/TAGGING only — volume comes from area_measurements. Use quantity to represent how many of that item type are present across all photos.
- Each area gets one entry in area_measurements.

IMPORTANT: Do NOT use the inch symbol (") anywhere. Write "in" for inches.

Return this EXACT JSON structure:
{
  "scale_references": [
    {"name": "reference object used", "known_dimensions": "actual size", "photo_number": 1}
  ],
  "area_measurements": [
    {"area_name": "garage", "width_in": 0, "depth_in": 0, "height_in": 0, "packing_factor": 0.65, "estimated_cy": 0.0, "photo_numbers": [1,2]}
  ],
  "items": [
    {"name": "item name", "quantity": 1, "is_special": false, "photo_numbers": [1]}
  ],
  "totals": {
    "cubic_yards_low": 0.0,
    "cubic_yards_mid": 0.0,
    "cubic_yards_high": 0.0
  },
  "job_type": "standard",
  "conditions": [],
  "confidence": 75,
  "notes": "Brief explanation of your spatial reasoning and any concerns."
}

CRITICAL RULES:
- Total CY comes from summing area_measurements ONLY. Items do NOT contribute to volume.
- Be thorough with scale references — bad scale = bad total.
- When in doubt about dimensions, be slightly conservative (mid-point), not aggressive.
- Do NOT label as hoarding, whole-house, or construction debris unless clearly supported.
- Keep your response concise. Omit null fields entirely.""",

        "sizing_prompt": """You are an expert at estimating real-world dimensions of objects from photographs. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

You will receive:
- Photos of items to be removed
- Area measurements from the primary estimator (below)

{{AREA_MEASUREMENTS}}

Your job is to verify and correct the spatial measurements (dimensions and packing factors) for each area. Do NOT add new areas. Do NOT re-identify objects — only verify the existing measurements.

SIZING METHOD:
1. USE REFERENCE OBJECTS FOR SCALE. Look for these FIXED background references:
    - Standard interior door: 80inH x 36inW
    - Standard person (adult): ~66inH
    - Wooden pallet: 48in x 40in x 6in
    - Railroad tie: 7in x 9in x 8.5ft
    - 5-gallon bucket: 14.5inH x 12in diameter
    - 32-gallon trash can: 22in diameter x 27inH
    - Dollar bill: 6.14in x 2.61in
    - Soda can: 4.83inH x 2.13in diameter

2. VERIFY SPATIAL MEASUREMENTS FROM PRIMARY ESTIMATE.
   - Check that the primary estimator's scale references are reasonable.
   - Verify area dimensions (width_in, depth_in, height_in) for each area.
   - Check that packing factors match the visible material density.
   - Recalculate: (width_in × depth_in × height_in) / 46656 × packing_factor = estimated_cy.

3. IF AN AREA MEASUREMENT LOOKS WRONG:
   - Check scale: Did the primary use the right reference object size?
   - Check dimensions: Is the bounding box too tight or too loose?
   - Check packing factor: Is the material denser or looser than the primary thought?
   - Provide corrected values in your response.

4. DO NOT CHANGE THE TOTAL by adding or removing areas. Only correct existing area measurements.

5. CLASSIFY THE JOB:
   - "standard": Easy access, manageable load
   - "premium": Stairs, very heavy items (200+ lbs), difficult access, 10+ CY
   - "hoarder": Floor-to-ceiling overflow with blocked pathways only
   - "truck_load": 14+ CY, full or near-full truck

IMPORTANT: Do NOT use the inch symbol (") anywhere in your JSON. Write "in" for inches.

Return this EXACT JSON structure:
{
  "scale_references": [
    {"name": "reference object used", "known_dimensions": "actual size", "photo_number": 1}
  ],
  "area_measurements": [
    {"area_name": "garage", "width_in": 0, "depth_in": 0, "height_in": 0, "packing_factor": 0.65, "estimated_cy": 0.0, "photo_numbers": [1,2]}
  ],
  "items": [
    {"name": "item name", "quantity": 1, "is_special": false, "photo_numbers": [1]}
  ],
  "totals": {
    "cubic_yards_low": 0.0,
    "cubic_yards_mid": 0.0,
    "cubic_yards_high": 0.0
  },
  "job_type": "standard",
  "conditions": [],
  "confidence": 75,
  "notes": "Brief explanation of corrections made and any concerns."
}

CRITICAL RULES:
- ONLY verify/correct area measurements from the primary estimate. Do NOT add new areas.
- Do NOT add or remove items from the list. Only verify is_special flags.
- Total CY must equal the sum of area_measurements estimated_cy values.
- When uncertain, set is_uncertain: true and explain in notes.
- Do NOT label as hoarding unless clearly supported.
- Keep your response concise. Omit null fields entirely.""",

        # Calibration: known item volumes that override AI guesses
        "calibration_items": {
            "5-gallon bucket": {"cubic_yards": 0.025, "confidence": 1.0},
            "5 gallon bucket": {"cubic_yards": 0.025, "confidence": 1.0},
            "pallet": {"cubic_yards": 0.15, "confidence": 1.0},
            "wooden pallet": {"cubic_yards": 0.15, "confidence": 1.0},
            "cardboard box": {"cubic_yards": 0.15, "confidence": 0.9},
            "moving box": {"cubic_yards": 0.15, "confidence": 0.9},
            "trash bag": {"cubic_yards": 0.03, "confidence": 0.9},
            "garbage bag": {"cubic_yards": 0.03, "confidence": 0.9},
            "tarp": {"cubic_yards": 0.03, "confidence": 0.8},
            "plastic sheeting": {"cubic_yards": 0.03, "confidence": 0.8},
        },

        # Items to exclude from volume redistribution
        "exclude_from_redistribution": [
            "couch", "sofa", "loveseat", "sectional", "recliner",
            "mattress", "box spring", "bed frame", "headboard",
            "tv", "television", "monitor", "screen",
            "refrigerator", "fridge", "freezer",
            "washer", "dryer", "washing machine",
            "dishwasher", "microwave", "oven", "stove", "range",
            "desk", "dresser", "nightstand", "bookshelf", "bookcase",
            "chair", "office chair", "rocking chair",
            "table", "dining table", "coffee table", "end table",
            "bike", "bicycle", "treadmill", "elliptical",
            "grill", "bbq", "water heater", "hot water heater",
            "piano", "pool table"
        ],

        # Keywords for bulk/debris items that CAN be redistributed
        "redistributable_keywords": [
            "debris", "rubble", "scrap", "misc", "miscellaneous",
            "bulk", "mixed", "assorted", "pile", "junk pile",
            "trash pile", "lumber pile", "wood pile",
            "bags", "boxes", "loose items", "clutter",
            "construction debris", "demolition debris", "yard waste"
        ],

        # Business rules
        "rules": {
            "max_item_cy": 16.0,  # Cap single items at truck capacity
            "packing_factor_default": 1.0,
            "packing_factor_hoarding": 1.25,
            "price_range_low_multiplier": 0.90,  # -10%
            "price_range_high_multiplier": 1.20,  # +20%
            "heavy_job_types": ["premium", "hoarder", "truck_load"],
            "heavy_conditions": ["stairs", "heavy_items"],
            "heavy_cy_threshold": 10,
        },

        # Special disposal items (flagged with warnings)
        "special_items": [
            "tv", "television", "monitor", "flat screen",
            "mattress", "box spring",
            "tire", "tires",
            "propane tank", "propane",
            "refrigerator", "fridge", "freezer",
            "air conditioner", "ac unit", "window unit",
            "paint", "paint cans", "stain",
            "chemicals", "solvents", "pesticides",
            "batteries", "car battery",
            "fluorescent", "fluorescent bulbs", "cfl",
            "e-waste", "electronics"
        ],

        # Pricing setup fields required during onboarding
        "pricing_fields": {
            "rate_field": "price_per_cy",
            "rate_label": "Price per Cubic Yard ($)",
            "rate_help": "What you charge per cubic yard of junk removed",
            "supports_dual_rate": True,
            "dual_rate_labels": {
                "standard": "Standard Rate ($/CY)",
                "heavy": "Heavy/Hoarding Rate ($/CY)"
            },
            "default_standard": 35.0,
            "default_heavy": 50.0,
            "min_charge_label": "Minimum Job Charge ($)",
            "default_min_charge": 75.0,
            "truck_capacity_label": "Truck Capacity (CY)",
            "default_truck_capacity": 16.0
        },

        # SEO and marketing
        "landing_page": {
            "headline": "AI-Powered Junk Removal Estimates from Photos",
            "subheadline": "Stop guessing. Start profiting. Get accurate cubic yard estimates in seconds.",
            "target_keywords": [
                "junk removal pricing",
                "how to price junk removal jobs",
                "junk removal estimate tool",
                "starting a junk removal business"
            ]
        }
    }
}

def get_industry_config(industry_id: str) -> dict:
    """Get configuration for a specific industry."""
    config = INDUSTRIES.get(industry_id)
    if not config:
        raise ValueError(f"Unknown industry: {industry_id}. Available: {list(INDUSTRIES.keys())}")
    return config

def get_system_prompt(industry_id: str) -> str:
    """Get the Claude Vision system prompt for an industry."""
    config = get_industry_config(industry_id)
    return config.get("system_prompt") or config.get("extraction_prompt", "")


def get_extraction_prompt(industry_id: str) -> str:
    config = get_industry_config(industry_id)
    return (
        config.get("combined_prompt")
        or config.get("spotting_prompt")
        or config.get("extraction_prompt")
        or config.get("system_prompt", "")
    )


def get_verification_prompt(industry_id: str, area_measurements: list[dict] | None = None, item_list: list[dict] | None = None) -> str:
    """Build verification prompt from primary estimate data.

    New spatial-first format: injects area_measurements into the prompt.
    Falls back to item_list for backward compatibility with old-format responses.
    """
    config = get_industry_config(industry_id)
    base = (
        config.get("sizing_prompt")
        or config.get("verification_prompt")
        or config.get("extraction_prompt")
        or config.get("system_prompt", "")
    )

    # Prefer area_measurements (spatial-first format)
    if area_measurements:
        lines = ["PRIMARY ESTIMATOR AREA MEASUREMENTS:"]
        for area in area_measurements:
            if not isinstance(area, dict):
                continue
            name = area.get("area_name", "?")
            w = area.get("width_in", 0)
            d = area.get("depth_in", 0)
            h = area.get("height_in", 0)
            pf = area.get("packing_factor", 0.65)
            cy = area.get("estimated_cy", 0)
            lines.append(
                f'  - {name}: {int(w)}inW x {int(d)}inD x {int(h)}inH '
                f'(packing {int(pf*100)}%) = {cy:.2f} CY'
            )
        area_text = "\n".join(lines) if len(lines) > 1 else "  (no areas)"
        base = base.replace("{{AREA_MEASUREMENTS}}", area_text)
        # Also replace old placeholder if present
        base = base.replace("{{ITEM_LIST}}", area_text)
    elif item_list:
        # Backward compat: old-format item list
        lines = []
        for it in item_list:
            if not isinstance(it, dict):
                continue
            name = it.get("name", "?")
            qty = it.get("quantity", 1)
            cy = it.get("cubic_yards", 0)
            h = it.get("height_in", 0)
            w = it.get("width_in", 0)
            d = it.get("depth_in", 0)
            dims = f" {int(h)}x{int(w)}x{int(d)} in" if (h and w and d) else ""
            lines.append(f"  - {name} (qty: {qty}, estimated {cy:.2f} CY{dims})")
        item_text = "\n".join(lines) if lines else "  (no items)"
        base = base.replace("{{ITEM_LIST}}", item_text)
        base = base.replace("{{AREA_MEASUREMENTS}}", item_text)
    return base

def get_calibration_items(industry_id: str) -> dict:
    """Get calibration items (known volumes) for an industry."""
    return get_industry_config(industry_id).get("calibration_items", {})

def get_business_rules(industry_id: str) -> dict:
    """Get business rules for an industry."""
    return get_industry_config(industry_id).get("rules", {})

def list_industries() -> list:
    """List all available industries."""
    return [
        {"id": k, "display_name": v["display_name"], "slug": v["slug"]}
        for k, v in INDUSTRIES.items()
    ]
