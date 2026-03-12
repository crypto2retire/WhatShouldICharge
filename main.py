import os
import re
import json
import base64
import secrets
import time
import collections
from datetime import datetime, timedelta
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import anthropic
import bcrypt
import stripe
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, Float, DateTime, Text, String, Boolean, select, text, func
import asyncio
from PIL import Image
import io

app = FastAPI(title="WhatShouldICharge")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://js.stripe.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' https://api.stripe.com; "
            "frame-src https://js.stripe.com; "
            "frame-ancestors 'self' *; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(SecurityHeadersMiddleware)


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: dict[str, list[float]] = collections.defaultdict(list)

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        self.requests[key] = [t for t in self.requests[key] if t > window_start]
        if len(self.requests[key]) >= self.max_requests:
            return True
        self.requests[key].append(now)
        return False

    def cleanup(self):
        now = time.time()
        window_start = now - self.window_seconds
        empty_keys = [k for k, v in self.requests.items() if not v or v[-1] < window_start]
        for k in empty_keys:
            del self.requests[k]


auth_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
forgot_password_rate_limiter = RateLimiter(max_requests=5, window_seconds=300)


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

def _get_database_url() -> str:
    """Resolve database URL for Railway PostgreSQL or local SQLite fallback."""
    for key in ("DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = os.environ.get(key, "")
        if url:
            # Normalize to async driver
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url
    # Local dev fallback to SQLite
    return "sqlite+aiosqlite:///./estimates.db"


DATABASE_URL = _get_database_url()
_is_postgres = "asyncpg" in DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    **({
        "pool_size": 10,
        "max_overflow": 5,
        "pool_recycle": 3600,
    } if _is_postgres else {})
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

TIER_LIMITS = {
    "free": 3,
    "starter": 20,
    "pro": 40,
    "agency": 999,
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
    subscription_tier = Column(String, default="free", index=True)
    estimates_used = Column(Integer, default=0)
    estimates_limit = Column(Integer, default=3)
    stripe_customer_id = Column(String, default="")
    stripe_subscription_id = Column(String, default="")
    price_per_cy_low = Column(Float, default=35.0)
    price_per_cy_high = Column(Float, default=40.0)
    price_per_cy_premium = Column(Float, default=55.0)
    min_charge = Column(Float, default=75.0)
    truck_capacity_cy = Column(Float, default=16.0)
    is_admin = Column(Boolean, default=False)
    company_slug = Column(String, default="", index=True)
    company_phone = Column(String, default="")
    company_logo_url = Column(String, default="")


class TeamMember(Base):
    __tablename__ = "team_members"
    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, nullable=False, index=True)
    name = Column(String, nullable=False)
    pin_hash = Column(String, nullable=False)
    role = Column(String, default="estimator")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TeamSession(Base):
    __tablename__ = "team_sessions"
    id = Column(Integer, primary_key=True, index=True)
    team_member_id = Column(Integer, nullable=False)
    owner_user_id = Column(Integer, nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)


class SiteConfig(Base):
    __tablename__ = "site_config"
    id = Column(Integer, primary_key=True, index=True)
    config_key = Column(String, unique=True, nullable=False, index=True)
    config_value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)


class PlanConfig(Base):
    __tablename__ = "plan_configs"
    id = Column(Integer, primary_key=True, index=True)
    tier_name = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=False)
    price_cents = Column(Integer, default=0)
    estimate_limit = Column(Integer, default=3)
    features_json = Column(Text, default="[]")
    stripe_price_id = Column(String, default="")
    is_active = Column(Boolean, default=True)


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
    user_id = Column(Integer, default=0, index=True)
    team_member_id = Column(Integer, default=0, index=True)
    estimate_name = Column(String, default="")
    customer_name = Column(String, default="")
    customer_email = Column(String, default="")
    customer_phone = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
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
    dimensions = Column(String, default="")
    is_special = Column(Boolean, default=False)
    special_fee = Column(Float, default=0.0)
    confidence = Column(Float, default=1.0)
    source = Column(String, default="builtin")
    search_query_used = Column(String, default="")
    times_seen = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


async def init_db():
    """Create tables with retry logic for Railway PostgreSQL startup race."""
    import logging
    logger = logging.getLogger("wsic")

    for attempt in range(5):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info(f"Database tables created (attempt {attempt + 1})")
            break
        except Exception as e:
            if attempt < 4:
                wait = (attempt + 1) * 2
                logger.warning(f"DB init attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"DB init failed after 5 attempts: {e}")
                raise

    # Run migrations — use IF NOT EXISTS for PostgreSQL, try/except for SQLite
    if _is_postgres:
        alter_statements = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS team_member_id INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customer_name TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customer_email TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS customer_phone TEXT DEFAULT ''",
            "ALTER TABLE item_reference_library ADD COLUMN IF NOT EXISTS dimensions TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS estimate_name TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_slug TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_phone TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_logo_url TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_low DOUBLE PRECISION DEFAULT 35.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_high DOUBLE PRECISION DEFAULT 40.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_premium DOUBLE PRECISION DEFAULT 55.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS min_charge DOUBLE PRECISION DEFAULT 75.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS truck_capacity_cy DOUBLE PRECISION DEFAULT 16.0",
        ]
    else:
        alter_statements = [
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN team_member_id INTEGER",
            "ALTER TABLE estimates ADD COLUMN customer_name TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_email TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_phone TEXT DEFAULT ''",
            "ALTER TABLE item_reference_library ADD COLUMN dimensions TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN estimate_name TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN company_slug TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN company_phone TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN company_logo_url TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN price_per_cy_low REAL DEFAULT 35.0",
            "ALTER TABLE users ADD COLUMN price_per_cy_high REAL DEFAULT 40.0",
            "ALTER TABLE users ADD COLUMN price_per_cy_premium REAL DEFAULT 55.0",
            "ALTER TABLE users ADD COLUMN min_charge REAL DEFAULT 75.0",
            "ALTER TABLE users ADD COLUMN truck_capacity_cy REAL DEFAULT 16.0",
        ]

    async with engine.begin() as conn:
        for stmt in alter_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass


SEED_ITEMS = [
    # Volumes audited 2026-03-12. Calibrated against: 33-gal bag=0.15 CY, contractor bag=0.30 CY.
    # 1 CY = 46,656 cubic inches = 27 cubic feet.
    # Mattresses & bedding
    ("king mattress", "furniture", 1.50, True, 25.0, "76×80×11 in"),
    ("queen mattress", "furniture", 1.25, True, 25.0, "60×80×11 in"),
    ("full mattress", "furniture", 1.00, True, 25.0, "54×75×11 in"),
    ("twin mattress", "furniture", 0.75, True, 25.0, "38×75×11 in"),
    ("box spring", "furniture", 1.00, True, 25.0, "60×80×9 in"),
    # Seating
    ("large sectional sofa", "furniture", 5.50, False, 0, "120×90×36 in"),
    ("sofa", "furniture", 2.00, False, 0, "84×36×34 in"),
    ("loveseat", "furniture", 1.50, False, 0, "60×36×34 in"),
    ("recliner", "furniture", 1.25, False, 0, "36×38×40 in"),
    ("armchair", "furniture", 0.75, False, 0, "32×34×34 in"),
    # Bed frames
    ("king bed frame", "furniture", 1.50, False, 0, "80×76×14 in"),
    ("queen bed frame", "furniture", 1.25, False, 0, "80×60×14 in"),
    ("twin bed frame", "furniture", 0.75, False, 0, "75×38×14 in"),
    # Bedroom furniture
    ("large dresser", "furniture", 1.25, False, 0, "60×18×34 in"),
    ("small dresser", "furniture", 0.75, False, 0, "36×18×30 in"),
    ("nightstand", "furniture", 0.25, False, 0, "24×16×26 in"),
    # Tables
    ("coffee table", "furniture", 0.50, False, 0, "48×24×18 in"),
    ("dining table large", "furniture", 1.75, False, 0, "72×42×30 in"),
    ("dining table small", "furniture", 0.75, False, 0, "48×30×30 in"),
    ("dining chair", "furniture", 0.25, False, 0, "18×20×38 in"),
    # Workbenches & desks
    ("large workbench", "furniture", 2.50, False, 0, "96×30×36 in"),
    ("small workbench", "furniture", 1.25, False, 0, "60×24×34 in"),
    ("bookshelf large", "furniture", 0.75, False, 0, "36×12×72 in"),
    ("bookshelf small", "furniture", 0.35, False, 0, "30×10×48 in"),
    ("desk large", "furniture", 1.25, False, 0, "60×30×30 in"),
    ("desk small", "furniture", 0.75, False, 0, "42×24×30 in"),
    # Appliances
    ("refrigerator large", "appliance", 2.00, False, 0, "36×30×70 in"),
    ("refrigerator small", "appliance", 1.25, False, 0, "28×28×60 in"),
    ("washing machine", "appliance", 1.00, False, 0, "27×27×38 in"),
    ("dryer", "appliance", 1.00, False, 0, "27×29×38 in"),
    ("dishwasher", "appliance", 0.75, False, 0, "24×24×35 in"),
    ("stove", "appliance", 1.00, False, 0, "30×26×36 in"),
    ("microwave large", "appliance", 0.25, False, 0, "24×18×14 in"),
    ("microwave small", "appliance", 0.15, False, 0, "18×14×11 in"),
    ("air conditioner window unit", "appliance", 0.35, False, 0, "24×20×16 in"),
    ("dehumidifier", "appliance", 0.25, False, 0, "16×12×24 in"),
    ("water heater", "appliance", 0.75, False, 0, "22×22×54 in"),
    # Electronics
    ("large flat screen tv 55+", "electronics", 0.50, True, 25.0, "49×4×29 in"),
    ("medium flat screen tv 32-54", "electronics", 0.35, True, 25.0, "37×3×22 in"),
    ("small flat screen tv under 32", "electronics", 0.20, True, 25.0, "28×3×17 in"),
    ("crt television", "electronics", 0.50, True, 25.0, "24×20×20 in"),
    ("desktop computer tower", "electronics", 0.15, False, 0, "18×8×17 in"),
    ("monitor", "electronics", 0.20, False, 0, "24×8×18 in"),
    ("printer large", "electronics", 0.20, False, 0, "20×18×14 in"),
    # Boxes & containers
    ("large cardboard box", "debris", 0.15, False, 0, "24×18×18 in"),
    ("medium cardboard box", "debris", 0.10, False, 0, "18×14×14 in"),
    ("small cardboard box", "debris", 0.05, False, 0, "12×12×12 in"),
    ("large plastic tote with lid", "debris", 0.20, False, 0, "30×20×16 in"),
    ("small plastic tote", "debris", 0.10, False, 0, "22×16×12 in"),
    # Trash bags (calibration anchors — used for scale reference)
    ("contractor trash bag full", "debris", 0.30, False, 0, "24×24×30 in"),
    ("standard trash bag full 33 gal", "debris", 0.15, False, 0, "22×20×26 in"),
    ("small trash bag full", "debris", 0.10, False, 0, "18×18×24 in"),
    # Outdoor furniture
    ("plastic outdoor chair", "outdoor", 0.25, False, 0, "22×24×34 in"),
    ("metal outdoor chair", "outdoor", 0.35, False, 0, "22×24×34 in"),
    ("outdoor dining set 4 chairs table", "outdoor", 2.50, False, 0, "48×48×30 in + 4 chairs"),
    ("plastic outdoor table", "outdoor", 0.60, False, 0, "36×36×28 in"),
    # Outdoor equipment
    ("riding lawn mower", "outdoor", 3.00, False, 0, "66×42×44 in"),
    ("push lawn mower", "outdoor", 1.00, False, 0, "56×22×42 in"),
    ("gas grill large", "outdoor", 1.25, False, 0, "56×22×44 in"),
    ("gas grill small", "outdoor", 0.75, False, 0, "40×18×38 in"),
    ("trampoline", "outdoor", 4.00, False, 0, "144 in diameter × 36 in tall"),
    ("swing set", "outdoor", 6.00, False, 0, "144×96×84 in"),
    ("hot tub", "outdoor", 6.00, False, 0, "84×84×36 in"),
    ("above ground pool", "outdoor", 8.00, False, 0, "180 in diameter × 52 in tall"),
    # Office
    ("4 drawer file cabinet", "other", 0.75, False, 0, "15×25×52 in"),
    ("2 drawer file cabinet", "other", 0.40, False, 0, "15×25×29 in"),
    ("lateral file cabinet", "other", 0.75, False, 0, "36×18×28 in"),
    # Exercise equipment
    ("treadmill", "sports", 2.50, False, 0, "72×34×56 in"),
    ("elliptical", "sports", 2.50, False, 0, "70×28×64 in"),
    ("stationary bike", "sports", 1.00, False, 0, "42×22×48 in"),
    ("weight bench", "sports", 1.25, False, 0, "56×26×46 in"),
    ("weight set with rack", "sports", 2.00, False, 0, "48×24×52 in"),
    ("ping pong table", "sports", 2.50, False, 0, "108×60×30 in"),
    ("pool table", "sports", 5.00, False, 0, "100×56×32 in"),
    # Medical
    ("wheelchair", "medical", 0.50, False, 0, "26×16×36 in"),
    ("hospital bed", "medical", 2.50, False, 0, "84×36×24 in"),
    ("walker", "medical", 0.25, False, 0, "22×18×34 in"),
    # Hazardous
    ("propane tank large", "hazardous", 0.25, True, 50.0, "12×12×48 in"),
    ("propane tank small", "hazardous", 0.10, True, 25.0, "12×12×18 in"),
    ("paint cans box", "hazardous", 0.15, True, 25.0, "18×12×12 in"),
    ("car battery", "hazardous", 0.10, True, 15.0, "10×7×8 in"),
    ("tire car", "hazardous", 0.25, True, 15.0, "26 in diameter × 8 in wide"),
    ("tire truck", "hazardous", 0.35, True, 25.0, "34 in diameter × 12 in wide"),
    # Construction debris
    ("lumber pile small", "debris", 0.75, False, 0, "48×24×24 in"),
    ("lumber pile large", "debris", 1.75, False, 0, "96×24×36 in"),
    ("drywall sheets", "debris", 0.15, False, 0, "96×48×0.5 in per sheet"),
    ("carpet room", "debris", 1.25, False, 0, "rolled: 12 ft × 18 in diameter"),
]


