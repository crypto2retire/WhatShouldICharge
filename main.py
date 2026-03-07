import os
import re
import json
import base64
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import anthropic
import bcrypt
import stripe
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, Float, DateTime, Text, String, Boolean, select
import asyncio
from PIL import Image
import io

app = FastAPI(title="WhatShouldICharge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = "sqlite+aiosqlite:///./estimates.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

TIER_LIMITS = {
    "free": 3,
    "starter": 20,
    "pro": 40,
    "agency": 999,
}

STRIPE_PRICES = {
    "price_1T7PXXAPEzwLONiqIIrAtsQZ": "starter",
    "price_1T6iUPAPEzwLONiqp31lIw9T": "pro",
    "price_1T7PXXAPEzwLONiqpQbgpgZ8": "agency",
}


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    company_name = Column(String, default="")
    company_city = Column(String, default="")
    company_state = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    subscription_tier = Column(String, default="free")
    estimates_used = Column(Integer, default=0)
    estimates_limit = Column(Integer, default=3)
    stripe_customer_id = Column(String, default="")
    stripe_subscription_id = Column(String, default="")
    price_per_cy_low = Column(Float, default=35.0)
    price_per_cy_high = Column(Float, default=40.0)
    price_per_cy_premium = Column(Float, default=55.0)
    min_charge = Column(Float, default=75.0)
    truck_capacity_cy = Column(Float, default=16.0)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)


class Estimate(Base):
    __tablename__ = "estimates"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    photos_count = Column(Integer)
    result_json = Column(Text)
    price_low = Column(Float)
    price_high = Column(Float)
    cy_estimate = Column(Float)
    pass1_json = Column(Text, default="")
    pass2_json = Column(Text, default="")
    lookups_json = Column(Text, default="")


class ItemReferenceLibrary(Base):
    __tablename__ = "item_reference_library"
    id = Column(Integer, primary_key=True, index=True)
    item_name = Column(String, unique=True, nullable=False, index=True)
    item_category = Column(String, default="other")
    cubic_yards = Column(Float, nullable=False)
    is_special = Column(Boolean, default=False)
    special_fee = Column(Float, default=0.0)
    confidence = Column(Float, default=1.0)
    source = Column(String, default="builtin")
    search_query_used = Column(String, default="")
    times_seen = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


