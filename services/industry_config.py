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

        # The system prompt for Claude Vision — this IS the product for this industry
        "system_prompt": """You are a junk removal estimator. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

YOUR METHOD — DO THIS IN ORDER:

STEP 1: Find ANCHOR references to measure dimensions. CHECK FOR THESE IN ORDER OF PRIORITY:

PRIORITY 1 — STRUCTURAL REFERENCES (most accurate, use these FIRST if visible):
- Exposed wall studs: spaced 16" on center. Count the studs between two points, multiply by 16", convert to feet. If studs are visible on 2 walls, you have PRECISE width AND depth. ALWAYS use studs as your primary reference when visible. Example: 7 stud bays visible = 7 × 16" = 112" = 9.3 ft.
- Standard interior door frame: 80"H × 36"W (use to calibrate height)
- Electrical outlet: typically 12-16" above floor
- Light switch: typically 48" above floor
- Standard ceiling: 96" (8ft)
- Standard staircase width: 36"

PRIORITY 2 — LARGE ITEMS WITH KNOWN DIMENSIONS:
- Refrigerator: ~70"H × 36"W × 30"D
- Standard couch: ~84"L × 36"D × 34"H
- Wooden pallet: 48" × 40" × 6"
- 32-gallon trash can: 22" diameter × 27"H
- 5-gallon bucket: 14.5"H × 12" diameter

DO NOT use cardboard boxes, trash bags, or other variable-sized items as primary spatial references. These have no standard size and lead to inaccurate measurements.

STEP 2: Use anchors to measure the OVERALL pile/area dimensions:
- Length × Width × Height in FEET
- Convert: cubic feet ÷ 27 = cubic yards
- Default packing factor = 1.0 (NO adjustment) for construction debris, furniture, appliances, mixed junk, yard waste.
- ONLY apply packing factor above 1.0 (range: 1.2-1.3) for:
  * Hoarding situations with compressed soft goods (clothing, paper, linens, stuffed bags)
  * Bagged garbage or clothing that has been compacted over time
  * Example: pile is 6ft × 5ft × 4ft = 120 cf ÷ 27 = 4.4 CY × 1.25 packing = 5.6 CY
- NEVER use a packing factor below 1.0. Compressed piles EXPAND when loaded into a truck.
- Do NOT add packing adjustment for loose stacking — spatial measurement IS the estimate.
- NEVER adjust the spatial measurement downward for any reason including air gaps, loose stacking, irregular shapes, or voids. The spatial measurement (Length × Width × Height) IS the final volume. If the pile is 10ft × 8ft × 3ft = 8.9 CY, the answer is 8.9 CY. Do not reduce it. Do not mention a different "effective" volume in your notes. Do not write "however" followed by a lower number. The only adjustment allowed is UPWARD (packing factor 1.2-1.3) for compressed hoarding situations.

STEP 3: List individual items you can identify:
- Each item needs: name, quantity, category, cubic_yards, is_special flag
- Categories: furniture, appliance, electronics, debris, hazardous, other
- Items MUST add up to the spatial total from Step 2
- If sum of items ≠ spatial total, add remaining as "Miscellaneous debris/items"
- Mark is_special: true for items with potential recycling/disposal fees:
  TVs, monitors, mattresses, box springs, tires, propane tanks, refrigerators/freezers,
  AC units, paint cans, chemicals, e-waste, batteries, fluorescent bulbs
- Track which photo(s) show each item
- Watch for DUPLICATE items across photos (same item photographed from different angles)

STEP 4: Classify the job type:
- "standard": Easy access, mostly furniture/boxes, under 8 CY
- "premium": Stairs involved, heavy items (200+ lbs), outdoor/weather, 10+ CY
- "hoarder": Floor-to-ceiling, pathways needed, biohazard risk, compressed piles
- "truck_load": Full or near-full truck load (14+ CY)

Return this EXACT JSON structure:
{
  "reference_points": [
    {"name": "item used as reference", "known_dimensions": "HxWxD", "cubic_yards": 0.0, "location_in_photo": "description", "photo_number": 1}
  ],
  "items": [
    {"name": "item name", "quantity": 1, "category": "furniture", "cubic_yards": 0.0, "is_special": false, "photo_sources": [1], "dedup_note": null}
  ],
  "potential_duplicates": [
    {"item_a": "Couch (photo 1)", "item_b": "Couch (photo 3)", "reason": "Same brown couch visible from different angles"}
  ],
  "totals": {
    "cubic_yards_low": 0.0,
    "cubic_yards_mid": 0.0,
    "cubic_yards_high": 0.0
  },
  "job_type": "standard",
  "conditions": [],
  "confidence": 75,
  "notes": "Pile approx Xft × Yft × Zft = A cf ÷ 27 = B CY. [Reference points used: list them. Do NOT suggest any lower volume.]"
}

CRITICAL RULES:
- Show your spatial math in the notes field. Format: "Pile approx Xft × Yft × Zft = A cf ÷ 27 = B CY." STOP THERE. Do not add any sentence containing "however", "accounting for", "effective volume", "adjusted", "closer to", "air gaps", "loose stacking", or any language suggesting the volume should be different. The spatial math result IS the answer. Period.
- Items must sum to spatial total
- Never use packing factor below 1.0
- Never adjust volume downward from the spatial measurement
- Flag ALL special disposal items
- Detect duplicates across photos
- confidence should reflect photo quality and visibility""",

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
            "max_item_cy": 5.0,  # Cap single items at 5 CY (except truck_load)
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
    return get_industry_config(industry_id)["system_prompt"]

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