async def seed_reference_library():
    dims_map = {name: dims for name, _, _, _, _, dims in SEED_ITEMS}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ItemReferenceLibrary).limit(1))
        existing = result.scalar_one_or_none()
        if existing:
            # Update items with missing dimensions
            all_result = await db.execute(
                select(ItemReferenceLibrary).where(
                    (ItemReferenceLibrary.dimensions == None) | (ItemReferenceLibrary.dimensions == "")
                )
            )
            empty_dims = all_result.scalars().all()
            for item in empty_dims:
                if item.item_name in dims_map:
                    item.dimensions = dims_map[item.item_name]

            # Add any new SEED_ITEMS not yet in the database
            name_result = await db.execute(
                select(ItemReferenceLibrary.item_name)
            )
            existing_names = {row[0] for row in name_result.fetchall()}
            added = 0
            for name, cat, cy, special, fee, dims in SEED_ITEMS:
                if name not in existing_names:
                    db.add(ItemReferenceLibrary(
                        item_name=name,
                        item_category=cat,
                        cubic_yards=cy,
                        dimensions=dims,
                        is_special=special,
                        special_fee=fee,
                        confidence=1.0,
                        source="builtin",
                        times_seen=0,
                    ))
                    added += 1
            if empty_dims or added > 0:
                await db.commit()
            return
        for name, cat, cy, special, fee, dims in SEED_ITEMS:
            db.add(ItemReferenceLibrary(
                item_name=name,
                item_category=cat,
                cubic_yards=cy,
                dimensions=dims,
                is_special=special,
                special_fee=fee,
                confidence=1.0,
                source="builtin",
                times_seen=0,
            ))
        await db.commit()


async def seed_plan_configs():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PlanConfig).limit(1))
        if result.scalar_one_or_none():
            return
        plans = [
            PlanConfig(tier_name="free", display_name="Free", price_cents=0, estimate_limit=3,
                       features_json='["3 estimates total","AI photo analysis","Basic pricing"]',
                       stripe_price_id="", is_active=True),
            PlanConfig(tier_name="starter", display_name="Starter", price_cents=2900, estimate_limit=20,
                       features_json='["20 estimates/month","AI photo analysis","Market rate lookup","Item library","Email support"]',
                       stripe_price_id="price_1T7PXXAPEzwLONiqIIrAtsQZ", is_active=True),
            PlanConfig(tier_name="pro", display_name="Pro", price_cents=5900, estimate_limit=40,
                       features_json='["40 estimates/month","Everything in Starter","Priority analysis","Team dashboard","PDF estimates"]',
                       stripe_price_id="price_1T6iUPAPEzwLONiqp31lIw9T", is_active=True),
            PlanConfig(tier_name="agency", display_name="Agency", price_cents=9900, estimate_limit=999,
                       features_json='["Unlimited estimates","Everything in Pro","Unlimited team members","Custom branding","API access"]',
                       stripe_price_id="price_1T7PXXAPEzwLONiqpQbgpgZ8", is_active=True),
        ]
        for p in plans:
            db.add(p)
        await db.commit()


async def seed_site_config():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SiteConfig).limit(1))
        if result.scalar_one_or_none():
            updates = {
                "hero_subtitle": "AI-Assisted Estimates.",
                "hero_description": "Upload customer photos. Get an AI-assisted price range with cubic yard estimates. Close more jobs — without the guesswork.",
                "feature_1_title": "Photo-Based Estimates",
                "feature_1_desc": "No site visit needed. Upload up to 6 photos and let the AI do the heavy lifting. Works with any phone camera.",
                "feature_2_title": "AI-Assisted Pricing",
                "feature_2_desc": "Purpose-built for junk removal — not generic object detection. The AI uses a reference library of 86+ items with real dimensions to estimate volume.",
                "feature_3_title": "Premium Detection",
                "feature_3_desc": "Automatically flags hoarder situations, heavy items, stairs, and outdoor piles — and switches to premium pricing rates.",
            }
            for k, v in updates.items():
                row = await db.execute(select(SiteConfig).where(SiteConfig.config_key == k))
                existing = row.scalar_one_or_none()
                if existing:
                    existing.config_value = v
                else:
                    db.add(SiteConfig(config_key=k, config_value=v))
            await db.commit()
            return
        defaults = {
            "hero_title": "Junk Removal Pricing.",
            "hero_subtitle": "AI-Assisted Estimates.",
            "hero_description": "Upload customer photos. Get an AI-assisted price range with cubic yard estimates. Close more jobs — without the guesswork.",
            "cta_primary": "Try It Free →",
            "cta_secondary": "See How It Works",
            "feature_1_title": "Photo-Based Estimates",
            "feature_1_desc": "No site visit needed. Upload up to 6 photos and let the AI do the heavy lifting. Works with any phone camera.",
            "feature_2_title": "AI-Assisted Pricing",
            "feature_2_desc": "Purpose-built for junk removal — not generic object detection. The AI uses a reference library of 86+ items with real dimensions to estimate volume.",
            "feature_3_title": "Premium Detection",
            "feature_3_desc": "Automatically flags hoarder situations, heavy items, stairs, and outdoor piles — and switches to premium pricing rates.",
            "faq_1_q": "How accurate are the estimates?",
            "faq_1_a": "Our AI-assisted estimates give you a solid starting point for pricing conversations. Accuracy improves with more photos from different angles.",
            "faq_2_q": "What types of junk can it estimate?",
            "faq_2_a": "Furniture, appliances, electronics, yard waste, construction debris, and more. 86+ item types in our reference library with real-world dimensions.",
            "faq_3_q": "How many photos should I upload?",
            "faq_3_a": "We recommend 2-3 photos per room from different angles. You can upload up to 30 photos total. Room labels are optional but improve accuracy.",
        }
        for k, v in defaults.items():
            db.add(SiteConfig(config_key=k, config_value=v))
        await db.commit()


async def ensure_admin_user():
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    if not admin_email:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == admin_email))
        user = result.scalar_one_or_none()
        if user and not user.is_admin:
            user.is_admin = True
            await db.commit()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    await seed_reference_library()
    await seed_plan_configs()
    await seed_site_config()
    await ensure_admin_user()
    yield
    await engine.dispose()


app.router.lifespan_context = lifespan


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


async def require_admin(request: Request) -> User:
    user = await require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def get_team_member(request: Request):
    token = request.cookies.get("team_token")
    if not token:
        return None, None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamSession).where(TeamSession.token == token, TeamSession.expires_at > datetime.utcnow())
        )
        sess = result.scalar_one_or_none()
        if not sess:
            return None, None
        result = await db.execute(select(TeamMember).where(TeamMember.id == sess.team_member_id, TeamMember.is_active == True))
        member = result.scalar_one_or_none()
        if not member:
            return None, None
        result = await db.execute(select(User).where(User.id == sess.owner_user_id))
        owner = result.scalar_one_or_none()
        return member, owner


async def require_team_member(request: Request):
    member, owner = await get_team_member(request)
    if not member or not owner:
        raise HTTPException(status_code=401, detail="Team authentication required")
    return member, owner


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


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")


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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse(url="/estimate", status_code=302)
    return FileResponse("static/admin.html")


@app.get("/team", response_class=HTMLResponse)
async def team_login_page():
    return FileResponse("static/team-login.html")


@app.get("/team/app", response_class=HTMLResponse)
async def team_app_page(request: Request):
    member, owner = await get_team_member(request)
    if not member:
        return RedirectResponse(url="/team", status_code=302)
    return FileResponse("static/team.html")


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/upgrade.html")


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success_page():
    return FileResponse("static/payment-success.html")


