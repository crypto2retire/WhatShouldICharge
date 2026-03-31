import os
import re
import json
import base64
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import anthropic
import bcrypt
import stripe
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, Float, DateTime, Text, String, Boolean, ForeignKey, select, text, func, update
import asyncio
from PIL import Image
import io
from cryptography.fernet import Fernet, InvalidToken

from services.volume_lookup import validate_estimate
from services.industry_config import get_industry_config, get_system_prompt, get_calibration_items, get_business_rules

_encryption_key = os.environ.get("ENCRYPTION_KEY")
_fernet = Fernet(_encryption_key.encode()) if _encryption_key else None


def encrypt_pii(plaintext: str) -> str:
    if not plaintext or not _fernet:
        return plaintext or ""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_pii(ciphertext: str) -> str:
    if not ciphertext or not _fernet:
        return ciphertext or ""
    # Graceful fallback for pre-encryption data
    if not ciphertext.startswith("gAAAAA"):
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        return ciphertext


def _is_loopback_or_rfc1918(host: str) -> bool:
    """True when the TCP peer is local/private (typical when sitting behind Railway's proxy)."""
    if not host:
        return False
    h = host.lower().strip()
    if h in ("127.0.0.1", "::1", "localhost"):
        return True
    if h.startswith("10."):
        return True
    if h.startswith("192.168."):
        return True
    parts = h.split(".")
    if len(parts) == 4 and parts[0] == "172":
        try:
            second = int(parts[1])
            if 16 <= second <= 31:
                return True
        except ValueError:
            pass
    if h.startswith("fc") or h.startswith("fd"):  # IPv6 ULA
        return True
    return False


def _trust_forwarded_headers(request: Request) -> bool:
    if os.environ.get("TRUST_FORWARDED_HEADERS", "").strip().lower() in ("1", "true", "yes"):
        return True
    peer = request.client.host if request.client else ""
    return _is_loopback_or_rfc1918(peer)


def get_client_ip(request: Request) -> str:
    """Client IP for rate limiting. Use proxy headers only when peer is a trusted hop (e.g. Railway)."""
    if _trust_forwarded_headers(request):
        for header in ("x-real-ip", "cf-connecting-ip", "true-client-ip"):
            raw = request.headers.get(header)
            if raw:
                ip = raw.split(",")[0].strip()
                if ip:
                    return ip
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[0]
    return request.client.host if request.client else "unknown"


def _is_production_env() -> bool:
    env = (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("ENVIRONMENT") or "production").strip().lower()
    return env != "development"


def _cors_allow_origins() -> list[str]:
    """Explicit browser origins only. Override with CORS_ORIGINS=comma-separated URLs."""
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if raw:
        return sorted({o.strip().rstrip("/") for o in raw.split(",") if o.strip()})
    origins: set[str] = {
        "https://whatshouldicharge.app",
        "https://www.whatshouldicharge.app",
    }
    pub = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if pub:
        origins.add(("https://" + pub).rstrip("/") if not pub.startswith("http") else pub.rstrip("/"))
    if not _is_production_env():
        origins.update(
            {
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:5000",
                "http://localhost:5000",
                "http://127.0.0.1:8000",
                "http://localhost:8000",
            }
        )
    return sorted(origins)


_prod = _is_production_env()
app = FastAPI(
    title="WhatShouldICharge",
    docs_url=None if _prod else "/docs",
    redoc_url=None if _prod else "/redoc",
    openapi_url=None if _prod else "/openapi.json",
)


limiter = Limiter(key_func=get_client_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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
            "frame-ancestors 'self' whatshouldicharge.app *.whatshouldicharge.app; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif "application/json" in ct:
            response.headers["Cache-Control"] = "no-store"
        return response


CSRF_EXEMPT_PATHS = {
    "/api/auth/login", "/api/auth/signup", "/api/auth/forgot-password",
    "/api/team/auth",
    "/api/stripe/webhook",
    "/api/payments/webhook",
}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE"):
            path = request.url.path
            # Exempt public endpoints, auth endpoints, and webhook
            if not path.startswith("/api/public/") and path not in CSRF_EXEMPT_PATHS:
                cookie_token = request.cookies.get("csrf_token")
                header_token = request.headers.get("x-csrf-token")
                if not cookie_token or not header_token or cookie_token != header_token:
                    return JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"})

        response = await call_next(request)

        if "csrf_token" not in request.cookies:
            csrf_token = secrets.token_hex(32)
            response.set_cookie(
                "csrf_token", csrf_token, httponly=False, samesite="lax",
                secure=True, max_age=30 * 24 * 3600, path="/"
            )

        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)


_response_cache: dict[str, dict] = {}


def cache_get(key: str):
    entry = _response_cache.get(key)
    if entry and entry["expires"] > time.time():
        return entry["data"]
    if entry:
        del _response_cache[key]
    return None


def cache_set(key: str, data, ttl: int = 60):
    _response_cache[key] = {"data": data, "expires": time.time() + ttl}


def cache_invalidate(key: str):
    _response_cache.pop(key, None)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
)

def _get_database_url() -> str:
    """Resolve database URL for Railway PostgreSQL or local SQLite fallback."""
    import logging
    logger = logging.getLogger("wsic.db")

    # Method 1: Check for full connection URL env vars
    for key in ("DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = os.environ.get(key, "").strip()
        if url and url.startswith("postgres"):
            # Normalize to async driver
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            logger.info(f"[db] Using {key} (PostgreSQL)")
            return url

    # Method 2: Build URL from individual PG* variables (Railway Postgres always has these)
    pghost = os.environ.get("PGHOST", "").strip()
    pgport = os.environ.get("PGPORT", "5432").strip()
    pguser = os.environ.get("PGUSER", "").strip()
    pgpassword = os.environ.get("PGPASSWORD", "").strip()
    pgdatabase = os.environ.get("PGDATABASE", "").strip()
    if pghost and pguser and pgpassword and pgdatabase:
        url = f"postgresql+asyncpg://{pguser}:{pgpassword}@{pghost}:{pgport}/{pgdatabase}"
        logger.info(f"[db] Built URL from PG* env vars (host={pghost})")
        return url

    # Method 3: Local dev fallback to SQLite
    logger.warning("[db] No PostgreSQL config found — falling back to SQLite (DATA WILL BE LOST ON DEPLOY)")
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

# Legacy — kept for reference but no longer used for gating
TIER_LIMITS = {
    "free": 3,
    "solo": 999,
    "team": 999,
    "enterprise": 999,
    "custom": 999,
    "starter": 20,
    "pro": 40,
    "agency": 999,
}

# Seeded into `credit_packs` on first deploy (see seed_credit_packs). Runtime reads packs from the database.
_DEFAULT_CREDIT_PACKS_SEED = {
    "single": {
        "name": "Single Estimate",
        "credits": 1,
        "price_cents": 1000,
        "discount_pct": 0,
        "stripe_price_id": "price_1TDUHaAPEzwLONiqUUjQTuTS",
        "description": "1 estimate credit",
        "is_featured": False,
    },
    "10_pack": {
        "name": "10-Pack",
        "credits": 10,
        "price_cents": 6000,
        "discount_pct": 40,
        "stripe_price_id": "price_1TDUHbAPEzwLONiqhcyemzyF",
        "description": "10 estimate credits (40% off)",
        "is_featured": False,
    },
    "25_pack": {
        "name": "25-Pack",
        "credits": 25,
        "price_cents": 12500,
        "discount_pct": 50,
        "stripe_price_id": "price_1TDUHcAPEzwLONiqwG3OZf9I",
        "description": "25 estimate credits (50% off)",
        "is_featured": False,
    },
    "50_pack": {
        "name": "50-Pack",
        "credits": 50,
        "price_cents": 20000,
        "discount_pct": 60,
        "stripe_price_id": "price_1TDUHdAPEzwLONiqFtviLtbK",
        "description": "50 estimate credits (60% off)",
        "is_featured": True,
    },
    "100_pack": {
        "name": "100-Pack",
        "credits": 100,
        "price_cents": 30000,
        "discount_pct": 70,
        "stripe_price_id": "price_1TDUHeAPEzwLONiqETAdtOiz",
        "description": "100 estimate credits (70% off)",
        "is_featured": False,
    },
    "250_pack": {
        "name": "250-Pack",
        "credits": 250,
        "price_cents": 50000,
        "discount_pct": 80,
        "stripe_price_id": "price_1TDUHeAPEzwLONiqOoqQr7UP",
        "description": "250 estimate credits (80% off)",
        "is_featured": False,
    },
}

_PACK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,62}$")

STATE_TIMEZONE_MAP = {
    "CT": "America/New_York", "DE": "America/New_York", "GA": "America/New_York",
    "MA": "America/New_York", "MD": "America/New_York", "ME": "America/New_York",
    "NC": "America/New_York", "NH": "America/New_York", "NJ": "America/New_York",
    "NY": "America/New_York", "OH": "America/New_York", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "VA": "America/New_York",
    "VT": "America/New_York", "WV": "America/New_York", "DC": "America/New_York",
    "MI": "America/New_York", "FL": "America/New_York",
    "AL": "America/Chicago", "AR": "America/Chicago", "IA": "America/Chicago",
    "IL": "America/Chicago", "KS": "America/Chicago", "KY": "America/Chicago",
    "LA": "America/Chicago", "MN": "America/Chicago", "MO": "America/Chicago",
    "MS": "America/Chicago", "OK": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "WI": "America/Chicago", "IN": "America/Chicago",
    "ND": "America/Chicago", "NE": "America/Chicago", "SD": "America/Chicago",
    "AZ": "America/Phoenix", "CO": "America/Denver", "MT": "America/Denver",
    "NM": "America/Denver", "UT": "America/Denver", "WY": "America/Denver",
    "ID": "America/Boise",
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}


PLAN_CALL_LIMITS = {"free": 3, "solo": 150, "team": 750, "enterprise": 2500, "custom": 999}
OVERAGE_RATE_CENTS = {"solo": 10, "team": 10, "enterprise": 8, "custom": 10}


def _reset_billing_cycle_if_needed(user):
    """Reset monthly usage if billing cycle has elapsed. Returns True if reset."""
    today = datetime.utcnow()
    if user.billing_cycle_start is None or (today - user.billing_cycle_start).days >= 30:
        user.monthly_calls_used = 0
        user.overage_charges_cents = 0
        user.billing_cycle_start = today
        return True
    return False


def _check_usage_limit(user):
    """Check if user can make an estimate. Returns (allowed, error_response_dict_or_None)."""
    _reset_billing_cycle_if_needed(user)
    limit = user.monthly_call_limit or PLAN_CALL_LIMITS.get(user.subscription_tier, 3)
    used = user.monthly_calls_used or 0

    if used < limit:
        return True, None

    # Over limit — check overage mode
    mode = getattr(user, 'overage_mode', 'warn_and_charge') or 'warn_and_charge'

    if mode == 'hard_stop':
        return False, {
            "detail": "monthly_limit_reached",
            "message": "You've reached your monthly estimate limit. An owner or manager can add more funds or change your overage settings.",
            "used": used, "limit": limit
        }

    if mode == 'capped':
        cap = getattr(user, 'overage_cap_cents', 0) or 0
        charged = getattr(user, 'overage_charges_cents', 0) or 0
        if charged >= cap:
            return False, {
                "detail": "overage_cap_reached",
                "message": f"You've reached your ${cap/100:.2f} overage cap. An owner or manager can increase the cap or add funds.",
                "used": used, "limit": limit, "overage_spent": charged
            }

    return True, None


def _record_usage(user):
    """Increment usage and add overage charge if over limit."""
    user.monthly_calls_used = (user.monthly_calls_used or 0) + 1
    limit = user.monthly_call_limit or PLAN_CALL_LIMITS.get(user.subscription_tier, 3)
    if user.monthly_calls_used > limit:
        rate = OVERAGE_RATE_CENTS.get(user.subscription_tier, 10)
        user.overage_charges_cents = (user.overage_charges_cents or 0) + rate


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
    price_per_cy_standard = Column(Float, default=None)
    price_per_cy_heavy = Column(Float, default=None)
    is_active = Column(Boolean, default=True)
    admin_notes = Column(Text, default="")
    timezone = Column(String, default="America/Chicago")
    monthly_call_limit = Column(Integer, default=150)
    monthly_calls_used = Column(Integer, default=0)
    billing_cycle_start = Column(DateTime, default=None)
    overage_mode = Column(String, default="warn_and_charge")
    overage_cap_cents = Column(Integer, default=0)
    overage_charges_cents = Column(Integer, default=0)
    role = Column(String, default="owner")
    industry = Column(String, default="junk_removal")
    credit_balance = Column(Integer, default=0)
    credits_purchased_total = Column(Integer, default=0)
    credits_used_total = Column(Integer, default=0)
    free_trial_used = Column(Integer, default=0)
    free_trial_email = Column(String, nullable=True)
    google_tag_id = Column(String, default="")
    fb_pixel_id = Column(String, default="")


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


class CreditPack(Base):
    __tablename__ = "credit_packs"
    id = Column(Integer, primary_key=True, index=True)
    pack_key = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    credits = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    description = Column(Text, default="")
    stripe_product_id = Column(String(120), default="")
    stripe_price_id = Column(String(120), default="")
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    transaction_type = Column(String, nullable=False)  # "purchase", "usage", "free_trial", "refund", "bonus"
    credits = Column(Integer, nullable=False)  # positive = added, negative = used
    balance_after = Column(Integer, nullable=False)  # balance after this transaction
    description = Column(String, nullable=True)
    stripe_session_id = Column(String, nullable=True)
    pack_type = Column(String, nullable=True)
    amount_cents = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now(), index=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_type = Column(String(20), nullable=False)  # 'percentage' or 'fixed'
    discount_value = Column(Float, nullable=False)
    applies_to = Column(Text, default='{"products":["all"]}')
    usage_limit = Column(Integer, default=0)  # 0 = unlimited
    times_used = Column(Integer, default=0)
    expires_at = Column(DateTime, default=None)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    preferred_contact = Column(String, default="phone")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    photos_count = Column(Integer)
    result_json = Column(Text)
    price_low = Column(Float)
    price_high = Column(Float)
    cy_estimate = Column(Float)
    pass1_json = Column(Text, default="")
    pass2_json = Column(Text, default="")
    lookups_json = Column(Text, default="")
    photos_json = Column(Text, default="")
    actual_price = Column(Float, default=None)
    actual_cy = Column(Float, default=None)
    accuracy_notes = Column(Text, default="")
    preferred_contact = Column(String, default="phone")
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    api_cost_cents = Column(Integer, default=0)
    model_used = Column(String(50), default="")
    appointment_requested = Column(Boolean, default=False)
    appointment_contact_method = Column(String, default="")
    appointment_preferred_day = Column(String, default="")
    appointment_preferred_time = Column(String, default="")
    appointment_requested_at = Column(DateTime, default=None)
    additional_items_text = Column(Text, default="")


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

    # Log which database we're connecting to
    db_type = "PostgreSQL" if _is_postgres else "SQLite (EPHEMERAL - DATA WILL BE LOST ON DEPLOY)"
    env_keys = [k for k in ("DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL", "DATABASE_URL") if os.environ.get(k)]
    logger.warning(f"[init_db] Database type: {db_type}")
    logger.warning(f"[init_db] Database URL prefix: {DATABASE_URL[:40]}...")
    logger.warning(f"[init_db] Environment variables found: {env_keys}")
    if not _is_postgres:
        logger.error("[init_db] ⚠️ USING SQLITE — ALL DATA WILL BE LOST ON NEXT DEPLOY. Set DATABASE_URL to PostgreSQL!")

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
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS preferred_contact VARCHAR DEFAULT 'phone'",
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
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS photos_json TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_standard DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_heavy DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS actual_price DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS actual_cy DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS accuracy_notes TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS preferred_contact VARCHAR DEFAULT 'phone'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_notes TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR(50) DEFAULT 'America/Chicago'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_call_limit INTEGER DEFAULT 150",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_calls_used INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS billing_cycle_start TIMESTAMP DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS overage_mode VARCHAR(20) DEFAULT 'warn_and_charge'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS overage_cap_cents INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS overage_charges_cents INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'owner'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS industry VARCHAR DEFAULT 'junk_removal'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_balance INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_purchased_total INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_used_total INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_trial_used INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_trial_email VARCHAR",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS api_cost_cents INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS model_used VARCHAR(50) DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_requested BOOLEAN DEFAULT FALSE",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_contact_method VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_preferred_day VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_preferred_time VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_requested_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS additional_items_text TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_tag_id VARCHAR DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS fb_pixel_id VARCHAR DEFAULT ''",
        ]
    else:
        alter_statements = [
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN team_member_id INTEGER",
            "ALTER TABLE estimates ADD COLUMN customer_name TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_email TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_phone TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN preferred_contact TEXT DEFAULT 'phone'",
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
            "ALTER TABLE estimates ADD COLUMN photos_json TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN price_per_cy_standard REAL DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN price_per_cy_heavy REAL DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN actual_price REAL DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN actual_cy REAL DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN accuracy_notes TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN preferred_contact TEXT DEFAULT 'phone'",
            "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN admin_notes TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'America/Chicago'",
            "ALTER TABLE users ADD COLUMN monthly_call_limit INTEGER DEFAULT 150",
            "ALTER TABLE users ADD COLUMN monthly_calls_used INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN billing_cycle_start TIMESTAMP DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN overage_mode TEXT DEFAULT 'warn_and_charge'",
            "ALTER TABLE users ADD COLUMN overage_cap_cents INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN overage_charges_cents INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'owner'",
            "ALTER TABLE users ADD COLUMN industry TEXT DEFAULT 'junk_removal'",
            "ALTER TABLE users ADD COLUMN credit_balance INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN credits_purchased_total INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN credits_used_total INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN free_trial_used INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN free_trial_email TEXT",
            "ALTER TABLE users ADD COLUMN google_tag_id TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN fb_pixel_id TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN output_tokens INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN api_cost_cents INTEGER DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN model_used TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_requested BOOLEAN DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN appointment_contact_method TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_preferred_day TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_preferred_time TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_requested_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN additional_items_text TEXT DEFAULT ''",
        ]

    async with engine.begin() as conn:
        for stmt in alter_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass

    # Create credit_transactions table
    async with engine.begin() as conn:
        try:
            if _is_postgres:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS credit_transactions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        transaction_type VARCHAR NOT NULL,
                        credits INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        description VARCHAR,
                        stripe_session_id VARCHAR,
                        pack_type VARCHAR,
                        amount_cents INTEGER,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id ON credit_transactions(user_id)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_credit_transactions_created_at ON credit_transactions(created_at)"))
            else:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS credit_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        transaction_type TEXT NOT NULL,
                        credits INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        description TEXT,
                        stripe_session_id TEXT,
                        pack_type TEXT,
                        amount_cents INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
        except Exception:
            pass

    # Backfill NULL values to defaults for existing users
    backfill_statements = [
        "UPDATE users SET price_per_cy_low = 35.0 WHERE price_per_cy_low IS NULL",
        "UPDATE users SET price_per_cy_high = 40.0 WHERE price_per_cy_high IS NULL",
        "UPDATE users SET price_per_cy_premium = 55.0 WHERE price_per_cy_premium IS NULL",
        "UPDATE users SET min_charge = 75.0 WHERE min_charge IS NULL",
        "UPDATE users SET truck_capacity_cy = 16.0 WHERE truck_capacity_cy IS NULL",
        "UPDATE users SET company_phone = '' WHERE company_phone IS NULL",
        "UPDATE users SET company_slug = '' WHERE company_slug IS NULL",
        "UPDATE users SET company_logo_url = '' WHERE company_logo_url IS NULL",
    ]
    async with engine.begin() as conn:
        for stmt in backfill_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass

    # Grant 999 credits to admin accounts for testing
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "UPDATE users SET credit_balance = 999 WHERE is_admin = true AND (credit_balance IS NULL OR credit_balance < 999)"
            ))
        except Exception:
            pass

    # Set CTC (Clear The Clutter) custom rates if not already set
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "UPDATE users SET price_per_cy_standard = 35.0, price_per_cy_heavy = 50.0 "
                "WHERE company_slug = 'clear-the-clutter' AND price_per_cy_standard IS NULL"
            ))
        except Exception:
            pass
    logger.info("Database migrations and backfills complete")


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
    ("refrigerator large", "appliance", 2.00, True, 25.0, "36×30×70 in"),
    ("refrigerator small", "appliance", 1.25, True, 25.0, "28×28×60 in"),
    ("washing machine", "appliance", 1.00, True, 25.0, "27×27×38 in"),
    ("dryer", "appliance", 1.00, False, 0, "27×29×38 in"),
    ("dishwasher", "appliance", 0.75, False, 0, "24×24×35 in"),
    ("stove", "appliance", 1.00, False, 0, "30×26×36 in"),
    ("microwave large", "appliance", 0.25, False, 0, "24×18×14 in"),
    ("microwave small", "appliance", 0.15, False, 0, "18×14×11 in"),
    ("air conditioner window unit", "appliance", 0.35, True, 25.0, "24×20×16 in"),
    ("dehumidifier", "appliance", 0.25, False, 0, "16×12×24 in"),
    ("water heater", "appliance", 0.75, True, 25.0, "22×22×54 in"),
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
            PlanConfig(tier_name="solo", display_name="Solo", price_cents=14900, estimate_limit=999,
                       features_json='["1 user","AI photo estimates","Item detection & volume calc","Premium job detection","Customer estimate link","Estimate history","Email support"]',
                       stripe_price_id="price_1TDJ2wAPEzwLONiqTut1n11W", is_active=True),
            PlanConfig(tier_name="team", display_name="Team", price_cents=29900, estimate_limit=999,
                       features_json='["Up to 3 users","Everything in Solo","Truck load calculator","Custom rate settings","Priority support"]',
                       stripe_price_id="price_1TDJ2xAPEzwLONiq56jpA1fH", is_active=True),
            PlanConfig(tier_name="enterprise", display_name="Enterprise", price_cents=49900, estimate_limit=999,
                       features_json='["Unlimited users","Everything in Team","API access","Dedicated onboarding","Phone support"]',
                       stripe_price_id="price_1TDJ5OAPEzwLONiqVhcBQjPn", is_active=True),
            PlanConfig(tier_name="custom", display_name="Custom", price_cents=99900, estimate_limit=999,
                       features_json='["Fully customized solution","Custom integrations","White-label options","Dedicated support"]',
                       stripe_price_id="", is_active=True),
            # Legacy tiers (inactive)
            PlanConfig(tier_name="starter", display_name="Starter (Legacy)", price_cents=2900, estimate_limit=20,
                       features_json='["Legacy plan"]',
                       stripe_price_id="price_1T7PXXAPEzwLONiqIIrAtsQZ", is_active=False),
            PlanConfig(tier_name="pro", display_name="Pro (Legacy)", price_cents=5900, estimate_limit=40,
                       features_json='["Legacy plan"]',
                       stripe_price_id="price_1T6iUPAPEzwLONiqp31lIw9T", is_active=False),
            PlanConfig(tier_name="agency", display_name="Agency (Legacy)", price_cents=9900, estimate_limit=999,
                       features_json='["Legacy plan"]',
                       stripe_price_id="price_1T7PXXAPEzwLONiqpQbgpgZ8", is_active=False),
        ]
        for p in plans:
            db.add(p)
        await db.commit()


