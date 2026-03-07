import os
import json
import base64
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import anthropic
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, Float, DateTime, Text, select
from PIL import Image
import io

app = FastAPI(title="JunkEstimate AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = "sqlite+aiosqlite:///./estimates.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Estimate(Base):
    __tablename__ = "estimates"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    photos_count = Column(Integer)
    result_json = Column(Text)
    price_low = Column(Float)
    price_high = Column(Float)
    cy_estimate = Column(Float)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.on_event("startup")
async def startup():
    await init_db()


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/landing.html")


@app.get("/estimate", response_class=HTMLResponse)
async def estimator():
    return FileResponse("static/index.html")


def calculate_price(result_data: dict) -> tuple:
    job_type = result_data.get("job_type", "standard")
    totals = result_data.get("totals", {})
    conditions = result_data.get("conditions", [])
    items = result_data.get("items", [])

    cy_mid = float(totals.get("cubic_yards_mid", totals.get("cubic_yards_low", 2.0)))
    cy_low = float(totals.get("cubic_yards_low", cy_mid * 0.8))
    cy_high = float(totals.get("cubic_yards_high", cy_mid * 1.2))

    is_premium = (
        job_type in ("premium", "hoarder", "truck_load")
        or "stairs" in conditions
        or "heavy_items" in conditions
        or "hoarder" in conditions
        or cy_mid > 10
    )

    if is_premium:
        rate_low = 55.0
        rate_high = 55.0
    else:
        rate_low = 35.0
        rate_high = 40.0

    price_low = cy_low * rate_low
    price_high = cy_high * rate_high

    surcharge = 0.0
    for item in items:
        if item.get("is_special"):
            qty = int(item.get("quantity", 1))
            surcharge += qty * 25.0

    price_low += surcharge
    price_high += surcharge

    price_low = max(price_low, 75.0)
    price_high = max(price_high, 75.0)

    return round(price_low, 2), round(price_high, 2), round(cy_mid, 1)


def compress_image(image_bytes: bytes, max_size_kb: int = 1000) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    max_dim = 1600
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    output = io.BytesIO()
    quality = 85
    img.save(output, format="JPEG", quality=quality)

    while output.tell() > max_size_kb * 1024 and quality > 30:
        quality -= 10
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality)

    return output.getvalue()