SEED_ITEMS = [
    ("king mattress", "furniture", 2.5, True, 25.0),
    ("queen mattress", "furniture", 2.0, True, 25.0),
    ("full mattress", "furniture", 1.5, True, 25.0),
    ("twin mattress", "furniture", 1.5, True, 25.0),
    ("box spring", "furniture", 1.5, True, 25.0),
    ("large sectional sofa", "furniture", 5.0, False, 0),
    ("sofa", "furniture", 3.5, False, 0),
    ("loveseat", "furniture", 2.0, False, 0),
    ("recliner", "furniture", 1.5, False, 0),
    ("armchair", "furniture", 1.2, False, 0),
    ("king bed frame", "furniture", 3.0, False, 0),
    ("queen bed frame", "furniture", 2.5, False, 0),
    ("twin bed frame", "furniture", 1.5, False, 0),
    ("large dresser", "furniture", 2.0, False, 0),
    ("small dresser", "furniture", 1.0, False, 0),
    ("nightstand", "furniture", 0.5, False, 0),
    ("coffee table", "furniture", 0.8, False, 0),
    ("dining table large", "furniture", 3.0, False, 0),
    ("dining table small", "furniture", 1.5, False, 0),
    ("dining chair", "furniture", 0.4, False, 0),
    ("large workbench", "furniture", 4.5, False, 0),
    ("small workbench", "furniture", 2.0, False, 0),
    ("bookshelf large", "furniture", 1.5, False, 0),
    ("bookshelf small", "furniture", 0.8, False, 0),
    ("desk large", "furniture", 2.5, False, 0),
    ("desk small", "furniture", 1.2, False, 0),
    ("refrigerator large", "appliance", 2.5, False, 0),
    ("refrigerator small", "appliance", 1.5, False, 0),
    ("washing machine", "appliance", 2.0, False, 0),
    ("dryer", "appliance", 2.0, False, 0),
    ("dishwasher", "appliance", 1.5, False, 0),
    ("stove", "appliance", 2.0, False, 0),
    ("microwave large", "appliance", 0.5, False, 0),
    ("microwave small", "appliance", 0.3, False, 0),
    ("air conditioner window unit", "appliance", 0.8, False, 0),
    ("dehumidifier", "appliance", 0.6, False, 0),
    ("water heater", "appliance", 1.5, False, 0),
    ("large flat screen tv 55+", "electronics", 0.6, True, 25.0),
    ("medium flat screen tv 32-54", "electronics", 0.4, True, 25.0),
    ("small flat screen tv under 32", "electronics", 0.25, True, 25.0),
    ("crt television", "electronics", 0.8, True, 25.0),
    ("desktop computer tower", "electronics", 0.2, False, 0),
    ("monitor", "electronics", 0.2, False, 0),
    ("printer large", "electronics", 0.3, False, 0),
    ("large cardboard box", "debris", 0.15, False, 0),
    ("medium cardboard box", "debris", 0.10, False, 0),
    ("small cardboard box", "debris", 0.06, False, 0),
    ("large plastic tote with lid", "debris", 0.25, False, 0),
    ("small plastic tote", "debris", 0.15, False, 0),
    ("large trash bag full", "debris", 0.15, False, 0),
    ("small trash bag full", "debris", 0.08, False, 0),
    ("plastic outdoor chair", "outdoor", 0.5, False, 0),
    ("metal outdoor chair", "outdoor", 0.5, False, 0),
    ("outdoor dining set 4 chairs table", "outdoor", 4.0, False, 0),
    ("plastic outdoor table", "outdoor", 0.8, False, 0),
    ("riding lawn mower", "outdoor", 4.0, False, 0),
    ("push lawn mower", "outdoor", 1.0, False, 0),
    ("gas grill large", "outdoor", 1.5, False, 0),
    ("gas grill small", "outdoor", 0.8, False, 0),
    ("trampoline", "outdoor", 5.0, False, 0),
    ("swing set", "outdoor", 6.0, False, 0),
    ("hot tub", "outdoor", 15.0, False, 0),
    ("above ground pool", "outdoor", 8.0, False, 0),
    ("4 drawer file cabinet", "other", 0.75, False, 0),
    ("2 drawer file cabinet", "other", 0.4, False, 0),
    ("lateral file cabinet", "other", 1.0, False, 0),
    ("treadmill", "sports", 3.0, False, 0),
    ("elliptical", "sports", 2.5, False, 0),
    ("stationary bike", "sports", 1.5, False, 0),
    ("weight bench", "sports", 1.5, False, 0),
    ("weight set with rack", "sports", 3.0, False, 0),
    ("ping pong table", "sports", 3.0, False, 0),
    ("pool table", "sports", 8.0, False, 0),
    ("wheelchair", "medical", 1.0, False, 0),
    ("hospital bed", "medical", 4.0, False, 0),
    ("walker", "medical", 0.3, False, 0),
    ("propane tank large", "hazardous", 0.5, True, 50.0),
    ("propane tank small", "hazardous", 0.2, True, 25.0),
    ("paint cans box", "hazardous", 0.3, True, 25.0),
    ("car battery", "hazardous", 0.1, True, 15.0),
    ("tire car", "hazardous", 0.5, True, 15.0),
    ("tire truck", "hazardous", 0.8, True, 25.0),
    ("lumber pile small", "debris", 1.0, False, 0),
    ("lumber pile large", "debris", 3.0, False, 0),
    ("drywall sheets", "debris", 0.5, False, 0),
    ("carpet room", "debris", 2.0, False, 0),
]


async def seed_reference_library():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ItemReferenceLibrary).limit(1))
        if result.scalar_one_or_none():
            return
        for name, cat, cy, special, fee in SEED_ITEMS:
            db.add(ItemReferenceLibrary(
                item_name=name,
                item_category=cat,
                cubic_yards=cy,
                is_special=special,
                special_fee=fee,
                confidence=1.0,
                source="builtin",
                times_seen=0,
            ))
        await db.commit()


@app.on_event("startup")
async def startup():
    await init_db()
    await seed_reference_library()


app.mount("/static", StaticFiles(directory="static"), name="static")


