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
from sqlalchemy import Column, Integer, Float, DateTime, Text, String, select
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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.on_event("startup")
async def startup():
    await init_db()


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


SYSTEM_PROMPT = """You are an expert junk removal estimator with years of field experience.
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
- FLAT SCREEN TVs: Examine ALL dark rectangular shapes carefully — TVs are often dark colored and blend into backgrounds. Look for the bezel edge, stand base, or screen glare. Any flat rectangular object leaning against a wall that could be a TV MUST be flagged as is_special: true, special_reason: "TV disposal fee". Check behind furniture and along walls — TVs are frequently missed. When in doubt, flag it as a TV.
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


@app.post("/api/estimate")
async def create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
):
    user = await require_user(request)

    if user.estimates_used >= user.estimates_limit:
        raise HTTPException(
            status_code=403,
            detail="estimate_limit_reached"
        )

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
        market_rates = {"low": 35, "high": 40, "premium": 55, "source": "default_rates"}

    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": image_content + [{
                    "type": "text",
                    "text": "Analyze these junk removal photos and provide your estimate as JSON."
                }]
            }]
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid ANTHROPIC_API_KEY.")
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

    price_low, price_high, cy_mid = calculate_price(
        result_data,
        rate_low=user.price_per_cy_low or 35.0,
        rate_high=user.price_per_cy_high or 40.0,
        rate_premium=user.price_per_cy_premium or 55.0,
        min_charge=user.min_charge or 75.0,
    )
    result_data["price_low"] = price_low
    result_data["price_high"] = price_high
    result_data["cy_estimate"] = cy_mid

    async with AsyncSessionLocal() as db:
        est = Estimate(
            user_id=user.id,
            photos_count=len(files),
            result_json=json.dumps(result_data),
            price_low=price_low,
            price_high=price_high,
            cy_estimate=cy_mid,
        )
        db.add(est)
        await db.commit()
        await db.refresh(est)
        estimate_id = est.id

        await db.execute(
            User.__table__.update()
            .where(User.id == user.id)
            .values(estimates_used=User.estimates_used + 1)
        )
        await db.commit()

    confidence = result_data.get("confidence", 75)
    remaining = max(0, user.estimates_limit - user.estimates_used - 1)

    resp = {
        "id": estimate_id,
        "price_low": price_low,
        "price_high": price_high,
        "cy_estimate": cy_mid,
        "items": result_data.get("items", []),
        "job_type": result_data.get("job_type", "standard"),
        "conditions": result_data.get("conditions", []),
        "notes": result_data.get("notes", ""),
        "confidence": confidence,
        "estimates_remaining": remaining,
    }

    if market_context:
        resp["market_context"] = market_context

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
