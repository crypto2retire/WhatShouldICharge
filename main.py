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

    special_keywords = {"tv", "television", "mattress", "tire", "tyre"}
    surcharge = 0.0
    for item in items:
        name_lower = item.get("name", "").lower()
        if item.get("is_special") or any(kw in name_lower for kw in special_keywords):
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

    system_prompt = (
        "You are an expert junk removal estimator. Analyze the photos and "
        "return ONLY valid JSON (no markdown, no explanation) in this exact format:\n\n"
        "{\n"
        '  "items": [\n'
        '    {"name": "item name", "quantity": 1, "category": "furniture|appliance|debris|other", "is_special": false}\n'
        "  ],\n"
        '  "totals": {\n'
        '    "cubic_yards_low": 3.0,\n'
        '    "cubic_yards_mid": 4.0,\n'
        '    "cubic_yards_high": 5.0\n'
        "  },\n"
        '  "job_type": "standard|premium|hoarder|truck_load",\n'
        '  "conditions": ["stairs", "heavy_items", "outdoor", "hoarder"],\n'
        '  "notes": "Brief description of the job"\n'
        "}\n\n"
        "PRICING RULES:\n"
        "- Standard jobs (clean, easy access, under 8 CY): use standard rate\n"
        "- Premium jobs (hoarder, heavy, stairs, outdoor piles, 10+ bags, loaded truck, over 10 CY): use premium rate, set job_type=premium\n"
        "- Truck load photos: set job_type=truck_load, estimate fill % of a 16 CY truck\n"
        "- Hoarder jobs: multiply normal CY by 1.5-2x\n"
        "- Packed bedroom = 10-14 CY minimum\n"
        "- 15+ large bags = 6-8 CY minimum\n"
        "- Loaded truck bed = 8-14 CY depending on fill level\n"
        "Mark is_special=true for: TVs, televisions, mattresses, tires."
    )

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