@app.post("/api/estimate")
async def create_estimate(
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY is not configured. Please add it to your Replit Secrets."
        )

    if not files:
        raise HTTPException(status_code=400, detail="At least one photo is required.")
    if len(files) > 6:
        raise HTTPException(status_code=400, detail="Maximum 6 photos allowed.")

    try:
        rooms_list = json.loads(rooms)
    except Exception:
        rooms_list = []

    image_content = []
    for i, file in enumerate(files):
        raw = await file.read()
        compressed = compress_image(raw)
        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        room_label = rooms_list[i] if i < len(rooms_list) else "Unknown"

        image_content.append({"type": "text", "text": f"Photo {i + 1} (Room: {room_label}):"})
        image_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })

    if truck_load_pct is not None:
        truck_cy = round((truck_load_pct / 100.0) * 16.0, 1)
        image_content.append({
            "type": "text",
            "text": (
                f"\nNote: Customer indicated TRUCK LOAD job. "
                f"Truck filled to {truck_load_pct:.0f}% = {truck_cy} cubic yards (16 CY truck). "
                f"Set job_type=truck_load and use premium pricing."
            )
        })

    system_prompt = """You are an expert junk removal estimator with years of field experience.
Analyze ALL photos carefully and return ONLY valid JSON with no markdown, no explanation, no code blocks — raw JSON only.

REQUIRED JSON FORMAT:
{
  "items": [
    {
      "name": "specific item name",
      "quantity": 1,
      "category": "furniture|appliance|electronics|debris|hazardous|other",
      "cubic_yards": 0.5,
      "is_special": false,
      "special_reason": ""
    }
  ],
  "totals": {
    "cubic_yards_low": 3.0,
    "cubic_yards_mid": 4.0,
    "cubic_yards_high": 5.0
  },
  "job_type": "standard|premium|hoarder|truck_load",
  "conditions": [],
  "confidence": 75,
  "notes": "Brief job description for the crew"
}

ITEM IDENTIFICATION RULES:
- Identify every visible item individually, do not group unless identical
- Assign cubic_yards to each item based on actual physical size
- Look specifically along walls, in corners, behind other items
- FLAT SCREEN TVs: look carefully — often leaning against walls or furniture. Any TV = is_special: true, special_reason: "TV disposal fee"
- Mattresses/box springs = is_special: true
- Tires = is_special: true
- Propane tanks = is_special: true, special_reason: "hazardous"
- Wheelchairs and medical equipment: note in items, not special fee but flag in notes for crew (may be donateable)

CUBIC YARD REFERENCE GUIDE:
- King mattress: 2.5 CY
- Queen mattress: 2.0 CY
- Full/twin mattress: 1.5 CY
- Large sofa/sectional: 3-5 CY
- Loveseat: 2.0 CY
- Recliner: 1.5 CY
- Dresser (large): 2.0 CY
- Dresser (small): 1.0 CY
- Workbench (large): 3-5 CY
- File cabinet (4-drawer): 0.75 CY
- File cabinet (2-drawer): 0.4 CY
- Refrigerator: 2.5 CY
- Washer or dryer: 2.0 CY
- Flat screen TV (large): 0.5 CY
- Flat screen TV (small): 0.25 CY
- Cardboard box (large): 0.15 CY
- Cardboard box (small): 0.08 CY
- Trash bag (full): 0.15 CY
- Lumber/wood pieces: measure actual pile volume

JOB TYPE RULES — read carefully:
STANDARD ($35-40/CY): Clean loads, easy access, under 8 CY, no stairs, no heavy items, no clutter

PREMIUM ($55/CY) — set job_type premium if ANY of these:
- Stairs required (basement, upper floor, no elevator)
- Heavy items (appliances, workbenches, safes, exercise equipment)
- Outdoor piles or overgrown areas
- 10+ large trash bags
- Mixed heavy debris
- Total job over 10 CY

HOARDER ($55/CY + volume multiplier):
- Floor to ceiling clutter in multiple rooms
- Narrow pathways through items
- Bags and boxes stacked on furniture
- Multiply estimated CY by 1.5-2.0x
- Packed bedroom minimum: 10-14 CY
- 15+ bags visible: minimum 6-8 CY

TRUCK LOAD — if photo shows items loaded in a truck bed:
- Estimate fill percentage of a standard 16 CY truck
- Set job_type: truck_load
- CY = fill_percentage * 16
- Always premium rate

CONDITIONS LIST — include all that apply:
stairs, heavy_items, outdoor, hoarder, disassembly_needed, multiple_floors, elevator_available, long_carry, truck_load, hazardous_materials, electronics, donation_possible

CONFIDENCE SCORE:
- 90-100: Clear photos, all items visible, straightforward job
- 70-89: Some items obscured, reasonable estimate
- 50-69: Poor lighting, many items hidden, estimate may vary
- Under 50: Cannot see enough to estimate accurately

Always err toward the higher end of CY when visibility is limited. Better to quote slightly high and come down than to under-quote."""

    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": image_content + [{
                    "type": "text",
                    "text": "Analyze these junk removal photos and provide your estimate as JSON."
                }]
            }]
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid ANTHROPIC_API_KEY. Please check your Replit Secrets.")
    except anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="Rate limit reached. Please try again shortly.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")

    raw_text = message.content[0].text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        result_data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"AI returned invalid response: {raw_text[:200]}")

    price_low, price_high, cy_mid = calculate_price(result_data)
    result_data["price_low"] = price_low
    result_data["price_high"] = price_high
    result_data["cy_estimate"] = cy_mid

    async with AsyncSessionLocal() as session:
        est = Estimate(
            photos_count=len(files),
            result_json=json.dumps(result_data),
            price_low=price_low,
            price_high=price_high,
            cy_estimate=cy_mid,
        )
        session.add(est)
        await session.commit()
        await session.refresh(est)
        estimate_id = est.id

    return {
        "id": estimate_id,
        "price_low": price_low,
        "price_high": price_high,
        "cy_estimate": cy_mid,
        "items": result_data.get("items", []),
        "job_type": result_data.get("job_type", "standard"),
        "conditions": result_data.get("conditions", []),
        "notes": result_data.get("notes", ""),
    }


@app.get("/api/estimates")
async def get_estimates():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Estimate).order_by(Estimate.created_at.desc()).limit(50)
        )
        estimates = result.scalars().all()
        return [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "photos_count": e.photos_count,
                "price_low": e.price_low,
                "price_high": e.price_high,
                "cy_estimate": e.cy_estimate,
            }
            for e in estimates
        ]