async def get_current_user(request: Request) -> Optional[User]:
    token = request.cookies.get("session_token")
    if not token:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Session).where(Session.token == token, Session.expires_at > datetime.utcnow())
        )
        sess = result.scalar_one_or_none()
        if not sess:
            return None
        result = await db.execute(select(User).where(User.id == sess.user_id))
        return result.scalar_one_or_none()


async def require_user(request: Request) -> User:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def send_email(to_email: str, subject: str, html_content: str):
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        return
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email="noreply@whatshouldicharge.app",
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        sg = SendGridAPIClient(api_key)
        sg.send(message)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/landing.html")


@app.get("/estimate", response_class=HTMLResponse)
async def estimator(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/index.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return FileResponse("static/login.html")


@app.get("/signup", response_class=HTMLResponse)
async def signup_page():
    return FileResponse("static/signup.html")


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/library.html")


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/upgrade.html")


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success_page():
    return FileResponse("static/payment-success.html")


@app.post("/api/auth/signup")
async def auth_signup(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    company_name = body.get("company_name", "").strip()
    company_city = body.get("company_city", "").strip()
    company_state = body.get("company_state", "").strip()

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

        user = User(
            email=email,
            password_hash=pw_hash,
            company_name=company_name,
            company_city=company_city,
            company_state=company_state,
            subscription_tier="free",
            estimates_used=0,
            estimates_limit=3,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        token = secrets.token_hex(32)
        sess = Session(
            user_id=user.id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(sess)
        await db.commit()

    send_email(
        email,
        "Welcome to WhatShouldICharge!",
        f"<h2>Welcome, {company_name or 'there'}!</h2>"
        "<p>You have <strong>3 free estimates</strong> to try out the platform.</p>"
        "<p>Upload customer photos and get instant AI-powered pricing.</p>"
        "<p>— The WhatShouldICharge Team</p>"
    )

    response = JSONResponse({"success": True, "redirect": "/estimate"})
    response.set_cookie(
        "session_token", token, httponly=True, samesite="lax",
        max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        if not bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        token = secrets.token_hex(32)
        sess = Session(
            user_id=user.id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(sess)
        await db.commit()

    response = JSONResponse({"success": True, "redirect": "/estimate"})
    response.set_cookie(
        "session_token", token, httponly=True, samesite="lax",
        max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Session).where(Session.token == token))
            sess = result.scalar_one_or_none()
            if sess:
                await db.delete(sess)
                await db.commit()

    response = JSONResponse({"success": True})
    response.delete_cookie("session_token", path="/")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)

    return {
        "authenticated": True,
        "email": user.email,
        "company_name": user.company_name,
        "company_city": user.company_city,
        "company_state": user.company_state,
        "subscription_tier": user.subscription_tier,
        "estimates_used": user.estimates_used,
        "estimates_limit": user.estimates_limit,
        "price_per_cy_low": user.price_per_cy_low,
        "price_per_cy_high": user.price_per_cy_high,
        "price_per_cy_premium": user.price_per_cy_premium,
        "min_charge": user.min_charge,
        "truck_capacity_cy": user.truck_capacity_cy,
    }


@app.get("/api/library")
async def get_library(request: Request):
    await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary).order_by(ItemReferenceLibrary.times_seen.desc())
        )
        items = result.scalars().all()
        return [
            {
                "id": i.id,
                "item_name": i.item_name,
                "item_category": i.item_category,
                "cubic_yards": i.cubic_yards,
                "is_special": i.is_special,
                "special_fee": i.special_fee,
                "confidence": i.confidence,
                "source": i.source,
                "times_seen": i.times_seen,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "updated_at": i.updated_at.isoformat() if i.updated_at else None,
            }
            for i in items
        ]


@app.get("/api/library/search")
async def search_library(request: Request, q: str = ""):
    await require_user(request)
    if not q.strip():
        return []
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary)
            .where(ItemReferenceLibrary.item_name.contains(q.lower().strip()))
            .order_by(ItemReferenceLibrary.times_seen.desc())
            .limit(50)
        )
        items = result.scalars().all()
        return [
            {
                "id": i.id,
                "item_name": i.item_name,
                "item_category": i.item_category,
                "cubic_yards": i.cubic_yards,
                "is_special": i.is_special,
                "special_fee": i.special_fee,
                "source": i.source,
                "times_seen": i.times_seen,
            }
            for i in items
        ]