def _validate_pack_key(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not _PACK_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="pack_key must be 1–63 chars: lowercase letters, digits, underscores only; must start with letter or digit.",
        )
    return key


def _credit_pack_to_public(p: CreditPack) -> dict:
    c = p.credits or 1
    per = int(round(p.price_cents / c)) if c else 0
    return {
        "type": p.pack_key,
        "name": p.name,
        "credits": p.credits,
        "price_cents": p.price_cents,
        "per_credit_cents": per,
        "discount_pct": p.discount_pct or 0,
        "description": p.description or "",
        "featured": bool(p.is_featured),
    }


def _credit_pack_admin_dict(p: CreditPack) -> dict:
    return {
        "id": p.id,
        "pack_key": p.pack_key,
        "name": p.name,
        "credits": p.credits,
        "price_cents": p.price_cents,
        "discount_pct": p.discount_pct or 0,
        "description": p.description or "",
        "stripe_product_id": p.stripe_product_id or "",
        "stripe_price_id": p.stripe_price_id or "",
        "is_active": bool(p.is_active),
        "is_featured": bool(p.is_featured),
        "sort_order": p.sort_order or 0,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _stripe_create_product_and_price_sync(
    name: str, description: str, price_cents: int, pack_key: str, credits: int
) -> tuple[str, str]:
    product = stripe.Product.create(
        name=name[:200],
        description=((description or "")[:500] or None),
        metadata={"pack_key": pack_key, "credits": str(credits)},
    )
    price = stripe.Price.create(
        product=product.id,
        unit_amount=int(price_cents),
        currency="usd",
        metadata={"pack_key": pack_key, "credits": str(credits)},
    )
    return product.id, price.id


def _stripe_create_price_on_product_sync(
    product_id: str, price_cents: int, pack_key: str, credits: int
) -> str:
    price = stripe.Price.create(
        product=product_id,
        unit_amount=int(price_cents),
        currency="usd",
        metadata={"pack_key": pack_key, "credits": str(credits)},
    )
    return price.id


def _stripe_update_product_sync(product_id: str, name: str, description: str) -> None:
    stripe.Product.modify(
        product_id,
        name=name[:200],
        description=((description or "")[:500] or None),
    )


def _stripe_deactivate_price_sync(price_id: str) -> None:
    if not price_id:
        return
    try:
        stripe.Price.modify(price_id, active=False)
    except Exception:
        pass


def _stripe_product_id_from_price_sync(price_id: str) -> str:
    pri = stripe.Price.retrieve(price_id)
    pid = getattr(pri, "product", None)
    if isinstance(pid, str):
        return pid
    if pid is not None and getattr(pid, "id", None):
        return str(pid.id)
    return ""


async def seed_credit_packs():
    import logging
    logger = logging.getLogger("wsic")
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count(CreditPack.id)))).scalar() or 0
        if n > 0:
            logger.info("[seed_credit_packs] credit_packs already populated (%s rows)", n)
            return
        order = 0
        for pack_key, v in _DEFAULT_CREDIT_PACKS_SEED.items():
            db.add(
                CreditPack(
                    pack_key=pack_key,
                    name=v["name"],
                    credits=int(v["credits"]),
                    price_cents=int(v["price_cents"]),
                    discount_pct=int(v.get("discount_pct", 0)),
                    description=v.get("description", "") or "",
                    stripe_product_id="",
                    stripe_price_id=v.get("stripe_price_id", "") or "",
                    is_active=True,
                    is_featured=bool(v.get("is_featured", False)),
                    sort_order=order,
                )
            )
            order += 10
        await db.commit()
        logger.info("[seed_credit_packs] Inserted %s default credit packs", len(_DEFAULT_CREDIT_PACKS_SEED))


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
        if user:
            changed = False
            if not user.is_admin:
                user.is_admin = True
                changed = True
            # Admin gets unlimited estimates for testing
            if user.estimates_limit < 999:
                user.estimates_limit = 999
                user.estimates_used = 0
                changed = True
            if not getattr(user, 'timezone', None):
                user.timezone = "America/Chicago"
                changed = True
            if changed:
                await db.commit()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    await seed_reference_library()
    await seed_plan_configs()
    await seed_credit_packs()
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
    import logging
    logger = logging.getLogger("wsic.email")
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@whatshouldicharge.app")
    if not api_key:
        logger.error("[send_email] SENDGRID_API_KEY not set — email not sent")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"[send_email] Sent to {to_email} from {from_email}, status={response.status_code}")
        if response.status_code >= 400:
            logger.error(f"[send_email] SendGrid error status {response.status_code}")
            return False
        return True
    except Exception as e:
        logger.error(f"[send_email] FAILED to send to {to_email}: {type(e).__name__}: {e}")
        return False