@app.get("/estimate/{slug}", response_class=HTMLResponse)
async def customer_estimate_page(slug: str):
    """Public customer-facing estimate page — server-side rendered for SEO."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.company_slug == slug.lower().strip()))
        u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Company not found")

    import html as html_mod
    name = html_mod.escape(u.company_name or "Junk Removal")
    city = html_mod.escape(u.company_city or "")
    state = html_mod.escape(u.company_state or "")
    phone = html_mod.escape(u.company_phone or "")
    logo = html_mod.escape(u.company_logo_url or "")
    safe_slug = html_mod.escape(u.company_slug or slug)
    location = f"{city}, {state}" if city and state else city or state or ""

    title = f"{name} - Free Junk Removal Estimate"
    if location:
        title += f" | {location}"
    meta_desc = f"Get a free instant junk removal estimate from {name}"
    if location:
        meta_desc += f" in {location}"
    meta_desc += ". Upload photos of your items and receive an AI-powered price quote in under 60 seconds. No obligation."

    canonical = f"https://whatshouldicharge.app/estimate/{safe_slug}"

    # --- Rich FAQ content (8 questions, detailed answers, unique per company) ---
    loc_phrase = f" in {location}" if location else ""
    contact_phrase = f"call {name} at {phone}" if phone else f"contact {name}"

    faq_items = [
        (
            f"How much does junk removal cost in {city or 'my area'}?",
            f"Junk removal pricing{loc_phrase} typically ranges from $75 for a minimum load to $500 or more for a full truckload. The exact cost depends on the volume of items, the type of materials being removed, and whether any items require special handling such as appliances with refrigerants or electronics that need proper recycling. {name} uses an AI-powered photo estimate system that calculates your specific price based on the actual items in your photos — so you get a personalized quote, not a generic range. Most single-item pickups like a couch or mattress fall between $75 and $150, while full garage cleanouts or estate cleanouts can range from $300 to $600 depending on volume."
        ),
        (
            f"What is the cheapest way to get rid of junk in {city or 'my area'}?",
            f"The most affordable option depends on what you're removing and how much of it there is. For small amounts, you could haul items to the local transfer station yourself, but you'll need a truck, pay dump fees, and spend your own time loading and driving. For larger jobs, hiring a professional junk removal service like {name} is often more cost-effective when you factor in your time, vehicle rental, and disposal fees. {name} offers a free photo-based estimate so you can see the exact cost before committing — no surprises. We also handle the sorting, loading, hauling, and responsible disposal so you don't have to."
        ),
        (
            f"Does {name} offer same-day junk removal?",
            f"Yes, {name} offers same-day and next-day junk removal{loc_phrase} based on availability. For the fastest service, {contact_phrase} directly after getting your photo estimate. We understand that junk removal is often time-sensitive — whether you're preparing for a move, finishing a renovation, or just need items gone quickly. Our photo estimate tool gives you a price in under 60 seconds so you can make a decision and book immediately."
        ),
        (
            "What items can't be removed?",
            f"Most household and commercial items can be removed, but there are some exceptions for safety and regulatory reasons. Items that {name} and most junk removal services cannot accept include hazardous materials (paint, chemicals, solvents, pesticides), asbestos-containing materials, medical waste, and certain types of batteries. Large propane tanks and materials contaminated with biohazards also require specialized disposal services. If you're unsure whether your items qualify, upload a photo and our system will identify anything that may require special handling. We always prioritize safe, legal, and environmentally responsible disposal."
        ),
        (
            f"How does {name}'s photo estimate work?",
            f"Our AI-powered photo estimate system uses advanced image recognition to identify every item in your photos, calculate the total volume in cubic yards, and generate an accurate price range — all in under 60 seconds. Here's how it works: you upload one or more photos of the items you need removed. Our AI identifies each item (furniture, appliances, boxes, debris, etc.), measures the approximate dimensions using reference objects in the photo for scale, and calculates the total truck space required. The system then applies {name}'s pricing rates to give you a low-to-high price range. This estimate is based on the same volume-based pricing that professional junk removal companies use industry-wide."
        ),
        (
            f"Is there a minimum charge for junk removal{loc_phrase}?",
            f"Yes, {name} has a minimum charge that covers the base cost of dispatching a truck and crew to your location. This minimum typically applies to very small jobs — a single item or a few bags of junk. The minimum charge covers labor, fuel, truck operation, and disposal fees. Even for minimum-charge jobs, we handle all the lifting, loading, and hauling. Your photo estimate will automatically show if the minimum charge applies and what the exact amount is, so you'll know the cost before you book."
        ),
        (
            f"Does {name} donate or recycle items?",
            f"{name} is committed to responsible disposal. Whenever possible, we divert items from landfills by donating usable furniture, clothing, and household goods to local charities and thrift stores{loc_phrase}. Recyclable materials like metal, cardboard, electronics, and appliances are taken to appropriate recycling facilities. Construction debris is sorted for recycling where available. Our goal is to recycle or donate as much as possible from every job. Responsible disposal is not just good for the environment — it's the right thing to do for our community."
        ),
        (
            f"How do I schedule a junk removal pickup{loc_phrase}?",
            f"Scheduling is simple. Start by uploading photos of your items on this page to get a free instant estimate. Once you see your price range and decide to move forward, {contact_phrase} to book your pickup. We offer flexible scheduling including weekday, evening, and weekend availability{loc_phrase}. On the day of your appointment, our crew will arrive at the scheduled time, confirm the items and pricing with you on-site, then handle all the loading and hauling. You don't need to move anything — just point to what needs to go and we take care of the rest."
        ),
    ]

    faq_schema = json.dumps([
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
        for q, a in faq_items
    ])

    jsonld_local = json.dumps({
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": u.company_name or "Junk Removal",
        "description": f"Professional junk removal services{loc_phrase}. Get a free instant AI-powered estimate in under 60 seconds. We remove furniture, appliances, electronics, yard waste, construction debris, and more.",
        "priceRange": "$$",
        **({"telephone": u.company_phone} if u.company_phone else {}),
        **({"image": u.company_logo_url} if u.company_logo_url else {}),
        **({"areaServed": {"@type": "City", "name": u.company_city, "addressRegion": u.company_state}} if u.company_city else {}),
        "url": canonical,
        "knowsAbout": ["junk removal", "furniture removal", "appliance removal", "estate cleanout", "construction debris removal", "yard waste removal"],
    })

    # Speakable schema for voice search
    jsonld_speakable = json.dumps({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "url": canonical,
        "speakable": {
            "@type": "SpeakableSpecification",
            "cssSelector": [".quick-facts", ".hero", ".faq-section"]
        },
    })

    logo_html = f'<img src="{logo}" alt="{name}" class="logo">' if logo else ""
    phone_header = f'<div class="phone"><a href="tel:{phone}">{phone}</a></div>' if phone else ""
    phone_cta = f'<a href="tel:{phone}" class="btn btn-outline">{phone} — Call Now</a>' if phone else ""

    faq_html = ""
    for q, a in faq_items:
        faq_html += f'<details><summary>{html_mod.escape(q)}</summary><p>{html_mod.escape(a)}</p></details>\n'

    page_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{html_mod.escape(meta_desc)}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{html_mod.escape(meta_desc)}">
<meta property="og:url" content="{canonical}">
{f'<meta property="og:image" content="{logo}">' if logo else ''}
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{html_mod.escape(meta_desc)}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script type="application/ld+json">{jsonld_local}</script>
<script type="application/ld+json">{{
  "@context":"https://schema.org",
  "@type":"FAQPage",
  "mainEntity":{faq_schema}
}}</script>
<script type="application/ld+json">{jsonld_speakable}</script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',system-ui,sans-serif;background:#f8fafc;color:#1e293b;min-height:100vh;-webkit-font-smoothing:antialiased}}
.page-wrap{{max-width:640px;margin:0 auto;padding:16px}}

/* Header */
.site-header{{text-align:center;padding:28px 0 20px}}
.logo{{max-height:56px;margin-bottom:12px;border-radius:8px}}
.site-header h1{{font-size:1.4rem;font-weight:800;color:#0f172a;line-height:1.25}}
.location-badge{{display:inline-block;margin-top:6px;padding:3px 12px;background:#e0f2fe;color:#0369a1;border-radius:20px;font-size:0.78rem;font-weight:600}}
.phone{{margin-top:8px;font-size:0.9rem}}
.phone a{{color:#16a34a;text-decoration:none;font-weight:600}}

/* Hero */
.hero{{text-align:center;padding:20px 0 8px}}
.hero h2{{font-size:1.6rem;font-weight:800;color:#0f172a;line-height:1.2;margin-bottom:8px}}
.hero p{{font-size:0.92rem;color:#64748b;max-width:420px;margin:0 auto}}

/* Steps */
.steps{{display:flex;gap:12px;margin:20px 0;padding:0 4px}}
.step{{flex:1;text-align:center;padding:14px 8px;background:#fff;border:1px solid #e2e8f0;border-radius:12px}}
.step-num{{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;background:#16a34a;color:#fff;border-radius:50%;font-size:0.8rem;font-weight:700;margin-bottom:6px}}
.step-title{{font-size:0.78rem;font-weight:600;color:#0f172a}}
.step-sub{{font-size:0.7rem;color:#94a3b8;margin-top:2px}}

/* Cards */
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:20px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.card-title{{font-size:0.9rem;font-weight:700;color:#0f172a;margin-bottom:14px}}
label{{display:block;font-size:0.78rem;color:#64748b;margin-bottom:4px;font-weight:500}}
input,select{{width:100%;padding:10px 14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;color:#1e293b;font-size:0.9rem;margin-bottom:12px;font-family:inherit;transition:border-color .2s}}
input:focus{{outline:none;border-color:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,0.1)}}

/* Upload */
.drop-zone{{border:2px dashed #cbd5e1;border-radius:14px;padding:32px 20px;text-align:center;cursor:pointer;transition:all .2s;background:#fafbfc}}
.drop-zone:hover,.drop-zone.drag-over{{border-color:#16a34a;background:#f0fdf4}}
.drop-icon{{font-size:2rem;margin-bottom:8px}}
.drop-label{{font-size:0.95rem;font-weight:600;color:#0f172a}}
.drop-sub{{font-size:0.78rem;color:#94a3b8;margin-top:4px}}
.previews{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}
.preview-thumb{{width:72px;height:72px;border-radius:10px;object-fit:cover;border:2px solid #e2e8f0}}

/* Buttons */
.btn{{display:block;width:100%;padding:14px;background:#16a34a;color:#fff;border:none;border-radius:12px;font-size:1rem;font-weight:700;cursor:pointer;font-family:inherit;transition:background .2s;text-align:center;text-decoration:none}}
.btn:hover{{background:#15803d}}
.btn:disabled{{opacity:0.5;cursor:not-allowed}}
.btn-outline{{background:transparent;border:2px solid #16a34a;color:#16a34a;margin-top:10px}}
.btn-outline:hover{{background:#f0fdf4}}

/* Loading */
.loading{{text-align:center;padding:48px 20px;display:none}}
.spinner{{display:inline-block;width:36px;height:36px;border:3px solid #e2e8f0;border-top-color:#16a34a;border-radius:50%;animation:spin .8s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.loading-text{{font-size:0.9rem;color:#64748b;margin-top:14px}}

/* Results */
.results{{display:none}}
.price-card{{text-align:center;padding:24px 20px}}
.price-range{{font-size:2.2rem;font-weight:800;color:#16a34a;letter-spacing:-1px}}
.price-note{{font-size:0.78rem;color:#94a3b8;margin-top:4px}}
.min-charge-note{{font-size:0.8rem;color:#d97706;font-weight:500;margin-top:6px}}
.badge{{display:inline-block;padding:3px 12px;border-radius:20px;font-size:0.75rem;font-weight:600;margin-bottom:8px}}
.badge-standard{{background:#dcfce7;color:#16a34a}}
.badge-premium{{background:#fef3c7;color:#d97706}}
.badge-hoarder{{background:#fee2e2;color:#ef4444}}
.item-row{{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:0.85rem}}
.item-row:last-child{{border-bottom:none}}
.item-name{{flex:1;font-weight:500;color:#1e293b}}
.item-cy{{color:#94a3b8;font-size:0.78rem}}
.item-qty{{color:#94a3b8;font-size:0.78rem;min-width:30px;text-align:right}}
.special-note{{margin-top:14px;padding:14px;border-radius:12px;background:#fffbeb;border:1px solid #fde68a;font-size:0.8rem;color:#92400e}}
.dupe-note{{margin-top:14px;padding:14px;border-radius:12px;background:#fefce8;border:1px solid #fde68a;font-size:0.8rem;color:#854d0e}}

/* CTA */
.cta-section{{text-align:center;padding:24px 20px;margin-top:8px}}
.cta-section .subtext{{font-size:0.85rem;color:#64748b;margin-bottom:12px}}

/* FAQ */
.faq-section{{margin-top:20px}}
.faq-section h2{{font-size:1.1rem;font-weight:700;color:#0f172a;margin-bottom:14px}}
details{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;margin-bottom:8px;overflow:hidden}}
summary{{padding:14px 18px;font-size:0.88rem;font-weight:600;cursor:pointer;color:#0f172a;list-style:none;display:flex;align-items:center;justify-content:space-between}}
summary::after{{content:"+";font-size:1.2rem;color:#94a3b8;font-weight:400;transition:transform .2s}}
details[open] summary::after{{content:"−"}}
details p{{padding:0 18px 14px;font-size:0.84rem;color:#64748b;line-height:1.6}}

/* Content */
.content-section{{margin-top:20px;padding:22px;background:#fff;border:1px solid #e2e8f0;border-radius:14px}}
.content-section h2{{font-size:1.05rem;font-weight:700;color:#0f172a;margin-bottom:10px}}
.content-section p{{font-size:0.85rem;color:#475569;line-height:1.75;margin-bottom:12px}}
.content-section p:last-child{{margin-bottom:0}}

/* Quick Facts */
.quick-facts{{margin-top:20px;padding:22px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:14px}}
.quick-facts h2{{font-size:1.05rem;font-weight:700;color:#0f172a;margin-bottom:14px}}
.quick-facts dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:0.85rem}}
.quick-facts dt{{color:#64748b;font-weight:500}}
.quick-facts dd{{color:#1e293b;font-weight:600;margin:0}}

.footer{{text-align:center;padding:24px 0;font-size:0.72rem;color:#94a3b8}}
.footer a{{color:#94a3b8;text-decoration:none}}
.error{{color:#ef4444;font-size:0.85rem;text-align:center;padding:12px;display:none;background:#fef2f2;border-radius:10px;border:1px solid #fecaca}}
</style>
</head>
<body>

<div class="page-wrap">
  <header class="site-header">
    {logo_html}
    <h1>{name}</h1>
    {f'<span class="location-badge">{location}</span>' if location else ''}
    {phone_header}
  </header>

  <section class="hero">
    <h2>Get Your Free Estimate in 60 Seconds</h2>
    <p>Upload photos of the items you need removed and get an instant price quote — no obligation, no waiting.</p>
  </section>

  <div class="steps">
    <div class="step"><div class="step-num">1</div><div class="step-title">Upload</div><div class="step-sub">Snap photos</div></div>
    <div class="step"><div class="step-num">2</div><div class="step-title">Analyze</div><div class="step-sub">AI identifies items</div></div>
    <div class="step"><div class="step-num">3</div><div class="step-title">Quote</div><div class="step-sub">Get your price</div></div>
  </div>

  <!-- Upload Section -->
  <div id="upload-section">
    <div class="card">
      <div class="card-title">Your Contact Info (Optional)</div>
      <label>Name</label>
      <input type="text" id="cust-name" placeholder="Your name">
      <label>Email</label>
      <input type="email" id="cust-email" placeholder="your@email.com">
      <label>Phone</label>
      <input type="tel" id="cust-phone" placeholder="(555) 123-4567">
    </div>

    <div class="card">
      <div class="card-title">Upload Photos of Items for Removal</div>
      <div class="drop-zone" id="drop-zone">
        <div class="drop-icon">📷</div>
        <div class="drop-label">Tap to upload photos</div>
        <div class="drop-sub">Up to 10 photos — JPG, PNG, WEBP</div>
      </div>
      <input type="file" id="file-input" accept="image/*" multiple style="display:none">
      <div class="previews" id="previews"></div>
    </div>

    <div class="error" id="error-msg"></div>
    <button class="btn" id="submit-btn" disabled>Get My Free Estimate</button>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div class="loading-text" id="loading-text">Analyzing your photos...</div>
  </div>

  <!-- Results -->
  <div class="results" id="results">
    <div class="card price-card">
      <span class="badge" id="res-badge"></span>
      <div class="price-range" id="res-price"></div>
      <div id="min-charge-msg" class="min-charge-note" style="display:none"></div>
      <div class="price-note">Estimated price range based on photo analysis</div>
      <div style="font-size:0.85rem;color:#94a3b8;margin-top:6px" id="res-cy"></div>
    </div>

    <div class="card">
      <div class="card-title">Items Detected</div>
      <div id="res-items"></div>
    </div>

    <div id="res-special" class="special-note" style="display:none"></div>
    <div id="res-dupes" class="dupe-note" style="display:none"></div>

    <div class="card" id="res-notes-card" style="display:none">
      <div class="card-title">Notes</div>
      <div id="res-notes" style="font-size:0.85rem;color:#64748b"></div>
    </div>

    <div class="cta-section" id="cta-section">
      <div class="subtext">Ready to schedule your pickup?</div>
      {phone_cta}
    </div>
  </div>

  <!-- Quick Facts (LLM-optimized structured data for quick answers) -->
  <section class="quick-facts" aria-label="Quick Facts">
    <h2>Quick Facts — {name}</h2>
    <dl>
      <dt>Business</dt><dd>{name}</dd>
      {f'<dt>Location</dt><dd>{location}</dd>' if location else ''}
      {f'<dt>Phone</dt><dd><a href="tel:{phone}" style="color:#16a34a;text-decoration:none">{phone}</a></dd>' if phone else ''}
      <dt>Service</dt><dd>Junk Removal &amp; Hauling</dd>
      <dt>Estimate</dt><dd>Free — AI photo analysis in 60 seconds</dd>
      <dt>Items Accepted</dt><dd>Furniture, appliances, electronics, mattresses, yard waste, construction debris, and more</dd>
      <dt>Pricing</dt><dd>Volume-based — get your exact price from photos</dd>
    </dl>
  </section>

  <!-- About Section -->
  <section class="content-section">
    <h2>About {name}</h2>
    <p>{name} is a professional junk removal service{f' based in {location}' if location else ''} dedicated to making cleanouts fast, affordable, and stress-free for homeowners and businesses. We specialize in residential and commercial junk removal — from single-item pickups to full property cleanouts, estate cleanouts, and post-renovation debris removal.</p>
    <p>What sets {name} apart is our technology-first approach to pricing. Instead of vague phone quotes, we use AI-powered photo analysis to identify exactly what you need removed, measure the volume, and calculate a fair price — all before we arrive. This means no surprises on pickup day, no hidden fees, and no haggling. You see the price upfront and decide on your terms.</p>
    <p>{f'{name} proudly serves {city} and the surrounding {state} communities' if city and state else f'{name} serves local homes and businesses'}, handling everything from routine furniture removal to complex cleanout projects. Whether you{"'" + "re moving out of a home in " + city + ", renovating a space" if city else "'re moving, renovating"}, or finally clearing out that garage — we make it easy.</p>
  </section>

  <!-- What We Remove Section -->
  <section class="content-section">
    <h2>What We Remove{f' in {city}' if city else ''}</h2>
    <p>{name} removes a wide range of household, commercial, and outdoor items. Common items include couches, recliners, dining tables, desks, dressers, bed frames, and mattresses of all sizes — twin, full, queen, and king. We also haul away large appliances including refrigerators, washing machines, dryers, dishwashers, stoves, water heaters, and window AC units.</p>
    <p>For electronics, we handle TVs, monitors, computers, printers, and other e-waste that requires proper recycling. Outdoor and yard items include patio furniture, grills, swing sets, fencing, lumber, branches, and bagged yard waste. We also take on heavier items like hot tubs, pianos, pool tables, exercise equipment, and safes — items that most haulers won't touch.</p>
    <p>Construction and renovation debris is another specialty: drywall, flooring, tile, cabinets, carpet, roofing materials, and general contractor debris. For estate cleanouts and hoarding situations, {name} can handle large-volume jobs that require multiple truck loads. If you're unsure whether we can take something, upload a photo and our AI will identify it instantly.</p>
  </section>

  <!-- How Pricing Works Section -->
  <section class="content-section">
    <h2>How Our Pricing Works</h2>
    <p>{name} uses volume-based pricing, which is the industry standard for junk removal. The cost is determined by how much space your items take up in the truck, measured in cubic yards. A single piece of furniture like a couch takes about 2 cubic yards, while a full garage cleanout might be 10 to 15 cubic yards.</p>
    <p>Our AI photo estimate calculates the exact volume by identifying each item in your photos and measuring its dimensions using reference objects in the scene for scale. The system then applies per-cubic-yard pricing rates to generate a low-to-high price range. Factors that can affect your final price include total volume, the presence of heavy items (concrete, dirt, large appliances), items that require special disposal (refrigerators, TVs, mattresses), and accessibility — whether items are at ground level or require stair carries.</p>
    <p>There are no hidden fees with {name}. The estimate you receive is based on the same pricing our crew uses on-site. If the actual volume on pickup day differs from the photos, the crew will confirm the adjusted price before starting work. Most estimates are within 10-15% of the final price.</p>
  </section>

  <!-- Service Area Section -->
  {f"""<section class="content-section">
    <h2>Junk Removal Service Area — {location}</h2>
    <p>{name} provides junk removal services throughout {city}, {state} and the surrounding area. We serve residential neighborhoods, commercial districts, apartment complexes, and construction sites across the {city} metro area. Whether you're in downtown {city} or the surrounding suburbs, our team can reach you for same-day or next-day pickup.</p>
    <p>If you're located near {city} but outside the immediate area, {contact_phrase} to confirm service availability. We accommodate most locations within a reasonable driving distance and can often provide same-week scheduling for areas just outside our primary service zone.</p>
  </section>""" if city and state else ""}

  <!-- FAQ Section -->
  <section class="faq-section">
    <h2>Junk Removal FAQ{f' — {city}, {state}' if city and state else ''}</h2>
    {faq_html}
  </section>

  <footer class="footer">
    &copy; {name} {f"&middot; {location}" if location else ""}<br>
    Powered by <a href="https://whatshouldicharge.app">WhatShouldICharge</a>
  </footer>
</div>

<script>
var slug="{safe_slug}";
var companyPhone="{phone}";
var photos=[];

function esc(s){{var d=document.createElement('div');d.textContent=s;return d.innerHTML}}

var dropZone=document.getElementById('drop-zone');
var fileInput=document.getElementById('file-input');
dropZone.addEventListener('click',function(){{fileInput.click()}});
dropZone.addEventListener('dragover',function(e){{e.preventDefault();dropZone.classList.add('drag-over')}});
dropZone.addEventListener('dragleave',function(){{dropZone.classList.remove('drag-over')}});
dropZone.addEventListener('drop',function(e){{e.preventDefault();dropZone.classList.remove('drag-over');addFiles(Array.from(e.dataTransfer.files))}});
fileInput.addEventListener('change',function(e){{addFiles(Array.from(e.target.files))}});

function addFiles(files){{
  var remaining=10-photos.length;
  files.filter(function(f){{return f.type.startsWith('image/')}}).slice(0,remaining).forEach(function(f){{
    photos.push(f);
    var img=document.createElement('img');
    img.className='preview-thumb';
    img.src=URL.createObjectURL(f);
    document.getElementById('previews').appendChild(img);
  }});
  document.getElementById('submit-btn').disabled=photos.length===0;
}}

document.getElementById('submit-btn').addEventListener('click',async function(){{
  var btn=this;btn.disabled=true;btn.textContent='Submitting...';
  document.getElementById('error-msg').style.display='none';
  var fd=new FormData();
  photos.forEach(function(f){{fd.append('files',f)}});
  fd.append('customer_name',document.getElementById('cust-name').value.trim());
  fd.append('customer_email',document.getElementById('cust-email').value.trim());
  fd.append('customer_phone',document.getElementById('cust-phone').value.trim());
  fd.append('rooms',JSON.stringify(photos.map(function(){{return 'Main'}})));
  try{{
    var resp=await fetch('/api/public/estimate/'+encodeURIComponent(slug),{{method:'POST',body:fd}});
    if(!resp.ok){{var err=await resp.json();throw new Error(err.detail||'Failed to submit')}}
    var data=await resp.json();
    document.getElementById('upload-section').style.display='none';
    document.getElementById('loading').style.display='block';
    pollStatus(data.job_id);
  }}catch(e){{
    document.getElementById('error-msg').textContent=e.message;
    document.getElementById('error-msg').style.display='block';
    btn.disabled=false;btn.textContent='Get My Free Estimate';
  }}
}});

async function pollStatus(jobId){{
  var lt=document.getElementById('loading-text');var attempts=0;
  var iv=setInterval(async function(){{
    attempts++;
    try{{
      var resp=await fetch('/api/public/estimate/status/'+jobId);
      var data=await resp.json();
      lt.textContent=data.message||'Analyzing...';
      if(data.status==='complete'&&data.result){{clearInterval(iv);document.getElementById('loading').style.display='none';showResults(data.result)}}
      else if(data.status==='error'){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent=data.message||'An error occurred. Please try again.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Get My Free Estimate'}}
    }}catch(e){{}}
    if(attempts>90){{clearInterval(iv);lt.textContent='Taking longer than expected...'}}
  }},2000);
}}

function showResults(r){{
  document.getElementById('results').style.display='block';
  var pL=r.price_low||0,pH=r.price_high||0;
  document.getElementById('res-price').textContent='$'+pL.toLocaleString()+' — $'+pH.toLocaleString();
  if(r.min_charge_applied){{var m=document.getElementById('min-charge-msg');m.textContent='Minimum charge applied';m.style.display='block'}}
  document.getElementById('res-cy').textContent=(r.cy_estimate||0)+' cubic yards estimated';
  var bl={{standard:'Standard',premium:'Premium',hoarder:'Hoarder',truck_load:'Truck Load'}};
  var jt=r.job_type||'standard';
  document.getElementById('res-badge').textContent=bl[jt]||jt;
  document.getElementById('res-badge').className='badge badge-'+jt;
  var el=document.getElementById('res-items');el.innerHTML='';
  (r.items||[]).forEach(function(item){{
    var row=document.createElement('div');row.className='item-row';
    row.innerHTML='<div class="item-name">'+esc(item.name||'Item')+'</div><div class="item-cy">'+(item.cubic_yards||0)+' CY</div><div class="item-qty">x'+(item.quantity||1)+'</div>';
    el.appendChild(row);
  }});
  var sp=r.special_items||[];
  if(sp.length>0){{var sh='<strong>Recycling/Disposal Fee Items:</strong><br>';sp.forEach(function(s){{sh+=esc(s.name)+' x'+(s.quantity||1)+'<br>'}});sh+='<em style="font-size:0.75rem">Fees confirmed on arrival.</em>';document.getElementById('res-special').innerHTML=sh;document.getElementById('res-special').style.display='block'}}
  var dp=r.potential_duplicates||[];
  if(dp.length>0){{var dh='<strong>Items to verify (may be duplicates):</strong><br>';dp.forEach(function(d){{dh+=esc(d.item_a)+' vs '+esc(d.item_b)+'<br>'}});document.getElementById('res-dupes').innerHTML=dh;document.getElementById('res-dupes').style.display='block'}}
  if(r.notes){{document.getElementById('res-notes').textContent=r.notes;document.getElementById('res-notes-card').style.display='block'}}
  if(!companyPhone){{document.getElementById('cta-section').style.display='none'}}
}}

if(window.parent!==window){{
  function postHeight(){{window.parent.postMessage({{type:'wsic-resize',height:document.body.scrollHeight+40}},'*')}}
  new MutationObserver(postHeight).observe(document.body,{{childList:true,subtree:true,attributes:true}});
  setInterval(postHeight,1000);postHeight();
}}
</script>

</body>
</html>'''
    return HTMLResponse(content=page_html)


@app.get("/api/public/company/{slug}")
async def public_company_info(slug: str):
    """Public endpoint — returns company branding info, no auth required."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.company_slug == slug.lower().strip()))
        u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="Company not found")
    return {
        "company_name": u.company_name or "Junk Removal",
        "company_phone": u.company_phone or "",
        "company_logo_url": u.company_logo_url or "",
        "company_city": u.company_city or "",
        "company_state": u.company_state or "",
        "slug": u.company_slug,
    }