@app.post("/api/library/add")
async def add_library_item(request: Request):
    user = await require_user(request)
    body = await request.json()
    name = body.get("item_name", "").lower().strip()
    if not name:
        raise HTTPException(status_code=400, detail="item_name is required.")
    cy = float(body.get("cubic_yards", 0))
    if cy <= 0:
        raise HTTPException(status_code=400, detail="cubic_yards must be positive.")

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(ItemReferenceLibrary).where(ItemReferenceLibrary.item_name == name)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Item already exists in library.")
        item = ItemReferenceLibrary(
            item_name=name,
            item_category=body.get("item_category", "other"),
            cubic_yards=cy,
            is_special=bool(body.get("is_special", False)),
            special_fee=float(body.get("special_fee", 0)),
            confidence=float(body.get("confidence", 0.9)),
            source="manual",
            times_seen=0,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return {"id": item.id, "item_name": item.item_name, "cubic_yards": item.cubic_yards}


@app.put("/api/library/{item_id}")
async def update_library_item(request: Request, item_id: int):
    await require_user(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary).where(ItemReferenceLibrary.id == item_id)
        )
        item = result.scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found.")
        if "cubic_yards" in body:
            item.cubic_yards = float(body["cubic_yards"])
        if "is_special" in body:
            item.is_special = bool(body["is_special"])
        if "special_fee" in body:
            item.special_fee = float(body["special_fee"])
        if "item_category" in body:
            item.item_category = body["item_category"]
        item.updated_at = datetime.utcnow()
        await db.commit()
        return {"success": True}


@app.get("/api/library/stats")
async def library_stats(request: Request):
    await require_user(request)
    async with AsyncSessionLocal() as db:
        all_items = await db.execute(select(ItemReferenceLibrary))
        items = all_items.scalars().all()
        by_source = {}
        for i in items:
            by_source[i.source] = by_source.get(i.source, 0) + 1
        top_seen = sorted(items, key=lambda x: x.times_seen, reverse=True)[:10]
        return {
            "total_items": len(items),
            "by_source": by_source,
            "top_seen": [
                {"item_name": i.item_name, "times_seen": i.times_seen, "cubic_yards": i.cubic_yards}
                for i in top_seen
            ],
        }


def calculate_price(result_data: dict, rate_low=35.0, rate_high=40.0, rate_premium=55.0, min_charge=75.0) -> tuple:
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
        r_low = rate_premium
        r_high = rate_premium
    else:
        r_low = rate_low
        r_high = rate_high

    price_low = cy_low * r_low
    price_high = cy_high * r_high

    surcharge = 0.0
    for item in items:
        if item.get("is_special"):
            qty = int(item.get("quantity", 1))
            surcharge += qty * 25.0

    price_low += surcharge
    price_high += surcharge

    price_low = max(price_low, min_charge)
    price_high = max(price_high, min_charge)

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


async def get_market_rates(city: str, state: str) -> dict:
    if not city or not state:
        return {"low": 35, "high": 40, "premium": 55, "source": "default_rates"}

    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        return {"low": 35, "high": 40, "premium": 55, "source": "default_rates"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": f"junk removal prices cost per cubic yard {city} {state} 2024 2025",
                    "search_depth": "basic",
                    "max_results": 5
                },
                timeout=8.0
            )
            data = response.json()

            content = " ".join([r.get("content", "") for r in data.get("results", [])])

            cy_prices = re.findall(
                r'\$(\d+(?:\.\d+)?)\s*(?:per|/)\s*cubic\s*yard',
                content, re.IGNORECASE
            )

            if cy_prices:
                prices = [float(p) for p in cy_prices]
                avg = sum(prices) / len(prices)
                return {
                    "low": round(avg * 0.85, 2),
                    "high": round(avg * 1.15, 2),
                    "premium": round(avg * 1.5, 2),
                    "market_avg": round(avg, 2),
                    "source": "live_market_search"
                }
    except Exception:
        pass

    return {"low": 35, "high": 40, "premium": 55, "source": "default_rates"}


