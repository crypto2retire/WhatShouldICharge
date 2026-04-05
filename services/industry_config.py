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

        # Extraction prompt for Claude Vision
        "extraction_prompt": """You are a junk removal estimator with years of field experience. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

Look at the photo(s) and estimate the total cubic yards of junk/debris to be removed. Use your best judgment as an experienced estimator — consider what these items would actually take up when loaded into a truck.

IMPORTANT GUIDELINES:

1. ESTIMATE ACTUAL LOADED VOLUME, NOT FOOTPRINT.
   - Items spread across the ground take up very little truck space. A pile of lumber laid flat across a 10ft x 8ft area might only be 2-3 CY when loaded.
   - Scattered items across a yard are NOT a solid block. Estimate each item/group separately and add them up.
   - Furniture has air gaps but loads bulky — a couch is roughly 1.5-2 CY, a recliner about 0.7-1.0 CY, a mattress about 0.5-0.7 CY.
   - Think about how items load into a truck: loose boards stack tight, furniture has voids, bags compress.

2. USE REFERENCE ITEMS FOR SCALE (if visible):
   - Standard interior door: 80"H x 36"W
   - Refrigerator: ~70"H x 36"W x 30"D (~1.5 CY)
   - Standard couch: ~84"L x 36"D x 34"H (~1.5-2.0 CY)
   - Wooden pallet: 48" x 40" x 6" (~0.15 CY)
   - Standard railroad tie: 7" x 9" x 8.5ft (~0.17 CY each)
   - 5-gallon bucket: 14.5"H x 12" diameter (~0.025 CY)
   - 32-gallon trash can: 22" diameter x 27"H (~0.12 CY)

COMMON ITEM VOLUME BENCHMARKS:
   - Contractor trash bag (full, 42-gal): 0.2-0.4 CY
   - Kitchen trash bag (full, 13-gal): 0.05-0.1 CY  
   - Cardboard box (small, 1.5 cu ft): 0.05 CY
   - Cardboard box (medium, 3 cu ft): 0.1 CY
   - Cardboard box (large, 4.5 cu ft): 0.15 CY
   - 5-gallon bucket: 0.025 CY
   - Plastic storage container (standard): 0.15-0.3 CY
   - Milk crate: 0.04 CY
   - Standard wooden pallet: 0.15 CY

BROKEN/DISASSEMBLED ITEMS:
   - Broken furniture pieces, drawer units, cabinet fragments: estimate the ACTUAL size of the piece, NOT the size of the original intact furniture. A single dresser drawer is ~0.1-0.2 CY, not 1.5 CY like a full dresser.
   - Disassembled wood framing, fence sections laid flat: estimate loaded/stacked volume, not ground footprint.
   - Rolled carpet (standard room): 0.3-0.5 CY
   - Rolled carpet pad/underlayment: 0.1-0.2 CY

3. BUILD YOUR ESTIMATE BOTTOM-UP FROM ITEMS.
   - Identify each item or group of items you can see
   - Estimate each one's volume in cubic yards
   - Your total is the SUM of individual items — nothing more
   - Do NOT calculate a bounding box and force items to match it
   - Do NOT invent "miscellaneous debris" to pad the total — only list what you can actually see

4. FLAG SPECIAL DISPOSAL ITEMS (is_special: true):
   TVs, monitors, mattresses, box springs, tires, propane tanks, refrigerators/freezers,
   AC units, paint cans, chemicals, e-waste, batteries, fluorescent bulbs

5. CHECK FOR DUPLICATES across multiple photos — same item from different angles should not be counted twice.

6. DO NOT COUNT INSTALLED OR BACKGROUND STORAGE/FIXTURES unless they are clearly staged for removal.
   - Garage shelving, wall shelving, mounted shelves, and background storage systems are usually part of the space, not the haul-away pile.
   - Items sitting on shelves in the background should not be counted unless the photo clearly shows they are included for removal.
   - When in doubt, count the foreground haul-away items only.

7. CLASSIFY THE JOB:
   - "standard": Easy access, mostly furniture/boxes, manageable load
   - "premium": Stairs, very heavy items (200+ lbs), difficult access, large volume (10+ CY)
   - "hoarder": Only for true floor-to-ceiling or room-wide overflow with blocked pathways, not for a few bags and scattered garage items
   - "truck_load": Full or near-full truck load (14+ CY)

Return this EXACT JSON structure:
{
  "reference_points": [
    {"name": "item used for scale reference", "known_dimensions": "description", "cubic_yards": 0.0, "location_in_photo": "description", "photo_number": 1}
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
  "notes": "Brief description of what you see, your volume reasoning, and any concerns."
}

CRITICAL RULES:
- Your total MUST be the sum of individual item estimates — bottom-up, not top-down
- Do NOT draw a bounding box around everything and call it the volume
- Do NOT invent phantom "miscellaneous" items to reach a spatial total
- When you see many small items (bags, boxes, buckets, small debris), estimate each one individually at its ACTUAL size. Do NOT round up small items to large volumes. Eight contractor bags at 0.3 CY each = 2.4 CY total, not 19.2 CY.
- For flat/spread-out items, estimate their LOADED truck volume, not ground coverage
- Same bag pile, same couch, or same appliance shown from two angles should still be counted once
- Do NOT label a job as hoarding, whole-house, or construction debris unless the photos clearly support that scale/job type
- The low/mid/high range should reflect estimation uncertainty (roughly -15% to +15%)
- Confidence should reflect photo quality and how well you can see everything
- Flag ALL special disposal items with is_special: true
- Detect duplicates across multiple photos""",

        "verification_prompt": """You are a senior junk removal estimator doing a second-pass verification. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

You will receive:
- the same photos from Pass 1
- the first estimator's JSON result

Your job is to VERIFY, REMOVE, or CORRECT items using the actual photos. Be skeptical. Do not inflate the estimate.

VERIFICATION RULES:
1. Only keep items you can visually confirm in the photos.
2. If an item appears to be the same object from multiple angles, count it once.
3. Recount grouped items carefully:
   - contractor trash bags are usually about 0.2-0.4 CY each when full
   - kitchen bags are usually about 0.05-0.1 CY each
   - paint buckets/cans should stay small per item
4. Remove background fixtures and storage systems unless clearly staged for removal.
5. Keep special disposal flags only when you can visually confirm the item.
6. If uncertain, keep the item but mark it uncertain rather than padding the estimate.
7. Your corrected total must be the sum of the corrected items only.

Return this EXACT JSON structure:
{
  "reference_points": [
    {"name": "item used for scale reference", "known_dimensions": "description", "cubic_yards": 0.0, "location_in_photo": "description", "photo_number": 1}
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
  "notes": "Brief description of what you see, your corrected volume reasoning, and any concerns.",
  "verification_notes": [
    "Removed background shelving from haul-away count.",
    "Reduced trash bags from 8 to 4 after recounting visible bags."
  ],
  "confirmed_items": ["pedestal fan", "brown trash barrel"],
  "uncertain_items": ["paint buckets and containers"],
  "removed_items": ["metal shelving units"]
}

CRITICAL RULES:
- Be subtractive and corrective, not creative
- Remove items you cannot actually find in the photos
- Do NOT increase a small-job total just to match floor footprint
- Do NOT label a job as hoarding, whole-house, or construction debris unless the photos clearly support that scale/job type
- Keep verification_notes concise and specific""",

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
    """Get the pass-1 extraction prompt for an industry."""
    config = get_industry_config(industry_id)
    return config.get("extraction_prompt") or config.get("system_prompt", "")


def get_verification_prompt(industry_id: str) -> str:
    """Get the pass-2 verification prompt for an industry."""
    config = get_industry_config(industry_id)
    return config.get("verification_prompt") or config.get("extraction_prompt") or config.get("system_prompt", "")

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