@app.post("/api/public/estimate/{slug}")
async def public_create_estimate(
    slug: str,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    customer_name: str = Form(default=""),
    customer_email: str = Form(default=""),
    customer_phone: str = Form(default=""),
):
    """Public estimate endpoint — customer submits photos, charges against company's estimate count."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.company_slug == slug.lower().strip()))
        company_user = result.scalar_one_or_none()
    if not company_user:
        raise HTTPException(status_code=404, detail="Company not found")
    if company_user.estimates_used >= company_user.estimates_limit:
        raise HTTPException(status_code=403, detail="This company has reached their estimate limit. Please contact them directly.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service unavailable")

    if not files:
        raise HTTPException(status_code=400, detail="At least one photo is required.")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 photos for customer estimates.")

    try:
        rooms_list = json.loads(rooms)
    except Exception:
        rooms_list = []

    ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
    MAX_FILE_SIZE = 20 * 1024 * 1024
    photo_data = []
    for i, file in enumerate(files):
        if file.content_type and file.content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Photo {i+1}: unsupported file type.")
        raw = await file.read()
        if len(raw) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} exceeds 20MB limit.")
        compressed = compress_image(raw)
        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        room_label = rooms_list[i] if i < len(rooms_list) else "Main"
        photo_data.append({"b64": b64, "room": room_label, "index": i + 1})

    room_groups = {}
    for pd in photo_data:
        room = pd["room"]
        if room not in room_groups:
            room_groups[room] = []
        room_groups[room].append(pd)

    image_content = []
    for room, group_photos in room_groups.items():
        if len(group_photos) > 1:
            image_content.append({
                "type": "text",
                "text": f"\n--- ROOM: {room} ({len(group_photos)} photos — SAME space, different angles. Do NOT double-count.) ---"
            })
        for pd in group_photos:
            label = f"Photo {pd['index']} (Room: {room})"
            image_content.append({"type": "text", "text": f"{label}:"})
            image_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": pd["b64"]}
            })

    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": company_user.id,
        "estimate_name": f"Customer: {customer_name or 'Walk-in'}",
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "created_at": datetime.utcnow(),
    }

    asyncio.create_task(run_estimate(
        job_id=job_id,
        user=company_user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


@app.get("/api/public/estimate/status/{job_id}")
async def public_estimate_status(job_id: str):
    """Public status check — no auth, but limited response."""
    job = estimate_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    if job["status"] == "complete" and job.get("result"):
        r = job["result"]
        return {
            "status": "complete",
            "message": job["message"],
            "result": {
                "id": r.get("id"),
                "price_low": r.get("price_low"),
                "price_high": r.get("price_high"),
                "cy_estimate": r.get("cy_estimate"),
                "items": r.get("items", []),
                "job_type": r.get("job_type"),
                "conditions": r.get("conditions", []),
                "notes": r.get("notes", ""),
                "confidence": r.get("confidence"),
                "special_items": r.get("special_items", []),
                "min_charge_applied": r.get("min_charge_applied", False),
                "potential_duplicates": r.get("potential_duplicates", []),
            }
        }
    return {"status": job["status"], "message": job["message"], "result": None}


@app.post("/api/auth/signup")
async def auth_signup(request: Request):
    client_ip = get_client_ip(request)
    if auth_rate_limiter.is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    company_name = body.get("company_name", "").strip()
    company_city = body.get("company_city", "").strip()
    company_state = body.get("company_state", "").strip()

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Invalid email format.")
    if len(email) > 254:
        raise HTTPException(status_code=400, detail="Email address is too long.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="Password is too long.")

    pw_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    )

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
        "<p>Upload customer photos and get AI-assisted pricing in seconds.</p>"
        "<p>— The WhatShouldICharge Team</p>"
    )

    response = JSONResponse({"success": True, "redirect": "/estimate"})
    response.set_cookie(
        "session_token", token, httponly=True, samesite="lax", secure=True,
        max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.post("/api/auth/login")
async def auth_login(request: Request):
    client_ip = get_client_ip(request)
    if auth_rate_limiter.is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

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

        valid = await asyncio.to_thread(
            lambda: bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8"))
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        token = secrets.token_hex(32)
        sess = Session(
            user_id=user.id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(sess)
        await db.commit()

    redirect_url = "/admin" if user.is_admin else "/estimate"
    response = JSONResponse({"success": True, "redirect": redirect_url, "is_admin": bool(user.is_admin)})
    response.set_cookie(
        "session_token", token, httponly=True, samesite="lax", secure=True,
        max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.post("/api/auth/forgot-password")
async def auth_forgot_password(request: Request):
    client_ip = get_client_ip(request)
    if forgot_password_rate_limiter.is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    body = await request.json()
    email = body.get("email", "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            return JSONResponse({"success": True, "message": "If an account with that email exists, a new password has been sent."})

        new_password = secrets.token_urlsafe(12)
        new_hash = await asyncio.to_thread(
            lambda: bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        )
        user.password_hash = new_hash
        await db.commit()

    send_email(
        email,
        "Your WhatShouldICharge Password Has Been Reset",
        f"<h2>Password Reset</h2>"
        f"<p>Your password has been reset. Here is your new temporary password:</p>"
        f"<div style='background:#f4f4f4;padding:16px;border-radius:8px;font-family:monospace;font-size:18px;margin:16px 0;text-align:center;'>{new_password}</div>"
        f"<p>Please log in with this password. We recommend changing it after logging in.</p>"
        f"<p>If you did not request this reset, please contact support immediately.</p>"
        f"<p>— The WhatShouldICharge Team</p>"
    )

    return JSONResponse({"success": True, "message": "If an account with that email exists, a new password has been sent."})


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
        "company_name": user.company_name or "",
        "company_city": user.company_city or "",
        "company_state": user.company_state or "",
        "subscription_tier": user.subscription_tier,
        "estimates_used": user.estimates_used,
        "estimates_limit": user.estimates_limit,
        "price_per_cy_low": user.price_per_cy_low if user.price_per_cy_low is not None else 35.0,
        "price_per_cy_high": user.price_per_cy_high if user.price_per_cy_high is not None else 40.0,
        "price_per_cy_premium": user.price_per_cy_premium if user.price_per_cy_premium is not None else 55.0,
        "min_charge": user.min_charge if user.min_charge is not None else 75.0,
        "truck_capacity_cy": user.truck_capacity_cy if user.truck_capacity_cy is not None else 16.0,
        "is_admin": bool(user.is_admin),
        "company_slug": user.company_slug or "",
        "company_phone": user.company_phone or "",
        "company_logo_url": user.company_logo_url or "",
    }


@app.get("/api/settings")
async def get_settings(request: Request):
    """Return all user settings from the database."""
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "company_name": u.company_name or "",
        "company_city": u.company_city or "",
        "company_state": u.company_state or "",
        "price_per_cy_low": u.price_per_cy_low if u.price_per_cy_low is not None else 35.0,
        "price_per_cy_high": u.price_per_cy_high if u.price_per_cy_high is not None else 40.0,
        "price_per_cy_premium": u.price_per_cy_premium if u.price_per_cy_premium is not None else 55.0,
        "min_charge": u.min_charge if u.min_charge is not None else 75.0,
        "truck_capacity_cy": u.truck_capacity_cy if u.truck_capacity_cy is not None else 16.0,
        "company_slug": u.company_slug or "",
        "company_phone": u.company_phone or "",
        "company_logo_url": u.company_logo_url or "",
    }


@app.put("/api/settings")
async def update_settings(request: Request):
    user = await require_user(request)
    body = await request.json()

    allowed_fields = {
        "company_name": str,
        "company_city": str,
        "company_state": str,
        "price_per_cy_low": float,
        "price_per_cy_high": float,
        "price_per_cy_premium": float,
        "min_charge": float,
        "truck_capacity_cy": float,
        "company_slug": str,
        "company_phone": str,
        "company_logo_url": str,
    }

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")

        # Validate and sanitize slug if provided
        if "company_slug" in body:
            slug = re.sub(r'[^a-z0-9-]', '', str(body["company_slug"]).lower().strip().replace(" ", "-"))
            slug = re.sub(r'-+', '-', slug).strip('-')[:60]
            if slug:
                existing_slug = await db.execute(
                    select(User).where(User.company_slug == slug, User.id != user.id)
                )
                if existing_slug.scalar_one_or_none():
                    raise HTTPException(status_code=400, detail=f"Slug '{slug}' is already taken")
            body["company_slug"] = slug

        updated = []
        for field, typ in allowed_fields.items():
            if field in body:
                val = body[field]
                if typ == float:
                    val = float(val) if val not in (None, "") else None
                elif typ == str:
                    val = str(val).strip()
                setattr(u, field, val)
                updated.append(field)

        if updated:
            await db.commit()
            await db.refresh(u)

        return {
            "ok": True,
            "updated": updated,
            "company_name": u.company_name,
            "company_city": u.company_city,
            "company_state": u.company_state,
            "price_per_cy_low": u.price_per_cy_low,
            "price_per_cy_high": u.price_per_cy_high,
            "price_per_cy_premium": u.price_per_cy_premium,
            "min_charge": u.min_charge,
            "truck_capacity_cy": u.truck_capacity_cy,
            "company_slug": u.company_slug or "",
            "company_phone": u.company_phone or "",
            "company_logo_url": u.company_logo_url or "",
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
                "dimensions": i.dimensions or "",
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
            dimensions=str(body.get("dimensions", "")),
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
        if "dimensions" in body:
            item.dimensions = str(body["dimensions"])
        item.updated_at = datetime.utcnow()
        await db.commit()
        return {"success": True}


@app.get("/api/library/stats")
async def library_stats(request: Request):
    await require_user(request)
    async with AsyncSessionLocal() as db:
        total_count = (await db.execute(
            select(func.count(ItemReferenceLibrary.id))
        )).scalar() or 0

        source_counts = (await db.execute(
            select(ItemReferenceLibrary.source, func.count(ItemReferenceLibrary.id))
            .group_by(ItemReferenceLibrary.source)
        )).all()
        by_source = {row[0]: row[1] for row in source_counts}

        top_result = await db.execute(
            select(ItemReferenceLibrary)
            .order_by(ItemReferenceLibrary.times_seen.desc())
            .limit(10)
        )
        top_seen = top_result.scalars().all()

        return {
            "total_items": total_count,
            "by_source": by_source,
            "top_seen": [
                {"item_name": i.item_name, "times_seen": i.times_seen, "cubic_yards": i.cubic_yards}
                for i in top_seen
            ],
        }


def calculate_price(result_data: dict, rate_low=35.0, rate_high=40.0, rate_premium=55.0, min_charge=75.0, market_rates=None) -> tuple:
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

    # If live market rates are available, blend them with user rates
    # Market rates inform what competitors charge; user rates are the floor
    if market_rates and market_rates.get("source") == "live_market_search":
        mkt_low = market_rates.get("low", rate_low)
        mkt_high = market_rates.get("high", rate_high)
        mkt_premium = market_rates.get("premium", rate_premium)
        # Use the higher of user rate or market rate (don't underprice the market)
        eff_low = max(rate_low, mkt_low)
        eff_high = max(rate_high, mkt_high)
        eff_premium = max(rate_premium, mkt_premium)
    else:
        eff_low = rate_low
        eff_high = rate_high
        eff_premium = rate_premium

    if is_premium:
        r_low = eff_premium
        r_high = eff_premium
    else:
        r_low = eff_low
        r_high = eff_high

    price_low = cy_low * r_low
    price_high = cy_high * r_high

    min_charge_applied = price_low < min_charge or price_high < min_charge
    price_low = max(price_low, min_charge)
    price_high = max(price_high, min_charge)

    special_items = [
        {"name": item.get("name", "Unknown"), "quantity": int(item.get("quantity", 1))}
        for item in items if item.get("is_special")
    ]

    return round(price_low, 2), round(price_high, 2), round(cy_mid, 1), special_items, min_charge_applied


def compress_image(image_bytes: bytes, max_size_kb: int = 1000) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    try:
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
    finally:
        img.close()


async def get_market_rates(city: str, state: str) -> dict:
    default = {"low": 35, "high": 40, "premium": 55, "source": "default_rates"}
    if not city or not state:
        return default

    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        return default

    try:
        async with httpx.AsyncClient() as client:
            # Search for local junk removal pricing
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": f"junk removal prices cost per cubic yard {city} {state} 2025 2026",
                    "search_depth": "basic",
                    "max_results": 5
                },
                timeout=8.0
            )
            data = response.json()
            content = " ".join([r.get("content", "") for r in data.get("results", [])])

            # Extract $/cubic yard prices with multiple patterns
            cy_prices = re.findall(
                r'\$(\d+(?:\.\d+)?)\s*(?:per|/)\s*(?:cubic\s*yard|cu\.?\s*yd|CY)',
                content, re.IGNORECASE
            )
            # Also match "XX dollars per cubic yard"
            word_prices = re.findall(
                r'(\d+(?:\.\d+)?)\s*dollars?\s*(?:per|/)\s*(?:cubic\s*yard|cu\.?\s*yd|CY)',
                content, re.IGNORECASE
            )
            all_prices = [float(p) for p in cy_prices + word_prices]

            # Filter out outliers (prices below $15 or above $120 per CY are likely errors)
            all_prices = [p for p in all_prices if 15 <= p <= 120]

            if all_prices:
                avg = sum(all_prices) / len(all_prices)
                return {
                    "low": round(avg * 0.85, 2),
                    "high": round(avg * 1.15, 2),
                    "premium": round(avg * 1.5, 2),
                    "market_avg": round(avg, 2),
                    "source": "live_market_search",
                    "city": city,
                    "state": state,
                    "samples": len(all_prices),
                }
    except Exception:
        pass

    return default


SYSTEM_PROMPT_BASE = """You are an expert junk removal estimator with years of field experience.
Analyze ALL photos carefully and return ONLY valid JSON with no markdown, no explanation, no code blocks — raw JSON only.