SYSTEM_PROMPT_BASE = """You are an expert junk removal estimator with years of field experience.
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
- FLAT SCREEN TVs vs WINDOW SCREENS: Dark rectangular objects leaning against walls may be TVs OR window screens — distinguish carefully. A TV will have a visible stand base, port connections on the back/side, a brand logo, a glossy screen surface, or a thick plastic bezel. A window screen has a thin metal or wooden frame with mesh visible through it. Only flag as is_special: true with special_reason: "TV disposal fee" when you can positively identify TV features. When uncertain, do NOT flag as TV — instead add "possible TV or window screen, verify on site" in the notes field for the crew to check.
- Mattresses/box springs = is_special: true
- Tires = is_special: true
- Propane tanks = is_special: true, special_reason: "hazardous"
- Wheelchairs and medical equipment: note in items, not special fee but flag in notes for crew (may be donateable)

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


async def get_library_context() -> str:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary)
            .order_by(ItemReferenceLibrary.times_seen.desc())
            .limit(150)
        )
        items = result.scalars().all()
    if not items:
        return ""
    lines = ["\nKNOWN ITEM REFERENCE LIBRARY (use these CY values when items match):"]
    for item in items:
        line = f"- {item.item_name}: {item.cubic_yards} CY"
        if item.is_special:
            line += f" [SPECIAL FEE: ${item.special_fee}]"
        lines.append(line)
    return "\n".join(lines)


def build_pass2_prompt(pass1_result: dict, library_context: str) -> str:
    return f"""You are a skeptical senior junk removal estimator reviewing a junior estimator's work. You have NOT seen the photos.

JUNIOR ESTIMATOR'S REPORT:
{json.dumps(pass1_result, indent=2)}

YOUR JOB - be skeptical and verify:

1. SANITY CHECK TOTALS
   - Do the individual item CY values add up to the total?
   - Does the total CY make sense for a {pass1_result.get('job_type', 'standard')} job?
   - If total seems off by more than 20%, adjust and explain why

2. VERIFY EACH ITEM against the known reference library:
   {library_context}

   For each item:
   - If it matches a known item, use the library CY value
   - If CY differs from library by more than 25%, flag it
   - If item is unknown (not in library), mark needs_lookup: true

3. COMMON MISIDENTIFICATION FLAGS — be extra skeptical of these:

   TV vs WINDOW SCREEN:
   - Flat screen TVs and window screens look nearly identical in photos — both are dark rectangles with a frame
   - A TV requires visual confirmation of AT LEAST ONE of: brand logo, visible stand/base, port connections, power cable, remote nearby, or retail packaging
   - If none of these are confirmed in the Pass 1 notes or item descriptions, REMOVE the TV from the item list
   - Replace with: "possible TV or window screen — verify on site"
   - Do NOT charge the $25 TV fee unless confirmed
   - Add to verification_notes: "TV flagged but not visually confirmed — marked for on-site verification"

   CARPET vs AREA RUG vs CARPET ROLL:
   - Loose area rugs are different from rolled carpet
   - Rolled carpet = likely building material leftover
   - Only flag as large CY if clearly a full room carpet

   LUMBER vs SHELVING vs PEGBOARD:
   - Verify lumber is actual lumber pile, not shelving units or pegboard panels leaning against wall

   GENERAL RULE: When in doubt about ANY item, mark it as "verify on site" rather than confidently including it with a fee attached.

   - Bags: recount visible bags, use 0.15 CY each
   - Boxes: recount visible boxes

4. CONFIDENCE GATES & SPECIAL ITEM VERIFICATION
   You must be able to verify at least 70% of the total CY from known/confident items.

   For EVERY is_special item (TV, mattress, tire, propane, etc.):
   - Check if Pass 1 description provides enough visual evidence to confirm identification
   - If an is_special item CANNOT be visually confirmed from the Pass 1 description, reduce confidence by 10 points per unconfirmed special item
   - Add all unconfirmed special items to a new field: "verify_on_site" (array of strings describing what needs checking, e.g. "large flat screen TV — possible window screen")
   - Remove the special fee for unconfirmed items (set is_special: false) until verified on site

   If confidence < 70%:
   - List exactly which items you cannot verify
   - Reduce confidence score accordingly
   - Add to notes: "Recommend additional photos of [specific areas]"

5. FINAL ADJUSTMENTS
   - Adjust any CY values that seem wrong
   - Recalculate totals
   - Update job_type if needed
   - Update conditions if needed

