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

        "combined_prompt": """You are an expert junk removal estimator. Analyze the photos to identify every item staged for removal AND estimate its loaded volume in cubic yards. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

1 CUBIC YARD = 3ft x 3ft x 3ft = 27 cubic feet. A standard washing machine is ~1 CY. A pickup truck bed is ~2 CY.

IDENTIFICATION RULES:
1. List every distinct item or group of same-type items you can see.
2. Count quantities carefully — if you see 4 trash bags, list quantity: 4, not 1.
3. Flag special disposal items (is_special: true): TVs, monitors, mattresses, box springs, tires, propane tanks, refrigerators/freezers, AC units, paint cans, chemicals, e-waste, batteries, fluorescent bulbs.
4. Check for duplicates across multiple photos — same item from different angles should be counted once.
5. Do NOT count installed or background fixtures (wall shelving, garage shelving, mounted items) unless clearly staged for removal.
6. If you see a person, doorframe, standard appliance, or other known-size object, note it as a reference_point for scale.

VOLUME REFERENCE TABLE — USE THESE VALUES. DO NOT EXCEED THESE FOR SIMILAR ITEMS:

BAGS & SOFT GOODS:
- Kitchen/trash bag (13 gal): 0.1 CY
- Large trash bag (30-40 gal, yard/leaf bag): 0.2-0.33 CY
- Contractor bag (full, heavy): 0.3-0.4 CY
- Duffel bag / gym bag: 0.2 CY
- Suitcase (carry-on): 0.15 CY
- Suitcase (large/checked): 0.3 CY
- Pillow: 0.1 CY
- Sleeping bag: 0.15 CY

BOXES & CONTAINERS:
- Small box (book box, ~1.5 cu ft): 0.06 CY
- Medium box (~3 cu ft): 0.11 CY
- Large box (~4.5 cu ft): 0.17 CY
- Wardrobe box: 0.5 CY
- Plastic storage tote (small): 0.1 CY
- Plastic storage tote (large): 0.2 CY
- Cardboard box (small): 0.05-0.08 CY
- Cardboard box (medium): 0.1-0.15 CY
- Cardboard box (large): 0.15-0.2 CY

FURNITURE — SEATING:
- Office chair: 0.4 CY
- Dining chair (wood): 0.15 CY
- Folding chair: 0.1 CY
- Armchair / recliner: 1.0-1.5 CY
- Loveseat: 1.0-1.5 CY
- Sofa / couch (3-seat): 1.5-2.0 CY
- Sectional (per piece): 1.0 CY
- Ottoman / footstool: 0.2-0.4 CY

FURNITURE — TABLES & SURFACES:
- Side table / end table: 0.2-0.3 CY
- Coffee table: 0.4-0.6 CY
- Dining table: 1.0-1.5 CY
- Desk (small): 0.6-0.8 CY
- Desk (large / L-shaped): 1.0-1.5 CY
- TV stand / media console: 0.5-0.8 CY
- Dresser (small, 3-drawer): 0.6 CY
- Dresser (large, 6-drawer): 1.0 CY
- Nightstand: 0.2-0.3 CY
- Bookshelf (small): 0.4-0.6 CY
- Bookshelf (large): 0.8-1.0 CY
- Filing cabinet (2-drawer): 0.4 CY
- Filing cabinet (4-drawer): 0.6 CY

FURNITURE — BEDS & BEDDING:
- Twin mattress: 0.5 CY
- Full/double mattress: 0.65 CY
- Queen mattress: 0.75 CY
- King mattress: 0.9 CY
- Box spring (any size): same as matching mattress
- Bed frame (twin/full): 0.3-0.5 CY
- Bed frame (queen/king): 0.5-0.8 CY
- Headboard: 0.3-0.5 CY
- Bunk bed (set): 1.5 CY

APPLIANCES:
- Microwave (small): 0.2 CY
- Microwave (large): 0.4 CY
- Toaster oven: 0.15 CY
- Mini fridge: 0.5 CY
- Refrigerator (full): 1.0 CY
- Washing machine: 0.8-1.0 CY
- Dryer: 0.8-1.0 CY
- Dishwasher: 0.5 CY
- Stove / range: 0.8-1.0 CY
- Window AC unit (small): 0.3 CY
- Window AC unit (large): 0.5 CY
- Dehumidifier: 0.3 CY
- Water heater: 0.8 CY

ELECTRONICS:
- TV (32in): 0.2 CY
- TV (50in): 0.3 CY
- TV (65in+): 0.5 CY
- Computer monitor: 0.15 CY
- Desktop computer (tower): 0.2 CY
- Laptop: 0.03 CY
- Printer (small): 0.15 CY
- Printer (large/office): 0.3 CY
- Stereo / speaker (small): 0.1 CY
- Stereo / speaker (large): 0.3 CY

MISC HOUSEHOLD:
- Bicycle: 0.5 CY
- Exercise equipment (small, weights): 0.2-0.4 CY
- Treadmill: 1.0 CY
- Lawn mower (push): 0.5 CY
- Lawn mower (riding): 2.0 CY
- Grill (charcoal): 0.5 CY
- Grill (gas, full-size): 0.8 CY
- Patio chair: 0.3 CY
- Patio table: 0.6 CY
- Stroller: 0.3 CY
- Car seat: 0.2 CY
- High chair: 0.3 CY
- Playpen: 0.4 CY
- Christmas tree (artificial): 0.4 CY
- Vacuum cleaner: 0.15 CY
- Floor lamp: 0.15 CY
- Table lamp: 0.05 CY
- Area rug (rolled): 0.3-0.5 CY
- Mirror (wall, large): 0.1 CY
- Picture/frame (large): 0.05 CY

PAPER / CLOTHING / BOOKS:
- Stack of papers / documents (1 box worth): 0.06 CY
- Bag of clothing: 0.2-0.3 CY
- Box of books (small): 0.06 CY
- Box of books (large): 0.11 CY
- Shelf of books: 0.2-0.3 CY
- Shoes (pair): 0.02 CY

CONSTRUCTION / OUTDOOR:
- Lumber (2x4, 8ft bundle ~10 boards): 0.3 CY
- Plywood sheet: 0.1 CY
- Drywall sheet: 0.1 CY
- Bag of concrete (60lb): 0.05 CY
- Potted plant (small): 0.05 CY
- Potted plant (large): 0.2 CY
- Tire (car): 0.2 CY
- Tire (truck): 0.3 CY
- Propane tank (20lb): 0.15 CY
- Paint can (1 gallon): 0.03 CY
- Paint can (5 gallon): 0.1 CY
- Single wooden board / plank: 0.05-0.1 CY
- Wooden boards and lumber pieces (per piece): 0.08-0.15 CY
- Scrap wood pieces (per piece): 0.05-0.1 CY

BROKEN / DAMAGED ITEMS — these take LESS space than intact items:
- Broken chair frame: 0.15-0.25 CY (less than intact chair)
- Broken wooden furniture piece: 0.15-0.3 CY (less than intact)
- Broken table leg / furniture fragment: 0.05-0.1 CY
- Broken appliance parts: 0.15-0.25 CY
- Metal framework pieces: 0.15-0.3 CY
- Miscellaneous small debris (loose): 0.2-0.4 CY total, NOT per piece

MAXIMUM PER-ITEM CAPS (loaded volume, never exceed):
- Single trash bag: NEVER more than 0.4 CY
- Single box: NEVER more than 0.25 CY
- Single chair: NEVER more than 2.0 CY
- Single mattress: NEVER more than 1.0 CY
- Single appliance: NEVER more than 1.5 CY
- Single piece of furniture: NEVER more than 2.5 CY
- Loose papers/documents pile: NEVER more than 0.5 CY total
- Clothing pile: NEVER more than 1.0 CY total

SIZING METHOD:
- USE REFERENCE OBJECTS for scale (standard interior door: 80x36 in, adult: ~66inH, refrigerator: ~70x36x30 in, couch: ~84x36x34 in).
- ESTIMATE ACTUAL LOADED VOLUME, not ground footprint. Items compress when loaded into a truck.
- BUILD BOTTOM-UP: Total = sum of individual items, nothing more.
- When in doubt, use the LOWER end of the range. Overestimating is worse than underestimating.

PILE / MOUND DEPTH ESTIMATION:
When items form a pile, mound, or stacked heap (NOT neatly lined up), you cannot see everything from the front. Items hide behind and beneath other items.
1. Estimate the pile's total dimensions using reference objects:
   - Standard interior door: 80x36in
   - Cinder block: 16x8x8in
   - Standard step: ~7in tall
   - Chain-link fence (residential): 48in (4ft) or 72in (6ft) tall — posts typically 8ft apart center-to-center
   - Car/pickup truck bed: ~8ft long, ~4.5ft wide
2. Calculate pile volume: width_in × depth_in × height_in / 46656 (converts cubic inches to cubic yards).
3. Apply a material-specific packing factor:
   - General mixed junk: 0.65 (35% air gaps between irregular items)
   - Shingles / roofing materials: 0.85 (flat, dense, stack tightly)
   - Cardboard / paper / soft goods: 0.55 (lots of air, compressible)
   - Lumber / wood / construction: 0.70 (some air between pieces)
   - Dirt / soil / gravel / concrete chunks: 0.90 (nearly solid)
4. Include this as a "pile_estimate" field in your response (see JSON structure below).
5. ONLY include pile_estimate when items are clearly piled/stacked — not when items are neatly arranged side by side.
6. Do NOT add items you cannot see just because the pile is big. Let the pile estimate speak for hidden depth.
7. When you see a fence, USE IT FOR SCALE. Fences have known standard heights and the pile's height relative to the fence is one of the most reliable scale references.

IMPORTANT: Do NOT use the inch symbol (") anywhere. Write "in" for inches.

Return this EXACT JSON structure:
{
  "reference_points": [
    {"name": "reference object", "known_dimensions": "actual size", "location_in_photo": "where", "photo_number": 1}
  ],
  "items": [
    {"name": "item name", "quantity": 1, "cubic_yards": 0.0, "height_in": 0, "width_in": 0, "depth_in": 0, "is_special": false, "is_uncertain": false}
  ],
  "totals": {
    "cubic_yards_low": 0.0,
    "cubic_yards_mid": 0.0,
    "cubic_yards_high": 0.0
  },
  "pile_estimate": {
    "is_pile": false,
    "width_in": 0,
    "depth_in": 0,
    "height_in": 0,
    "packing_factor": 0.65,
    "estimated_cy": 0.0
  },
  "job_type": "standard",
  "conditions": [],
  "confidence": 75,
  "notes": "Brief sizing reasoning."
}

CRITICAL RULES:
- Be thorough — miss nothing. Every bag, box, piece of furniture, appliance, pile counts.
- Same object in multiple photos = count once.
- Do NOT invent phantom "miscellaneous" items to pad the total.
- When uncertain about size, set is_uncertain: true and give your best estimate.
- The low/mid/high range should reflect estimation uncertainty (roughly -15% to +15%).
- Do NOT label as hoarding, whole-house, or construction debris unless clearly supported.
- SIZE BY WHAT YOU SEE, NOT BY CATEGORY NAME. A small wooden table is 0.2 CY, not 1.5 CY just because it is "furniture." Use the reference table above and match the ACTUAL visible size.
- If an item is smaller than a standard refrigerator (~1 CY), it should almost never exceed 1 CY.
- End tables, nightstands, small shelves, stools, and small wood pieces are 0.1-0.4 CY, NOT 1+ CY.
- Do NOT group dissimilar items into vague names like "wood furniture pieces." Name each item specifically (e.g., "end table", "wooden shelf", "dining chair") and size it individually.
- Keep your response concise. Omit null fields entirely.""",

        "sizing_prompt": """You are an expert at estimating real-world dimensions of objects from photographs. Return ONLY valid JSON — no markdown, no explanation, no code blocks.

You will receive:
- Photos of items to be removed
- An item list from the spotting agent identifying each object

Your job is to estimate the ACTUAL LOADED VOLUME in cubic yards for each spotted item ONLY. Do NOT add items that were not in the spotted list. Do NOT re-identify objects from the photos — that was already done. Only estimate sizes for the items you are given.

SIZING METHOD:
1. USE REFERENCE OBJECTS FOR SCALE. Look for these common references:
   - Standard interior door: 80inH x 36inW
   - Standard person (adult): ~66inH
   - Refrigerator: ~70inH x 36inW x 30inD
   - Standard couch: ~84inL x 36inD x 34inH
   - Wooden pallet: 48in x 40in x 6in
   - Railroad tie: 7in x 9in x 8.5ft
   - 5-gallon bucket: 14.5inH x 12in diameter
   - 32-gallon trash can: 22in diameter x 27inH
   - Dollar bill: 6.14in x 2.61in
   - Soda can: 4.83inH x 2.13in diameter

2. ESTIMATE ACTUAL LOADED VOLUME, NOT GROUND FOOTPRINT.
   - Items spread across the ground take up little truck space.
   - A lumber pile laid flat across 10ft × 8ft might only be 2-3 CY when loaded.
   - Furniture has air gaps but loads bulky — a couch ≈ 1.5-2.0 CY, a recliner ≈ 0.7-1.0 CY.
   - Bags compress when loaded. Contractor bags ≈ 0.2-0.4 CY each.

3. COMMON VOLUME BENCHMARKS:
   - Contractor trash bag (full, 42-gal): 0.2-0.4 CY
   - Kitchen trash bag (full, 13-gal): 0.05-0.1 CY
   - Cardboard box small/medium/large: 0.05/0.10/0.15 CY
   - 5-gallon bucket: 0.025 CY
   - Plastic storage container: 0.15-0.3 CY
   - Standard wooden pallet: 0.15 CY
   - Mattress (queen): 0.75 CY, (king): 1.0 CY

4. BROKEN/DISASSEMBLED ITEMS:
   - Estimate the ACTUAL piece size, not the original intact furniture size.
   - A single dresser drawer ≈ 0.1-0.2 CY, not the full dresser volume.
   - Rolled carpet (standard room): 0.3-0.5 CY

5. BUILD BOTTOM-UP: Your total MUST be the sum of individual items — nothing more.
   Do NOT draw a bounding box and inflate. Do NOT invent "miscellaneous" items.

6. CLASSIFY THE JOB:
   - "standard": Easy access, manageable load
   - "premium": Stairs, very heavy items (200+ lbs), difficult access, 10+ CY
   - "hoarder": Floor-to-ceiling overflow with blocked pathways only
   - "truck_load": 14+ CY, full or near-full truck

IMPORTANT: Do NOT use the inch symbol (") anywhere in your JSON. Write "in" for inches. Example: write "80in" not "80\"" or "80"H".

Return this EXACT JSON structure:
{
  "reference_points": [
    {"name": "reference object used", "known_dimensions": "actual size", "estimated_distance_to_item": "description", "photo_number": 1}
  ],
  "items": [
    {"name": "item name", "quantity": 1, "cubic_yards": 0.0, "height_in": 0, "width_in": 0, "depth_in": 0, "is_special": false, "is_uncertain": false}
  ],
  "potential_duplicates": [
    {"item_a": "Couch (photo 1)", "item_b": "Couch (photo 3)", "reason": "Same couch from different angles"}
  ],
  "totals": {
    "cubic_yards_low": 0.0,
    "cubic_yards_mid": 0.0,
    "cubic_yards_high": 0.0
  },
  "pile_estimate": {
    "is_pile": false,
    "width_in": 0,
    "depth_in": 0,
    "height_in": 0,
    "packing_factor": 0.65,
    "estimated_cy": 0.0
  },
  "job_type": "standard",
  "conditions": [],
  "confidence": 75,
  "notes": "Brief explanation of your sizing reasoning and any concerns about accuracy."
}

CRITICAL RULES:
- ONLY estimate volume for items in the spotted list. Do NOT add new items from the photos.
- If a potential duplicate is flagged, include it only ONCE in your items list.
- Estimate cubic_yards for EACH item individually. Total = sum of items, nothing more.
- Do NOT invent phantom "miscellaneous" items to pad the total.
- When uncertain about a size, set is_uncertain: true and give your best estimate.
- Do NOT label a job as hoarding, whole-house, or construction debris unless clearly supported.
- The low/mid/high range should reflect estimation uncertainty (roughly -15% to +15%).
- Confidence should reflect photo quality and how well sizes can be determined.
- Same item shown from multiple angles = count once only.
- Keep your response concise. Omit null fields entirely. Do not repeat fields that are not needed.""",

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


def get_verification_prompt(industry_id: str) -> str:
    config = get_industry_config(industry_id)
    return (
        config.get("sizing_prompt")
        or config.get("verification_prompt")
        or config.get("extraction_prompt")
        or config.get("system_prompt", "")
    )

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