REQUIRED JSON FORMAT:
{
  "reference_points": [
    {
      "name": "item or fixture used as reference",
      "known_dimensions": "80in x 32in",
      "cubic_yards": 0.0,
      "location_in_photo": "left foreground",
      "photo_number": 1
    }
  ],
  "items": [
    {
      "name": "specific item name",
      "quantity": 1,
      "category": "furniture|appliance|electronics|debris|hazardous|other",
      "cubic_yards": 0.5,
      "is_special": false,
      "photo_sources": [1],
      "dedup_note": ""
    }
  ],
  "potential_duplicates": [
    {
      "item_a": "item name (photo X, location description)",
      "item_b": "item name (photo Y, location description)",
      "reason": "why these might be the same item"
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

TWO TYPES OF ITEMS — UNDERSTAND THIS DISTINCTION:

FIXED REFERENCE ITEMS (marked [FIXED] in the library below):
- Items with known, standardized dimensions — manufactured to exact specs
- Examples: 5-gallon bucket (always 12×12×15in), trash bags (standard sizes), appliances (published dimensions), mattresses (industry standard sizes), tires, propane tanks
- These are your CALIBRATION ANCHORS. When you spot one in a photo, you know its exact real-world size. Use it to establish scale for everything around it.
- Use the cubic yards value from the library for these items — it is pre-verified.

VARIABLE ITEMS (marked [VARIABLE] in the library below):
- Items that come in many different sizes — no single "standard" dimension
- Examples: dressers, couches, tables, bookshelves, desks, outdoor furniture
- DO NOT use a default cubic yardage for these. The library CY is only a rough average.
- Instead, you MUST measure the variable item's actual dimensions from the photo using nearby fixed reference items or architectural fixtures as calibration rulers.
- Calculate cubic yards from the measured dimensions: (L × W × H) / 46,656 = CY, then apply a packing factor (60-80% for most furniture).

SPATIAL REASONING METHOD (THIS IS HOW YOU ESTIMATE — FOLLOW EXACTLY):

Step 1 — FIND FIXED REFERENCE ITEMS AND ARCHITECTURAL FIXTURES:
Scan the photo for fixed reference items from the library AND architectural fixtures. These are your calibration anchors. You need at least 2-3 anchors spread across the photo at different depths.

ARCHITECTURAL FIXTURES — built-in calibration rulers visible in almost every room:

PRIMARY ANCHORS (present in nearly every indoor photo — look for these FIRST):
- Standard door frame: 80"H (6'8") tall × 32-36"W — visible in almost every room. The HEIGHT is code-mandated and virtually universal. Use it as your primary vertical ruler. The width varies but is typically 32" for bedrooms/bathrooms and 36" for exterior doors.
- Ceiling height: 96" (8') is the most common residential standard. 108" (9') is common in newer construction. 120" (10') exists but is rare. If you can see both the floor and ceiling, you have a full-height vertical ruler for the entire room.
- Electrical outlet cover plate: 4.5"H × 2.75"W — small but precise. Standard outlet height from floor is 12-16".
- Light switch cover plate: 4.5"H × 2.75"W — standard height from floor is 48".

These four fixtures give you calibration in virtually every indoor photo. A door frame + ceiling gives you two vertical rulers at known heights. Outlet and switch plates give you precise small-scale references.

SECONDARY ANCHORS (common, reliable):
- Wall stud spacing (visible in unfinished garages/basements): 16" on center
- Exterior door: 80"H × 36"W typical
- Sliding patio door: 80"H × 72"W typical
- Garage door single: 84"H × 108"W (7' × 9') typical
- Garage door double: 84"H × 192"W (7' × 16') typical
- Standard stair riser height: 7-8"
- Standard stair tread depth: 10-11"

APPROXIMATE (use only when no better reference available):
- Double-hung window: 36"H × 24"W varies widely
- Baseboard trim height: 3-5" varies

Step 2 — APPLY PERSPECTIVE CORRECTION TO EACH ANCHOR:
Before using any reference item as a calibration ruler, you MUST account for perspective distortion.

PERSPECTIVE CORRECTION (CRITICAL — DO NOT SKIP):
- Objects CLOSER to the camera appear LARGER. Objects FARTHER appear SMALLER.
- A trash bag in the foreground (3ft from camera) looks twice as large as an identical bag in the background (10ft away). They are the SAME SIZE.
- To correct: compare each item to the nearest reference anchor at a SIMILAR DEPTH in the photo.
- Foreground items: calibrate against foreground anchors. Background items: calibrate against background anchors.
- Floor lines, wall edges, and ceiling lines converging toward a vanishing point reveal the depth gradient. Use converging lines to estimate how much objects shrink with distance.
- COMMON MISTAKE: Items piled in the foreground near the camera look enormous. A pile of bags at 3 feet looks like half the frame but may only be 2-3 CY. Always calibrate against an anchor at the same depth.

Step 3 — MEASURE VARIABLE ITEMS USING NEARBY ANCHORS:
For each variable item (dresser, couch, table, etc.):
a) Find the nearest fixed reference item or architectural fixture at the SAME DEPTH in the photo
b) Apply perspective correction if the anchor is at a different depth
c) Use the anchor's known dimensions to estimate the variable item's actual L × W × H in inches
d) Calculate cubic yards: (L × W × H) / 46,656, then apply packing factor (60-80%)
e) If NO reference anchor is visible near a variable item, flag it as lower confidence in the item entry