Return the SAME JSON format as the junior's report but with corrections applied.
Add a new field: "verification_notes" listing what you changed and why.
Add "items_needing_lookup": ["item name 1", "item name 2"] for any items not in the reference library.
Add "verify_on_site": ["description of item needing on-site verification"] for any unconfirmed special items.
Return ONLY valid JSON with no markdown, no explanation, no code blocks — raw JSON only."""


def parse_ai_json(raw_text: str) -> dict:
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw_text)


async def lookup_item_dimensions(item_name: str, api_key: str) -> dict:
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        return {"cubic_yards": 0, "confidence": 0}

    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": f"{item_name} dimensions inches length width height size",
                    "search_depth": "basic",
                    "max_results": 3
                },
                timeout=8.0
            )
            data = response.json()
            content = " ".join([r.get("content", "") for r in data.get("results", [])])

        client = anthropic.Anthropic(api_key=api_key)

        def run_lookup_call():
            return client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": f"""From this text, extract the dimensions of a {item_name} and calculate cubic yards.

Text: {content[:2000]}

Return ONLY JSON:
{{
    "length_inches": 0,
    "width_inches": 0,
    "height_inches": 0,
    "cubic_yards": 0.0,
    "confidence": 0.8,
    "source_note": "brief note on what was found"
}}