@app.get("/api/health")
async def health_check():
    """Minimal health check for load balancers — no DB URLs, counts, or env metadata."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy"})


@app.get("/api/industries")
async def list_available_industries():
    from services.industry_config import list_industries
    return {"industries": list_industries()}


@app.get("/api/credits")
async def get_credit_balance(request: Request):
    """Get current credit balance and transaction history."""
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user.id)
            .order_by(CreditTransaction.created_at.desc())
            .limit(50)
        )
        transactions = result.scalars().all()

    return {
        "credit_balance": user.credit_balance or 0,
        "credits_purchased_total": user.credits_purchased_total or 0,
        "credits_used_total": user.credits_used_total or 0,
        "free_trial_remaining": max(0, 2 - (user.free_trial_used or 0)),
        "transactions": [
            {
                "id": t.id,
                "type": t.transaction_type,
                "credits": t.credits,
                "balance_after": t.balance_after,
                "description": t.description,
                "pack_type": t.pack_type,
                "amount_cents": t.amount_cents,
                "created_at": t.created_at.isoformat() if t.created_at else None
            }
            for t in transactions
        ]
    }


@app.get("/api/credits/packs")
async def get_available_packs():
    """List available credit packs for purchase."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CreditPack)
            .where(CreditPack.is_active == True)  # noqa: E712
            .order_by(CreditPack.sort_order, CreditPack.id)
        )
        rows = result.scalars().all()
    return {"packs": [_credit_pack_to_public(p) for p in rows]}


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/landing.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return FileResponse("static/terms.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return FileResponse("static/privacy.html")


@app.get("/blog", response_class=HTMLResponse)
async def blog_index_page():
    return FileResponse("static/blog/index.html")


@app.get("/blog/how-to-price-junk-removal-jobs", response_class=HTMLResponse)
async def blog_how_to_price_junk_removal_jobs():
    return FileResponse("static/blog/how-to-price-junk-removal-jobs.html")


@app.get("/blog/junk-removal-startup-costs", response_class=HTMLResponse)
async def blog_junk_removal_startup_costs():
    return FileResponse("static/blog/junk-removal-startup-costs.html")


@app.get("/blog/junk-removal-marketing", response_class=HTMLResponse)
async def blog_junk_removal_marketing():
    return FileResponse("static/blog/junk-removal-marketing.html")


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
            f"Junk removal pricing{loc_phrase} typically ranges from $75 for a minimum load to $500 or more for a full truckload. The exact cost depends on the volume of items, the type of materials being removed, and whether any items require special handling such as appliances with refrigerants or electronics that need proper recycling. {name} uses an AI-powered photo estimate system that calculates your specific price based on the actual items in your photos — so you get a personalized quote, not a generic range. Most single-item pickups like a couch or mattress fall between $75 and $150, while full garage cleanouts or estate cleanouts can range from $1,000 to $3,000+ depending on volume."
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
        faq_html += f'<details><summary>{html_mod.escape(q)}</summary><div class="faq-answer">{html_mod.escape(a)}</div></details>\n'

    # Build tracking scripts from user's configured IDs
    tracking_head = ""
    if u.google_tag_id:
        gtid = html_mod.escape(u.google_tag_id.strip())
        tracking_head += f'<!-- Google tag (gtag.js) --><script async src="https://www.googletagmanager.com/gtag/js?id={gtid}"></script><script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}gtag("js",new Date());gtag("config","{gtid}");</script>'
    if u.fb_pixel_id:
        fbid = html_mod.escape(u.fb_pixel_id.strip())
        tracking_head += f'<!-- Facebook Pixel --><script>!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version="2.0";n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}(window,document,"script","https://connect.facebook.net/en_US/fbevents.js");fbq("init","{fbid}");fbq("track","PageView");</script><noscript><img height="1" width="1" style="display:none" src="https://www.facebook.com/tr?id={fbid}&amp;ev=PageView&amp;noscript=1"/></noscript>'

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
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script type="application/ld+json">{jsonld_local}</script>
<script type="application/ld+json">{{
  "@context":"https://schema.org",
  "@type":"FAQPage",
  "mainEntity":{faq_schema}
}}</script>
<script type="application/ld+json">{jsonld_speakable}</script>
{tracking_head}
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'DM Sans',system-ui,-apple-system,sans-serif;background:#ffffff;color:#1e293b;min-height:100vh;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}

/* --- Layout --- */
.page-wrap{{max-width:680px;margin:0 auto;padding:20px 16px}}

/* --- Header --- */
.site-header{{text-align:center;padding:32px 0 12px}}
.logo{{max-height:64px;margin-bottom:14px;border-radius:10px}}
.site-header h1{{font-size:1.5rem;font-weight:800;color:#0f172a;line-height:1.2;letter-spacing:-0.02em}}
.location-badge{{display:inline-flex;align-items:center;gap:4px;margin-top:8px;padding:5px 14px;background:#ecfdf5;color:#059669;border-radius:24px;font-size:0.78rem;font-weight:600}}
.location-badge svg{{width:14px;height:14px;flex-shrink:0}}
.header-phone{{margin-top:10px}}
.header-phone a{{display:inline-flex;align-items:center;gap:6px;color:#16a34a;text-decoration:none;font-weight:600;font-size:0.9rem;padding:6px 16px;border-radius:24px;transition:background .2s}}
.header-phone a:hover{{background:#f0fdf4}}
.header-phone svg{{width:16px;height:16px}}

/* --- Hero --- */
.hero{{text-align:center;padding:28px 0 20px}}
.hero h2{{font-size:2rem;font-weight:800;color:#0f172a;line-height:1.15;letter-spacing:-0.03em;margin-bottom:12px}}
.hero h2 span{{color:#16a34a}}
.hero p{{font-size:1rem;color:#64748b;max-width:460px;margin:0 auto;line-height:1.65}}
.trust-pills{{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:18px}}
.trust-pill{{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:24px;font-size:0.78rem;font-weight:600;color:#475569}}
.trust-pill svg{{width:14px;height:14px;color:#16a34a}}

/* --- Steps --- */
.steps-section{{padding:8px 0 24px}}
.steps-section h3{{text-align:center;font-size:0.82rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:16px}}
.steps{{display:flex;gap:12px}}
.step{{flex:1;text-align:center;padding:20px 12px 18px;background:#fff;border:1px solid #e2e8f0;border-radius:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04),0 4px 12px rgba(0,0,0,0.02);transition:transform .2s,box-shadow .2s;cursor:default}}
.step:hover{{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,0.08)}}
.step-icon{{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;background:#ecfdf5;border-radius:12px;margin-bottom:10px;color:#16a34a}}
.step-icon svg{{width:22px;height:22px}}
.step-title{{font-size:0.85rem;font-weight:700;color:#0f172a;margin-bottom:2px}}
.step-sub{{font-size:0.75rem;color:#94a3b8;line-height:1.4}}

/* --- Cards --- */
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04),0 4px 12px rgba(0,0,0,0.02)}}
.card-title{{font-size:0.92rem;font-weight:700;color:#0f172a;margin-bottom:16px}}
label{{display:block;font-size:0.8rem;color:#64748b;margin-bottom:5px;font-weight:500}}
input[type="text"],input[type="email"],input[type="tel"]{{width:100%;padding:12px 16px;background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;color:#1e293b;font-size:0.95rem;margin-bottom:14px;font-family:inherit;transition:border-color .2s,box-shadow .2s}}
input:focus{{outline:none;border-color:#16a34a;box-shadow:0 0 0 4px rgba(22,163,74,0.08)}}
input::placeholder{{color:#94a3b8}}

/* --- Upload Zone --- */
.drop-zone{{border:2px dashed #cbd5e1;border-radius:16px;padding:40px 20px;text-align:center;cursor:pointer;transition:all .25s;background:#fafbfc;position:relative;overflow:hidden}}
.drop-zone::before{{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 50%,rgba(22,163,74,0.04) 0%,transparent 70%);opacity:0;transition:opacity .3s}}
.drop-zone:hover,.drop-zone.drag-over{{border-color:#16a34a;border-style:solid;background:#f0fdf4}}
.drop-zone:hover::before,.drop-zone.drag-over::before{{opacity:1}}
.drop-zone-icon{{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;background:#ecfdf5;border-radius:16px;margin-bottom:12px;color:#16a34a}}
.drop-zone-icon svg{{width:28px;height:28px}}
.drop-label{{font-size:1rem;font-weight:700;color:#0f172a}}
.drop-sub{{font-size:0.8rem;color:#94a3b8;margin-top:5px}}
.previews{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}
.preview-wrap{{position:relative;width:80px;height:80px}}
.preview-thumb{{width:80px;height:80px;border-radius:12px;object-fit:cover;border:2px solid #e2e8f0;transition:border-color .2s}}
.preview-wrap:hover .preview-thumb{{border-color:#16a34a}}
.preview-remove{{position:absolute;top:-6px;right:-6px;width:22px;height:22px;background:#ef4444;color:#fff;border:2px solid #fff;border-radius:50%;font-size:0.7rem;display:flex;align-items:center;justify-content:center;cursor:pointer;line-height:1;font-weight:700;box-shadow:0 2px 4px rgba(0,0,0,0.15);opacity:0;transition:opacity .15s}}
.preview-wrap:hover .preview-remove{{opacity:1}}
.photo-count{{font-size:0.78rem;color:#64748b;margin-top:8px;text-align:center}}

/* --- Buttons --- */
.btn{{display:block;width:100%;padding:16px;background:#16a34a;color:#fff;border:none;border-radius:14px;font-size:1.05rem;font-weight:700;cursor:pointer;font-family:inherit;transition:background .2s,transform .1s;text-align:center;text-decoration:none;box-shadow:0 2px 8px rgba(22,163,74,0.25)}}
.btn:hover{{background:#15803d;transform:translateY(-1px)}}
.btn:active{{transform:translateY(0)}}
.btn:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none}}
.btn-outline{{background:transparent;border:2px solid #16a34a;color:#16a34a;margin-top:12px;box-shadow:none}}
.btn-outline:hover{{background:#f0fdf4;transform:translateY(-1px)}}
.btn-call{{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:16px 32px;width:auto;font-size:1.1rem}}
.btn-call svg{{width:20px;height:20px}}

/* --- Loading --- */
.loading{{text-align:center;padding:60px 20px;display:none}}
.loading-dots{{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:20px}}
.loading-dots span{{width:12px;height:12px;background:#16a34a;border-radius:50%;animation:dotPulse 1.2s ease-in-out infinite}}
.loading-dots span:nth-child(2){{animation-delay:0.15s}}
.loading-dots span:nth-child(3){{animation-delay:0.3s}}
@keyframes dotPulse{{0%,80%,100%{{opacity:0.3;transform:scale(0.8)}}40%{{opacity:1;transform:scale(1.1)}}}}
.loading-text{{font-size:0.95rem;color:#475569;font-weight:500}}
.loading-sub{{font-size:0.8rem;color:#94a3b8;margin-top:6px}}
.loading-steps{{display:flex;justify-content:center;gap:20px;margin-top:20px}}
.loading-step{{display:flex;align-items:center;gap:6px;font-size:0.78rem;color:#94a3b8;font-weight:500}}
.loading-step.active{{color:#16a34a}}
.loading-step svg{{width:16px;height:16px}}

/* --- Results --- */
.results{{display:none}}
.results.show{{display:block;animation:fadeUp .4s ease-out}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(12px)}}to{{opacity:1;transform:translateY(0)}}}}
.price-card{{text-align:center;padding:32px 20px;background:linear-gradient(135deg,#f0fdf4 0%,#ecfdf5 100%);border:1px solid #bbf7d0}}
.price-label{{font-size:0.82rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px}}
.price-range{{font-size:2.6rem;font-weight:800;color:#16a34a;letter-spacing:-0.03em;line-height:1.1}}
.price-note{{font-size:0.8rem;color:#94a3b8;margin-top:8px}}
.min-charge-note{{display:none;font-size:0.82rem;color:#d97706;font-weight:600;margin-top:8px;padding:6px 14px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a}}
.badge{{display:inline-block;padding:4px 14px;border-radius:24px;font-size:0.75rem;font-weight:700;margin-bottom:10px;letter-spacing:0.02em}}
.badge-standard{{background:#dcfce7;color:#16a34a}}
.badge-premium{{background:#fef3c7;color:#d97706}}
.badge-hoarder{{background:#fee2e2;color:#ef4444}}
.badge-truck_load{{background:#e0f2fe;color:#0369a1}}
.cy-display{{font-size:0.85rem;color:#64748b;margin-top:8px}}
.item-row{{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid #f1f5f9;font-size:0.88rem}}
.item-row:last-child{{border-bottom:none}}
.item-name{{flex:1;font-weight:500;color:#1e293b}}
.item-cy{{color:#94a3b8;font-size:0.78rem;font-weight:500}}
.item-qty{{color:#64748b;font-size:0.8rem;min-width:32px;text-align:right;font-weight:600}}
.special-note{{margin-top:16px;padding:16px;border-radius:14px;background:#fffbeb;border:1px solid #fde68a;font-size:0.82rem;color:#92400e;line-height:1.6}}
.dupe-note{{margin-top:12px;padding:16px;border-radius:14px;background:#fefce8;border:1px solid #fde68a;font-size:0.82rem;color:#854d0e;line-height:1.6}}
.followup-notice{{display:none;margin-top:16px;padding:18px 20px;border-radius:14px;background:linear-gradient(135deg,#eff6ff 0%,#f0f9ff 100%);border:1px solid #bfdbfe;font-size:0.85rem;color:#1e40af;line-height:1.6;text-align:center}}
.followup-notice svg{{width:20px;height:20px;vertical-align:middle;margin-right:6px;stroke:#2563eb}}
.followup-notice strong{{color:#1e3a8a}}

/* --- CTA / Appointment Form --- */
.cta-section{{text-align:center;padding:28px 20px;margin-top:8px}}
.cta-section .subtext{{font-size:0.9rem;color:#475569;margin-bottom:14px;font-weight:500}}
.appt-form{{text-align:left;max-width:400px;margin:0 auto}}
.appt-form .form-label{{font-size:0.82rem;font-weight:600;color:#334155;margin-bottom:8px;display:block}}
.appt-form .form-group{{margin-bottom:18px}}
.toggle-row{{display:flex;gap:8px}}
.toggle-btn{{flex:1;padding:12px 8px;border:2px solid #e2e8f0;border-radius:12px;background:#fff;color:#475569;font-size:0.88rem;font-weight:600;cursor:pointer;text-align:center;transition:all .2s;font-family:inherit}}
.toggle-btn:hover{{border-color:#16a34a;color:#16a34a}}
.toggle-btn.selected{{background:#f0fdf4;border-color:#16a34a;color:#16a34a}}
.toggle-btn svg{{width:16px;height:16px;vertical-align:-3px;margin-right:4px}}
.appt-form input[type="date"]{{width:100%;padding:12px 16px;border:2px solid #e2e8f0;border-radius:12px;font-size:0.92rem;font-family:inherit;color:#1e293b;background:#fff;transition:border-color .2s}}
.appt-form input[type="date"]:focus{{outline:none;border-color:#16a34a}}
.appt-success{{display:none;text-align:center;padding:24px 16px}}
.appt-success svg{{width:48px;height:48px;color:#16a34a;margin-bottom:12px}}
.appt-success .success-title{{font-size:1.1rem;font-weight:700;color:#0f172a;margin-bottom:6px}}
.appt-success .success-sub{{font-size:0.88rem;color:#64748b}}
.appt-or{{font-size:0.82rem;color:#94a3b8;margin:16px 0 12px;position:relative}}
.appt-or::before,.appt-or::after{{content:'';position:absolute;top:50%;width:calc(50% - 20px);height:1px;background:#e2e8f0}}
.appt-or::before{{left:0}}
.appt-or::after{{right:0}}

/* --- Divider --- */
.section-divider{{border:none;border-top:1px solid #f1f5f9;margin:28px 0}}

/* --- Quick Facts --- */
.quick-facts{{margin-top:28px;padding:24px;background:#ecfdf5;border:1px solid #bbf7d0;border-radius:16px}}
.quick-facts h2{{font-size:1.05rem;font-weight:700;color:#0f172a;margin-bottom:16px;display:flex;align-items:center;gap:8px}}
.quick-facts h2 svg{{width:20px;height:20px;color:#16a34a}}
.quick-facts dl{{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:0.88rem}}
.quick-facts dt{{color:#64748b;font-weight:500}}
.quick-facts dd{{color:#1e293b;font-weight:600;margin:0}}

/* --- Content Sections --- */
.content-section{{margin-top:24px;padding:24px;background:#fff;border:1px solid #e2e8f0;border-radius:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}}
.content-section h2{{font-size:1.1rem;font-weight:700;color:#0f172a;margin-bottom:12px;letter-spacing:-0.01em}}
.content-section p{{font-size:0.88rem;color:#475569;line-height:1.75;margin-bottom:14px}}
.content-section p:last-child{{margin-bottom:0}}

/* --- FAQ --- */
.faq-section{{margin-top:28px}}
.faq-section h2{{font-size:1.15rem;font-weight:700;color:#0f172a;margin-bottom:16px;letter-spacing:-0.01em}}
details{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;margin-bottom:10px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,0.03);transition:box-shadow .2s}}
details:hover{{box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
details[open]{{border-color:#bbf7d0;box-shadow:0 2px 8px rgba(22,163,74,0.08)}}
details[open]>summary{{border-bottom:1px solid #f0fdf4}}
summary{{padding:16px 20px;font-size:0.9rem;font-weight:600;cursor:pointer;color:#0f172a;list-style:none;display:flex;align-items:center;justify-content:space-between;gap:12px;line-height:1.4}}
summary::-webkit-details-marker{{display:none}}
summary::after{{content:'';width:20px;height:20px;flex-shrink:0;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");background-size:contain;background-repeat:no-repeat;transition:transform .25s ease}}
details[open] summary::after{{transform:rotate(180deg)}}
details div.faq-answer{{padding:4px 20px 18px;font-size:0.86rem;color:#475569;line-height:1.7}}

/* --- Footer --- */
.footer{{text-align:center;padding:32px 0 20px}}
.footer-company{{font-size:0.85rem;font-weight:600;color:#475569}}
.footer-location{{font-size:0.78rem;color:#94a3b8;margin-top:2px}}
.footer-powered{{font-size:0.72rem;color:#cbd5e1;margin-top:12px}}
.footer-powered a{{color:#cbd5e1;text-decoration:none}}
.footer-powered a:hover{{color:#94a3b8}}

/* --- Error --- */
.error{{color:#ef4444;font-size:0.88rem;text-align:center;padding:14px;display:none;background:#fef2f2;border-radius:12px;border:1px solid #fecaca;margin-bottom:12px}}

/* --- Responsive --- */
@media(max-width:480px){{
  .hero h2{{font-size:1.6rem}}
  .steps{{flex-direction:column;gap:10px}}
  .price-range{{font-size:2.2rem}}
  .trust-pills{{gap:6px}}
}}
</style>
</head>
<body>

<div class="page-wrap">
  <header class="site-header">
    {logo_html}
    <h1>{name}</h1>
    {f"""<span class="location-badge"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 1118 0z"/><circle cx="12" cy="10" r="3"/></svg>{location}</span>""" if location else ''}
    {f"""<div class="header-phone"><a href="tel:{phone}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>{phone}</a></div>""" if phone else ''}
  </header>

  <section class="hero">
    <h2>Get Your <span>Free Estimate</span> in 60 Seconds</h2>
    <p>Upload photos of the items you need removed and get an instant, AI-powered price quote. No obligation, no waiting.</p>
    <div class="trust-pills">
      <span class="trust-pill"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/></svg>Free Estimate</span>
      <span class="trust-pill"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>60-Second Results</span>
      <span class="trust-pill"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>No Obligation</span>
    </div>
  </section>

  <section class="steps-section">
    <h3>How It Works</h3>
    <div class="steps">
      <div class="step">
        <div class="step-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
        <div class="step-title">Upload Photos</div>
        <div class="step-sub">Snap a few photos of items to remove</div>
      </div>
      <div class="step">
        <div class="step-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg></div>
        <div class="step-title">AI Analysis</div>
        <div class="step-sub">Every item identified and measured</div>
      </div>
      <div class="step">
        <div class="step-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg></div>
        <div class="step-title">Get Your Price</div>
        <div class="step-sub">Accurate quote in under 60 seconds</div>
      </div>
    </div>
  </section>

  <!-- Step 1: Contact Info + Email Verification -->
  <div id="verify-section">
    <div class="card">
      <div class="card-title">Your Contact Info</div>
      <label for="cust-name">Name</label>
      <input type="text" id="cust-name" placeholder="Your name" autocomplete="name">
      <label for="cust-email">Email</label>
      <div style="display:flex;gap:8px;margin-bottom:14px">
        <input type="email" id="cust-email" placeholder="your@email.com" autocomplete="email" style="margin-bottom:0;flex:1">
        <button class="btn" id="send-code-btn" onclick="sendVerifyCode()" style="width:auto;padding:12px 20px;font-size:0.85rem;white-space:nowrap;box-shadow:none">Verify</button>
      </div>
      <div id="code-section" style="display:none">
        <label for="verify-code">Enter verification code</label>
        <input type="text" id="verify-code" placeholder="------" maxlength="6" autocomplete="one-time-code" style="text-align:center;letter-spacing:6px;font-size:1.2rem;font-weight:700">
        <div style="font-size:0.75rem;color:#94a3b8;text-align:center;margin-top:-10px;margin-bottom:14px">Check your email for a 6-digit code</div>
      </div>
      <label for="cust-phone">Phone</label>
      <input type="tel" id="cust-phone" placeholder="(555) 123-4567" autocomplete="tel">
      <div class="error" id="verify-error"></div>
      <button class="btn" id="continue-btn" onclick="verifyAndContinue()">Continue to Estimate</button>
      <div style="font-size:0.72rem;color:#94a3b8;text-align:center;margin-top:14px;line-height:1.6">By continuing, you agree to the <a href="/terms" target="_blank" style="color:#94a3b8;text-decoration:underline">Terms of Service</a> and <a href="/privacy" target="_blank" style="color:#94a3b8;text-decoration:underline">Privacy Policy</a>.<br>Estimates are AI-generated approximations and not binding quotes.</div>
    </div>
    <div style="text-align:center;padding:12px;font-size:0.72rem;color:#cbd5e1;line-height:1.5;margin-top:4px">This tool is currently in <strong>beta</strong>. Estimates may contain errors. {name} is not liable for differences between estimated and actual pricing. This estimate covers items shown in your photos only — additional items will be priced at standard rates. Recycling fees apply to freon-containing appliances, tires, TVs, and some electronics. Final pricing is confirmed on-site.</div>
  </div>

  <!-- Step 2: Upload Section (hidden until verified) -->
  <div id="upload-section" style="display:none">
    <div class="card">
      <div class="card-title">Upload Photos of Items for Removal</div>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:0.82rem;color:#166534;line-height:1.5;">
        <strong>Tip:</strong> Take photos of the area — our AI will identify all items. After the estimate, you can uncheck anything you're keeping and add items that weren't in the photos.
      </div>
      <div class="drop-zone" id="drop-zone">
        <div class="drop-zone-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg></div>
        <div class="drop-label">Tap to upload or drag photos here</div>
        <div class="drop-sub">Up to 10 photos — JPG, PNG, WEBP</div>
      </div>
      <input type="file" id="file-input" accept="image/*" multiple style="display:none">
      <div class="previews" id="previews"></div>
      <div class="photo-count" id="photo-count" style="display:none"></div>
    </div>

    <div class="error" id="error-msg"></div>
    <button class="btn" id="submit-btn" disabled>Get Your Estimate</button>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading">
    <div class="loading-dots"><span></span><span></span><span></span></div>
    <div class="loading-text" id="loading-text">Analyzing your photos...</div>
    <div class="loading-sub">This usually takes 30-60 seconds</div>
    <div class="loading-steps">
      <span class="loading-step active" id="ls-upload"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>Upload</span>
      <span class="loading-step" id="ls-analyze"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>Analyze</span>
      <span class="loading-step" id="ls-price"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg>Price</span>
    </div>
  </div>

  <!-- Results -->
  <div class="results" id="results">
    <div class="card price-card">
      <span class="badge" id="res-badge"></span>
      <div class="price-label">Your Estimated Price</div>
      <div class="price-range" id="res-price"></div>
      <div id="min-charge-msg" class="min-charge-note"></div>
      <div class="price-note">Based on AI photo analysis of your items</div>
      <div class="cy-display" id="res-cy"></div>
    </div>

    <div class="followup-notice" id="followup-notice">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      <strong>Whole house cleanouts &amp; hoarding situations:</strong> For accuracy on larger jobs, we will follow up within 24 hours to confirm pricing or ask a few additional questions.
    </div>

    <div class="card" id="res-photos-card" style="display:none">
      <div class="card-title">Your Photos</div>
      <div id="res-photos" style="display:flex;flex-wrap:wrap;gap:8px;"></div>
      <div style="font-size:0.75rem;color:#94a3b8;margin-top:8px;">These photos were used to generate your estimate. Only items visible in these photos are included in the price above.</div>
    </div>

    <div class="card">
      <div class="card-title">Items Detected</div>
      <div id="res-items"></div>
    </div>

    <div id="res-special" class="special-note" style="display:none"></div>
    <div id="res-dupes" class="dupe-note" style="display:none"></div>

    <div class="card" id="res-notes-card" style="display:none">
      <div class="card-title">Notes</div>
      <div id="res-notes" style="font-size:0.88rem;color:#475569;line-height:1.6"></div>
    </div>

    <div class="card">
      <div class="card-title">Additional Items Not in Photos</div>
      <div style="font-size:0.82rem;color:#64748b;margin-bottom:10px;">Have items that weren't in your photos? List them below and we'll factor them in on arrival. Additional items will be priced at standard rates.</div>
      <textarea id="additional-items" placeholder="e.g. 2 mattresses in upstairs bedroom, old washer in garage, pile of lumber behind shed..." style="width:100%;padding:12px 16px;background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;color:#1e293b;font-size:0.9rem;font-family:inherit;min-height:80px;resize:vertical;"></textarea>
    </div>

    <div class="cta-section" id="cta-section">
      <div class="subtext">Ready to schedule your pickup?</div>
      <div id="appt-form-wrap" class="appt-form">
        <div class="form-group">
          <label class="form-label">How should we contact you?</label>
          <div class="toggle-row">
            <button type="button" class="toggle-btn selected" id="appt-text" onclick="selectContact('text')">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
              Text Me
            </button>
            <button type="button" class="toggle-btn" id="appt-email" onclick="selectContact('email')">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
              Email Me
            </button>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Preferred Day</label>
          <input type="date" id="appt-day" />
        </div>
        <div class="form-group">
          <label class="form-label">Preferred Time</label>
          <div class="toggle-row">
            <button type="button" class="toggle-btn selected" id="appt-morning" onclick="selectTime('morning')">Morning (8am–12pm)</button>
            <button type="button" class="toggle-btn" id="appt-afternoon" onclick="selectTime('afternoon')">Afternoon (12–5pm)</button>
          </div>
        </div>
        <button class="btn" id="appt-submit-btn" onclick="submitAppointment()">Request Appointment</button>
        <div id="appt-error" style="display:none;color:#ef4444;font-size:0.85rem;margin-top:10px;text-align:center"></div>
        {f"""<div class="appt-or">or</div>
        <a href="tel:{phone}" class="btn btn-call" style="margin:0 auto"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>Call Us — {phone}</a>""" if phone else ''}
      </div>
      <div id="appt-success" class="appt-success">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        <div class="success-title">Appointment Request Sent!</div>
        <div class="success-sub" id="appt-confirm-msg">We'll text you to confirm your appointment.</div>
      </div>
    </div>
    <div style="font-size:0.75rem;color:#94a3b8;text-align:center;padding:12px;line-height:1.5;border-top:1px solid #f1f5f9;margin-top:8px">This estimate covers only the items visible in your uploaded photos. Any additional items not shown in the photos will be priced at our standard rates upon arrival. Recycling fees apply to items containing freon (refrigerators, freezers, AC units), tires, TVs, and certain electronics. This estimate is AI-generated and approximate only. Actual pricing may vary based on item weight, accessibility, and on-site conditions. This is not a binding quote. See <a href="/terms" target="_blank" style="color:#94a3b8;text-decoration:underline">Terms of Service</a> and <a href="/privacy" target="_blank" style="color:#94a3b8;text-decoration:underline">Privacy Policy</a>.</div>
  </div>

  <hr class="section-divider">

  <!-- Quick Facts (LLM-optimized structured data for quick answers) -->
  <section class="quick-facts" aria-label="Quick Facts">
    <h2><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>Quick Facts — {name}</h2>
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
    <div class="footer-company">{name}</div>
    {f'<div class="footer-location">{location}</div>' if location else ''}
    <div class="footer-powered">Powered by <a href="https://whatshouldicharge.app">WhatShouldICharge</a> by <a href="https://donelocal.io">DoneLocal.io</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a></div>
  </footer>
</div>

<script>
var slug="{safe_slug}";
var companyPhone="{phone}";
var photos=[];
var verificationToken=null;

function esc(s){{var d=document.createElement('div');d.textContent=s;return d.innerHTML}}

// --- Email verification ---
async function sendVerifyCode(){{
  var email=document.getElementById('cust-email').value.trim();
  var errEl=document.getElementById('verify-error');
  errEl.style.display='none';
  if(!email||!email.includes('@')){{errEl.textContent='Please enter a valid email address.';errEl.style.display='block';return}}
  var btn=document.getElementById('send-code-btn');
  btn.disabled=true;btn.textContent='Sending...';
  try{{
    var resp=await fetch('/api/public/verify/send',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:email,slug:slug}})}});
    var data=await resp.json();
    if(!resp.ok) throw new Error(data.detail||'Failed to send code');
    if(data.ok===false){{errEl.textContent=data.message||'Could not send verification code. Please try again.';errEl.style.display='block';btn.textContent='Verify';btn.disabled=false;return}}
    document.getElementById('code-section').style.display='block';
    btn.textContent='Resend';btn.disabled=false;
  }}catch(e){{errEl.textContent=e.message;errEl.style.display='block';btn.textContent='Verify';btn.disabled=false}}
}}

async function verifyAndContinue(){{
  var name=document.getElementById('cust-name').value.trim();
  var email=document.getElementById('cust-email').value.trim();
  var phone=document.getElementById('cust-phone').value.trim();
  var code=document.getElementById('verify-code').value.trim();
  var errEl=document.getElementById('verify-error');
  errEl.style.display='none';
  if(!name){{errEl.textContent='Please enter your name.';errEl.style.display='block';return}}
  if(!email){{errEl.textContent='Please enter your email.';errEl.style.display='block';return}}
  if(!phone){{errEl.textContent='Please enter your phone number.';errEl.style.display='block';return}}
  if(!code||code.length<6){{errEl.textContent='Please enter the 6-digit verification code from your email.';errEl.style.display='block';return}}
  var btn=document.getElementById('continue-btn');
  btn.disabled=true;btn.textContent='Verifying...';
  try{{
    var resp=await fetch('/api/public/verify/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:email,code:code}})}});
    var data=await resp.json();
    if(!resp.ok) throw new Error(data.detail||'Verification failed');
    verificationToken=data.token;
    document.getElementById('verify-section').style.display='none';
    document.getElementById('upload-section').style.display='block';
  }}catch(e){{errEl.textContent=e.message;errEl.style.display='block';btn.disabled=false;btn.textContent='Continue to Estimate'}}
}}

var dropZone=document.getElementById('drop-zone');
var fileInput=document.getElementById('file-input');
dropZone.addEventListener('click',function(){{fileInput.click()}});
dropZone.addEventListener('dragover',function(e){{e.preventDefault();dropZone.classList.add('drag-over')}});
dropZone.addEventListener('dragleave',function(){{dropZone.classList.remove('drag-over')}});
dropZone.addEventListener('drop',function(e){{e.preventDefault();dropZone.classList.remove('drag-over');addFiles(Array.from(e.dataTransfer.files))}});
fileInput.addEventListener('change',function(e){{addFiles(Array.from(e.target.files))}});

function updatePhotoCount(){{
  var ct=document.getElementById('photo-count');
  if(photos.length>0){{ct.textContent=photos.length+' of 10 photos selected';ct.style.display='block'}}
  else{{ct.style.display='none'}}
}}

function addFiles(files){{
  var remaining=10-photos.length;
  files.filter(function(f){{return f.type.startsWith('image/')}}).slice(0,remaining).forEach(function(f,idx){{
    photos.push(f);
    var wrap=document.createElement('div');wrap.className='preview-wrap';
    var img=document.createElement('img');img.className='preview-thumb';img.src=URL.createObjectURL(f);
    var rm=document.createElement('span');rm.className='preview-remove';rm.textContent='X';
    rm.setAttribute('role','button');rm.setAttribute('aria-label','Remove photo');rm.setAttribute('tabindex','0');
    var photoIdx=photos.length-1;
    rm.addEventListener('click',function(e){{e.stopPropagation();removePhoto(photoIdx,wrap)}});
    wrap.appendChild(img);wrap.appendChild(rm);
    document.getElementById('previews').appendChild(wrap);
  }});
  document.getElementById('submit-btn').disabled=photos.length===0;
  updatePhotoCount();
}}

function removePhoto(idx,wrap){{
  photos.splice(idx,1);wrap.remove();
  document.getElementById('submit-btn').disabled=photos.length===0;
  updatePhotoCount();
  // Re-index remaining remove buttons
  var wraps=document.getElementById('previews').querySelectorAll('.preview-wrap');
  wraps.forEach(function(w,i){{
    var btn=w.querySelector('.preview-remove');
    var newBtn=btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn,btn);
    newBtn.addEventListener('click',function(e){{e.stopPropagation();removePhoto(i,w)}});
  }});
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
  if(verificationToken) fd.append('verification_token',verificationToken);
  try{{
    var resp=await fetch('/api/public/estimate/'+encodeURIComponent(slug),{{method:'POST',body:fd}});
    if(!resp.ok){{var err=await resp.json();throw new Error(err.detail||'Failed to submit')}}
    var data=await resp.json();
    document.getElementById('upload-section').style.display='none';
    document.getElementById('loading').style.display='block';
    document.getElementById('ls-upload').classList.add('active');
    pollStatus(data.job_id);
  }}catch(e){{
    document.getElementById('error-msg').textContent=e.message;
    document.getElementById('error-msg').style.display='block';
    btn.disabled=false;btn.textContent='Get Your Estimate';
  }}
}});

async function pollStatus(jobId){{
  var lt=document.getElementById('loading-text');var attempts=0;
  var iv=setInterval(async function(){{
    attempts++;
    // Animate loading steps
    if(attempts===3){{document.getElementById('ls-analyze').classList.add('active');lt.textContent='Identifying items...'}}
    if(attempts===8){{document.getElementById('ls-price').classList.add('active');lt.textContent='Calculating price...'}}
    try{{
      var resp=await fetch('/api/public/estimate/status/'+jobId);
      var data=await resp.json();
      if(data.status==='complete'&&data.result){{clearInterval(iv);document.getElementById('loading').style.display='none';showResults(data.result)}}
      else if(data.status==='error'){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent=data.message||'An error occurred. Please try again.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Schedule an Appointment'}}
    }}catch(e){{}}
    if(attempts>90){{clearInterval(iv);lt.textContent='Taking longer than expected...'}}
  }},2000);
}}

var lastResult=null;
function recalcPrice(){{
  if(!lastResult) return;
  var items=lastResult.items||[];
  var totalCY=0;
  items.forEach(function(item,idx){{
    var cb=document.getElementById('item-cb-'+idx);
    if(cb&&cb.checked){{totalCY+=((item.cubic_yards||0)*(item.quantity||1))}}
  }});
  totalCY=Math.round(totalCY*10)/10;
  var rateLow=lastResult.rate_low||35;
  var rateHigh=lastResult.rate_high||40;
  var minCharge=lastResult.min_charge||75;
  var newLow=Math.max(minCharge,Math.round(totalCY*rateLow));
  var newHigh=Math.max(minCharge,Math.round(totalCY*rateHigh));
  document.getElementById('res-price').textContent='$'+newLow.toLocaleString()+' — $'+newHigh.toLocaleString();
  document.getElementById('res-cy').textContent=totalCY+' cubic yards estimated';
}}

function showResults(r){{
  lastResult=r;
  var el=document.getElementById('results');
  el.style.display='block';
  el.classList.add('show');
  var pL=r.price_low||0,pH=r.price_high||0;
  document.getElementById('res-price').textContent='$'+pL.toLocaleString()+' — $'+pH.toLocaleString();
  if(r.min_charge_applied){{var m=document.getElementById('min-charge-msg');m.textContent='Minimum charge applied';m.style.display='block'}}
  document.getElementById('res-cy').textContent=(r.cy_estimate||0)+' cubic yards estimated';
  var bl={{standard:'Standard Load',premium:'Premium Items',hoarder:'Heavy Cleanout',truck_load:'Full Truck'}};
  var jt=r.job_type||'standard';
  document.getElementById('res-badge').textContent=bl[jt]||jt;
  document.getElementById('res-badge').className='badge badge-'+jt;
  var items=document.getElementById('res-items');items.innerHTML='';
  items.innerHTML='<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px;">Uncheck items that are NOT being removed:</div>';
  (r.items||[]).forEach(function(item,idx){{
    var row=document.createElement('div');row.className='item-row';
    row.innerHTML='<label style="display:flex;align-items:center;gap:10px;cursor:pointer;flex:1;margin:0"><input type="checkbox" id="item-cb-'+idx+'" checked onchange="recalcPrice()" style="width:18px;height:18px;accent-color:#16a34a;flex-shrink:0"><span class="item-name">'+esc(item.name||'Item')+'</span></label><div class="item-cy">'+(item.cubic_yards||0)+' CY</div><div class="item-qty">&times;'+(item.quantity||1)+'</div>';
    items.appendChild(row);
  }});
  var sp=r.special_items||[];
  if(sp.length>0){{var sh='<strong>Recycling/Disposal Fee Items:</strong><br>';sp.forEach(function(s){{sh+=esc(s.name)+' &times;'+(s.quantity||1)+'<br>'}});sh+='<em style="font-size:0.75rem;opacity:0.8">Fees confirmed on arrival.</em>';document.getElementById('res-special').innerHTML=sh;document.getElementById('res-special').style.display='block'}}
  var dp=r.potential_duplicates||[];
  if(dp.length>0){{var dh='<strong>Items to verify (may be duplicates):</strong><br>';dp.forEach(function(d){{dh+=esc(d.item_a)+' vs '+esc(d.item_b)+'<br>'}});document.getElementById('res-dupes').innerHTML=dh;document.getElementById('res-dupes').style.display='block'}}
  if(r.notes){{document.getElementById('res-notes').textContent=r.notes;document.getElementById('res-notes-card').style.display='block'}}
  // Show follow-up notice for large/hoarding jobs
  var fn=document.getElementById('followup-notice');
  if(fn){{var cy=r.cy_estimate||0;if(jt==='hoarder'||jt==='truck_load'||cy>=10){{fn.style.display='block'}}else{{fn.style.display='none'}}}}
  // Show customer photos
  var photos=r.photos||[];
  if(photos.length>0){{
    var pe=document.getElementById('res-photos');pe.innerHTML='';
    photos.forEach(function(b64,idx){{
      var img=document.createElement('img');
      img.src='data:image/jpeg;base64,'+b64;
      img.style='width:100px;height:100px;object-fit:cover;border-radius:10px;border:2px solid #e2e8f0;';
      img.alt='Photo '+(idx+1);
      pe.appendChild(img);
    }});
    document.getElementById('res-photos-card').style.display='block';
  }}
  // Appointment form always shows; phone call link is conditional via server template
  // Scroll to results
  el.scrollIntoView({{behavior:'smooth',block:'start'}});
}}

// --- Appointment form ---
var apptContact='text';
var apptTime='morning';

// Set min date to tomorrow
(function(){{
  var d=new Date();d.setDate(d.getDate()+1);
  var dd=d.toISOString().split('T')[0];
  var el=document.getElementById('appt-day');
  if(el){{el.min=dd;el.value=dd}}
}})();

function selectContact(method){{
  apptContact=method;
  document.getElementById('appt-text').classList.toggle('selected',method==='text');
  document.getElementById('appt-email').classList.toggle('selected',method==='email');
}}
function selectTime(time){{
  apptTime=time;
  document.getElementById('appt-morning').classList.toggle('selected',time==='morning');
  document.getElementById('appt-afternoon').classList.toggle('selected',time==='afternoon');
}}

async function submitAppointment(){{
  var btn=document.getElementById('appt-submit-btn');
  var errEl=document.getElementById('appt-error');
  errEl.style.display='none';
  var day=document.getElementById('appt-day').value;
  if(!day){{errEl.textContent='Please select a preferred day.';errEl.style.display='block';return}}
  if(!lastResult||!lastResult.id){{errEl.textContent='No estimate found. Please get an estimate first.';errEl.style.display='block';return}}
  btn.disabled=true;btn.textContent='Submitting...';
  var additionalItems=(document.getElementById('additional-items')||{{}}).value||'';
  try{{
    var resp=await fetch('/api/public/appointment-request',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        estimate_id:lastResult.id,
        slug:slug,
        contact_method:apptContact,
        preferred_day:day,
        preferred_time:apptTime,
        additional_items:additionalItems
      }})
    }});
    if(!resp.ok){{var err=await resp.json();throw new Error(err.detail||'Failed to submit')}}
    document.getElementById('appt-form-wrap').style.display='none';
    var confirmMsg=apptContact==='text'?"We'll text you to confirm your appointment.":"We'll email you to confirm your appointment.";
    document.getElementById('appt-confirm-msg').textContent=confirmMsg;
    document.getElementById('appt-success').style.display='block';
  }}catch(e){{
    errEl.textContent=e.message;errEl.style.display='block';
    btn.disabled=false;btn.textContent='Request Appointment';
  }}
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


# ── Email Verification for Customer Estimates ──────────────────────────────
# In-memory store for verification codes (keyed by email, TTL 10 minutes)
_verify_codes: dict[str, dict] = {}


@app.post("/api/public/verify/send")
@limiter.limit("30/minute")
async def public_verify_send(request: Request):
    """Send a 6-digit verification code to customer email."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    slug_val = (body.get("slug") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    # Rate limit: max 3 codes per email per 10 minutes
    existing = _verify_codes.get(email)
    if existing and existing.get("count", 0) >= 3 and time.time() - existing.get("first_sent", 0) < 600:
        raise HTTPException(status_code=429, detail="Too many requests. Try again in a few minutes.")

    code = f"{secrets.randbelow(900000) + 100000}"
    if not existing or time.time() - existing.get("first_sent", 0) >= 600:
        _verify_codes[email] = {"code": code, "expires": time.time() + 600, "count": 1, "first_sent": time.time()}
    else:
        _verify_codes[email]["code"] = code
        _verify_codes[email]["expires"] = time.time() + 600
        _verify_codes[email]["count"] = existing.get("count", 0) + 1

    email_sent = send_email(
        email,
        "Your verification code",
        f"""<div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:20px;">
        <h2 style="color:#16a34a;margin-bottom:16px;">Verify Your Email</h2>
        <p>Your verification code is:</p>
        <div style="font-size:32px;font-weight:800;letter-spacing:8px;text-align:center;padding:20px;background:#f0fdf4;border-radius:8px;margin:16px 0;">{code}</div>
        <p style="color:#666;font-size:14px;">This code expires in 10 minutes. If you didn't request this, you can ignore this email.</p>
        </div>"""
    )
    if not email_sent:
        import logging
        logging.getLogger("wsic.verify").warning(f"[verify/send] Email delivery failed for {email}")
        return {
            "ok": False,
            "message": "We could not send the code right now. Please try again in a few minutes or call the business directly.",
        }
    return {"ok": True, "message": "Verification code sent"}


@app.post("/api/public/verify/check")
@limiter.limit("60/minute")
async def public_verify_check(request: Request):
    """Verify the 6-digit code and return a verification token."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    if not email or not code:
        raise HTTPException(status_code=400, detail="Email and code required")

    stored = _verify_codes.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="No verification code found. Please request a new one.")
    if time.time() > stored.get("expires", 0):
        del _verify_codes[email]
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Invalid code. Please try again.")

    # Code is valid — generate a short-lived token
    del _verify_codes[email]
    token = secrets.token_urlsafe(32)
    # Store token with 30-minute expiry
    _verify_codes[f"token:{token}"] = {"email": email, "expires": time.time() + 1800}
    return {"ok": True, "token": token}


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
        "timezone": getattr(u, 'timezone', None) or "America/Chicago",
    }


MAGIC_BYTES = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89\x50\x4e\x47"],
    "image/webp": [b"RIFF"],
    "image/gif": [b"GIF87a", b"GIF89a"],
}


def validate_magic_bytes(raw: bytes, content_type: str) -> bool:
    """Check actual file header bytes match declared MIME type."""
    signatures = MAGIC_BYTES.get(content_type)
    if not signatures:
        return True  # HEIC/HEIF — no simple magic byte check, allow through
    return any(raw[:len(sig)] == sig for sig in signatures)


@app.post("/api/public/estimate/{slug}")
@limiter.limit("10/hour")
async def public_create_estimate(
    request: Request,
    slug: str,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    customer_name: str = Form(default=""),
    customer_email: str = Form(default=""),
    customer_phone: str = Form(default=""),
    preferred_contact: str = Form(default="phone"),
):
    """Public estimate endpoint — customer submits photos, charges against company's estimate count."""
    cleanup_expired_jobs()
    check_concurrent_limit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.company_slug == slug.lower().strip()))
        company_user = result.scalar_one_or_none()
    if not company_user:
        raise HTTPException(status_code=404, detail="Company not found")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == company_user.id))
        cu = result.scalar_one_or_none()
        if cu:
            # Credit-based gating for widget estimates
            free_remaining = max(0, 2 - (cu.free_trial_used or 0))
            if (cu.credit_balance or 0) <= 0 and free_remaining <= 0:
                await db.commit()
                raise HTTPException(status_code=403, detail="This estimator is temporarily unavailable. Please contact the company directly.")
            # Deduct credit
            if free_remaining > 0 and (cu.credit_balance or 0) <= 0:
                cu.free_trial_used = (cu.free_trial_used or 0) + 1
                txn = CreditTransaction(
                    user_id=cu.id,
                    transaction_type="free_trial",
                    credits=-1,
                    balance_after=cu.credit_balance or 0,
                    description=f"Free Trial Widget Estimate #{cu.free_trial_used}"
                )
            else:
                cu.credit_balance = (cu.credit_balance or 0) - 1
                cu.credits_used_total = (cu.credits_used_total or 0) + 1
                txn = CreditTransaction(
                    user_id=cu.id,
                    transaction_type="usage",
                    credits=-1,
                    balance_after=cu.credit_balance,
                    description="Widget Estimate"
                )
            cu.estimates_used = (cu.estimates_used or 0) + 1
            db.add(txn)
            await db.commit()
            company_user = cu

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
        ct = (file.content_type or "").lower()
        if not validate_magic_bytes(raw, ct):
            raise HTTPException(status_code=400, detail=f"Photo {i+1}: file contents don't match declared type.")
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

    # Store compressed photos for persistence (thumbnails for DB storage)
    stored_photos = []
    for pd in photo_data:
        stored_photos.append(pd["b64"])

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
        "preferred_contact": preferred_contact,
        "created_at": datetime.utcnow(),
        "stored_photos": stored_photos,
        "company_email": company_user.email,
        "company_name": company_user.company_name or "Junk Removal Company",
        "company_phone": company_user.company_phone or "",
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
@limiter.limit("120/minute")
async def public_estimate_status(request: Request, job_id: str):
    """Public status check — no auth, but limited response."""
    job = estimate_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    if job["status"] == "complete" and job.get("result"):
        r = job["result"]
        # Include photo count (not full base64) for customer display
        stored_photos = job.get("stored_photos", [])
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
                "photos": stored_photos,
            }
        }
    return {"status": job["status"], "message": job["message"], "result": None}


@app.post("/api/public/appointment-request")
@limiter.limit("10/minute")
async def public_appointment_request(request: Request):
    """Customer requests an appointment after receiving an estimate."""
    body = await request.json()
    estimate_id = body.get("estimate_id")
    slug = body.get("slug", "")
    contact_method = body.get("contact_method", "text")
    preferred_day = body.get("preferred_day", "")
    preferred_time = body.get("preferred_time", "morning")
    additional_items = body.get("additional_items", "")

    if not estimate_id:
        raise HTTPException(status_code=400, detail="Missing estimate ID")
    if not preferred_day:
        raise HTTPException(status_code=400, detail="Please select a preferred day")

    async with AsyncSessionLocal() as db:
        # Find the estimate
        result = await db.execute(
            select(Estimate).where(Estimate.id == estimate_id)
        )
        est = result.scalar_one_or_none()
        if not est:
            raise HTTPException(status_code=404, detail="Estimate not found")

        # Update the estimate with appointment details
        est.appointment_requested = True
        est.appointment_contact_method = contact_method
        est.appointment_preferred_day = preferred_day
        est.appointment_preferred_time = preferred_time
        est.appointment_requested_at = datetime.utcnow()
        if additional_items:
            est.additional_items_text = additional_items

        # Get the operator info
        user_result = await db.execute(
            select(User).where(User.id == est.user_id)
        )
        user = user_result.scalar_one_or_none()

        await db.commit()

        # Send comprehensive appointment notification email to operator
        # This includes full estimate details (items, photos) + scheduling info
        if user and user.email:
            cust_name = est.customer_name or "Customer"
            cust_email = est.customer_email or "N/A"
            cust_phone = est.customer_phone or "N/A"
            price_low = est.price_low or 0
            price_high = est.price_high or 0
            cy_mid = est.cy_estimate or 0
            contact_label = "Text" if contact_method == "text" else "Email"
            contact_value = cust_phone if contact_method == "text" else cust_email
            time_label = "Morning (8am\u201312pm)" if preferred_time == "morning" else "Afternoon (12\u20135pm)"
            additional_html = ""
            if additional_items:
                additional_html = f"""
                    <tr><td style="padding:8px 0;color:#64748b;vertical-align:top">Additional Items</td>
                    <td style="padding:8px 0;font-weight:500">{additional_items}</td></tr>"""

            # Load full estimate data for items + photos
            items_html = ""
            special_html = ""
            photos_html = ""
            try:
                import json as _json
                if est.result_json:
                    result_data = _json.loads(est.result_json)
                    for item in result_data.get("items", []):
                        items_html += f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'>{item.get('name','Item')}</td><td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center'>{item.get('quantity',1)}</td><td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{item.get('cubic_yards',0)} CY</td></tr>"
                    special_items = [it for it in result_data.get("items", []) if it.get("is_special")]
                    if special_items:
                        special_html = "<p style='color:#d97706;font-weight:600;margin-top:12px'>\u26a0\ufe0f Special disposal items detected:</p><ul>"
                        for si in special_items:
                            special_html += f"<li>{si.get('name','')} x{si.get('quantity',1)}</li>"
                        special_html += "</ul>"
                if est.photos_json:
                    photo_list = _json.loads(est.photos_json)
                    for idx, photo_b64 in enumerate(photo_list[:5]):
                        if isinstance(photo_b64, dict):
                            photo_b64 = photo_b64.get("b64", "")
                        if photo_b64:
                            photos_html += f'<img src="data:image/jpeg;base64,{photo_b64}" style="width:150px;height:150px;object-fit:cover;border-radius:8px;margin:4px;border:1px solid #ddd" alt="Photo {idx+1}">'
            except Exception:
                pass  # If we can't load items/photos, still send the email

            items_section = ""
            if items_html:
                items_section = f"""
                    <h2 style="margin:20px 0 12px;font-size:1.1rem;color:#0f172a">Items Detected</h2>
                    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
                      <tr style="background:#f8fafc"><th style="padding:8px 12px;text-align:left;font-size:0.85rem">Item</th><th style="padding:8px 12px;text-align:center;font-size:0.85rem">Qty</th><th style="padding:8px 12px;text-align:right;font-size:0.85rem">Volume</th></tr>
                      {items_html}
                    </table>
                    {special_html}"""

            photos_section = ""
            if photos_html:
                num_photos = est.photos_count or 0
                photos_section = f"""
                    <h2 style="margin:20px 0 12px;font-size:1.1rem;color:#0f172a">Customer Photos</h2>
                    <div style="margin-bottom:16px">{photos_html}</div>
                    <p style="font-size:0.8rem;color:#94a3b8">View all {num_photos} photo(s) and full details in your <a href="https://whatshouldicharge.app/estimate">WSIC dashboard</a>.</p>"""

            appt_html = f"""
                <div style="max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
                  <div style="background:#dc2626;padding:24px;border-radius:12px 12px 0 0;text-align:center">
                    <h1 style="margin:0;color:#fff;font-size:1.4rem">\U0001f514 Schedule Request</h1>
                    <p style="margin:4px 0 0;opacity:0.9;color:#fff;font-size:0.9rem">Customer wants to book — respond ASAP</p>
                  </div>
                  <div style="background:#fff;border:1px solid #e2e8f0;padding:24px;border-radius:0 0 12px 12px">
                    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:16px;margin-bottom:20px">
                      <h2 style="margin:0 0 12px;font-size:1.1rem;color:#991b1b">\u23f0 Appointment Details</h2>
                      <table style="width:100%;border-collapse:collapse">
                        <tr><td style="padding:8px 0;color:#64748b;width:120px">Preferred Day</td><td style="padding:8px 0;font-weight:700;font-size:1.05rem">{preferred_day}</td></tr>
                        <tr><td style="padding:8px 0;color:#64748b">Preferred Time</td><td style="padding:8px 0;font-weight:700;font-size:1.05rem">{time_label}</td></tr>
                        <tr><td style="padding:8px 0;color:#64748b">Contact Via</td><td style="padding:8px 0;font-weight:600">{contact_label}: {contact_value}</td></tr>{additional_html}
                      </table>
                    </div>

                    <h2 style="margin:0 0 12px;font-size:1.1rem;color:#0f172a">Customer Information</h2>
                    <table style="width:100%;border-collapse:collapse;margin-bottom:20px">
                      <tr><td style="padding:6px 0;color:#64748b;width:100px">Name</td><td style="padding:6px 0;font-weight:600">{cust_name}</td></tr>
                      <tr><td style="padding:6px 0;color:#64748b">Email</td><td style="padding:6px 0"><a href="mailto:{cust_email}">{cust_email}</a></td></tr>
                      <tr><td style="padding:6px 0;color:#64748b">Phone</td><td style="padding:6px 0"><a href="tel:{cust_phone}">{cust_phone}</a></td></tr>
                    </table>

                    <h2 style="margin:0 0 12px;font-size:1.1rem;color:#0f172a">Estimate Summary</h2>
                    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:16px;text-align:center;margin-bottom:16px">
                      <div style="font-size:2rem;font-weight:800;color:#16a34a">${price_low:,.0f} \u2014 ${price_high:,.0f}</div>
                      <div style="color:#64748b;font-size:0.85rem;margin-top:4px">{cy_mid} cubic yards estimated</div>
                    </div>

                    {items_section}
                    {photos_section}

                    <div style="margin-top:20px;padding:16px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;text-align:center">
                      <p style="margin:0 0 6px;font-weight:700;color:#991b1b">\u26a1 Action Required</p>
                      <p style="margin:0;font-size:0.88rem;color:#78350f">{contact_label} the customer ASAP to confirm the {preferred_day} appointment.</p>
                    </div>
                  </div>
                </div>"""

            try:
                send_email(
                    user.email,
                    f"\U0001f514 SCHEDULE REQUEST: {cust_name} \u2014 {preferred_day} {time_label}",
                    appt_html,
                )
            except Exception:
                pass  # Don't fail if email fails

    return {"ok": True, "message": "Appointment request submitted"}


@app.post("/api/auth/signup")
@limiter.limit("10/minute")
async def auth_signup(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    company_name = body.get("company_name", "").strip()
    company_city = body.get("company_city", "").strip()
    company_state = body.get("company_state", "").strip()
    price_per_cy_standard = body.get("price_per_cy_standard")
    price_per_cy_heavy = body.get("price_per_cy_heavy")
    min_charge = body.get("min_charge")
    industry = body.get("industry", "junk_removal")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Invalid email format.")
    if len(email) > 254:
        raise HTTPException(status_code=400, detail="Email address is too long.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="Password is too long.")

    pw_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    )

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

        detected_tz = STATE_TIMEZONE_MAP.get(company_state.upper(), "America/Chicago")
        user = User(
            email=email,
            password_hash=pw_hash,
            company_name=company_name,
            company_city=company_city,
            company_state=company_state,
            timezone=detected_tz,
            subscription_tier="free",
            estimates_used=0,
            estimates_limit=3,
            price_per_cy_standard=float(price_per_cy_standard) if price_per_cy_standard else None,
            price_per_cy_heavy=float(price_per_cy_heavy) if price_per_cy_heavy else None,
            min_charge=float(min_charge) if min_charge else 75.0,
            industry=industry,
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

    # Send to settings first if pricing not set during signup
    has_pricing = bool(price_per_cy_standard and price_per_cy_heavy)
    redirect_url = "/estimate" if has_pricing else "/admin#my-settings"

    response = JSONResponse({"success": True, "redirect": redirect_url, "needs_pricing": not has_pricing})
    response.set_cookie(
        "session_token", token, httponly=True, samesite="lax", secure=True,
        max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.post("/api/auth/login")
@limiter.limit("10/minute")
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
@limiter.limit("5/15minutes")
async def auth_forgot_password(request: Request):
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
        "timezone": getattr(user, 'timezone', None) or "America/Chicago",
        "monthly_call_limit": getattr(user, 'monthly_call_limit', 150) or 150,
        "monthly_calls_used": getattr(user, 'monthly_calls_used', 0) or 0,
        "overage_mode": getattr(user, 'overage_mode', 'warn_and_charge') or 'warn_and_charge',
        "role": getattr(user, 'role', 'owner') or 'owner',
        "credit_balance": getattr(user, 'credit_balance', 0) or 0,
        "free_trial_remaining": max(0, 2 - (getattr(user, 'free_trial_used', 0) or 0)),
    }


@app.get("/api/settings")
async def get_settings(request: Request):
    """Return all user settings from the database."""
    user = await require_user(request)
    cache_key = f"settings:{user.id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    async with AsyncSessionLocal() as db:
        raw = await db.execute(
            text("SELECT id, company_name, company_city, company_state, company_phone, company_slug, company_logo_url, price_per_cy_low, price_per_cy_high, price_per_cy_premium, min_charge, truck_capacity_cy, price_per_cy_standard, price_per_cy_heavy, google_tag_id, fb_pixel_id FROM users WHERE id = :uid"),
            {"uid": user.id}
        )
        row = raw.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    data = {
        "company_name": row["company_name"] or "",
        "company_city": row["company_city"] or "",
        "company_state": row["company_state"] or "",
        "price_per_cy_low": row["price_per_cy_low"] if row["price_per_cy_low"] is not None else 35.0,
        "price_per_cy_high": row["price_per_cy_high"] if row["price_per_cy_high"] is not None else 40.0,
        "price_per_cy_premium": row["price_per_cy_premium"] if row["price_per_cy_premium"] is not None else 55.0,
        "min_charge": row["min_charge"] if row["min_charge"] is not None else 75.0,
        "truck_capacity_cy": row["truck_capacity_cy"] if row["truck_capacity_cy"] is not None else 16.0,
        "company_slug": row["company_slug"] or "",
        "company_phone": row["company_phone"] or "",
        "company_logo_url": row["company_logo_url"] or "",
        "price_per_cy_standard": row["price_per_cy_standard"],
        "price_per_cy_heavy": row["price_per_cy_heavy"],
        "google_tag_id": row["google_tag_id"] or "",
        "fb_pixel_id": row["fb_pixel_id"] or "",
        "credit_balance": getattr(user, 'credit_balance', 0) or 0,
        "free_trial_remaining": max(0, 2 - (getattr(user, 'free_trial_used', 0) or 0)),
    }
    cache_set(cache_key, data, ttl=60)
    return data


@app.put("/api/settings")
async def update_settings(request: Request):
    import logging
    logger = logging.getLogger("wsic.settings")
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
        "price_per_cy_standard": float,
        "price_per_cy_heavy": float,
        "timezone": str,
        "google_tag_id": str,
        "fb_pixel_id": str,
    }

    # Auto-detect timezone from state if state is being updated and timezone isn't explicitly set
    if "company_state" in body and "timezone" not in body:
        state_upper = str(body["company_state"]).strip().upper()
        detected_tz = STATE_TIMEZONE_MAP.get(state_upper)
        if detected_tz:
            body["timezone"] = detected_tz

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

        # Validate logo URL if provided
        if "company_logo_url" in body:
            logo_url = str(body["company_logo_url"]).strip()
            if logo_url:
                if logo_url.lower().startswith(("javascript:", "data:", "vbscript:")):
                    raise HTTPException(status_code=400, detail="Invalid logo URL scheme")
                if not logo_url.lower().startswith("https://"):
                    raise HTTPException(status_code=400, detail="Logo URL must use https://")
            body["company_logo_url"] = logo_url

        # Build raw SQL UPDATE to bypass ORM column-tracking issues
        set_clauses = []
        params = {"uid": user.id}
        updated = []
        for field, typ in allowed_fields.items():
            if field in body:
                val = body[field]
                if typ == float:
                    val = float(val) if val not in (None, "") else None
                elif typ == str:
                    val = str(val).strip()
                set_clauses.append(f"{field} = :{field}")
                params[field] = val
                updated.append(field)

        if updated:
            sql = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = :uid"
            logger.info(f"[PUT /api/settings] RAW SQL: {sql} params={params}")
            try:
                await db.execute(text(sql), params)
                await db.commit()
                logger.info(f"[PUT /api/settings] Committed {len(updated)} fields via raw SQL")
            except Exception as commit_err:
                logger.error(f"[PUT /api/settings] COMMIT FAILED: {commit_err}")
                await db.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to save settings: {commit_err}")

        cache_invalidate(f"settings:{user.id}")

        # Read back the saved values via raw SQL
        verify = await db.execute(
            text("SELECT company_name, company_city, company_state, company_phone, company_slug, company_logo_url, price_per_cy_low, price_per_cy_high, price_per_cy_premium, min_charge, truck_capacity_cy, price_per_cy_standard, price_per_cy_heavy, timezone, google_tag_id, fb_pixel_id FROM users WHERE id = :uid"),
            {"uid": user.id}
        )
        row = verify.mappings().first()
        logger.info(f"[PUT /api/settings] VERIFY: phone={row['company_phone']!r} slug={row['company_slug']!r} min_charge={row['min_charge']!r}")

        return {
            "ok": True,
            "updated": updated,
            "company_name": row["company_name"] or "",
            "company_city": row["company_city"] or "",
            "company_state": row["company_state"] or "",
            "price_per_cy_low": row["price_per_cy_low"],
            "price_per_cy_high": row["price_per_cy_high"],
            "price_per_cy_premium": row["price_per_cy_premium"],
            "min_charge": row["min_charge"],
            "truck_capacity_cy": row["truck_capacity_cy"],
            "company_slug": row["company_slug"] or "",
            "company_phone": row["company_phone"] or "",
            "company_logo_url": row["company_logo_url"] or "",
            "price_per_cy_standard": row["price_per_cy_standard"],
            "price_per_cy_heavy": row["price_per_cy_heavy"],
        }


@app.post("/api/settings/check-market-rates")
async def check_market_rates(request: Request):
    """On-demand market rate lookup via Tavily — not used in estimates."""
    user = await require_user(request)
    rates = await get_market_rates(user.company_city, user.company_state)
    return rates


@app.put("/api/settings/password")
async def change_password(request: Request):
    user = await require_user(request)
    body = await request.json()
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")
    confirm_password = body.get("confirm_password", "")

    if not current_password or not new_password:
        raise HTTPException(status_code=400, detail="All password fields are required.")
    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match.")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    if len(new_password) > 128:
        raise HTTPException(status_code=400, detail="Password is too long.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")

        valid = await asyncio.to_thread(
            lambda: bcrypt.checkpw(current_password.encode("utf-8"), u.password_hash.encode("utf-8"))
        )
        if not valid:
            raise HTTPException(status_code=403, detail="Current password is incorrect.")

        new_hash = await asyncio.to_thread(
            lambda: bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        )
        await db.execute(
            text("UPDATE users SET password_hash = :pw WHERE id = :uid"),
            {"pw": new_hash, "uid": user.id}
        )
        await db.commit()

    return {"ok": True}


@app.post("/api/settings/logout-all")
async def logout_all_devices(request: Request):
    user = await require_user(request)
    token = request.cookies.get("session_token")

    async with AsyncSessionLocal() as db:
        # Delete all sessions for this user
        await db.execute(
            text("DELETE FROM sessions WHERE user_id = :uid"),
            {"uid": user.id}
        )
        # Create a fresh session so the current user stays logged in
        new_token = secrets.token_hex(32)
        db.add(Session(
            user_id=user.id,
            token=new_token,
            expires_at=datetime.utcnow() + timedelta(days=30),
        ))
        await db.commit()

    response = JSONResponse({"ok": True, "message": "All other sessions have been revoked."})
    response.set_cookie(
        "session_token", new_token, httponly=True, samesite="lax",
        secure=True, max_age=30 * 24 * 3600, path="/"
    )
    return response


@app.get("/api/library")
async def get_library(request: Request):
    await require_user(request)
    cached = cache_get("library")
    if cached is not None:
        return cached
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ItemReferenceLibrary).order_by(ItemReferenceLibrary.times_seen.desc())
        )
        items = result.scalars().all()
        data = [
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
        cache_set("library", data, ttl=60)
        return data


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
        cache_invalidate("library")
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
        cache_invalidate("library")
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

    # Ensure there's always a meaningful price range (at least 50% spread)
    if price_high <= price_low:
        price_high = round(price_low * 1.5, 2)
    elif price_high < price_low * 1.15:
        # Range too narrow — widen to at least 15% spread
        price_high = round(price_low * 1.5, 2)

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


# System prompt now loaded from services/industry_config.py via get_system_prompt()
# Kept as a fallback in case config loading fails
SYSTEM_PROMPT_BASE = get_system_prompt("junk_removal")



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


# Anthropic $/million tokens — baseline for admin cost tracking (published rates, Mar 2026).
ANTHROPIC_PRICING_PER_MILLION = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4-6-20250311": (3.0, 15.0),
    "claude-sonnet-4-5-20241022": (3.0, 15.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-5-sonnet-20240620": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-3-5-haiku-20241022": (1.0, 5.0),
    "claude-opus-4-6-20250311": (5.0, 25.0),
}


def _claude_response_usage(resp) -> tuple[int, int, str]:
    """(input_tokens, output_tokens, model) from an Anthropic messages response."""
    try:
        u = getattr(resp, "usage", None)
        inp = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        out = int(getattr(u, "output_tokens", 0) or 0) if u else 0
        mod = str(getattr(resp, "model", "") or "")
        return inp, out, mod
    except Exception:
        return 0, 0, ""


def estimate_anthropic_cost_cents(input_tokens: int, output_tokens: int, model_name: str) -> int:
    """Approximate Claude API cost in US cents."""
    rates = ANTHROPIC_PRICING_PER_MILLION.get(model_name or "", (3.0, 15.0))
    cost_dollars = (input_tokens / 1_000_000.0) * rates[0] + (output_tokens / 1_000_000.0) * rates[1]
    return int(round(cost_dollars * 100))


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

        client = anthropic.Anthropic(api_key=api_key, timeout=60.0)

        def run_lookup_call():
            return client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                temperature=0,
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
        in_tok, out_tok, mod = _claude_response_usage(calc_response)
        token_meta = {"input": in_tok, "output": out_tok, "model": mod}

        try:
            result = json.loads(calc_response.content[0].text.strip())
        except (json.JSONDecodeError, IndexError, AttributeError):
            return {"cubic_yards": 0, "confidence": 0, "_token_usage": token_meta}

        result["_token_usage"] = token_meta

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
MAX_CONCURRENT_JOBS = 10


def count_active_jobs() -> int:
    return sum(1 for j in estimate_jobs.values() if j.get("status") in ("analyzing", "looking_up"))


def check_concurrent_limit():
    if count_active_jobs() >= MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail="Server is busy processing other estimates. Please try again in a minute.")


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
    check_concurrent_limit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        fresh_user = result.scalar_one_or_none()
        if not fresh_user:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        # Credit-based gating
        free_remaining = max(0, 2 - (fresh_user.free_trial_used or 0))
        if (fresh_user.credit_balance or 0) <= 0 and free_remaining <= 0:
            await db.commit()
            return JSONResponse(status_code=402, content={
                "detail": "no_credits",
                "message": "No estimate credits remaining. Purchase a credit pack to continue."
            })
        # Deduct credit
        if free_remaining > 0 and (fresh_user.credit_balance or 0) <= 0:
            fresh_user.free_trial_used = (fresh_user.free_trial_used or 0) + 1
            txn = CreditTransaction(
                user_id=fresh_user.id,
                transaction_type="free_trial",
                credits=-1,
                balance_after=fresh_user.credit_balance or 0,
                description=f"Free Trial Estimate #{fresh_user.free_trial_used}"
            )
        else:
            fresh_user.credit_balance = (fresh_user.credit_balance or 0) - 1
            fresh_user.credits_used_total = (fresh_user.credits_used_total or 0) + 1
            txn = CreditTransaction(
                user_id=fresh_user.id,
                transaction_type="usage",
                credits=-1,
                balance_after=fresh_user.credit_balance,
                description="Estimate"
            )
        fresh_user.estimates_used = (fresh_user.estimates_used or 0) + 1
        db.add(txn)
        await db.commit()
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
        ct = (file.content_type or "").lower()
        if not validate_magic_bytes(raw, ct):
            raise HTTPException(status_code=400, detail=f"Photo {i+1}: file contents don't match declared type.")
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

    # Store photos for persistence
    stored_photos = [pd["b64"] for pd in photo_data]

    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": user.id,
        "estimate_name": estimate_name.strip(),
        "created_at": datetime.utcnow(),
        "stored_photos": stored_photos,
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
        industry_id = getattr(user, "industry", "junk_removal") or "junk_removal"
        system_prompt = get_system_prompt(industry_id)
        if library_context:
            system_prompt += "\n" + library_context

        job["status"] = "analyzing"
        job["message"] = "Analyzing photos..."

        import logging
        logger = logging.getLogger("wsic.estimate")

        client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
        total_input_tokens = 0
        total_output_tokens = 0
        model_name = ""

        def run_pass1():
            return client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                temperature=0,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": image_content + [{
                        "type": "text",
                        "text": "Analyze these junk removal photos and provide your estimate as JSON."
                    }]
                }]
            )

        try:
            message = await asyncio.to_thread(run_pass1)
        except Exception as api_err:
            logger.error(f"[run_estimate] Anthropic API error for job {job_id}, user {user.id}: {type(api_err).__name__}: {api_err}")
            job["status"] = "error"
            job["message"] = "Our AI service is temporarily unavailable. Please try again in a few minutes."
            job["result"] = None
            return

        pin, pout, pmod = _claude_response_usage(message)
        total_input_tokens += pin
        total_output_tokens += pout
        if pmod:
            model_name = pmod

        pass1_result = parse_ai_json(message.content[0].text)
        pass1_json_str = json.dumps(pass1_result)

        # H7: Validate AI response schema
        if not isinstance(pass1_result.get("items"), list):
            logger.error(f"[run_estimate] Malformed AI response for job {job_id}: missing/invalid 'items' field")
            job["status"] = "error"
            job["message"] = "We received an unexpected response from our AI. Please try again."
            job["result"] = None
            return
        totals = pass1_result.get("totals")
        if not isinstance(totals, dict) or "cubic_yards_mid" not in totals:
            logger.error(f"[run_estimate] Malformed AI response for job {job_id}: missing/invalid 'totals' field")
            job["status"] = "error"
            job["message"] = "We received an unexpected response from our AI. Please try again."
            job["result"] = None
            return
        if not isinstance(pass1_result.get("job_type"), str):
            logger.error(f"[run_estimate] Malformed AI response for job {job_id}: missing/invalid 'job_type' field")
            job["status"] = "error"
            job["message"] = "We received an unexpected response from our AI. Please try again."
            job["result"] = None
            return

        result_data = pass1_result

        # ── Post-processing: use item-based total (bottom-up), not spatial bounding box ──
        # The new prompt generates bottom-up estimates (sum of items = total).
        # We trust the item sum and update totals to match, rather than scaling items
        # to match a spatial bounding box that often overestimates.
        totals = result_data.get("totals", {})
        spatial_mid = totals.get("cubic_yards_mid", 0)
        items = result_data.get("items", [])
        item_sum = sum((it.get("cubic_yards", 0) * it.get("quantity", 1)) for it in items)

        if item_sum > 0 and spatial_mid > 0 and spatial_mid > item_sum * 1.5:
            # Spatial total is significantly larger than items — trust items (bottom-up)
            logger.info(
                f"[run_estimate] Job {job_id}: spatial ({spatial_mid:.1f} CY) > 1.5x items "
                f"({item_sum:.1f} CY). Using item total instead of inflating."
            )
            result_data["totals"]["cubic_yards_mid"] = round(item_sum, 1)
            result_data["totals"]["cubic_yards_low"] = round(item_sum * 0.85, 1)
            result_data["totals"]["cubic_yards_high"] = round(item_sum * 1.15, 1)
        elif item_sum > 0 and abs(item_sum - spatial_mid) > 0.5:
            # Items and spatial are close-ish — use item sum as the truth
            logger.info(
                f"[run_estimate] Job {job_id}: syncing totals to item sum "
                f"({item_sum:.1f} CY) instead of spatial ({spatial_mid:.1f} CY)"
            )
            result_data["totals"]["cubic_yards_mid"] = round(item_sum, 1)
            result_data["totals"]["cubic_yards_low"] = round(item_sum * 0.85, 1)
            result_data["totals"]["cubic_yards_high"] = round(item_sum * 1.15, 1)

        # Sanity check: cap single items at truck capacity (16 CY)
        # Was 5.0 CY which was too aggressive — bulk items like railroad ties,
        # lumber piles, and debris often exceed 5 CY legitimately
        for it in items:
            if it.get("cubic_yards", 0) > 16.0:
                it["cubic_yards"] = min(it["cubic_yards"], 16.0)

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

            for lr in lookup_results:
                if isinstance(lr, dict):
                    tu = lr.pop("_token_usage", None)
                    if isinstance(tu, dict):
                        total_input_tokens += int(tu.get("input", 0) or 0)
                        total_output_tokens += int(tu.get("output", 0) or 0)
                        if tu.get("model"):
                            model_name = str(tu["model"])

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

        result_data = validate_estimate(result_data)

        # --- Custom per-company pricing (skip Tavily when rates are set) ---
        custom_standard = getattr(user, 'price_per_cy_standard', None)
        custom_heavy = getattr(user, 'price_per_cy_heavy', None)

        if custom_standard and custom_heavy:
            # Use company's own rates with asymmetric range (-10% low, +20% high)
            totals = result_data.get("totals", {})
            cy_mid = float(totals.get("cubic_yards_mid", totals.get("cubic_yards_low", 2.0)))

            job_type = result_data.get("job_type", "standard")
            conditions = result_data.get("conditions", [])
            is_heavy = (
                job_type in ("premium", "hoarder", "truck_load")
                or "stairs" in conditions
                or "heavy_items" in conditions
                or "hoarder" in conditions
                or cy_mid > 10
            )

            rate = custom_heavy if is_heavy else custom_standard
            base_price = cy_mid * rate
            price_low = round(base_price * 0.90, 2)   # -10%
            price_high = round(base_price * 1.20, 2)   # +20%

            min_ch = user.min_charge or 75.0
            min_charge_applied = price_low < min_ch or price_high < min_ch
            price_low = max(price_low, min_ch)
            price_high = max(price_high, min_ch)

            # Ensure there's always a meaningful price range (at least 50% spread)
            if price_high <= price_low:
                price_high = round(price_low * 1.5, 2)
            elif price_high < price_low * 1.15:
                price_high = round(price_low * 1.5, 2)

            items = result_data.get("items", [])
            special_items = [
                {"name": item.get("name", "Unknown"), "quantity": int(item.get("quantity", 1))}
                for item in items if item.get("is_special")
            ]
            cy_mid = round(cy_mid, 1)
            market_context = {"source": "custom_company_rate", "rate": rate, "is_heavy": is_heavy}
            logger.info(f"[run_estimate] Job {job_id}: custom rate ${rate}/CY ({'heavy' if is_heavy else 'standard'}) → ${price_low}-${price_high}")
        else:
            # Fall back to user's stored rates (no Tavily — users set their own rates)
            market_context = None
            price_low, price_high, cy_mid, special_items, min_charge_applied = calculate_price(
                result_data,
                rate_low=user.price_per_cy_low or 35.0,
                rate_high=user.price_per_cy_high or 40.0,
                rate_premium=user.price_per_cy_premium or 55.0,
                min_charge=user.min_charge or 75.0,
                market_rates=None,
            )

        # Serialize stored photos for DB persistence
        stored_photos = job.get("stored_photos", [])
        photos_json_str = json.dumps(stored_photos) if stored_photos else ""
        logger.info(f"[run_estimate] Job {job_id}: saving {len(stored_photos)} photos ({len(photos_json_str)} bytes)")

        try:
            api_cost_cents_val = estimate_anthropic_cost_cents(
                total_input_tokens,
                total_output_tokens,
                model_name or "claude-sonnet-4-20250514",
            )
            token_input = total_input_tokens
            token_output = total_output_tokens
            token_model = (model_name or "")[:50]
        except Exception as tok_err:
            logger.warning(f"[run_estimate] Job {job_id}: token/cost calc failed: {tok_err}")
            api_cost_cents_val = 0
            token_input = 0
            token_output = 0
            token_model = ""

        async with AsyncSessionLocal() as db:
            # First try to add the column if it doesn't exist (safety net)
            try:
                await db.execute(text("ALTER TABLE estimates ADD COLUMN IF NOT EXISTS photos_json TEXT DEFAULT ''"))
                await db.commit()
            except Exception:
                await db.rollback()

        async with AsyncSessionLocal() as db:
            est = Estimate(
                user_id=user.id,
                team_member_id=job.get("team_member_id", 0),
                estimate_name=job.get("estimate_name", ""),
                customer_name=encrypt_pii(job.get("customer_name", "")),
                customer_email=encrypt_pii(job.get("customer_email", "")),
                customer_phone=encrypt_pii(job.get("customer_phone", "")),
                preferred_contact=job.get("preferred_contact", "phone"),
                photos_count=num_photos,
                result_json=json.dumps(result_data),
                price_low=price_low,
                price_high=price_high,
                cy_estimate=cy_mid,
                pass1_json=pass1_json_str,
                pass2_json="",
                lookups_json=lookups_json_str,
                photos_json=photos_json_str,
                input_tokens=token_input,
                output_tokens=token_output,
                api_cost_cents=int(api_cost_cents_val),
                model_used=token_model,
            )
            db.add(est)
            try:
                await db.commit()
                await db.refresh(est)
                logger.info(f"[run_estimate] Job {job_id}: estimate saved as ID {est.id}, photos_json length={len(est.photos_json or '')}")
            except Exception as db_err:
                logger.error(f"[run_estimate] Job {job_id}: DB commit failed: {type(db_err).__name__}: {db_err}")
                await db.rollback()
                # Retry without photos if column issue
                est.photos_json = ""
                db.add(est)
                await db.commit()
                await db.refresh(est)
                logger.warning(f"[run_estimate] Job {job_id}: saved without photos as fallback, ID {est.id}")
            estimate_id = est.id

        try:
            await update_library_from_estimate(result_data.get("items", []))
        except Exception:
            pass

        # ── Send lead email to company with photos, customer info, and estimate ──
        try:
            company_email = job.get("company_email", "")
            cust_name = job.get("customer_name", "Unknown")
            cust_email = job.get("customer_email", "")
            cust_phone = job.get("customer_phone", "")
            company_name_val = job.get("company_name", "")

            if company_email:
                items_html = ""
                for item in result_data.get("items", []):
                    items_html += f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'>{item.get('name','Item')}</td><td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:center'>{item.get('quantity',1)}</td><td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{item.get('cubic_yards',0)} CY</td></tr>"

                special_html = ""
                if special_items:
                    special_html = "<p style='color:#d97706;font-weight:600;margin-top:12px'>⚠️ Special disposal items detected:</p><ul>"
                    for si in special_items:
                        special_html += f"<li>{si.get('name','')} x{si.get('quantity',1)}</li>"
                    special_html += "</ul>"

                # Embed up to 5 photo thumbnails inline
                photos_html = ""
                for idx, photo_b64 in enumerate(stored_photos[:5]):
                    photos_html += f'<img src="data:image/jpeg;base64,{photo_b64}" style="width:150px;height:150px;object-fit:cover;border-radius:8px;margin:4px;border:1px solid #ddd" alt="Photo {idx+1}">'

                lead_html = f"""
                <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
                  <div style="background:#16a34a;color:#fff;padding:20px;border-radius:12px 12px 0 0;text-align:center">
                    <h1 style="margin:0;font-size:1.3rem">New Customer Estimate Lead</h1>
                    <p style="margin:4px 0 0;opacity:0.9;font-size:0.9rem">via WhatShouldICharge</p>
                  </div>
                  <div style="background:#fff;border:1px solid #e2e8f0;padding:24px;border-radius:0 0 12px 12px">
                    <h2 style="margin:0 0 16px;font-size:1.1rem;color:#0f172a">Customer Information</h2>
                    <table style="width:100%;border-collapse:collapse;margin-bottom:20px">
                      <tr><td style="padding:6px 0;color:#64748b;width:100px">Name</td><td style="padding:6px 0;font-weight:600">{cust_name}</td></tr>
                      <tr><td style="padding:6px 0;color:#64748b">Email</td><td style="padding:6px 0"><a href="mailto:{cust_email}">{cust_email}</a></td></tr>
                      <tr><td style="padding:6px 0;color:#64748b">Phone</td><td style="padding:6px 0"><a href="tel:{cust_phone}">{cust_phone}</a></td></tr>
                    </table>

                    <h2 style="margin:0 0 12px;font-size:1.1rem;color:#0f172a">Estimate Summary</h2>
                    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:16px;text-align:center;margin-bottom:16px">
                      <div style="font-size:2rem;font-weight:800;color:#16a34a">${price_low:,.0f} — ${price_high:,.0f}</div>
                      <div style="color:#64748b;font-size:0.85rem;margin-top:4px">{cy_mid} cubic yards estimated</div>
                    </div>

                    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
                      <tr style="background:#f8fafc"><th style="padding:8px 12px;text-align:left;font-size:0.85rem">Item</th><th style="padding:8px 12px;text-align:center;font-size:0.85rem">Qty</th><th style="padding:8px 12px;text-align:right;font-size:0.85rem">Volume</th></tr>
                      {items_html}
                    </table>

                    {special_html}

                    <h2 style="margin:20px 0 12px;font-size:1.1rem;color:#0f172a">Customer Photos</h2>
                    <div style="margin-bottom:16px">{photos_html}</div>
                    <p style="font-size:0.8rem;color:#94a3b8">View all {num_photos} photo(s) and full details in your <a href="https://whatshouldicharge.app/estimate">WSIC dashboard</a>.</p>

                    <div style="margin-top:20px;padding:16px;background:#f8fafc;border-radius:8px;text-align:center">
                      <p style="margin:0 0 8px;font-weight:600;color:#0f172a">Ready to book this job?</p>
                      <p style="margin:0;font-size:0.85rem;color:#64748b">Contact the customer to schedule an appointment.</p>
                    </div>
                  </div>
                </div>"""

                send_email(
                    company_email,
                    f"New Estimate Lead: {cust_name} — ${price_low:,.0f}-${price_high:,.0f}",
                    lead_html,
                )
        except Exception:
            pass  # Don't fail the estimate if email fails

        credit_bal = getattr(user, 'credit_balance', 0) or 0
        free_left = max(0, 2 - (getattr(user, 'free_trial_used', 0) or 0))
        remaining = credit_bal + free_left

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
        import logging
        logging.getLogger("wsic.estimate").error(f"[run_estimate] Unhandled error for job {job_id}: {type(e).__name__}: {e}")
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


# Legacy mapping — kept for reference, no longer used for gating
PRICE_TO_TIER = {
    "price_1TDJ2wAPEzwLONiqTut1n11W": "solo",
    "price_1TDJ2xAPEzwLONiq56jpA1fH": "team",
    "price_1TDJ5OAPEzwLONiqVhcBQjPn": "enterprise",
    "price_1T7PXXAPEzwLONiqIIrAtsQZ": "starter",
    "price_1T6iUPAPEzwLONiqp31lIw9T": "pro",
    "price_1T7PXXAPEzwLONiqpQbgpgZ8": "agency",
}


@app.post("/api/payments/create-checkout")
async def create_checkout(request: Request):
    user = await require_user(request)
    body = await request.json()
    pack_type = (body.get("pack_type") or "").strip().lower()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CreditPack).where(
                CreditPack.pack_key == pack_type,
                CreditPack.is_active == True,  # noqa: E712
            )
        )
        pack_row = result.scalar_one_or_none()
    if not pack_row:
        raise HTTPException(status_code=400, detail="Invalid or inactive credit pack.")
    pack = {
        "credits": pack_row.credits,
        "stripe_price_id": pack_row.stripe_price_id or "",
    }
    if not pack["stripe_price_id"]:
        raise HTTPException(status_code=500, detail="Stripe price not configured for this pack")

    # Create or reuse Stripe customer
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        fresh_user = result.scalar_one_or_none()
        if fresh_user and not fresh_user.stripe_customer_id:
            customer = await asyncio.to_thread(
                lambda: stripe.Customer.create(email=fresh_user.email, name=fresh_user.company_name)
            )
            fresh_user.stripe_customer_id = customer.id
            await db.commit()
            user = fresh_user

    session = await asyncio.to_thread(
        lambda: stripe.checkout.Session.create(
            customer=user.stripe_customer_id if user.stripe_customer_id else None,
            customer_email=user.email if not user.stripe_customer_id else None,
            payment_method_types=["card"],
            line_items=[{"price": pack["stripe_price_id"], "quantity": 1}],
            mode="payment",
            metadata={
                "user_id": str(user.id),
                "pack_type": pack_type,
                "credits": str(pack["credits"]),
            },
            success_url=str(request.base_url) + f"payment-success?session_id={{CHECKOUT_SESSION_ID}}&pack={pack_type}",
            cancel_url=str(request.base_url) + "upgrade",
            allow_promotion_codes=True,
        )
    )

    return {"checkout_url": session.url}


@app.post("/api/payments/webhook")
@limiter.limit("300/minute")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook not configured.")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = int(session.get("metadata", {}).get("user_id", 0))
        customer_id = session.get("customer", "")
        pack_type = session.get("metadata", {}).get("pack_type")
        credits_to_add = int(session.get("metadata", {}).get("credits", 0))

        if user_id and pack_type and credits_to_add > 0:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()
                if user:
                    user.credit_balance = (user.credit_balance or 0) + credits_to_add
                    user.credits_purchased_total = (user.credits_purchased_total or 0) + credits_to_add
                    user.stripe_customer_id = customer_id or user.stripe_customer_id or ""

                    pr = await db.execute(select(CreditPack).where(CreditPack.pack_key == pack_type))
                    pack_obj = pr.scalar_one_or_none()
                    pack_name = pack_obj.name if pack_obj else pack_type
                    amount_cents = (
                        pack_obj.price_cents
                        if pack_obj
                        else int(session.get("amount_total") or 0)
                    )
                    txn = CreditTransaction(
                        user_id=user.id,
                        transaction_type="purchase",
                        credits=credits_to_add,
                        balance_after=user.credit_balance,
                        description=f"{pack_name} Purchase",
                        stripe_session_id=session.get("id", ""),
                        pack_type=pack_type,
                        amount_cents=amount_cents,
                    )
                    db.add(txn)
                    await db.commit()

                    send_email(
                        user.email,
                        f"Your {credits_to_add} estimate credits are ready!",
                        f"<h2>Credits added!</h2>"
                        f"<p><strong>{credits_to_add} estimate credits</strong> have been added to your account.</p>"
                        f"<p>Your new balance: <strong>{user.credit_balance} credits</strong></p>"
                        f"<p>Start estimating at whatshouldicharge.app/estimate</p>"
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
                "customer_name": decrypt_pii(e.customer_name or ""),
                "customer_email": decrypt_pii(e.customer_email or ""),
                "preferred_contact": e.preferred_contact or "phone",
                "has_photos": bool(getattr(e, 'photos_json', None)),
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
            "customer_name": decrypt_pii(e.customer_name or ""),
            "customer_email": decrypt_pii(e.customer_email or ""),
            "customer_phone": decrypt_pii(e.customer_phone or ""),
            "preferred_contact": e.preferred_contact or "phone",
            "result": result_data,
            "has_photos": bool(e.photos_json),
        }


@app.get("/api/estimates/{estimate_id}/photos")
async def get_estimate_photos(request: Request, estimate_id: int):
    """Return stored photos for an estimate (base64 JPEG array)."""
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate).where(Estimate.id == estimate_id, Estimate.user_id == user.id)
        )
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        if not e.photos_json:
            return {"photos": []}
        try:
            photos = json.loads(e.photos_json)
        except Exception:
            photos = []
        return {"photos": photos, "count": len(photos)}


@app.get("/api/estimates/{estimate_id}/photo/{photo_index}")
async def get_estimate_photo_image(request: Request, estimate_id: int, photo_index: int):
    """Serve a single photo as a JPEG image (for <img> tags)."""
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate).where(Estimate.id == estimate_id, Estimate.user_id == user.id)
        )
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        if not e.photos_json:
            raise HTTPException(status_code=404, detail="No photos stored.")
        try:
            photos = json.loads(e.photos_json)
        except Exception:
            raise HTTPException(status_code=404, detail="No photos stored.")
        if photo_index < 0 or photo_index >= len(photos):
            raise HTTPException(status_code=404, detail="Photo not found.")
        image_bytes = base64.standard_b64decode(photos[photo_index])
        return Response(content=image_bytes, media_type="image/jpeg")


# ============== USAGE API ==============


@app.get("/api/usage")
async def get_usage(request: Request):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404)
        _reset_billing_cycle_if_needed(u)
        await db.commit()
        limit = u.monthly_call_limit or PLAN_CALL_LIMITS.get(u.subscription_tier, 3)
        used = u.monthly_calls_used or 0
        pct = round(used / limit * 100, 1) if limit > 0 else 0
        overage_calls = max(0, used - limit)
        cycle_start = u.billing_cycle_start
        cycle_resets = (cycle_start + timedelta(days=30)).isoformat() if cycle_start else None
        return {
            "used": used, "limit": limit, "percent": pct,
            "overage_calls": overage_calls,
            "overage_charges_cents": u.overage_charges_cents or 0,
            "overage_mode": u.overage_mode or "warn_and_charge",
            "overage_cap_cents": u.overage_cap_cents or 0,
            "billing_cycle_start": cycle_start.isoformat() if cycle_start else None,
            "billing_cycle_resets": cycle_resets,
            "role": getattr(u, 'role', 'owner') or "owner",
        }


@app.put("/api/usage/settings")
async def update_usage_settings(request: Request):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404)
        role = getattr(u, 'role', 'owner') or 'owner'
        if role not in ('owner', 'manager'):
            raise HTTPException(status_code=403, detail="Only owners and managers can change overage settings")
        body = await request.json()
        if "overage_mode" in body:
            if body["overage_mode"] in ('warn_and_charge', 'hard_stop', 'capped'):
                u.overage_mode = body["overage_mode"]
        if "overage_cap_cents" in body:
            u.overage_cap_cents = int(body["overage_cap_cents"])
        await db.commit()
        return {"ok": True}


@app.post("/api/usage/add-funds")
async def add_usage_funds(request: Request):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404)
        role = getattr(u, 'role', 'owner') or 'owner'
        if role not in ('owner', 'manager'):
            raise HTTPException(status_code=403, detail="Only owners and managers can add funds")
        body = await request.json()
        additional = int(body.get("additional_cents", 0))
        if additional > 0:
            u.overage_cap_cents = (u.overage_cap_cents or 0) + additional
        await db.commit()
        return {"ok": True, "new_cap_cents": u.overage_cap_cents}


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
                 "tier": u.subscription_tier, "credit_balance": getattr(u, 'credit_balance', 0) or 0,
                 "created_at": u.created_at.isoformat() if u.created_at else None}
                for u in recent_users
            ]
        }


@app.get("/api/admin/api-costs")
async def admin_api_costs(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        result = await db.execute(
            select(
                func.count(Estimate.id).label("estimate_count"),
                func.coalesce(func.sum(Estimate.input_tokens), 0).label("total_input_tokens"),
                func.coalesce(func.sum(Estimate.output_tokens), 0).label("total_output_tokens"),
                func.coalesce(func.sum(Estimate.api_cost_cents), 0).label("total_cost_cents"),
            ).where(Estimate.created_at >= month_start)
        )
        row = result.one()

        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_result = await db.execute(
            select(
                func.count(Estimate.id).label("estimate_count"),
                func.coalesce(func.sum(Estimate.api_cost_cents), 0).label("total_cost_cents"),
            ).where(Estimate.created_at >= today_start)
        )
        today_row = today_result.one()

        mtd_n = int(row.estimate_count or 0)
        avg_cost = round(int(row.total_cost_cents or 0) / mtd_n) if mtd_n > 0 else 0
        total_tokens = int(row.total_input_tokens or 0) + int(row.total_output_tokens or 0)
        mtd_cost = int(row.total_cost_cents or 0)

        return {
            "month": now.strftime("%B %Y"),
            "mtd_estimates": mtd_n,
            "mtd_input_tokens": int(row.total_input_tokens or 0),
            "mtd_output_tokens": int(row.total_output_tokens or 0),
            "mtd_total_tokens": total_tokens,
            "mtd_cost_cents": mtd_cost,
            "mtd_cost_display": f"${mtd_cost / 100:.2f}",
            "avg_cost_per_estimate_cents": avg_cost,
            "avg_cost_display": f"${avg_cost / 100:.2f}",
            "today_estimates": int(today_row.estimate_count or 0),
            "today_cost_cents": int(today_row.total_cost_cents or 0),
            "today_cost_display": f"${int(today_row.total_cost_cents or 0) / 100:.2f}",
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
                 "credit_balance": getattr(u, 'credit_balance', 0) or 0,
                 "free_trial_used": getattr(u, 'free_trial_used', 0) or 0,
                 "pricing_setup": getattr(u, 'price_per_cy_standard', None) is not None,
                 "is_active": getattr(u, 'is_active', True),
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


@app.get("/api/admin/estimates/{estimate_id}")
async def admin_estimate_detail(request: Request, estimate_id: int):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Estimate).where(Estimate.id == estimate_id))
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")

        # Get user info
        user_email = "Unknown"
        company_name = ""
        company_city = ""
        company_state = ""
        company_timezone = "America/Chicago"
        if e.user_id:
            u_result = await db.execute(select(User).where(User.id == e.user_id))
            u = u_result.scalar_one_or_none()
            if u:
                user_email = u.email
                company_name = u.company_name or ""
                company_city = u.company_city or ""
                company_state = u.company_state or ""
                company_timezone = getattr(u, 'timezone', None) or "America/Chicago"

        # Parse result JSON
        result_data = {}
        if e.result_json:
            try:
                result_data = json.loads(e.result_json)
            except Exception:
                pass

        # Parse lookups
        lookups = []
        if e.lookups_json:
            try:
                lookups = json.loads(e.lookups_json)
            except Exception:
                pass

        # Build photos array with data URLs
        photos = []
        if e.photos_json:
            try:
                raw_photos = json.loads(e.photos_json)
                for idx, b64 in enumerate(raw_photos):
                    photos.append({"index": idx, "data_url": f"data:image/jpeg;base64,{b64}"})
            except Exception:
                pass

        return {
            "id": e.id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "user_email": user_email,
            "company_name": company_name,
            "company_city": company_city,
            "company_state": company_state,
            "photos": photos,
            "photos_count": e.photos_count or 0,
            "result": result_data,
            "price_low": e.price_low,
            "price_high": e.price_high,
            "cy_estimate": e.cy_estimate,
            "estimate_name": e.estimate_name or "",
            "lookups": lookups,
            "actual_price": e.actual_price,
            "actual_cy": getattr(e, 'actual_cy', None),
            "accuracy_notes": getattr(e, 'accuracy_notes', '') or "",
            "company_timezone": company_timezone,
        }


@app.put("/api/admin/estimates/{estimate_id}/actual-price")
async def admin_update_actual_price(request: Request, estimate_id: int):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Estimate).where(Estimate.id == estimate_id))
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        if "actual_price" in body:
            val = body["actual_price"]
            e.actual_price = float(val) if val is not None and val != "" else None
        if "actual_cy" in body:
            val = body["actual_cy"]
            e.actual_cy = float(val) if val is not None and val != "" else None
        if "accuracy_notes" in body:
            e.accuracy_notes = str(body["accuracy_notes"] or "")
        await db.commit()
        return {"ok": True, "actual_price": e.actual_price, "actual_cy": e.actual_cy, "accuracy_notes": e.accuracy_notes or ""}


@app.get("/api/admin/users/{user_id}")
async def admin_user_detail(request: Request, user_id: int):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        # Get their estimates
        est_result = await db.execute(
            select(Estimate).where(Estimate.user_id == user_id).order_by(Estimate.created_at.desc()).limit(50)
        )
        estimates = est_result.scalars().all()
        last_estimate_at = estimates[0].created_at.isoformat() if estimates and estimates[0].created_at else None

        return {
            "id": u.id, "email": u.email, "company_name": u.company_name or "",
            "company_city": u.company_city or "", "company_state": u.company_state or "",
            "company_slug": getattr(u, 'company_slug', '') or "",
            "company_phone": getattr(u, 'company_phone', '') or "",
            "company_logo_url": getattr(u, 'company_logo_url', '') or "",
            "subscription_tier": u.subscription_tier or "free",
            "estimates_used": u.estimates_used, "estimates_limit": u.estimates_limit,
            "credit_balance": getattr(u, 'credit_balance', 0) or 0,
            "credits_purchased_total": getattr(u, 'credits_purchased_total', 0) or 0,
            "credits_used_total": getattr(u, 'credits_used_total', 0) or 0,
            "free_trial_used": getattr(u, 'free_trial_used', 0) or 0,
            "is_admin": u.is_admin,
            "is_active": getattr(u, 'is_active', True),
            "admin_notes": getattr(u, 'admin_notes', '') or "",
            "timezone": getattr(u, 'timezone', None) or "America/Chicago",
            "monthly_call_limit": u.monthly_call_limit or PLAN_CALL_LIMITS.get(u.subscription_tier, 3),
            "monthly_calls_used": u.monthly_calls_used or 0,
            "overage_mode": u.overage_mode or "warn_and_charge",
            "overage_charges_cents": u.overage_charges_cents or 0,
            "overage_cap_cents": u.overage_cap_cents or 0,
            "role": getattr(u, 'role', 'owner') or "owner",
            "price_per_cy_low": u.price_per_cy_low, "price_per_cy_high": u.price_per_cy_high,
            "price_per_cy_premium": u.price_per_cy_premium,
            "price_per_cy_standard": getattr(u, 'price_per_cy_standard', None),
            "price_per_cy_heavy": getattr(u, 'price_per_cy_heavy', None),
            "min_charge": u.min_charge, "truck_capacity_cy": u.truck_capacity_cy,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_estimate_at": last_estimate_at,
            "estimates": [
                {"id": e.id, "photos_count": e.photos_count, "price_low": e.price_low,
                 "price_high": e.price_high, "cy_estimate": e.cy_estimate,
                 "created_at": e.created_at.isoformat() if e.created_at else None,
                 "actual_price": e.actual_price}
                for e in estimates
            ],
        }


@app.put("/api/admin/users/{user_id}")
async def admin_update_user(request: Request, user_id: int):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        for field in ["subscription_tier", "company_name", "company_city", "company_state",
                      "company_slug", "company_phone", "admin_notes", "timezone"]:
            if field in body:
                setattr(u, field, body[field])
        for field in ["price_per_cy_standard", "price_per_cy_heavy", "price_per_cy_low",
                      "price_per_cy_high", "price_per_cy_premium", "min_charge", "truck_capacity_cy"]:
            if field in body:
                val = body[field]
                setattr(u, field, float(val) if val is not None and val != "" else None)
        if "is_active" in body:
            u.is_active = bool(body["is_active"])
        if "estimates_limit" in body:
            u.estimates_limit = int(body["estimates_limit"])
        if "credit_balance" in body:
            u.credit_balance = int(body["credit_balance"])
        # If plan changed, update limit from plan config
        if "subscription_tier" in body:
            tier = body["subscription_tier"]
            plan_result = await db.execute(select(PlanConfig).where(PlanConfig.tier_name == tier))
            plan = plan_result.scalar_one_or_none()
            if plan:
                u.estimates_limit = plan.estimate_limit
        await db.commit()
        return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(request: Request, user_id: int):
    await require_admin(request)
    new_password = secrets.token_urlsafe(12)
    hashed = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        u.password_hash = hashed
        await db.commit()
        return {"ok": True, "new_password": new_password}


# ── Admin: Credit packs (DB + Stripe) ──

@app.get("/api/admin/credit-packs")
async def admin_list_credit_packs(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CreditPack).order_by(CreditPack.sort_order, CreditPack.id))
        rows = result.scalars().all()
    return {"packs": [_credit_pack_admin_dict(p) for p in rows]}


@app.post("/api/admin/credit-packs")
async def admin_create_credit_pack(request: Request):
    await require_admin(request)
    body = await request.json()
    pack_key = _validate_pack_key(body.get("pack_key", ""))
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    credits = int(body.get("credits", 0))
    if credits < 1:
        raise HTTPException(status_code=400, detail="credits must be at least 1")
    price_cents = int(body.get("price_cents", 0))
    if price_cents < 50:
        raise HTTPException(status_code=400, detail="price_cents must be at least 50 (Stripe minimum)")
    discount_pct = int(body.get("discount_pct", 0))
    description = (body.get("description") or "").strip()
    sort_order = int(body.get("sort_order", 0))
    is_active = bool(body.get("is_active", True))
    is_featured = bool(body.get("is_featured", False))

    stripe_key = bool(stripe.api_key)
    manual_prod = (body.get("stripe_product_id") or "").strip()
    manual_price = (body.get("stripe_price_id") or "").strip()

    prod_id = ""
    pri_id = ""
    if stripe_key:
        prod_id, pri_id = await asyncio.to_thread(
            _stripe_create_product_and_price_sync,
            name,
            description,
            price_cents,
            pack_key,
            credits,
        )
    elif manual_prod and manual_price:
        prod_id, pri_id = manual_prod, manual_price
    else:
        raise HTTPException(
            status_code=400,
            detail="Set STRIPE_SECRET_KEY for auto-created Stripe products, or pass stripe_product_id and stripe_price_id.",
        )

    async with AsyncSessionLocal() as db:
        exists = await db.execute(select(CreditPack).where(CreditPack.pack_key == pack_key))
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="A pack with this pack_key already exists")
        if is_featured:
            await db.execute(update(CreditPack).values(is_featured=False))
        pack = CreditPack(
            pack_key=pack_key,
            name=name,
            credits=credits,
            price_cents=price_cents,
            discount_pct=discount_pct,
            description=description,
            stripe_product_id=prod_id,
            stripe_price_id=pri_id,
            is_active=is_active,
            is_featured=is_featured,
            sort_order=sort_order,
        )
        db.add(pack)
        await db.commit()
        await db.refresh(pack)
    return {"ok": True, "pack": _credit_pack_admin_dict(pack)}


@app.put("/api/admin/credit-packs/{pack_id}")
async def admin_update_credit_pack(request: Request, pack_id: int):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CreditPack).where(CreditPack.id == pack_id))
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Credit pack not found")

        old_price_cents = p.price_cents
        old_stripe_price_id = p.stripe_price_id or ""

        if "name" in body and body.get("name") is not None:
            nm = str(body.get("name") or "").strip()
            if nm:
                p.name = nm
        if "credits" in body:
            cr = int(body["credits"])
            if cr < 1:
                raise HTTPException(status_code=400, detail="credits must be at least 1")
            p.credits = cr
        if "price_cents" in body:
            pc = int(body["price_cents"])
            if pc < 50:
                raise HTTPException(status_code=400, detail="price_cents must be at least 50")
            p.price_cents = pc
        if "discount_pct" in body:
            p.discount_pct = int(body["discount_pct"])
        if "description" in body:
            p.description = (body.get("description") or "").strip()
        if "sort_order" in body:
            p.sort_order = int(body["sort_order"])
        if "is_active" in body:
            p.is_active = bool(body["is_active"])
        if "is_featured" in body:
            if bool(body["is_featured"]):
                await db.execute(update(CreditPack).values(is_featured=False))
                p.is_featured = True
            else:
                p.is_featured = False

        stripe_key = bool(stripe.api_key)
        new_price_cents = p.price_cents

        if stripe_key and new_price_cents != old_price_cents:
            if not p.stripe_price_id and not p.stripe_product_id:
                prod_id, pri_id = await asyncio.to_thread(
                    _stripe_create_product_and_price_sync,
                    p.name,
                    p.description or "",
                    new_price_cents,
                    p.pack_key,
                    p.credits,
                )
                p.stripe_product_id = prod_id
                p.stripe_price_id = pri_id
            else:
                product_id = (p.stripe_product_id or "").strip()
                if not product_id and old_stripe_price_id:
                    product_id = await asyncio.to_thread(
                        _stripe_product_id_from_price_sync, old_stripe_price_id
                    )
                    p.stripe_product_id = product_id
                if not product_id:
                    prod_id, pri_id = await asyncio.to_thread(
                        _stripe_create_product_and_price_sync,
                        p.name,
                        p.description or "",
                        new_price_cents,
                        p.pack_key,
                        p.credits,
                    )
                    if old_stripe_price_id:
                        await asyncio.to_thread(_stripe_deactivate_price_sync, old_stripe_price_id)
                    p.stripe_product_id = prod_id
                    p.stripe_price_id = pri_id
                else:
                    await asyncio.to_thread(
                        _stripe_update_product_sync,
                        product_id,
                        p.name,
                        p.description or "",
                    )
                    new_pid = await asyncio.to_thread(
                        _stripe_create_price_on_product_sync,
                        product_id,
                        new_price_cents,
                        p.pack_key,
                        p.credits,
                    )
                    if old_stripe_price_id:
                        await asyncio.to_thread(_stripe_deactivate_price_sync, old_stripe_price_id)
                    p.stripe_price_id = new_pid
        elif stripe_key:
            product_id = (p.stripe_product_id or "").strip()
            if not product_id and p.stripe_price_id:
                product_id = await asyncio.to_thread(
                    _stripe_product_id_from_price_sync, p.stripe_price_id
                )
                p.stripe_product_id = product_id
            if product_id:
                await asyncio.to_thread(
                    _stripe_update_product_sync,
                    product_id,
                    p.name,
                    p.description or "",
                )

        await db.commit()
        await db.refresh(p)
    return {"ok": True, "pack": _credit_pack_admin_dict(p)}


@app.delete("/api/admin/credit-packs/{pack_id}")
async def admin_delete_credit_pack(request: Request, pack_id: int):
    """Deactivate pack (soft delete)."""
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CreditPack).where(CreditPack.id == pack_id))
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Credit pack not found")
        p.is_active = False
        await db.commit()
    return {"ok": True}


# ── Promo Code API ──

@app.get("/api/admin/promos")
async def admin_list_promos(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
        promos = result.scalars().all()
        return [
            {"id": p.id, "code": p.code, "discount_type": p.discount_type,
             "discount_value": p.discount_value, "applies_to": p.applies_to or '{"products":["all"]}',
             "usage_limit": p.usage_limit, "times_used": p.times_used,
             "expires_at": p.expires_at.isoformat() if p.expires_at else None,
             "is_active": p.is_active,
             "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in promos
        ]


@app.post("/api/admin/promos")
async def admin_create_promo(request: Request):
    await require_admin(request)
    body = await request.json()
    code = body.get("code", "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(PromoCode).where(PromoCode.code == code))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Code already exists")
        promo = PromoCode(
            code=code,
            discount_type=body.get("discount_type", "percentage"),
            discount_value=float(body.get("discount_value", 0)),
            applies_to=body.get("applies_to", '{"products":["all"]}'),
            usage_limit=int(body.get("usage_limit", 0)),
            expires_at=datetime.fromisoformat(body["expires_at"]) if body.get("expires_at") else None,
            is_active=body.get("is_active", True),
        )
        db.add(promo)
        await db.commit()
        await db.refresh(promo)
        return {"ok": True, "id": promo.id}


@app.put("/api/admin/promos/{promo_id}")
async def admin_update_promo(request: Request, promo_id: int):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PromoCode).where(PromoCode.id == promo_id))
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Promo code not found")
        if "code" in body:
            p.code = body["code"].strip().upper()
        if "discount_type" in body:
            p.discount_type = body["discount_type"]
        if "discount_value" in body:
            p.discount_value = float(body["discount_value"])
        if "usage_limit" in body:
            p.usage_limit = int(body["usage_limit"])
        if "is_active" in body:
            p.is_active = bool(body["is_active"])
        if "expires_at" in body:
            p.expires_at = datetime.fromisoformat(body["expires_at"]) if body["expires_at"] else None
        await db.commit()
        return {"ok": True}


@app.delete("/api/admin/promos/{promo_id}")
async def admin_delete_promo(request: Request, promo_id: int):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PromoCode).where(PromoCode.id == promo_id))
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="Promo code not found")
        await db.delete(p)
        await db.commit()
        return {"ok": True}


@app.post("/api/promo/validate")
async def validate_promo_code(request: Request):
    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        return {"valid": False, "reason": "No code provided"}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PromoCode).where(PromoCode.code == code))
        p = result.scalar_one_or_none()
        if not p:
            return {"valid": False, "reason": "Invalid code"}
        if not p.is_active:
            return {"valid": False, "reason": "Code is inactive"}
        if p.expires_at and p.expires_at < datetime.utcnow():
            return {"valid": False, "reason": "Code expired"}
        if p.usage_limit > 0 and p.times_used >= p.usage_limit:
            return {"valid": False, "reason": "Code usage limit reached"}
        return {"valid": True, "discount_type": p.discount_type, "discount_value": p.discount_value}


# ── Accuracy API ──

@app.get("/api/admin/accuracy")
async def admin_accuracy(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        # Estimates with actual data
        with_price = await db.execute(
            select(Estimate).where(Estimate.actual_price.isnot(None))
        )
        price_estimates = with_price.scalars().all()

        total_with_actuals = len(price_estimates)
        price_accuracies = []
        over_count = 0
        under_count = 0
        for e in price_estimates:
            mid = (e.price_low + e.price_high) / 2 if e.price_low and e.price_high else 0
            if e.actual_price and mid > 0:
                acc = 1 - abs(mid - e.actual_price) / e.actual_price
                price_accuracies.append(acc)
                if mid > e.actual_price:
                    over_count += 1
                elif mid < e.actual_price:
                    under_count += 1

        avg_price_accuracy = round(sum(price_accuracies) / len(price_accuracies) * 100, 1) if price_accuracies else 0

        # CY accuracy
        with_cy = await db.execute(
            select(Estimate).where(Estimate.actual_cy.isnot(None))
        )
        cy_estimates = with_cy.scalars().all()
        cy_accuracies = []
        for e in cy_estimates:
            if e.actual_cy and e.cy_estimate and e.actual_cy > 0:
                acc = 1 - abs(e.cy_estimate - e.actual_cy) / e.actual_cy
                cy_accuracies.append(acc)
        avg_cy_accuracy = round(sum(cy_accuracies) / len(cy_accuracies) * 100, 1) if cy_accuracies else 0

        # Needs data queue: estimates older than 7 days without actual_price
        cutoff = datetime.utcnow() - timedelta(days=7)
        needs_data_result = await db.execute(
            select(Estimate)
            .where(Estimate.actual_price.is_(None), Estimate.created_at < cutoff)
            .order_by(Estimate.created_at.desc())
            .limit(50)
        )
        needs_data = needs_data_result.scalars().all()
        # Map user emails
        nd_user_ids = list(set(e.user_id for e in needs_data if e.user_id))
        nd_users = {}
        if nd_user_ids:
            u_result = await db.execute(select(User).where(User.id.in_(nd_user_ids)))
            for u in u_result.scalars().all():
                nd_users[u.id] = {"email": u.email, "company": u.company_name or ""}

        # Per-company accuracy
        company_map = {}
        for e in price_estimates:
            if e.user_id not in company_map:
                company_map[e.user_id] = {"price_accs": [], "cy_accs": [], "count": 0}
            mid = (e.price_low + e.price_high) / 2 if e.price_low and e.price_high else 0
            if e.actual_price and mid > 0:
                company_map[e.user_id]["price_accs"].append(1 - abs(mid - e.actual_price) / e.actual_price)
                company_map[e.user_id]["count"] += 1
            if e.actual_cy and e.cy_estimate and e.actual_cy > 0:
                company_map[e.user_id]["cy_accs"].append(1 - abs(e.cy_estimate - e.actual_cy) / e.actual_cy)

        all_user_ids = list(company_map.keys())
        company_names = {}
        if all_user_ids:
            u_res = await db.execute(select(User).where(User.id.in_(all_user_ids)))
            for u in u_res.scalars().all():
                company_names[u.id] = u.company_name or u.email

        by_company = []
        for uid, data in company_map.items():
            by_company.append({
                "company": company_names.get(uid, "Unknown"),
                "count": data["count"],
                "avg_price_accuracy": round(sum(data["price_accs"]) / len(data["price_accs"]) * 100, 1) if data["price_accs"] else 0,
                "avg_cy_accuracy": round(sum(data["cy_accs"]) / len(data["cy_accs"]) * 100, 1) if data["cy_accs"] else 0,
            })

        return {
            "total_with_actuals": total_with_actuals,
            "avg_price_accuracy": avg_price_accuracy,
            "avg_cy_accuracy": avg_cy_accuracy,
            "overestimate_rate": round(over_count / total_with_actuals * 100, 1) if total_with_actuals else 0,
            "underestimate_rate": round(under_count / total_with_actuals * 100, 1) if total_with_actuals else 0,
            "needs_data": [
                {"id": e.id, "user_email": nd_users.get(e.user_id, {}).get("email", "Unknown"),
                 "company": nd_users.get(e.user_id, {}).get("company", ""),
                 "price_low": e.price_low, "price_high": e.price_high, "cy_estimate": e.cy_estimate,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in needs_data
            ],
            "by_company": by_company,
        }


# ── Env Status API ──

@app.get("/api/admin/env-status")
async def admin_env_status(request: Request) -> dict[str, bool]:
    await require_admin(request)
    keys = ["ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
            "TAVILY_API_KEY", "SENDGRID_API_KEY"]
    out: dict[str, bool] = {}
    for k in keys:
        v = os.environ.get(k)
        out[k] = bool(isinstance(v, str) and v.strip())
    return out


@app.get("/api/admin/usage")
async def admin_usage_overview(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        users_result = await db.execute(select(User))
        users = users_result.scalars().all()
        total_calls = 0
        total_overage_cents = 0
        approaching = []
        hard_stopped = []
        for u in users:
            used = u.monthly_calls_used or 0
            limit = u.monthly_call_limit or PLAN_CALL_LIMITS.get(u.subscription_tier, 3)
            total_calls += used
            total_overage_cents += (u.overage_charges_cents or 0)
            pct = (used / limit * 100) if limit > 0 else 0
            if pct >= 75 and pct < 100:
                approaching.append({"id": u.id, "email": u.email, "company": u.company_name or "", "used": used, "limit": limit, "percent": round(pct, 1)})
            mode = getattr(u, 'overage_mode', 'warn_and_charge') or 'warn_and_charge'
            if mode == 'hard_stop' and used >= limit:
                hard_stopped.append({"id": u.id, "email": u.email, "company": u.company_name or "", "used": used, "limit": limit})
            elif mode == 'capped' and used >= limit and (u.overage_charges_cents or 0) >= (u.overage_cap_cents or 0):
                hard_stopped.append({"id": u.id, "email": u.email, "company": u.company_name or "", "used": used, "limit": limit})
        return {
            "total_calls": total_calls,
            "total_overage_cents": total_overage_cents,
            "approaching_limit": approaching,
            "hard_stopped": hard_stopped,
        }


@app.get("/api/admin/users/{user_id}/usage")
async def admin_user_usage(request: Request, user_id: int):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404)
        limit = u.monthly_call_limit or PLAN_CALL_LIMITS.get(u.subscription_tier, 3)
        used = u.monthly_calls_used or 0
        return {
            "used": used, "limit": limit,
            "percent": round(used / limit * 100, 1) if limit > 0 else 0,
            "overage_calls": max(0, used - limit),
            "overage_charges_cents": u.overage_charges_cents or 0,
            "overage_mode": u.overage_mode or "warn_and_charge",
            "overage_cap_cents": u.overage_cap_cents or 0,
            "billing_cycle_start": u.billing_cycle_start.isoformat() if u.billing_cycle_start else None,
        }


# ============== TEAM API ==============


@app.post("/api/team/members")
async def create_team_member(request: Request):
    user = await require_user(request)
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
    user = await require_user(request)
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
    user = await require_user(request)
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
    user = await require_user(request)
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
@limiter.limit("10/minute")
async def team_auth(request: Request):
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
    credit_bal = getattr(owner, 'credit_balance', 0) or 0
    free_left = max(0, 2 - (getattr(owner, 'free_trial_used', 0) or 0))
    remaining = credit_bal + free_left
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
    cleanup_expired_jobs()
    check_concurrent_limit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == owner.id))
        fresh_owner = result.scalar_one_or_none()
        if not fresh_owner:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        _reset_billing_cycle_if_needed(fresh_owner)
        allowed, err = _check_usage_limit(fresh_owner)
        if not allowed:
            await db.commit()
            return JSONResponse(status_code=429, content=err)
        _record_usage(fresh_owner)
        fresh_owner.estimates_used = (fresh_owner.estimates_used or 0) + 1
        await db.commit()
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
        ct = (file.content_type or "").lower()
        if not validate_magic_bytes(raw, ct):
            raise HTTPException(status_code=400, detail=f"Photo {i+1}: file contents don't match declared type.")
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

    # Store photos for persistence
    stored_photos = [pd["b64"] for pd in photo_data]

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
        "stored_photos": stored_photos,
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
             "customer_name": decrypt_pii(e.customer_name or "")}
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
        info_data.append(["Customer:", decrypt_pii(estimate.customer_name), "", ""])

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