For fixed reference items: use the pre-verified CY value from the library. No measurement needed.

Step 4 — CALCULATE TOTALS: Sum all items (fixed CY values + measured variable CY values) for totals. Every variable item's CY must be derived from photo measurement, not from a default.

List your reference points in the "reference_points" array. For variable items, include which anchor you used to measure them in the item's notes.

If fewer than 3 calibration anchors are visible (fixed items + architectural fixtures), note this in "notes" and lower confidence. Without anchors, variable item measurements are unreliable.

MULTI-PHOTO DEDUPLICATION (CRITICAL — follow this 3-step process):

All photos submitted together are from the SAME JOB. Photos labeled with the same room name show DIFFERENT ANGLES of ONE space. You must NOT count the same item twice.

For EVERY item, set "photo_sources" to the list of photo numbers where that item is visible.

DEDUP STEP 1 — BUILD A CANDIDATE LIST:
After identifying all items across all photos, scan for potential duplicates:
- Same item type appearing in multiple photos of the same room
- Similar-sized items in overlapping areas between photos
- Items at the same relative position in the room (e.g., "against the left wall") seen from different angles

DEDUP STEP 2 — COMPARE VISUAL FEATURES:
For each candidate pair, compare:
- Color and material (same wood finish? same fabric color?)
- Size relative to nearby anchors (same approximate dimensions?)
- Position relative to fixed landmarks (same wall, same corner, same distance from door?)
- Distinguishing details (handles, damage, labels, items on top of it)
If the features match: MERGE into one item. Set photo_sources to all photos it appears in. Add a dedup_note explaining the merge (e.g., "visible in photos 1 and 3 from different angles, same dark wood dresser against left wall").

DEDUP STEP 3 — FLAG UNCERTAIN CASES:
If you CANNOT confidently determine whether two items are the same or different:
- Count the item ONCE in the items list (do not double-count)
- Add an entry to the "potential_duplicates" array with item_a, item_b, and reason
- The user will review flagged duplicates and confirm
- Example: two similar dressers could be the same dresser from two angles, or two separate dressers side by side — flag it rather than guessing

DEDUP RULES:
- When in doubt, count ONCE and flag — better to undercount than overcount
- Use reference points from multiple angles to improve spatial accuracy
- Items in DIFFERENT rooms are never duplicates (a chair in the kitchen is not the same as a chair in the bedroom)
- Identical bulk items (e.g., "trash bags") in the same room CAN be separate items — count distinct piles/groups separately but note the total quantity

ITEM IDENTIFICATION RULES:
- Identify every visible item for removal individually, do not group unless identical (but if circled/marked items were detected, only list the marked items — see CIRCLED OR MARKED ITEMS section)
- Assign cubic_yards to each item based on its size RELATIVE TO YOUR REFERENCE POINTS — not generic guesses
- Look specifically along walls, in corners, behind other items
- FLAT SCREEN TVs vs OTHER THIN RECTANGLES — FALSE TV DETECTION IS A COMMON ERROR:
  Many thin, flat, rectangular objects get misidentified as TVs. DEFAULT ASSUMPTION: a thin rectangular object is NOT a TV unless you see clear TV-specific evidence.

  POSITIVE TV indicators (need at least 2 of these to call it a TV):
  - Glossy/reflective black screen surface (not matte)
  - Visible brand logo (Samsung, LG, Sony, Vizio, TCL, etc.)
  - Stand base or VESA mount bracket attached
  - Ports/connections visible on back or side edge
  - Thick plastic bezel (1-2 inches) framing the screen
  - Power cord or cable visible

  Things commonly MISIDENTIFIED as TVs — these are NOT TVs:
  - Window screens (thin metal/wood frame with mesh — look for the mesh texture)
  - Mirrors (reflective but shows room reflections, often has decorative frame)
  - Picture frames or artwork (has visible image or canvas texture)
  - Cabinet doors or panel boards (wood grain, hinges, or hardware visible)
  - Whiteboards or chalkboards (writing surface, marker tray)
  - Folding tables leaning on edge (metal legs visible, thicker than a TV)
  - Headboards (fabric or wood, wider than typical TV)
  - Solar panels or glass panels (metal frame, grid pattern)

  When uncertain: label as "flat rectangular object — verify if TV on site" and set is_special: false. Only flag as TV (is_special: true) when you are confident based on 2+ positive indicators.