Cubic yards = (L x W x H in inches) / 46656
Round cubic_yards to 2 decimal places.
If you cannot find dimensions, return cubic_yards: 0"""
                }]
            )

        calc_response = await asyncio.to_thread(run_lookup_call)

        result = json.loads(calc_response.content[0].text.strip())

        if result.get("cubic_yards", 0) > 0:
            async with AsyncSessionLocal() as db:
                existing = await db.execute(
                    select(ItemReferenceLibrary).where(
                        ItemReferenceLibrary.item_name == item_name.lower().strip()
                    )
                )
                if not existing.scalar_one_or_none():
                    db.add(ItemReferenceLibrary(
                        item_name=item_name.lower().strip(),
                        cubic_yards=result["cubic_yards"],
                        confidence=result.get("confidence", 0.7),
                        source="web_search",
                        search_query_used=f"{item_name} dimensions",
                        times_seen=1,
                    ))
                    await db.commit()
            return result

    except Exception as e:
        print(f"Lookup failed for {item_name}: {e}")

    return {"cubic_yards": 0, "confidence": 0}


async def update_library_from_estimate(items: list):
    async with AsyncSessionLocal() as db:
        for item in items:
            normalized_name = item.get("name", "").lower().strip()
            if not normalized_name:
                continue

            result = await db.execute(
                select(ItemReferenceLibrary).where(
                    ItemReferenceLibrary.item_name == normalized_name
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.times_seen = existing.times_seen + 1
                existing.updated_at = datetime.utcnow()
            else:
                cy = item.get("cubic_yards", 0)
                if cy and cy > 0:
                    db.add(ItemReferenceLibrary(
                        item_name=normalized_name,
                        item_category=item.get("category", "other"),
                        cubic_yards=cy,
                        is_special=bool(item.get("is_special", False)),
                        special_fee=25.0 if item.get("is_special") else 0.0,
                        confidence=0.7,
                        source="ai_learned",
                        times_seen=1,
                    ))
        await db.commit()


estimate_jobs = {}
JOB_TTL_SECONDS = 300


@app.post("/api/estimate")
async def create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
):
    user = await require_user(request)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        fresh_user = result.scalar_one_or_none()
        if not fresh_user or fresh_user.estimates_used >= fresh_user.estimates_limit:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        fresh_user.estimates_used = fresh_user.estimates_used + 1
        await db.commit()
        user = fresh_user

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured.")

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

    truck_cap = user.truck_capacity_cy or 16.0
    if truck_load_pct is not None:
        truck_cy = round((truck_load_pct / 100.0) * truck_cap, 1)
        image_content.append({
            "type": "text",
            "text": (
                f"\nNote: Customer indicated TRUCK LOAD job. "
                f"Truck filled to {truck_load_pct:.0f}% = {truck_cy} cubic yards ({truck_cap} CY truck). "
                f"Set job_type=truck_load and use premium pricing."
            )
        })

    now = datetime.utcnow()
    expired = [k for k, v in estimate_jobs.items() if (now - v.get("created_at", now)).total_seconds() > JOB_TTL_SECONDS]
    for k in expired:
        del estimate_jobs[k]

    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": user.id,
        "created_at": now,
    }

    asyncio.create_task(run_two_pass_estimate(
        job_id=job_id,
        user=user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


async def run_two_pass_estimate(
    job_id: str,
    user,
    image_content: list,
    api_key: str,
    num_photos: int,
):
    job = estimate_jobs[job_id]
    pass1_json_str = ""
    pass2_json_str = ""
    lookups_json_str = ""

    try:
        library_context = await get_library_context()
        system_prompt = SYSTEM_PROMPT_BASE
        if library_context:
            system_prompt += "\n" + library_context

        job["status"] = "analyzing"
        job["message"] = "Analyzing photos..."

        client = anthropic.Anthropic(api_key=api_key)

        def run_pass1():
            return client.messages.create(
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

        message = await asyncio.to_thread(run_pass1)

        pass1_result = parse_ai_json(message.content[0].text)
        pass1_json_str = json.dumps(pass1_result)

        result_data = pass1_result

        job["status"] = "verifying"
        job["message"] = "Verifying estimate..."

        try:
            pass2_prompt = build_pass2_prompt(pass1_result, library_context)

            def run_pass2():
                return client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2048,
                    messages=[{
                        "role": "user",
                        "content": pass2_prompt
                    }]
                )

            pass2_message = await asyncio.to_thread(run_pass2)
            pass2_result = parse_ai_json(pass2_message.content[0].text)
            pass2_json_str = json.dumps(pass2_result)
            result_data = pass2_result
        except Exception as e:
            print(f"Pass 2 failed, using Pass 1 result: {e}")

        items_needing_lookup = result_data.get("items_needing_lookup", [])
        lookups_done = []
        if items_needing_lookup:
            job["status"] = "looking_up"
            job["message"] = f"Looking up {len(items_needing_lookup)} unknown items..."

            lookup_tasks = [
                lookup_item_dimensions(item_name, api_key)
                for item_name in items_needing_lookup[:5]
            ]
            lookup_results = await asyncio.gather(*lookup_tasks, return_exceptions=True)

            for i, item_name in enumerate(items_needing_lookup[:5]):
                lr = lookup_results[i]
                if isinstance(lr, dict) and lr.get("cubic_yards", 0) > 0:
                    lookups_done.append({
                        "item_name": item_name,
                        "cubic_yards": lr["cubic_yards"],
                        "source_note": lr.get("source_note", ""),
                    })
                    for item in result_data.get("items", []):
                        if item.get("name", "").lower().strip() == item_name.lower().strip():
                            item["cubic_yards"] = lr["cubic_yards"]
                            item["looked_up"] = True
                            break

            if lookups_done:
                lookups_json_str = json.dumps(lookups_done)
                totals = result_data.get("totals", {})
                item_sum = sum(
                    it.get("cubic_yards", 0) * it.get("quantity", 1)
                    for it in result_data.get("items", [])
                )
                if item_sum > 0:
                    totals["cubic_yards_mid"] = round(item_sum, 1)
                    totals["cubic_yards_low"] = round(item_sum * 0.85, 1)
                    totals["cubic_yards_high"] = round(item_sum * 1.15, 1)
                    result_data["totals"] = totals

        market_context = None
        try:
            market_rates = await get_market_rates(user.company_city, user.company_state)
            if market_rates.get("source") == "live_market_search":
                market_context = {
                    "city": user.company_city,
                    "state": user.company_state,
                    "market_avg": market_rates.get("market_avg"),
                    "market_low": market_rates.get("low"),
                    "market_high": market_rates.get("high"),
                }
        except Exception:
            pass

        price_low, price_high, cy_mid = calculate_price(
            result_data,
            rate_low=user.price_per_cy_low or 35.0,
            rate_high=user.price_per_cy_high or 40.0,
            rate_premium=user.price_per_cy_premium or 55.0,
            min_charge=user.min_charge or 75.0,
        )

        async with AsyncSessionLocal() as db:
            est = Estimate(
                user_id=user.id,
                photos_count=num_photos,
                result_json=json.dumps(result_data),
                price_low=price_low,
                price_high=price_high,
                cy_estimate=cy_mid,
                pass1_json=pass1_json_str,
                pass2_json=pass2_json_str,
                lookups_json=lookups_json_str,
            )
            db.add(est)
            await db.commit()
            await db.refresh(est)
            estimate_id = est.id

        try:
            await update_library_from_estimate(result_data.get("items", []))
        except Exception:
            pass

        remaining = max(0, user.estimates_limit - user.estimates_used)

        resp = {
            "id": estimate_id,
            "price_low": price_low,
            "price_high": price_high,
            "cy_estimate": cy_mid,
            "items": result_data.get("items", []),
            "job_type": result_data.get("job_type", "standard"),
            "conditions": result_data.get("conditions", []),
            "notes": result_data.get("notes", ""),
            "confidence": result_data.get("confidence", 75),
            "estimates_remaining": remaining,
            "verification_notes": result_data.get("verification_notes", ""),
            "verify_on_site": result_data.get("verify_on_site", []),
            "items_looked_up": lookups_done,
            "two_pass_verified": bool(pass2_json_str),
        }

        if market_context:
            resp["market_context"] = market_context

        job["status"] = "complete"
        job["message"] = "Estimate ready!"
        job["result"] = resp

    except Exception as e:
        job["status"] = "error"
        job["message"] = str(e)
        job["result"] = None


@app.get("/api/estimate/status/{job_id}")
async def estimate_status(request: Request, job_id: str):
    user = await require_user(request)
    job = estimate_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    resp = {
        "status": job["status"],
        "message": job["message"],
    }
    if job["status"] == "complete":
        resp["result"] = job["result"]
        del estimate_jobs[job_id]
    elif job["status"] == "error":
        del estimate_jobs[job_id]

    return resp


@app.post("/api/payments/create-checkout")
async def create_checkout(request: Request):
    user = await require_user(request)
    body = await request.json()
    price_id = body.get("price_id")
    tier_name = body.get("tier_name", "")

    if not price_id:
        raise HTTPException(status_code=400, detail="price_id is required.")

    checkout_params = {
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": str(request.base_url) + "payment-success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": str(request.base_url) + "upgrade",
        "metadata": {"user_id": str(user.id), "tier_name": tier_name},
        "allow_promotion_codes": True,
    }

    if user.stripe_customer_id:
        checkout_params["customer"] = user.stripe_customer_id
    else:
        checkout_params["customer_email"] = user.email

    try:
        session = stripe.checkout.Session.create(**checkout_params)
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@app.post("/api/payments/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = int(session.get("metadata", {}).get("user_id", 0))
        tier_name = session.get("metadata", {}).get("tier_name", "starter")
        customer_id = session.get("customer", "")
        subscription_id = session.get("subscription", "")

        if user_id:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()
                if user:
                    user.subscription_tier = tier_name
                    user.stripe_customer_id = customer_id or ""
                    user.stripe_subscription_id = subscription_id or ""
                    user.estimates_limit = TIER_LIMITS.get(tier_name, 3)
                    user.estimates_used = 0
                    await db.commit()

                    send_email(
                        user.email,
                        f"Your {tier_name.title()} plan is active!",
                        f"<h2>You're all set!</h2>"
                        f"<p>Your <strong>{tier_name.title()}</strong> plan is now active.</p>"
                        f"<p>You have <strong>{TIER_LIMITS.get(tier_name, 3)} estimates</strong> per month.</p>"
                        f"<p>Start estimating at whatshouldicharge.app/estimate</p>"
                    )

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer", "")
        if customer_id:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(User).where(User.stripe_customer_id == customer_id)
                )
                user = result.scalar_one_or_none()
                if user:
                    user.subscription_tier = "free"
                    user.estimates_limit = 0
                    user.stripe_subscription_id = ""
                    await db.commit()

                    send_email(
                        user.email,
                        "Your subscription has been cancelled",
                        "<h2>Subscription cancelled</h2>"
                        "<p>Your WhatShouldICharge subscription has been cancelled.</p>"
                        "<p>You can resubscribe anytime at whatshouldicharge.app/upgrade</p>"
                    )

    return {"received": True}


@app.get("/api/estimates")
async def get_estimates(request: Request):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate)
            .where(Estimate.user_id == user.id)
            .order_by(Estimate.created_at.desc())
            .limit(50)
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