- BED SIZE IDENTIFICATION — COMMON MISIDENTIFICATION ERROR:
  Beds are frequently mis-sized (e.g., calling a queen a twin). ALWAYS determine bed size by measuring the mattress WIDTH against a nearby anchor. Standard mattress widths:
  - Twin: 38" wide (just over 3 feet — barely wider than a door frame)
  - Full: 54" wide (4.5 feet — about 1.5× a standard door width)
  - Queen: 60" wide (5 feet — nearly twice the width of a twin)
  - King: 76" wide (6.3 feet — over twice the width of a twin)

  HOW TO VERIFY: Compare the mattress width to the nearest door frame (32-36" wide), wall segment, or baseboard. A queen mattress is roughly TWO door-frame widths. A twin is roughly ONE door-frame width. This difference is visually obvious — if the bed looks significantly wider than a single door, it is NOT a twin.

  If bedding or sheets obscure the edges, look for the bed frame width instead. Captain's beds, platform beds, and storage beds may add 2-4 inches to each side but the mattress width determines the size name.

- BED VARIANTS — classify correctly:
  - Captain's bed: A bed frame with built-in storage drawers underneath. List as "[size] captain's bed" (e.g., "queen captain's bed"). CY includes the frame + drawers — typically 15-25% more than a standard bed frame of the same size.
  - Bookcase headboard: A headboard with shelves/storage built in. List as a SEPARATE item from the bed — "bookcase headboard" — not as a dresser or armoire. Typical CY: 0.3-0.5 depending on size. Do NOT misidentify as a tall dresser or armoire.
  - Platform bed: A low bed frame with no box spring. List as "[size] platform bed". Similar CY to standard bed frame.
  - Bunk bed: Two bed frames stacked. List as "bunk bed" with combined CY of both frames.
  - Trundle bed: A bed with a pull-out bed underneath. List as "[size] trundle bed".

- Wheelchairs and medical equipment: note in items, not special fee but flag in notes for crew (may be donateable)

SPECIAL ITEM FLAGGING — set is_special: true for ANY of these (do NOT calculate fees, just flag them):
- Any flat screen TV (all sizes)
- Any CRT television
- Mattress or box spring (any size)
- Car tire or truck tire
- Propane tank (any size)
- Car battery
- Paint cans or chemicals
- Refrigerator with freon
- Air conditioner (contains freon)
- Fluorescent light tubes
- Electronics with circuit boards

These items may have recycling or disposal fees that vary by location. Just identify them and set is_special: true.

CIRCLED OR MARKED ITEMS (CRITICAL):
- If the customer has drawn circles, arrows, or markings on items in the photo, ONLY include the circled/marked items in your estimate.
- Unmarked items in photos with markings should be EXCLUDED — they are items the customer wants to KEEP.
- If no markings or circles are visible in any photo, include ALL items as normal.
- When you detect markings, add "Customer circled specific items — only marked items included" to your notes.
- Still use uncircled items as reference points for spatial calibration, but do NOT include them in the items list or cubic yard totals.

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
- 90-100: Clear photos, 5+ reference points identified, all items visible, straightforward job
- 70-89: 3-4 reference points, some items obscured, reasonable estimate
- 50-69: 1-2 reference points, poor lighting, many items hidden, estimate may vary significantly
- Under 50: No reference points found, cannot calibrate scale, estimate is a rough guess

Always err toward the higher end of CY when visibility is limited. Better to quote slightly high and come down than to under-quote."""


# Items with standardized/manufactured dimensions — reliable calibration anchors.
# Variable items (furniture, tables, etc.) are measured from the photo instead.
FIXED_REFERENCE_ITEMS = {
    # Trash bags — exact standard sizes
    "contractor trash bag full", "standard trash bag full 33 gal", "small trash bag full",
    # Boxes & containers — standard moving/storage sizes
    "large cardboard box", "medium cardboard box", "small cardboard box",
    "large plastic tote with lid", "small plastic tote",
    # Appliances — manufactured to published specs
    "refrigerator large", "refrigerator small", "washing machine", "dryer",
    "dishwasher", "stove", "microwave large", "microwave small",
    "air conditioner window unit", "dehumidifier", "water heater",
    # Mattresses — industry standard sizes
    "king mattress", "queen mattress", "full mattress", "twin mattress", "box spring",
    # Electronics — manufactured sizes
    "large flat screen tv 55+", "medium flat screen tv 32-54", "small flat screen tv under 32",
    "crt television", "desktop computer tower", "monitor", "printer large",
    # Hazardous — standard sizes
    "propane tank large", "propane tank small", "car battery",
    "tire car", "tire truck", "paint cans box",
    # Office — standard sizes
    "4 drawer file cabinet", "2 drawer file cabinet", "lateral file cabinet",
}


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

    fixed_lines = []
    variable_lines = []

    for item in items:
        dims_str = f" [{item.dimensions}]" if item.dimensions else ""
        is_fixed = item.item_name in FIXED_REFERENCE_ITEMS
        tag = "[FIXED]" if is_fixed else "[VARIABLE]"
        line = f"- {item.item_name}: {item.cubic_yards} CY{dims_str} {tag}"
        if item.is_special:
            line += " [SPECIAL ITEM - flag for recycling/disposal]"
        if is_fixed:
            fixed_lines.append(line)
        else:
            variable_lines.append(line)

    lines = [
        "\nITEM REFERENCE LIBRARY:",
        "",
        "=== FIXED REFERENCE ITEMS (use as calibration anchors — dimensions are exact) ===",
        "These items have standardized sizes. When you spot one in a photo, you know its",
        "exact real-world dimensions. Use the CY value directly and use the dimensions",
        "to calibrate the scale of nearby items.",
        "",
    ]
    lines.extend(fixed_lines)
    lines.append("")
    lines.append("=== VARIABLE ITEMS (measure from photo — sizes vary widely) ===")
    lines.append("These items come in many sizes. The CY listed is only a rough average.")
    lines.append("Do NOT use the default CY. Instead, measure actual dimensions from the")
    lines.append("photo using nearby fixed anchors, then calculate CY from those measurements.")
    lines.append("")
    lines.extend(variable_lines)

    return "\n".join(lines)



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

    except Exception:
        pass

    return {"cubic_yards": 0, "confidence": 0}


async def update_library_from_estimate(items: list):
    item_map = {}
    for item in items:
        normalized_name = item.get("name", "").lower().strip()
        if normalized_name:
            item_map[normalized_name] = item

    if not item_map:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary).where(
                ItemReferenceLibrary.item_name.in_(list(item_map.keys()))
            )
        )
        existing_items = {row.item_name: row for row in result.scalars().all()}

        for name, item in item_map.items():
            if name in existing_items:
                existing_items[name].times_seen = existing_items[name].times_seen + 1
                existing_items[name].updated_at = datetime.utcnow()
            else:
                cy = item.get("cubic_yards", 0)
                if cy and cy > 0:
                    db.add(ItemReferenceLibrary(
                        item_name=name,
                        item_category=item.get("category", "other"),
                        cubic_yards=cy,
                        is_special=bool(item.get("is_special", False)),
                        special_fee=0.0,
                        confidence=0.7,
                        source="ai_learned",
                        times_seen=1,
                    ))
        await db.commit()


estimate_jobs = {}
JOB_TTL_SECONDS = 300


def cleanup_expired_jobs():
    now = datetime.utcnow()
    expired = [k for k, v in estimate_jobs.items()
               if (now - v.get("created_at", now)).total_seconds() > JOB_TTL_SECONDS]
    for k in expired:
        del estimate_jobs[k]


@app.post("/api/estimate")
async def create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
    estimate_name: str = Form(default=""),
):
    user = await require_user(request)
    cleanup_expired_jobs()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        fresh_user = result.scalar_one_or_none()
        if not fresh_user or fresh_user.estimates_used >= fresh_user.estimates_limit:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        user = fresh_user

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service is not configured. Please contact support.")

    if not files:
        raise HTTPException(status_code=400, detail="At least one photo is required.")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 photos allowed.")

    try:
        rooms_list = json.loads(rooms)
    except Exception:
        rooms_list = []

    ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
    MAX_FILE_SIZE = 20 * 1024 * 1024
    photo_data = []
    for i, file in enumerate(files):
        if file.content_type and file.content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} has an unsupported file type. Please upload images only.")
        raw = await file.read()
        if len(raw) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} exceeds 20MB limit.")
        compressed = compress_image(raw)
        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        room_label = rooms_list[i] if i < len(rooms_list) else "Unknown"
        photo_data.append({"b64": b64, "room": room_label, "index": i + 1})

    room_groups = {}
    for pd in photo_data:
        room = pd["room"]
        if room not in room_groups:
            room_groups[room] = []
        room_groups[room].append(pd)

    image_content = []
    for room, group_photos in room_groups.items():
        if len(group_photos) > 1:
            image_content.append({
                "type": "text",
                "text": f"\n--- ROOM: {room} ({len(group_photos)} photos — these show DIFFERENT ANGLES of the SAME space. DO NOT double-count items visible in multiple photos.) ---"
            })
        for pd in group_photos:
            label = f"Photo {pd['index']} (Room: {room})"
            if len(group_photos) > 1:
                label += f" [angle {group_photos.index(pd) + 1} of {len(group_photos)} for this room]"
            image_content.append({"type": "text", "text": f"{label}:"})
            image_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": pd["b64"]}
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

    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": user.id,
        "estimate_name": estimate_name.strip(),
        "created_at": datetime.utcnow(),
    }

    asyncio.create_task(run_estimate(
        job_id=job_id,
        user=user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


async def run_estimate(
    job_id: str,
    user,
    image_content: list,
    api_key: str,
    num_photos: int,
):
    job = estimate_jobs[job_id]
    pass1_json_str = ""
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
        market_rates = None
        try:
            market_rates = await get_market_rates(user.company_city, user.company_state)
            if market_rates.get("source") == "live_market_search":
                market_context = {
                    "city": user.company_city,
                    "state": user.company_state,
                    "market_avg": market_rates.get("market_avg"),
                    "market_low": market_rates.get("low"),
                    "market_high": market_rates.get("high"),
                    "samples": market_rates.get("samples", 0),
                }
        except Exception:
            pass

        price_low, price_high, cy_mid, special_items, min_charge_applied = calculate_price(
            result_data,
            rate_low=user.price_per_cy_low or 35.0,
            rate_high=user.price_per_cy_high or 40.0,
            rate_premium=user.price_per_cy_premium or 55.0,
            min_charge=user.min_charge or 75.0,
            market_rates=market_rates,
        )

        async with AsyncSessionLocal() as db:
            est = Estimate(
                user_id=user.id,
                team_member_id=job.get("team_member_id", 0),
                estimate_name=job.get("estimate_name", ""),
                customer_name=job.get("customer_name", ""),
                customer_email=job.get("customer_email", ""),
                customer_phone=job.get("customer_phone", ""),
                photos_count=num_photos,
                result_json=json.dumps(result_data),
                price_low=price_low,
                price_high=price_high,
                cy_estimate=cy_mid,
                pass1_json=pass1_json_str,
                pass2_json="",
                lookups_json=lookups_json_str,
            )
            db.add(est)
            await db.commit()
            await db.refresh(est)
            estimate_id = est.id

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user.id))
            u = result.scalar_one_or_none()
            if u:
                u.estimates_used = u.estimates_used + 1
                await db.commit()
                user = u

        try:
            await update_library_from_estimate(result_data.get("items", []))
        except Exception:
            pass

        remaining = max(0, user.estimates_limit - user.estimates_used)

        resp = {
            "id": estimate_id,
            "estimate_name": job.get("estimate_name", ""),
            "price_low": price_low,
            "price_high": price_high,
            "cy_estimate": cy_mid,
            "items": result_data.get("items", []),
            "reference_points": result_data.get("reference_points", []),
            "job_type": result_data.get("job_type", "standard"),
            "conditions": result_data.get("conditions", []),
            "notes": result_data.get("notes", ""),
            "confidence": result_data.get("confidence", 75),
            "estimates_remaining": remaining,
            "special_items": special_items,
            "items_looked_up": lookups_done,
            "rate_low": user.price_per_cy_low or 35.0,
            "rate_high": user.price_per_cy_high or 40.0,
            "rate_premium": user.price_per_cy_premium or 55.0,
            "min_charge": user.min_charge or 75.0,
            "min_charge_applied": min_charge_applied,
            "potential_duplicates": result_data.get("potential_duplicates", []),
        }

        if market_context:
            resp["market_context"] = market_context

        job["status"] = "complete"
        job["message"] = "Estimate ready!"
        job["result"] = resp

    except Exception as e:
        job["status"] = "error"
        job["message"] = "An error occurred while processing your estimate. Please try again."
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


PRICE_TO_TIER = {
    "price_1T7PXXAPEzwLONiqIIrAtsQZ": "starter",
    "price_1T6iUPAPEzwLONiqp31lIw9T": "pro",
    "price_1T7PXXAPEzwLONiqpQbgpgZ8": "agency",
}


@app.post("/api/payments/create-checkout")
async def create_checkout(request: Request):
    user = await require_user(request)
    body = await request.json()
    price_id = body.get("price_id")

    if not price_id:
        raise HTTPException(status_code=400, detail="price_id is required.")

    tier_name = PRICE_TO_TIER.get(price_id)
    if not tier_name:
        raise HTTPException(status_code=400, detail="Invalid price_id.")

    checkout_params = {
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": str(request.base_url) + "payment-success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": str(request.base_url) + "upgrade",
        "metadata": {"user_id": str(user.id)},
        "allow_promotion_codes": True,
    }

    if user.stripe_customer_id:
        checkout_params["customer"] = user.stripe_customer_id
    else:
        checkout_params["customer_email"] = user.email

    try:
        session = await asyncio.to_thread(
            lambda: stripe.checkout.Session.create(**checkout_params)
        )
        return {"checkout_url": session.url}
    except Exception:
        raise HTTPException(status_code=500, detail="Payment processing error. Please try again.")


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
        customer_id = session.get("customer", "")
        subscription_id = session.get("subscription", "")

        tier_name = "starter"
        try:
            line_items = stripe.checkout.Session.list_line_items(session["id"], limit=1)
            if line_items and line_items.data:
                price_id = line_items.data[0].price.id
                tier_name = PRICE_TO_TIER.get(price_id, "starter")
        except Exception:
            pass

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
                "estimate_name": e.estimate_name or "",
                "customer_name": e.customer_name or "",
                "customer_email": e.customer_email or "",
            }
            for e in estimates
        ]


@app.get("/api/estimates/{estimate_id}")
async def get_estimate_detail(request: Request, estimate_id: int):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate).where(Estimate.id == estimate_id, Estimate.user_id == user.id)
        )
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        result_data = {}
        if e.result_json:
            try:
                result_data = json.loads(e.result_json)
            except Exception:
                pass
        return {
            "id": e.id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "photos_count": e.photos_count,
            "price_low": e.price_low,
            "price_high": e.price_high,
            "cy_estimate": e.cy_estimate,
            "estimate_name": e.estimate_name or "",
            "customer_name": e.customer_name or "",
            "customer_email": e.customer_email or "",
            "customer_phone": e.customer_phone or "",
            "result": result_data,
        }


# ============== ADMIN API ==============


@app.get("/api/admin/analytics")
async def admin_analytics(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
        total_estimates = (await db.execute(select(func.count(Estimate.id)))).scalar() or 0
        estimates_today = (await db.execute(
            select(func.count(Estimate.id)).where(Estimate.created_at >= today)
        )).scalar() or 0
        estimates_week = (await db.execute(
            select(func.count(Estimate.id)).where(Estimate.created_at >= week_ago)
        )).scalar() or 0
        estimates_month = (await db.execute(
            select(func.count(Estimate.id)).where(Estimate.created_at >= month_ago)
        )).scalar() or 0

        tier_rows = (await db.execute(
            select(User.subscription_tier, func.count(User.id))
            .group_by(User.subscription_tier)
        )).all()
        tier_counts = {row[0]: row[1] for row in tier_rows}
        paid_users = sum(v for k, v in tier_counts.items() if k != "free")

        avg_cy = (await db.execute(select(func.avg(Estimate.cy_estimate)))).scalar() or 0
        avg_price = (await db.execute(select(func.avg(Estimate.price_low)))).scalar() or 0

        recent_users = (await db.execute(
            select(User).order_by(User.created_at.desc()).limit(10)
        )).scalars().all()

        team_count = (await db.execute(select(func.count(TeamMember.id)))).scalar() or 0

        return {
            "total_users": total_users,
            "paid_users": paid_users,
            "total_estimates": total_estimates,
            "estimates_today": estimates_today,
            "estimates_week": estimates_week,
            "estimates_month": estimates_month,
            "tier_counts": tier_counts,
            "avg_cy": round(float(avg_cy), 1),
            "avg_price": round(float(avg_price), 2),
            "team_members": team_count,
            "recent_users": [
                {"id": u.id, "email": u.email, "company_name": u.company_name,
                 "tier": u.subscription_tier, "created_at": u.created_at.isoformat() if u.created_at else None}
                for u in recent_users
            ]
        }


@app.get("/api/admin/users")
async def admin_users(request: Request, q: str = "", page: int = 1):
    await require_admin(request)
    limit = 25
    offset = (page - 1) * limit
    async with AsyncSessionLocal() as db:
        query = select(User)
        if q:
            query = query.where(User.email.contains(q) | User.company_name.contains(q))
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
        result = await db.execute(query.order_by(User.created_at.desc()).offset(offset).limit(limit))
        users = result.scalars().all()
        return {
            "users": [
                {"id": u.id, "email": u.email, "company_name": u.company_name,
                 "company_city": u.company_city, "company_state": u.company_state,
                 "tier": u.subscription_tier, "estimates_used": u.estimates_used,
                 "estimates_limit": u.estimates_limit, "is_admin": u.is_admin,
                 "created_at": u.created_at.isoformat() if u.created_at else None}
                for u in users
            ],
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }


@app.get("/api/admin/plans")
async def admin_plans(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PlanConfig).order_by(PlanConfig.price_cents))
        plans = result.scalars().all()
        return [
            {"id": p.id, "tier_name": p.tier_name, "display_name": p.display_name,
             "price_cents": p.price_cents, "estimate_limit": p.estimate_limit,
             "features": json.loads(p.features_json) if p.features_json else [],
             "stripe_price_id": p.stripe_price_id, "is_active": p.is_active}
            for p in plans
        ]


@app.put("/api/admin/plans/{plan_id}")
async def admin_update_plan(request: Request, plan_id: int):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PlanConfig).where(PlanConfig.id == plan_id))
        plan = result.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        if "display_name" in body:
            plan.display_name = body["display_name"]
        if "price_cents" in body:
            plan.price_cents = int(body["price_cents"])
        if "estimate_limit" in body:
            plan.estimate_limit = int(body["estimate_limit"])
        if "features" in body:
            plan.features_json = json.dumps(body["features"])
        if "is_active" in body:
            plan.is_active = bool(body["is_active"])
        if "stripe_price_id" in body:
            plan.stripe_price_id = body["stripe_price_id"]
        await db.commit()
        return {"success": True}


@app.get("/api/site-config")
async def public_site_config():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SiteConfig))
        configs = result.scalars().all()
        return {c.config_key: c.config_value for c in configs}


@app.get("/api/admin/site-config")
async def admin_get_site_config(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SiteConfig))
        configs = result.scalars().all()
        return {c.config_key: c.config_value for c in configs}


@app.put("/api/admin/site-config")
async def admin_update_site_config(request: Request):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        for key, value in body.items():
            result = await db.execute(select(SiteConfig).where(SiteConfig.config_key == key))
            config = result.scalar_one_or_none()
            if config:
                config.config_value = str(value)
                config.updated_at = datetime.utcnow()
            else:
                db.add(SiteConfig(config_key=key, config_value=str(value)))
        await db.commit()
        return {"success": True}


@app.get("/api/admin/estimates")
async def admin_estimates(request: Request, page: int = 1, q: str = ""):
    await require_admin(request)
    limit = 25
    offset = (page - 1) * limit
    async with AsyncSessionLocal() as db:
        query = select(Estimate)
        total = (await db.execute(select(func.count(Estimate.id)))).scalar() or 0
        result = await db.execute(query.order_by(Estimate.created_at.desc()).offset(offset).limit(limit))
        estimates = result.scalars().all()

        user_ids = list(set(e.user_id for e in estimates if e.user_id))
        users_map = {}
        if user_ids:
            u_result = await db.execute(select(User).where(User.id.in_(user_ids)))
            for u in u_result.scalars().all():
                users_map[u.id] = u.email

        return {
            "estimates": [
                {"id": e.id, "user_email": users_map.get(e.user_id, "Unknown"),
                 "photos_count": e.photos_count, "price_low": e.price_low,
                 "price_high": e.price_high, "cy_estimate": e.cy_estimate,
                 "team_member_id": e.team_member_id,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in estimates
            ],
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }


# ============== TEAM API ==============


@app.post("/api/team/members")
async def create_team_member(request: Request):
    user = await require_admin(request)
    body = await request.json()
    name = body.get("name", "").strip()
    pin = body.get("pin", "").strip()
    if not name or not pin or len(pin) < 4:
        raise HTTPException(status_code=400, detail="Name and PIN (min 4 digits) required.")

    pin_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    )
    async with AsyncSessionLocal() as db:
        member = TeamMember(
            owner_user_id=user.id,
            name=name,
            pin_hash=pin_hash,
            role=body.get("role", "estimator"),
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        return {"id": member.id, "name": member.name, "role": member.role}


@app.get("/api/team/members")
async def list_team_members(request: Request):
    user = await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(TeamMember.owner_user_id == user.id).order_by(TeamMember.created_at.desc())
        )
        members = result.scalars().all()
        return [
            {"id": m.id, "name": m.name, "role": m.role, "is_active": m.is_active,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in members
        ]


@app.put("/api/team/members/{member_id}")
async def update_team_member(request: Request, member_id: int):
    user = await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(TeamMember.id == member_id, TeamMember.owner_user_id == user.id)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=404, detail="Team member not found")
        if "name" in body:
            member.name = body["name"]
        if "role" in body:
            member.role = body["role"]
        if "is_active" in body:
            member.is_active = bool(body["is_active"])
        if "pin" in body and body["pin"]:
            member.pin_hash = await asyncio.to_thread(
                lambda: bcrypt.hashpw(body["pin"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            )
        await db.commit()
        return {"success": True}


@app.delete("/api/team/members/{member_id}")
async def delete_team_member(request: Request, member_id: int):
    user = await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TeamMember).where(TeamMember.id == member_id, TeamMember.owner_user_id == user.id)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=404, detail="Team member not found")
        member.is_active = False
        await db.commit()
        return {"success": True}


@app.post("/api/team/auth")
async def team_auth(request: Request):
    client_ip = get_client_ip(request)
    if auth_rate_limiter.is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    body = await request.json()
    company_code = body.get("company_code", "").strip().lower()
    pin = body.get("pin", "").strip()

    if not company_code or not pin:
        raise HTTPException(status_code=400, detail="Company code and PIN required.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(func.lower(User.email) == company_code)
        )
        owner = result.scalar_one_or_none()
        if not owner:
            result = await db.execute(
                select(User).where(func.lower(User.company_name) == company_code)
            )
            owner = result.scalar_one_or_none()
        if not owner:
            raise HTTPException(status_code=401, detail="Company not found.")

        result = await db.execute(
            select(TeamMember).where(
                TeamMember.owner_user_id == owner.id,
                TeamMember.is_active == True
            )
        )
        members = result.scalars().all()

        matched_member = None
        for m in members:
            valid = await asyncio.to_thread(
                lambda mem=m: bcrypt.checkpw(pin.encode("utf-8"), mem.pin_hash.encode("utf-8"))
            )
            if valid:
                matched_member = m
                break

        if not matched_member:
            raise HTTPException(status_code=401, detail="Invalid PIN.")

        token = secrets.token_hex(32)
        sess = TeamSession(
            team_member_id=matched_member.id,
            owner_user_id=owner.id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(hours=12),
        )
        db.add(sess)
        await db.commit()

    response = JSONResponse({
        "success": True,
        "name": matched_member.name,
        "company": owner.company_name,
        "redirect": "/team/app"
    })
    response.set_cookie(
        "team_token", token, httponly=True, samesite="lax", secure=True,
        max_age=12 * 3600, path="/"
    )
    return response


@app.get("/api/team/me")
async def team_me(request: Request):
    member, owner = await get_team_member(request)
    if not member:
        return JSONResponse({"authenticated": False})
    remaining = max(0, owner.estimates_limit - owner.estimates_used)
    return {
        "authenticated": True,
        "name": member.name,
        "role": member.role,
        "company_name": owner.company_name,
        "estimates_remaining": remaining,
    }


@app.post("/api/team/estimate")
async def team_create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
    estimate_name: str = Form(default=""),
    customer_name: str = Form(default=""),
    customer_email: str = Form(default=""),
    customer_phone: str = Form(default=""),
):
    member, owner = await require_team_member(request)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == owner.id))
        fresh_owner = result.scalar_one_or_none()
        if not fresh_owner or fresh_owner.estimates_used >= fresh_owner.estimates_limit:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        user = fresh_owner

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service is not configured. Please contact support.")

    if not files:
        raise HTTPException(status_code=400, detail="At least one photo is required.")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 photos allowed.")

    try:
        rooms_list = json.loads(rooms)
    except Exception:
        rooms_list = []

    ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
    MAX_FILE_SIZE = 20 * 1024 * 1024
    photo_data = []
    for i, file in enumerate(files):
        if file.content_type and file.content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} has an unsupported file type. Please upload images only.")
        raw = await file.read()
        if len(raw) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} exceeds 20MB limit.")
        compressed = compress_image(raw)
        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        room_label = rooms_list[i] if i < len(rooms_list) else "Unknown"
        photo_data.append({"b64": b64, "room": room_label, "index": i + 1})

    room_groups = {}
    for pd in photo_data:
        room = pd["room"]
        if room not in room_groups:
            room_groups[room] = []
        room_groups[room].append(pd)

    image_content = []
    for room, group_photos in room_groups.items():
        if len(group_photos) > 1:
            image_content.append({
                "type": "text",
                "text": f"\n--- ROOM: {room} ({len(group_photos)} photos — these show DIFFERENT ANGLES of the SAME space. DO NOT double-count items visible in multiple photos.) ---"
            })
        for pd in group_photos:
            label = f"Photo {pd['index']} (Room: {room})"
            if len(group_photos) > 1:
                label += f" [angle {group_photos.index(pd) + 1} of {len(group_photos)} for this room]"
            image_content.append({"type": "text", "text": f"{label}:"})
            image_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": pd["b64"]}
            })

    now = datetime.utcnow()
    job_id = f"team-{member.id}-{secrets.token_hex(8)}"
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": owner.id,
        "team_member_id": member.id,
        "estimate_name": estimate_name.strip(),
        "customer_name": customer_name,
        "customer_email": customer_email,
        "customer_phone": customer_phone,
        "created_at": now,
    }

    asyncio.create_task(run_estimate(
        job_id=job_id,
        user=user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


@app.get("/api/team/estimate/status/{job_id}")
async def team_estimate_status(request: Request, job_id: str):
    member, owner = await require_team_member(request)
    job = estimate_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.get("user_id") != owner.id:
        raise HTTPException(status_code=403, detail="Not authorized.")

    resp = {
        "status": job["status"],
        "message": job["message"],
    }
    if job["status"] == "complete" and job["result"]:
        resp["result"] = job["result"]
    elif job["status"] == "error":
        resp["error"] = job["message"]
    return resp


@app.get("/api/team/estimates")
async def team_estimates(request: Request):
    member, owner = await require_team_member(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate)
            .where(Estimate.team_member_id == member.id)
            .order_by(Estimate.created_at.desc())
            .limit(50)
        )
        estimates = result.scalars().all()
        return [
            {"id": e.id, "created_at": e.created_at.isoformat() if e.created_at else None,
             "photos_count": e.photos_count, "price_low": e.price_low,
             "price_high": e.price_high, "cy_estimate": e.cy_estimate,
             "estimate_name": e.estimate_name or "",
             "customer_name": e.customer_name}
            for e in estimates
        ]


@app.post("/api/team/logout")
async def team_logout(request: Request):
    token = request.cookies.get("team_token")
    if token:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TeamSession).where(TeamSession.token == token))
            sess = result.scalar_one_or_none()
            if sess:
                await db.delete(sess)
                await db.commit()
    response = JSONResponse({"success": True})
    response.delete_cookie("team_token", path="/")
    return response


# ============== PDF GENERATION ==============


def generate_estimate_pdf(estimate, user, items, special_items):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch,
                            leftMargin=0.75*inch, rightMargin=0.75*inch)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'], fontSize=22,
                                  textColor=colors.HexColor('#1a1a2e'), spaceAfter=6)
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=11,
                                     textColor=colors.HexColor('#666666'), spaceAfter=20)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading2'], fontSize=14,
                                    textColor=colors.HexColor('#1a1a2e'), spaceBefore=16, spaceAfter=8)
    body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=10,
                                 textColor=colors.HexColor('#333333'), leading=14)
    small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8,
                                  textColor=colors.HexColor('#999999'), leading=11)

    elements = []

    company_name = user.company_name or "WhatShouldICharge"
    elements.append(Paragraph(company_name, title_style))
    elements.append(Paragraph("Junk Removal Estimate", subtitle_style))

    info_data = [
        ["Estimate #:", str(estimate.id), "Date:", estimate.created_at.strftime("%B %d, %Y") if estimate.created_at else "N/A"],
        ["Photos:", str(estimate.photos_count or 0), "Volume:", f"{estimate.cy_estimate or 0} CY"],
    ]
    if estimate.estimate_name:
        info_data.append(["Job Name:", estimate.estimate_name, "", ""])
    if estimate.customer_name:
        info_data.append(["Customer:", estimate.customer_name, "", ""])

    info_table = Table(info_data, colWidths=[1.2*inch, 2.3*inch, 1.0*inch, 2.3*inch])
    info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#666666')),
        ('TEXTCOLOR', (2, 0), (2, -1), colors.HexColor('#666666')),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
        ('FONTNAME', (3, 0), (3, -1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 16))

    price_data = [[
        Paragraph(f"<b>${estimate.price_low:,.0f} — ${estimate.price_high:,.0f}</b>",
                  ParagraphStyle('Price', fontSize=20, textColor=colors.HexColor('#16a34a'), alignment=1))
    ]]
    price_table = Table(price_data, colWidths=[6.8*inch])
    price_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0fdf4')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#bbf7d0')),
        ('TOPPADDING', (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
    ]))
    elements.append(price_table)
    elements.append(Paragraph("Estimated price range based on volume", small_style))
    elements.append(Spacer(1, 8))

    if items:
        elements.append(Paragraph("Item Breakdown", heading_style))
        item_data = [["Item", "Qty", "Category", "Cubic Yards"]]
        for item in items:
            item_data.append([
                item.get("name", "Unknown"),
                str(item.get("quantity", 1)),
                item.get("category", "other").title(),
                f"{item.get('cubic_yards', 0)} CY"
            ])
        item_table = Table(item_data, colWidths=[3.0*inch, 0.8*inch, 1.5*inch, 1.5*inch])
        item_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(item_table)

    if special_items:
        elements.append(Spacer(1, 12))
        elements.append(Paragraph("Special Recycling Items", heading_style))
        elements.append(Paragraph(
            "The following items may require additional recycling or disposal fees. "
            "These fees are not included in the estimate above.",
            body_style
        ))
        for si in special_items:
            elements.append(Paragraph(f"• {si.get('name', 'Unknown')} × {si.get('quantity', 1)}", body_style))

    elements.append(Spacer(1, 24))
    elements.append(Paragraph("Important Notes", heading_style))
    elements.append(Paragraph(
        "This estimate is based on items visible in the provided photos. "
        "Actual pricing may vary based on job conditions, access, and items not pictured. "
        "Recycling fees for special items (TVs, mattresses, tires, etc.) are additional. "
        "Final pricing will be confirmed by your technician on arrival.",
        body_style
    ))

    elements.append(Spacer(1, 20))
    contact_parts = [company_name]
    if user.company_city and user.company_state:
        contact_parts.append(f"{user.company_city}, {user.company_state}")
    elements.append(Paragraph(" | ".join(contact_parts), small_style))
    elements.append(Paragraph("Powered by WhatShouldICharge.app", small_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer


@app.post("/api/estimate/{estimate_id}/pdf")
async def generate_pdf(request: Request, estimate_id: int):
    user = None
    member = None
    team_token = request.cookies.get("team_token")
    if team_token:
        member, owner = await get_team_member(request)
        if member and owner:
            user = owner
    if not user:
        user = await require_user(request)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Estimate).where(Estimate.id == estimate_id))
        estimate = result.scalar_one_or_none()
        if not estimate:
            raise HTTPException(status_code=404, detail="Estimate not found")
        if estimate.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized")

    result_data = json.loads(estimate.result_json) if estimate.result_json else {}
    items = result_data.get("items", [])
    special_items = [i for i in items if i.get("is_special")]

    buffer = await asyncio.to_thread(
        lambda: generate_estimate_pdf(estimate, user, items, special_items)
    )

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=estimate-{estimate_id}.pdf"}
    )


@app.post("/api/estimate/{estimate_id}/send")
async def send_estimate(request: Request, estimate_id: int):
    user = None
    team_token = request.cookies.get("team_token")
    if team_token:
        member, owner = await get_team_member(request)
        if member and owner:
            user = owner
    if not user:
        user = await require_user(request)

    body = await request.json()
    to_email = body.get("email", "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="Email address required.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Estimate).where(Estimate.id == estimate_id))
        estimate = result.scalar_one_or_none()
        if not estimate:
            raise HTTPException(status_code=404, detail="Estimate not found")
        if estimate.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized")

    company = user.company_name or "WhatShouldICharge"
    send_email(
        to_email,
        f"Your Junk Removal Estimate from {company}",
        f"<h2>Junk Removal Estimate</h2>"
        f"<p>Thank you for choosing <strong>{company}</strong>.</p>"
        f"<p><strong>Estimated Price Range: ${estimate.price_low:,.0f} — ${estimate.price_high:,.0f}</strong></p>"
        f"<p>Estimated Volume: {estimate.cy_estimate or 0} CY</p>"
        f"<p>Photos Analyzed: {estimate.photos_count or 0}</p>"
        f"<hr>"
        f"<p style='color:#666;font-size:12px;'>This estimate is based on items visible in provided photos. "
        f"Actual pricing may vary. Recycling fees for special items are additional. "
        f"Final pricing confirmed on arrival.</p>"
        f"<p style='color:#999;font-size:11px;'>Powered by WhatShouldICharge.app</p>"
    )
    return {"success": True}
