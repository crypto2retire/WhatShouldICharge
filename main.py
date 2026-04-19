import os
import re
import json
import math
import base64
import secrets
import time
import csv
import html
import shutil
import tempfile
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Response, Cookie, APIRouter
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
from sqlalchemy import select, text, func, update, or_
import asyncio
from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError
import io
from cryptography.fernet import Fernet, InvalidToken

from database import (
    engine, AsyncSessionLocal, Base, _is_postgres, DATABASE_URL,
    init_db, seed_reference_library, seed_plan_configs, seed_credit_packs,
    seed_site_config, ensure_admin_user, SEED_ITEMS, _PACK_KEY_RE,
    _STRIPE_PACK_PRICES, _STRIPE_PLAN_PRICES, _DEFAULT_CREDIT_PACKS_SEED,
)
from models import (
    User, TeamMember, TeamSession, SiteConfig, PlanConfig,
    CreditPack, CreditTransaction, PromoCode, Session, PasswordReset,
    Estimate, ItemReferenceLibrary, ProviderHealthEvent,
)
from cache import cache_get, cache_set, cache_invalidate
from auth import get_current_user, require_user, require_admin, get_team_member, require_team_member
from sendgrid_email import send_email
from billing import check_usage_limit, record_usage, PLAN_CALL_LIMITS, OVERAGE_RATE_CENTS
from pricing import calculate_price
from services.volume_lookup import validate_estimate, apply_pile_adjustment, detect_heavy_materials
from services.industry_config import (
    get_industry_config,
    get_system_prompt,
    get_extraction_prompt,
    get_verification_prompt,
    get_calibration_items,
    get_business_rules,
)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

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


import logging as _stdlib_logging
_wsic_logger = _stdlib_logging.getLogger("wsic")


def log(level: str, event: str, **kwargs):
    """Structured logging: log(level, event, **context)."""
    ctx = " | ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    msg = f"[{event}] {ctx}" if ctx else f"[{event}]"
    getattr(_wsic_logger, level, _wsic_logger.info)(msg)


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


# ── API Routers ──────────────────────────────────────────────────────────
router_health = APIRouter()
router_credits = APIRouter()
router_pages = APIRouter()
router_public = APIRouter()
router_auth = APIRouter()
router_settings = APIRouter()
router_library = APIRouter()
router_estimates = APIRouter()
router_payments = APIRouter()
router_admin = APIRouter()
router_team = APIRouter()
router_pdf = APIRouter()
router_promo = APIRouter()
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



app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
)

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
    except Exception as e:
        logger.warning("Operation failed: %s", e)


def _stripe_product_id_from_price_sync(price_id: str) -> str:
    pri = stripe.Price.retrieve(price_id)
    pid = getattr(pri, "product", None)
    if isinstance(pid, str):
        return pid
    if pid is not None and getattr(pid, "id", None):
        return str(pid.id)
    return ""


def _init_sentry():
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        _wsic_logger.debug("[sentry] SENTRY_DSN not set, skipping")
        return
    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        environment=os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("ENVIRONMENT") or "production",
        release=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12] or None,
        traces_sample_rate=0.05,
        _experiments={"max_spans": 1000},
    )
    _wsic_logger.info("[sentry] initialized")


def _shutdown_sentry():
    import sentry_sdk
    sentry_sdk.flush()
    _wsic_logger.info("[sentry] shutdown")


@asynccontextmanager
async def lifespan(app):
    await init_db()
    # Run any pending Alembic migrations automatically on startup (async-safe)
    try:
        from alembic.config import Config
        from alembic import context
        from sqlalchemy import pool
        from sqlalchemy.ext.asyncio import async_engine_from_config
        from database import Base
        import models  # noqa: F401

        alembic_cfg = Config("alembic.ini")
        # Set the database URL from env vars (same logic as alembic/env.py)
        import os
        db_url = os.environ.get("DATABASE_PRIVATE_URL") or os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL") or ""
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not db_url:
            db_url = "sqlite+aiosqlite:///./estimates.db"
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        target_metadata = Base.metadata

        connectable = async_engine_from_config(
            alembic_cfg.get_section(alembic_cfg.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        async with connectable.connect() as connection:
            def do_run_migrations(connection_context):
                context.configure(
                    connection=connection_context,
                    target_metadata=target_metadata,
                )
                with context.begin_transaction():
                    context.run_migrations()
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()
        import logging
        logging.getLogger("wsic.startup").info("[startup] Alembic migrations applied successfully")
    except Exception as e:
        import logging
        logging.getLogger("wsic.startup").warning("[startup] Alembic migration failed (non-fatal): %s", e)
    await seed_reference_library()
    await seed_plan_configs()
    await seed_credit_packs()
    await seed_site_config()
    await ensure_admin_user()
    _init_sentry()
    yield
    _shutdown_sentry()
    await engine.dispose()



@router_health.get("/api/health")
async def health_check():
    """Minimal health check for load balancers — no DB URLs, counts, or env metadata."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy"})


@router_health.get("/api/industries")
async def list_available_industries():
    from services.industry_config import list_industries
    return {"industries": list_industries()}


@router_credits.get("/api/credits")
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
        "free_trial_remaining": max(0, 5 - (user.free_trial_used or 0)),
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


@router_credits.get("/api/credits/packs")
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


@router_pages.get("/robots.txt")
async def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")


@router_pages.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")


@router_pages.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("static/landing.html")


@router_pages.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return FileResponse("static/terms.html")


@router_pages.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return FileResponse("static/privacy.html")


@router_pages.get("/blog", response_class=HTMLResponse)
async def blog_index_page():
    return FileResponse("static/blog/index.html")


@router_pages.get("/blog/how-to-price-junk-removal-jobs", response_class=HTMLResponse)
async def blog_how_to_price_junk_removal_jobs():
    return FileResponse("static/blog/how-to-price-junk-removal-jobs.html")


@router_pages.get("/blog/junk-removal-startup-costs", response_class=HTMLResponse)
async def blog_junk_removal_startup_costs():
    return FileResponse("static/blog/junk-removal-startup-costs.html")


@router_pages.get("/blog/junk-removal-marketing", response_class=HTMLResponse)
async def blog_junk_removal_marketing():
    return FileResponse("static/blog/junk-removal-marketing.html")

@router_pages.get("/blog/estimating-junk-removal-from-photos", response_class=HTMLResponse)
async def blog_estimating_from_photos():
    return FileResponse("static/blog/estimating-junk-removal-from-photos.html")

@router_pages.get("/blog/junk-removal-revenue-and-profit", response_class=HTMLResponse)
async def blog_revenue_and_profit():
    return FileResponse("static/blog/junk-removal-revenue-and-profit.html")


@router_pages.get("/estimate", response_class=HTMLResponse)
async def estimator(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/index.html")


@router_pages.get("/login", response_class=HTMLResponse)
async def login_page():
    return FileResponse("static/login.html")


@router_pages.get("/signup", response_class=HTMLResponse)
async def signup_page():
    return FileResponse("static/signup.html")


@router_pages.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page():
    return FileResponse("static/reset-password.html")


@router_pages.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/library.html")


@router_pages.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse(url="/estimate", status_code=302)
    return FileResponse("static/admin.html")


@router_pages.get("/team", response_class=HTMLResponse)
async def team_login_page():
    return FileResponse("static/team-login.html")


@router_pages.get("/team/app", response_class=HTMLResponse)
async def team_app_page(request: Request):
    member, owner = await get_team_member(request)
    if not member:
        return RedirectResponse(url="/team", status_code=302)
    return FileResponse("static/team.html")


@router_pages.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse("static/upgrade.html")


@router_pages.get("/payment-success", response_class=HTMLResponse)
async def payment_success_page():
    return FileResponse("static/payment-success.html")


@router_pages.get("/estimate/{slug}", response_class=HTMLResponse)
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
body{{font-family:'DM Sans',system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}

/* --- Layout --- */
.page-wrap{{max-width:680px;margin:0 auto;padding:20px 16px}}

/* --- Progress Stepper --- */
.progress-stepper{{display:flex;align-items:center;justify-content:center;gap:0;padding:20px 0 24px}}
.progress-step{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.progress-step-circle{{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.78rem;font-weight:700;border:2px solid #e2e8f0;color:#94a3b8;background:#fff;transition:all .3s}}
.progress-step.active .progress-step-circle{{border-color:#16a34a;background:#16a34a;color:#fff;box-shadow:0 2px 8px rgba(22,163,74,0.3)}}
.progress-step.done .progress-step-circle{{border-color:#16a34a;background:#dcfce7;color:#16a34a}}
.progress-step-label{{font-size:0.75rem;font-weight:600;color:#94a3b8;transition:color .3s}}
.progress-step.active .progress-step-label{{color:#16a34a}}
.progress-step.done .progress-step-label{{color:#16a34a}}
.progress-line{{width:40px;height:2px;background:#e2e8f0;margin:0 8px;transition:background .3s}}
.progress-line.done{{background:#16a34a}}

/* --- Header --- */
.site-header{{text-align:center;padding:32px 0 12px}}
.logo{{max-height:64px;margin-bottom:14px;border-radius:10px}}
.site-header h1{{font-size:1.5rem;font-weight:800;color:#e6edf3;line-height:1.2;letter-spacing:-0.02em}}
.location-badge{{display:inline-flex;align-items:center;gap:4px;margin-top:8px;padding:5px 14px;background:rgba(22,163,74,0.12);color:#22c55e;border:1px solid rgba(22,163,74,0.25);border-radius:24px;font-size:0.78rem;font-weight:600}}
.location-badge svg{{width:14px;height:14px;flex-shrink:0}}
.header-phone{{margin-top:10px}}
.header-phone a{{display:inline-flex;align-items:center;gap:6px;color:#16a34a;text-decoration:none;font-weight:600;font-size:0.9rem;padding:6px 16px;border-radius:24px;transition:background .2s}}
.header-phone a:hover{{background:rgba(22,163,74,0.1)}}
.header-phone svg{{width:16px;height:16px}}

/* --- Hero --- */
.hero{{text-align:center;padding:28px 0 20px;position:relative}}
.hero::before{{content:'';position:absolute;top:-100px;left:50%;transform:translateX(-50%);width:600px;height:350px;background:radial-gradient(ellipse,rgba(22,163,74,0.08) 0%,transparent 70%);pointer-events:none}}
.hero h2{{font-size:2rem;font-weight:800;color:#e6edf3;line-height:1.15;letter-spacing:-0.03em;margin-bottom:12px}}
.hero p{{font-size:1rem;color:#8b949e;max-width:460px;margin:0 auto;line-height:1.65}}
.trust-pills{{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:18px}}
.trust-pill{{display:inline-flex;align-items:center;gap:5px;padding:6px 14px;background:#161b22;border:1px solid #21262d;border-radius:24px;font-size:0.78rem;font-weight:600;color:#8b949e}}
.trust-pill svg{{width:14px;height:14px;color:#22c55e}}

/* --- Steps --- */
.steps-section{{padding:8px 0 24px}}
.steps-section h3{{text-align:center;font-size:0.82rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:16px}}
.steps{{display:flex;gap:12px}}
.step{{flex:1;text-align:center;padding:20px 12px 18px;background:#161b22;border:1px solid #21262d;border-radius:16px;transition:transform .2s,border-color .2s;cursor:default}}
.step:hover{{transform:translateY(-2px);border-color:rgba(22,163,74,0.3)}}
.step-icon{{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;background:rgba(22,163,74,0.12);border-radius:12px;margin-bottom:10px;color:#22c55e}}
.step-icon svg{{width:22px;height:22px}}
.step-title{{font-size:0.85rem;font-weight:700;color:#e6edf3;margin-bottom:2px}}
.step-sub{{font-size:0.75rem;color:#6e7681;line-height:1.4}}

/* --- Cards --- */
.card{{background:#161b22;border:1px solid #21262d;border-radius:16px;padding:24px;margin-bottom:16px}}
.card-title{{font-size:0.92rem;font-weight:700;color:#e6edf3;margin-bottom:16px}}
label{{display:block;font-size:0.8rem;color:#8b949e;margin-bottom:5px;font-weight:500}}
label .optional{{font-weight:400;color:#6e7681;font-size:0.72rem;margin-left:4px}}
input[type="text"],input[type="email"],input[type="tel"]{{width:100%;padding:12px 16px;background:#0d1117;border:1.5px solid #21262d;border-radius:12px;color:#e6edf3;font-size:0.95rem;margin-bottom:14px;font-family:inherit;transition:border-color .2s,box-shadow .2s}}
input:focus{{outline:none;border-color:#16a34a;box-shadow:0 0 0 4px rgba(22,163,74,0.12)}}
input::placeholder{{color:#6e7681}}
.optional-fields{{border-top:1px solid #21262d;padding-top:14px;margin-top:2px}}

/* --- Upload Zone --- */
.photo-tips{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap}}
.photo-tip{{flex:1;min-width:140px;display:flex;align-items:flex-start;gap:8px;padding:10px 12px;background:#161b22;border:1px solid #21262d;border-radius:10px;font-size:0.76rem;color:#8b949e;line-height:1.4}}
.photo-tip svg{{width:18px;height:18px;color:#22c55e;flex-shrink:0;margin-top:1px}}
.photo-tip strong{{color:#e6edf3;display:block;font-weight:600;font-size:0.78rem;margin-bottom:1px}}
.drop-zone{{border:2px dashed #30363d;border-radius:16px;padding:40px 20px;text-align:center;cursor:pointer;transition:all .25s;background:#161b22;position:relative;overflow:hidden}}
.drop-zone::before{{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 50%,rgba(22,163,74,0.06) 0%,transparent 70%);opacity:0;transition:opacity .3s}}
.drop-zone:hover,.drop-zone.drag-over{{border-color:#16a34a;border-style:solid;background:rgba(22,163,74,0.05)}}
.drop-zone:hover::before,.drop-zone.drag-over::before{{opacity:1}}
.drop-zone-icon{{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;background:rgba(22,163,74,0.12);border-radius:16px;margin-bottom:12px;color:#22c55e}}
.drop-zone-icon svg{{width:28px;height:28px}}
.drop-label{{font-size:1rem;font-weight:700;color:#e6edf3}}
.drop-sub{{font-size:0.8rem;color:#6e7681;margin-top:5px}}
.previews{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}
.preview-wrap{{position:relative;width:80px;height:80px}}
.preview-thumb{{width:80px;height:80px;border-radius:12px;object-fit:cover;border:2px solid #21262d;transition:border-color .2s}}
.preview-wrap:hover .preview-thumb{{border-color:#16a34a}}
.preview-remove{{position:absolute;top:-6px;right:-6px;width:22px;height:22px;background:#ef4444;color:#fff;border:2px solid #fff;border-radius:50%;font-size:0.7rem;display:flex;align-items:center;justify-content:center;cursor:pointer;line-height:1;font-weight:700;box-shadow:0 2px 4px rgba(0,0,0,0.15);opacity:0;transition:opacity .15s}}
.preview-wrap:hover .preview-remove{{opacity:1}}
.photo-count{{font-size:0.78rem;color:#64748b;margin-top:8px;text-align:center}}

/* --- Buttons --- */
.btn{{display:block;width:100%;padding:16px;background:#16a34a;color:#fff;border:none;border-radius:14px;font-size:1.05rem;font-weight:700;cursor:pointer;font-family:inherit;transition:all .15s;text-align:center;text-decoration:none;box-shadow:0 4px 20px rgba(22,163,74,0.35)}}
.btn:hover{{background:#22c55e;transform:translateY(-1px);box-shadow:0 6px 28px rgba(22,163,74,0.4)}}
.btn:active{{transform:translateY(0)}}
.btn:disabled{{opacity:0.4;cursor:not-allowed;transform:none;box-shadow:none}}
.btn-outline{{background:transparent;border:2px solid #16a34a;color:#22c55e;margin-top:12px;box-shadow:none}}
.btn-outline:hover{{background:rgba(22,163,74,0.1);transform:translateY(-1px)}}
.btn-call{{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:16px 32px;width:auto;font-size:1.1rem}}
.btn-call svg{{width:20px;height:20px}}

/* --- Loading --- */
.loading{{text-align:center;padding:60px 20px;display:none}}
.loading-dots{{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:20px}}
.loading-dots span{{width:12px;height:12px;background:#16a34a;border-radius:50%;animation:dotPulse 1.2s ease-in-out infinite}}
.loading-dots span:nth-child(2){{animation-delay:0.15s}}
.loading-dots span:nth-child(3){{animation-delay:0.3s}}
@keyframes dotPulse{{0%,80%,100%{{opacity:0.3;transform:scale(0.8)}}40%{{opacity:1;transform:scale(1.1)}}}}
.loading-text{{font-size:0.95rem;color:#8b949e;font-weight:500}}
.loading-sub{{font-size:0.8rem;color:#6e7681;margin-top:6px}}
.loading-steps{{display:flex;justify-content:center;gap:20px;margin-top:20px}}
.loading-step{{display:flex;align-items:center;gap:6px;font-size:0.78rem;color:#6e7681;font-weight:500}}
.loading-step.active{{color:#16a34a}}
.loading-step svg{{width:16px;height:16px}}

/* --- Results --- */
.results{{display:none}}
.results.show{{display:block;animation:fadeUp .4s ease-out}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(12px)}}to{{opacity:1;transform:translateY(0)}}}}
.price-card{{text-align:center;padding:32px 20px;background:linear-gradient(135deg,rgba(22,163,74,0.1) 0%,rgba(22,163,74,0.06) 100%);border:1px solid rgba(22,163,74,0.25)}}
.price-label{{font-size:0.82rem;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:4px}}
.price-range{{font-size:2.6rem;font-weight:800;color:#22c55e;letter-spacing:-0.03em;line-height:1.1}}
.price-note{{font-size:0.8rem;color:#6e7681;margin-top:8px}}
.min-charge-note{{display:none;font-size:0.82rem;color:#d97706;font-weight:600;margin-top:8px;padding:6px 14px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a}}
.badge{{display:inline-block;padding:4px 14px;border-radius:24px;font-size:0.75rem;font-weight:700;margin-bottom:10px;letter-spacing:0.02em}}
.badge-standard{{background:#dcfce7;color:#16a34a}}
.badge-premium{{background:#fef3c7;color:#d97706}}
.badge-hoarder{{background:#fee2e2;color:#ef4444}}
.badge-truck_load{{background:#e0f2fe;color:#0369a1}}
.cy-display{{font-size:0.85rem;color:#8b949e;margin-top:8px}}
.item-row{{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid #21262d;font-size:0.88rem;flex-wrap:wrap}}
.item-row:last-child{{border-bottom:none}}
.item-name{{flex:1;min-width:120px;font-weight:500;color:#e6edf3}}
.item-actions{{display:flex;align-items:center;gap:6px;flex-shrink:0}}
.item-cy{{color:#6e7681;font-size:0.78rem;font-weight:500;min-width:36px;text-align:right}}
.item-qty{{color:#8b949e;font-size:0.8rem;min-width:32px;text-align:right;font-weight:600}}
.special-note{{margin-top:16px;padding:16px;border-radius:14px;background:#fffbeb;border:1px solid #fde68a;font-size:0.82rem;color:#92400e;line-height:1.6}}
.dupe-note{{margin-top:12px;padding:16px;border-radius:14px;background:#fefce8;border:1px solid #fde68a;font-size:0.82rem;color:#854d0e;line-height:1.6}}
.followup-notice{{display:none;margin-top:16px;padding:18px 20px;border-radius:14px;background:linear-gradient(135deg,#eff6ff 0%,#f0f9ff 100%);border:1px solid #bfdbfe;font-size:0.85rem;color:#1e40af;line-height:1.6;text-align:center}}
.followup-notice svg{{width:20px;height:20px;vertical-align:middle;margin-right:6px;stroke:#2563eb}}
.followup-notice strong{{color:#1e3a8a}}

/* --- CTA / Appointment Form --- */
.cta-section{{text-align:center;padding:28px 20px;margin-top:8px;background:linear-gradient(135deg,#f0fdf4 0%,#ecfdf5 100%);border:1px solid #bbf7d0;border-radius:16px}}
.cta-section .subtext{{font-size:0.95rem;color:#0f172a;margin-bottom:14px;font-weight:700}}
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

/* --- Step Transitions --- */
.step-panel{{animation:stepFadeIn .35s ease-out}}
@keyframes stepFadeIn{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
.back-btn{{display:inline-flex;align-items:center;gap:6px;background:none;border:none;color:#64748b;font-size:0.82rem;font-weight:600;cursor:pointer;padding:8px 0;margin-bottom:8px;font-family:inherit;transition:color .2s}}
.back-btn:hover{{color:#0f172a}}
.back-btn svg{{width:16px;height:16px}}

/* --- Verify Code Input --- */
#verify-code{{letter-spacing:8px;font-size:1.4rem}}
.code-sent-msg{{display:flex;align-items:center;gap:6px;font-size:0.78rem;color:#16a34a;font-weight:600;margin-bottom:14px}}
.code-sent-msg svg{{width:16px;height:16px;flex-shrink:0}}
.resend-timer{{font-size:0.75rem;color:#94a3b8;text-align:center;margin-top:-8px;margin-bottom:14px}}

/* --- Responsive --- */
@media(max-width:480px){{
  .hero h2{{font-size:1.6rem}}
  .steps{{flex-direction:column;gap:10px}}
  .price-range{{font-size:2.2rem}}
  .trust-pills{{gap:6px}}
  .progress-step-label{{display:none}}
  .progress-line{{width:24px}}
  .photo-tips{{flex-direction:column}}
  .item-actions{{width:100%;justify-content:flex-end;margin-top:4px}}
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

  <!-- Progress Stepper -->
  <div class="progress-stepper" id="progress-stepper">
    <div class="progress-step active" id="ps-1">
      <div class="progress-step-circle">1</div>
      <span class="progress-step-label">Contact</span>
    </div>
    <div class="progress-line" id="pl-1"></div>
    <div class="progress-step" id="ps-2">
      <div class="progress-step-circle">2</div>
      <span class="progress-step-label">Photos</span>
    </div>
    <div class="progress-line" id="pl-2"></div>
    <div class="progress-step" id="ps-3">
      <div class="progress-step-circle">3</div>
      <span class="progress-step-label">Estimate</span>
    </div>
  </div>

  <!-- Step 1: Contact Info + Email Verification -->
  <div id="verify-section" class="step-panel">
    <div class="card">
      <div class="card-title">Your Contact Info</div>
      <label for="cust-email">Email <span style="color:#ef4444">*</span></label>
      <div style="display:flex;gap:8px;margin-bottom:14px">
        <input type="email" id="cust-email" placeholder="your@email.com" autocomplete="email" style="margin-bottom:0;flex:1">
        <button class="btn" id="send-code-btn" onclick="sendVerifyCode()" style="width:auto;padding:12px 20px;font-size:0.85rem;white-space:nowrap;box-shadow:none">Verify</button>
      </div>
      <div id="code-section" style="display:none">
        <label for="verify-code">Enter verification code</label>
        <input type="text" id="verify-code" placeholder="------" maxlength="6" autocomplete="one-time-code" style="text-align:center;letter-spacing:8px;font-size:1.4rem;font-weight:700">
        <div id="code-sent-msg" class="code-sent-msg" style="display:none"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>Code sent!</div>
        <div id="resend-timer" class="resend-timer" style="display:none"></div>
      </div>
      <div class="optional-fields">
        <label for="cust-name">Name <span class="optional">(optional)</span></label>
        <input type="text" id="cust-name" placeholder="Your name" autocomplete="name">
        <label for="cust-phone">Phone <span class="optional">(optional)</span></label>
        <input type="tel" id="cust-phone" placeholder="(555) 123-4567" autocomplete="tel">
      </div>
      <div class="error" id="verify-error"></div>
      <button class="btn" id="continue-btn" onclick="verifyAndContinue()">Continue to Photos</button>
      <div style="font-size:0.72rem;color:#94a3b8;text-align:center;margin-top:14px;line-height:1.6">By continuing, you agree to the <a href="/terms" target="_blank" style="color:#94a3b8;text-decoration:underline">Terms of Service</a> and <a href="/privacy" target="_blank" style="color:#94a3b8;text-decoration:underline">Privacy Policy</a>.<br>Estimates are AI-generated approximations and not binding quotes.</div>
    </div>
    <div style="text-align:center;padding:12px;font-size:0.72rem;color:#cbd5e1;line-height:1.5;margin-top:4px">This tool is currently in <strong>beta</strong>. Estimates may contain errors. {name} is not liable for differences between estimated and actual pricing. This estimate covers items shown in your photos only — additional items will be priced at standard rates. Recycling fees apply to freon-containing appliances, tires, TVs, and some electronics. Final pricing is confirmed on-site.</div>
  </div>

  <!-- Step 2: Upload Section (hidden until verified) -->
  <div id="upload-section" style="display:none" class="step-panel">
    <button class="back-btn" onclick="goBackToContact()"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>Back to contact info</button>
    <div class="card">
      <div class="card-title">Upload Photos of Items for Removal</div>
      <div class="photo-tips">
        <div class="photo-tip"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg><div><strong>Wide shots</strong>Stand back to capture the whole area</div></div>
        <div class="photo-tip"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg><div><strong>Multiple angles</strong>2-4 photos from different sides</div></div>
      </div>
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
      <strong>Larger or more uncertain jobs:</strong> We may follow up within 24 hours to confirm pricing or ask one or two clarifying questions.
    </div>

    <div class="card" id="res-scene-card" style="display:none">
      <div class="card-title">Estimate Context</div>
      <div id="res-scene-note" style="font-size:0.84rem;color:#64748b;line-height:1.6"></div>
      <div id="res-confidence-note" style="display:none;font-size:0.8rem;color:#94a3b8;line-height:1.6;margin-top:8px"></div>
      <div id="res-geometry-note" style="display:none;font-size:0.8rem;color:#94a3b8;line-height:1.6;margin-top:8px"></div>
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

var currentStep=1;
function updateStepper(step){{
  currentStep=step;
  for(var i=1;i<=3;i++){{
    var ps=document.getElementById('ps-'+i);
    ps.classList.remove('active','done');
    if(i<step) ps.classList.add('done');
    if(i===step) ps.classList.add('active');
  }}
  for(var i=1;i<=2;i++){{
    var pl=document.getElementById('pl-'+i);
    pl.classList.toggle('done',i<step);
  }}
}}

function goBackToContact(){{
  document.getElementById('upload-section').style.display='none';
  document.getElementById('verify-section').style.display='block';
  document.getElementById('verify-section').classList.remove('step-panel');
  void document.getElementById('verify-section').offsetWidth;
  document.getElementById('verify-section').classList.add('step-panel');
  updateStepper(1);
}}

var resendTimer=null;
function startResendTimer(){{
  var btn=document.getElementById('send-code-btn');
  var timerEl=document.getElementById('resend-timer');
  var seconds=45;
  btn.disabled=true;btn.style.opacity='0.5';btn.style.cursor='not-allowed';
  timerEl.style.display='block';
  clearInterval(resendTimer);
  resendTimer=setInterval(function(){{
    seconds--;
    timerEl.textContent='Resend available in '+seconds+'s';
    if(seconds<=0){{
      clearInterval(resendTimer);
      btn.disabled=false;btn.style.opacity='1';btn.style.cursor='pointer';
      timerEl.style.display='none';
    }}
  }},1000);
}}

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
    document.getElementById('code-sent-msg').style.display='flex';
    document.getElementById('verify-code').focus();
    btn.textContent='Resend';btn.disabled=false;
    startResendTimer();
  }}catch(e){{errEl.textContent=e.message;errEl.style.display='block';btn.textContent='Verify';btn.disabled=false}}
}}

async function verifyAndContinue(){{
  var email=document.getElementById('cust-email').value.trim();
  var code=document.getElementById('verify-code').value.trim();
  var errEl=document.getElementById('verify-error');
  errEl.style.display='none';
  if(!email){{errEl.textContent='Please enter your email.';errEl.style.display='block';return}}
  if(!code||code.length<6){{errEl.textContent='Please enter the 6-digit verification code from your email.';errEl.style.display='block';return}}
  var btn=document.getElementById('continue-btn');
  btn.disabled=true;btn.textContent='Verifying...';
  try{{
    var resp=await fetch('/api/public/verify/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:email,code:code}})}});
    var data=await resp.json();
    if(!resp.ok) throw new Error(data.detail||'Verification failed');
    verificationToken=data.token;
    clearInterval(resendTimer);
    document.getElementById('verify-section').style.display='none';
    document.getElementById('upload-section').style.display='block';
    document.getElementById('upload-section').classList.remove('step-panel');
    void document.getElementById('upload-section').offsetWidth;
    document.getElementById('upload-section').classList.add('step-panel');
    updateStepper(2);
  }}catch(e){{errEl.textContent=e.message;errEl.style.display='block';btn.disabled=false;btn.textContent='Continue to Photos'}}
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
    if(data.status==='retry_needed'){{throw new Error(data.message||'Please upload one clearer, wider photo and try again.')}}
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
      if(data.status==='complete'&&data.result){{clearInterval(iv);document.getElementById('loading').style.display='none';showResults(data.result);updateStepper(3)}}
      else if(data.status==='needs_review'){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent=data.message||'This estimate needs manual review before we can show pricing.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Get Your Estimate'}}
      else if(data.status==='retry_needed'){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent=data.message||'Please upload one clearer, wider photo and try again.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Get Your Estimate'}}
      else if(data.status==='error'){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent=data.message||'An error occurred. Please try again.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Schedule an Appointment'}}
    }}catch(e){{
      if(e.message&&e.message.includes('404')){{clearInterval(iv);document.getElementById('loading').style.display='none';document.getElementById('upload-section').style.display='block';document.getElementById('error-msg').textContent='Your estimate session expired. Please try submitting again.';document.getElementById('error-msg').style.display='block';document.getElementById('submit-btn').disabled=false;document.getElementById('submit-btn').textContent='Get Your Estimate'}}
    }}    if(attempts>90){{clearInterval(iv);lt.textContent='Taking longer than expected...'}}
  }},2000);
}}

var lastResult=null;
var adjustedQtys={{}};
function effectiveQty(item,idx){{
  if(!item) return 0;
  var orig=Math.max(1,parseInt(item.quantity)||1);
  var cb=document.getElementById('item-cb-'+idx);
  if(cb && !cb.checked) return 0;
  var q=adjustedQtys[idx];
  if(q==null || isNaN(q)) return orig;
  q=parseInt(q)||orig;
  return Math.max(1,Math.min(orig,q));
}}
function recalcPrice(){{
  if(!lastResult) return;
  var items=lastResult.items||[];
  var totalCY=0;
  items.forEach(function(item,idx){{
    var qty=effectiveQty(item,idx);
    if(qty>0){{totalCY+=((item.cubic_yards||0)*qty)}}
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
  adjustedQtys={{}};
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
    var originalQty=Math.max(1,parseInt(item.quantity)||1);
    row.innerHTML='<label style="display:flex;align-items:center;gap:10px;cursor:pointer;flex:1;min-width:120px;margin:0"><input type="checkbox" id="item-cb-'+idx+'" checked onchange="recalcPrice()" style="width:18px;height:18px;accent-color:#16a34a;flex-shrink:0"><span class="item-name">'+esc(item.name||'Item')+'</span></label><div class="item-actions"><span class="item-cy">'+(item.cubic_yards||0)+' CY</span><input type="number" id="item-qty-'+idx+'" min="1" max="'+originalQty+'" value="'+originalQty+'" onchange="(function(el,orig,i){{var v=parseInt(el.value)||orig;v=Math.max(1,Math.min(orig,v));el.value=v;if(v===orig){{delete adjustedQtys[i]}}else{{adjustedQtys[i]=v}}recalcPrice();}})(this,'+originalQty+','+idx+')" style="width:52px;padding:4px 6px;border-radius:8px;border:1px solid #e2e8f0;background:#fff;color:#0f172a;font-size:0.78rem"><button type="button" onclick="var cb=document.getElementById(\\'item-cb-'+idx+'\\');if(cb){{cb.checked=false;recalcPrice();}}" style="padding:4px 10px;border-radius:8px;border:1px solid #fecaca;background:#fef2f2;color:#ef4444;font-size:0.74rem;cursor:pointer;font-weight:600">Remove</button></div>';
    items.appendChild(row);
  }});
  var sp=r.special_items||[];
  if(sp.length>0){{var sh='<strong>Recycling/Disposal Fee Items:</strong><br>';sp.forEach(function(s){{sh+=esc(s.name)+' &times;'+(s.quantity||1)+'<br>'}});sh+='<em style="font-size:0.75rem;opacity:0.8">Fees confirmed on arrival.</em>';document.getElementById('res-special').innerHTML=sh;document.getElementById('res-special').style.display='block'}}
  var dp=r.potential_duplicates||[];
  if(dp.length>0){{var dh='<strong>Items to verify (could change quantity):</strong><br><span style="font-size:0.75rem;opacity:0.85">Only review these if they represent separate items. Already-confirmed duplicate angles are hidden.</span><br>';dp.forEach(function(d){{dh+=esc(d.item_a)+' vs '+esc(d.item_b)+'<br>'}});document.getElementById('res-dupes').innerHTML=dh;document.getElementById('res-dupes').style.display='block'}}
  if(r.notes){{document.getElementById('res-notes').textContent=r.notes;document.getElementById('res-notes-card').style.display='block'}}
  var sceneCard=document.getElementById('res-scene-card');
  var sceneNote=document.getElementById('res-scene-note');
  var confNote=document.getElementById('res-confidence-note');
  var geometryNote=document.getElementById('res-geometry-note');
  var sceneParts=[];
  if(r.scene_label){{sceneParts.push('Scene type: '+r.scene_label)}}
  if(r.range_widened){{sceneParts.push('Price range widened slightly for uncertainty')}}
  if(sceneParts.length){{sceneNote.textContent=sceneParts.join(' • ');sceneCard.style.display='block'}}else{{sceneCard.style.display='none'}}
  var confReasons=(r.confidence_reasons||[]).slice(0,2);
  if(confReasons.length){{confNote.textContent=confReasons.join(' ');confNote.style.display='block'}}else{{confNote.style.display='none'}}
  if(r.geometry_summary){{geometryNote.textContent='Geometry check: '+r.geometry_summary;geometryNote.style.display='block';sceneCard.style.display='block'}}else{{geometryNote.style.display='none'}}
  // Show follow-up notice for large/hoarding jobs
  var fn=document.getElementById('followup-notice');
  if(fn){{var cy=r.cy_estimate||0;if(jt==='truck_load'||(jt==='hoarder'&&cy>=12)||cy>=14){{fn.style.display='block'}}else{{fn.style.display='none'}}}}
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
_VERIFY_CODES_MAX_AGE = 600
_VERIFY_CODES_MAX_PER_EMAIL = 3


def _cleanup_expired_verify_codes():
    """Remove expired entries from the in-memory verification code store."""
    now = time.time()
    expired_keys = [
        k for k, v in _verify_codes.items()
        if now > v.get("expires", 0)
    ]
    for k in expired_keys:
        del _verify_codes[k]
_VERIFY_CODES_MAX_AGE = 600
_VERIFY_CODES_MAX_PER_EMAIL = 3


def _cleanup_expired_verify_codes():
    """Remove expired entries from the in-memory verification code store."""
    now = time.time()
    expired_keys = [
        k for k, v in _verify_codes.items()
        if now > v.get("expires", 0)
    ]
    for k in expired_keys:
        del _verify_codes[k]


@router_public.post("/api/public/verify/send")
@limiter.limit("30/minute")
async def public_verify_send(request: Request):
    """Send a 6-digit verification code to customer email."""
    _cleanup_expired_verify_codes()
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    slug_val = (body.get("slug") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    _cleanup_expired_verify_codes()
    existing = _verify_codes.get(email)
    if existing and existing.get("count", 0) >= _VERIFY_CODES_MAX_PER_EMAIL and time.time() - existing.get("first_sent", 0) < _VERIFY_CODES_MAX_AGE:
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


@router_public.post("/api/public/verify/check")
@limiter.limit("60/minute")
async def public_verify_check(request: Request):
    """Verify the 6-digit code and return a verification token."""
    _cleanup_expired_verify_codes()
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


@router_public.get("/api/public/company/{slug}")
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


def _safe_json_loads_list(raw: str) -> list[str]:
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception as e:
        return []


def _safe_json_loads(raw: str, default):
    try:
        return json.loads(raw)
    except Exception as e:
        return default


def _image_average_hash(image: Image.Image, size: int = 8) -> int:
    small = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for idx, px in enumerate(pixels):
        if px >= avg:
            bits |= 1 << idx
    return bits


def _hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def analyze_photo_quality(image_bytes: bytes, photo_index: int) -> dict:
    flags: list[str] = []
    metrics = {
        "brightness": 0.0,
        "contrast": 0.0,
        "edge_mean": 0.0,
        "edge_stddev": 0.0,
        "context_score": 0.0,
    }
    img = None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        gray = img.convert("L")
        stat = ImageStat.Stat(gray)
        brightness = float(stat.mean[0] or 0.0)
        contrast = float(stat.stddev[0] or 0.0)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_stat = ImageStat.Stat(edges)
        edge_mean = float(edge_stat.mean[0] or 0.0)
        edge_std = float(edge_stat.stddev[0] or 0.0)

        metrics.update(
            {
                "brightness": round(brightness, 2),
                "contrast": round(contrast, 2),
                "edge_mean": round(edge_mean, 2),
                "edge_stddev": round(edge_std, 2),
            }
        )

        if brightness < 42 or (brightness < 52 and contrast < 24):
            flags.append("too_dark")
        if edge_mean < 10 and edge_std < 18:
            flags.append("blurry")

        width, height = gray.size
        if width >= 300 and height >= 300:
            cx0 = int(width * 0.2)
            cy0 = int(height * 0.2)
            cx1 = int(width * 0.8)
            cy1 = int(height * 0.8)
            center = edges.crop((cx0, cy0, cx1, cy1))
            center_mean = float(ImageStat.Stat(center).mean[0] or 0.0)
            context_score = edge_mean / max(center_mean, 1.0)
            metrics["context_score"] = round(context_score, 2)
            if context_score < 0.62 and center_mean > 14:
                flags.append("needs_wider_context")
    except Exception as e:
        flags.append("unreadable")
    finally:
        if img is not None:
            try:
                img.close()
            except Exception as e:
                logger.debug("Fallback handled: %s", e)

    return {
        "photo_index": photo_index,
        "flags": flags,
        "metrics": metrics,
    }


def summarize_photo_quality(analyses: list[dict]) -> dict:
    all_flags: list[str] = []
    reasons: list[str] = []
    guidance: list[str] = []
    unique_hashes: list[int] = []
    duplicates: list[int] = []

    for a in analyses:
        for flag in a.get("flags", []):
            all_flags.append(flag)

    photo_count = len(analyses)
    unreadable_count = sum(1 for a in analyses if "unreadable" in a.get("flags", []))
    dark_count = sum(1 for a in analyses if "too_dark" in a.get("flags", []))
    blurry_count = sum(1 for a in analyses if "blurry" in a.get("flags", []))
    context_count = sum(1 for a in analyses if "needs_wider_context" in a.get("flags", []))

    hashes = [a.get("hash") for a in analyses if isinstance(a.get("hash"), int)]
    for idx, h in enumerate(hashes):
        if any(_hamming_distance(h, existing) <= 5 for existing in unique_hashes):
            duplicates.append(idx)
        else:
            unique_hashes.append(h)

    if photo_count == 1:
        all_flags.append("single_photo_only")
        reasons.append("Only one photo was uploaded.")
        guidance.append("Add one wider shot from a different angle to improve accuracy.")

    duplicate_count = len(duplicates)
    if duplicate_count > 0:
        all_flags.append("duplicate_angles")
        reasons.append("Multiple photos appear to show nearly the same angle.")
        guidance.append("Use photos from different angles instead of near-duplicates.")

    if unreadable_count:
        reasons.append(f"{unreadable_count} photo{'s are' if unreadable_count != 1 else ' is'} unreadable.")
    if dark_count:
        reasons.append(f"{dark_count} photo{'s are' if dark_count != 1 else ' is'} too dark.")
        guidance.append("Take photos with better lighting or stand where the pile is brighter.")
    if blurry_count:
        reasons.append(f"{blurry_count} photo{'s are' if blurry_count != 1 else ' is'} too blurry.")
        guidance.append("Hold the phone steady and let the camera focus before taking the shot.")
    if context_count:
        reasons.append(f"{context_count} photo{'s need' if context_count != 1 else ' needs'} a wider view.")
        guidance.append("Include the full pile and some floor around it.")

    severe_photo_count = sum(
        1
        for a in analyses
        if any(flag in a.get("flags", []) for flag in ("unreadable", "too_dark", "blurry"))
    )
    usable_photos = max(0, photo_count - severe_photo_count)

    retry_needed = False
    retry_message = ""
    if photo_count == 0:
        retry_needed = True
        retry_message = "At least one photo is required."
    elif usable_photos <= 0:
        retry_needed = True
        retry_message = "We couldn't get a usable estimate from these photos. Please retake them with better lighting and focus."
    elif photo_count >= 2 and duplicate_count >= photo_count - 1:
        retry_needed = True
        retry_message = "These photos are too similar. Please add a wider shot from a different angle."

    if retry_needed:
        confidence_bucket = "low"
    elif severe_photo_count > 0 or photo_count == 1 or duplicate_count > 0 or context_count > 0:
        confidence_bucket = "medium"
    else:
        confidence_bucket = "high"

    deduped_guidance: list[str] = []
    for line in guidance:
        if line not in deduped_guidance:
            deduped_guidance.append(line)

    return {
        "confidence_bucket": confidence_bucket,
        "flags": sorted(set(all_flags)),
        "reasons": reasons,
        "retry_needed": retry_needed,
        "retry_message": retry_message,
        "guidance": deduped_guidance[:3],
        "usable_photo_count": usable_photos,
        "duplicate_photo_count": duplicate_count,
    }


def _sanitize_customer_input(value: str, max_length: int = 200) -> str:
    """Sanitize customer-provided text fields to prevent stored XSS."""
    if not value:
        return ""
    value = value.strip()[:max_length]
    value = value.replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace('"', "&quot;").replace("'", "&#x27;")
    value = value.replace("&", "&amp;")
    return value


def _sanitize_customer_input(value: str, max_length: int = 200) -> str:
    """Sanitize customer-provided text fields to prevent stored XSS."""
    if not value:
        return ""
    value = value.strip()[:max_length]
    value = value.replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace('"', "&quot;").replace("'", "&#x27;")
    value = value.replace("&", "&amp;")
    return value


def normalize_capture_mode(raw_mode: str | None) -> str:
    mode = str(raw_mode or "").strip().lower()
    return "operator_assist" if mode == "operator_assist" else "remote"


def apply_capture_mode_quality_policy(photo_quality: dict, capture_mode: str) -> dict:
    if capture_mode != "operator_assist":
        return photo_quality

    adjusted = dict(photo_quality)
    flags = list(photo_quality.get("flags", []))
    reasons = list(photo_quality.get("reasons", []))
    guidance = list(photo_quality.get("guidance", []))
    usable_photo_count = int(photo_quality.get("usable_photo_count", 0) or 0)
    duplicate_photo_count = int(photo_quality.get("duplicate_photo_count", 0) or 0)

    if usable_photo_count < 3:
        flags.append("operator_assist_needs_three_angles")
        reasons.append("Operator assist mode needs at least three usable photos.")
        guidance.insert(0, "Capture a wide shot, then left and right angles before submitting.")
        adjusted["retry_needed"] = True
        adjusted["retry_message"] = "Operator assist mode needs 3 usable photos: wide shot, left angle, and right angle."
    elif duplicate_photo_count > 0 and usable_photo_count < 4:
        flags.append("operator_assist_duplicate_angles")
        reasons.append("Operator assist mode detected repeated angles.")
        guidance.insert(0, "Retake one photo from a different angle so the set covers the pile from multiple sides.")
        adjusted["confidence_bucket"] = "medium"

    if adjusted.get("retry_needed"):
        adjusted["confidence_bucket"] = "low"

    deduped_flags = sorted(set(flags))
    deduped_guidance: list[str] = []
    for line in guidance:
        if line and line not in deduped_guidance:
            deduped_guidance.append(line)

    adjusted["flags"] = deduped_flags
    adjusted["reasons"] = reasons[:5]
    adjusted["guidance"] = deduped_guidance[:4]
    return adjusted


async def prepare_estimate_photos(
    files: list[UploadFile],
    rooms_raw: str,
    *,
    max_files: int,
    default_room: str,
    capture_mode: str = "remote",
) -> tuple[list[dict], list, list[str], dict]:
    if not files:
        raise HTTPException(status_code=400, detail="At least one photo is required.")
    if len(files) > max_files:
        raise HTTPException(status_code=400, detail=f"Maximum {max_files} photos allowed.")

    rooms_list = _safe_json_loads_list(rooms_raw)
    allowed_content_types = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
    max_file_size = 20 * 1024 * 1024

    photo_data = []
    analyses = []
    for i, file in enumerate(files):
        if file.content_type and file.content_type.lower() not in allowed_content_types:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} has an unsupported file type. Please upload images only.")
        raw = await file.read()
        if len(raw) > max_file_size:
            raise HTTPException(status_code=400, detail=f"Photo {i+1} exceeds 20MB limit.")
        ct = (file.content_type or "").lower()
        if not validate_magic_bytes(raw, ct):
            raise HTTPException(status_code=400, detail=f"Photo {i+1}: file contents don't match declared type.")

        compressed = compress_image(raw)
        analysis = analyze_photo_quality(compressed, i + 1)
        try:
            img = Image.open(io.BytesIO(compressed))
            img.load()
            analysis["hash"] = _image_average_hash(img)
            img.close()
        except Exception as e:
            analysis["hash"] = None
        analyses.append(analysis)

        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        room_label = rooms_list[i] if i < len(rooms_list) and str(rooms_list[i]).strip() else default_room
        photo_data.append({"b64": b64, "room": room_label, "index": i + 1})

    quality = summarize_photo_quality(analyses)
    quality = apply_capture_mode_quality_policy(quality, normalize_capture_mode(capture_mode))
    quality["photos"] = [
        {
            "photo_index": a["photo_index"],
            "flags": a["flags"],
            "metrics": a["metrics"],
        }
        for a in analyses
    ]

    room_groups = {}
    for pd in photo_data:
        room_groups.setdefault(pd["room"], []).append(pd)

    image_content = []
    for room, group_photos in room_groups.items():
        if len(group_photos) > 1:
            image_content.append({
                "type": "text",
                "text": f"\n--- ROOM: {room} ({len(group_photos)} photos — these show DIFFERENT ANGLES of the SAME space. DO NOT double-count items visible in multiple photos.) ---",
            })
        for angle_idx, pd in enumerate(group_photos, start=1):
            label = f"Photo {pd['index']} (Room: {room})"
            if len(group_photos) > 1:
                label += f" [angle {angle_idx} of {len(group_photos)} for this room]"
            image_content.append({"type": "text", "text": f"{label}:"})
            image_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": pd["b64"]},
            })

    stored_photos = [pd["b64"] for pd in photo_data]
    return photo_data, image_content, stored_photos, quality


SCENE_DISPLAY_NAMES = {
    "curbside_mixed_junk": "Curbside Mixed Junk",
    "garage_clutter": "Garage Clutter",
    "room_interior_furniture": "Room Interior Furniture",
    "bagged_trash_soft_goods": "Bagged Trash / Soft Goods",
    "construction_debris": "Construction Debris",
    "yard_waste_outdoor_pile": "Yard Waste / Outdoor Pile",
    "storage_overflow": "Storage / Basement / Attic Overflow",
    "truck_load": "Truck Load",
    "mixed_junk": "Mixed Junk",
}

_BACKGROUND_FIXTURE_SUBSTRINGS = (
    "shelving",
    "shelving unit",
    "shelf with items",
    "storage shelf",
    "garage shelf",
    "mounted shelf",
    "wall shelf",
)

_DUPLICATE_GROUP_SUBSTRINGS = (
    "bag",
    "box",
    "bucket",
    "bin",
    "tote",
    "crate",
    "container",
    "misc",
    "tool",
)


def _normalized_item_name(raw_name: str) -> str:
    return re.sub(r"\s+", " ", str(raw_name or "").strip().lower())


def _duplicate_base_name(raw_name: str) -> str:
    base = re.sub(r"\(photo\s*\d+\)", "", str(raw_name or ""), flags=re.IGNORECASE)
    return _normalized_item_name(base)


def _scene_context_text(result_data: dict, room_labels: list[str]) -> str:
    labels = " ".join(str(label or "") for label in room_labels)
    notes = str(result_data.get("notes", "") or "")
    return f"{labels} {notes}".lower()


def _small_job_group_total_cy(item: dict) -> float:
    if not isinstance(item, dict):
        return 0.0
    norm_name = _normalized_item_name(item.get("name", ""))
    qty = max(1, int(item.get("quantity") or 1))
    total_cy = max(0.0, float(item.get("cubic_yards") or 0.0) * qty)
    if "bag" in norm_name and ("trash" in norm_name or "garbage" in norm_name):
        return min(total_cy, qty * 0.30)
    if "paint" in norm_name and ("bucket" in norm_name or "can" in norm_name):
        return min(total_cy, qty * 0.03)
    return total_cy


def filter_actionable_duplicates(result_data: dict) -> list[dict]:
    duplicates = result_data.get("potential_duplicates", []) or []
    items = result_data.get("items", []) or []
    if not isinstance(duplicates, list) or not duplicates or not isinstance(items, list):
        return []

    item_map: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        norm_name = _normalized_item_name(item.get("name", ""))
        if norm_name and norm_name not in item_map:
            item_map[norm_name] = item

    actionable: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    for dup in duplicates:
        if not isinstance(dup, dict):
            continue
        base_a = _duplicate_base_name(dup.get("item_a", ""))
        base_b = _duplicate_base_name(dup.get("item_b", ""))
        bases = tuple(sorted([base_a, base_b]))
        if not base_a or not base_b or bases in seen_pairs:
            continue
        seen_pairs.add(bases)

        candidate_item = None
        for base in (base_a, base_b):
            if base in item_map:
                candidate_item = item_map[base]
                break
            for item_name, item in item_map.items():
                if base in item_name or item_name in base:
                    candidate_item = item
                    break
            if candidate_item:
                break

        if not candidate_item:
            continue

        qty = max(1, int(candidate_item.get("quantity") or 1))
        if qty <= 1:
            continue

        actionable.append({
            "item_a": dup.get("item_a", ""),
            "item_b": dup.get("item_b", ""),
            "reason": dup.get("reason", ""),
        })

    return actionable


def _sync_result_totals_to_items(result_data: dict) -> float:
    items = result_data.get("items", []) or []
    item_sum = 0.0
    for item in items:
        try:
            item_sum += max(0.0, float(item.get("cubic_yards") or 0.0)) * max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            continue
    item_sum = round(item_sum, 2)
    if item_sum > 0:
        result_data.setdefault("totals", {})
        result_data["totals"]["cubic_yards_mid"] = round(item_sum, 1)
        result_data["totals"]["cubic_yards_low"] = round(item_sum * 0.85, 1)
        result_data["totals"]["cubic_yards_high"] = round(item_sum * 1.15, 1)
        result_data["total_cubic_yards"] = round(item_sum, 1)
    return item_sum


def apply_visual_estimate_guardrails(result_data: dict, room_labels: list[str]) -> tuple[dict, list[str]]:
    items = result_data.get("items", []) or []
    if not isinstance(items, list) or not items:
        return result_data, []

    labels = " ".join(room_labels).lower()
    garage_like = any(k in labels for k in ("garage", "basement", "attic", "shed", "storage"))
    duplicate_bases = set()
    for dup in result_data.get("potential_duplicates", []) or []:
        if not isinstance(dup, dict):
            continue
        duplicate_bases.add(_duplicate_base_name(dup.get("item_a", "")))
        duplicate_bases.add(_duplicate_base_name(dup.get("item_b", "")))
    duplicate_bases = {name for name in duplicate_bases if name}

    filtered_items = []
    notes: list[str] = []
    removed_fixture_names: list[str] = []
    dedupe_notes: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "")
        norm_name = _normalized_item_name(name)
        qty = max(1, int(item.get("quantity") or 1))
        photo_sources = item.get("photo_sources", []) or []

        if garage_like and any(substr in norm_name for substr in _BACKGROUND_FIXTURE_SUBSTRINGS):
            removed_fixture_names.append(name or "shelving")
            continue

        if any(norm_name == base or norm_name in base or base in norm_name for base in duplicate_bases):
            if any(substr in norm_name for substr in _DUPLICATE_GROUP_SUBSTRINGS) and qty > 1:
                new_qty = max(1, math.ceil(qty / 2))
            elif len(photo_sources) >= 2 and qty > 1:
                new_qty = 1
            else:
                new_qty = qty
            if new_qty != qty:
                item["quantity"] = new_qty
                item["dedup_note"] = f"Reduced from {qty} to {new_qty} because the same item group appears across multiple photos."
                dedupe_notes.append(f"{name}: counted once across multiple angles.")

        filtered_items.append(item)

    if removed_fixture_names:
        unique_removed = []
        for name in removed_fixture_names:
            if name not in unique_removed:
                unique_removed.append(name)
        notes.append("Background shelving/storage was excluded unless clearly staged for removal: " + ", ".join(unique_removed[:3]) + ".")

    if dedupe_notes:
        unique_dedupe = []
        for note in dedupe_notes:
            if note not in unique_dedupe:
                unique_dedupe.append(note)
        notes.append("Duplicate-angle guardrail applied: " + " ".join(unique_dedupe[:2]))

    result_data["items"] = filtered_items
    if notes:
        existing_notes = str(result_data.get("notes", "") or "").strip()
        result_data["notes"] = (existing_notes + "\n" if existing_notes else "") + " ".join(notes)
    return result_data, notes


def apply_small_job_volume_guardrails(result_data: dict, scene_type: str, room_labels: list[str]) -> tuple[dict, list[str]]:
    items = result_data.get("items", []) or []
    if not isinstance(items, list) or not items:
        return result_data, []

    context = _scene_context_text(result_data, room_labels)
    garage_like = any(k in context for k in ("garage", "basement", "storage", "shed"))
    if scene_type not in {"garage_clutter", "bagged_trash_soft_goods", "mixed_junk", "storage_overflow"} and not garage_like:
        return result_data, []

    notes: list[str] = []
    adjusted = False
    for item in items:
        if not isinstance(item, dict):
            continue
        norm_name = _normalized_item_name(item.get("name", ""))
        qty = max(1, int(item.get("quantity") or 1))
        current_cy = max(0.0, float(item.get("cubic_yards") or 0.0))
        current_total = current_cy * qty

        if "bag" in norm_name and ("trash" in norm_name or "garbage" in norm_name):
            corrected_cy = round(min(current_total, qty * 0.30) / qty, 3)
            if corrected_cy < current_cy:
                item["cubic_yards"] = corrected_cy
                adjusted = True
        elif "paint" in norm_name and ("bucket" in norm_name or "can" in norm_name):
            corrected_cy = round(min(current_total, qty * 0.03) / qty, 3)
            if corrected_cy < current_cy:
                item["cubic_yards"] = corrected_cy
                adjusted = True

    if adjusted:
        corrected_sum = sum(_small_job_group_total_cy(item) for item in items if isinstance(item, dict))
        result_data.setdefault("totals", {})
        result_data["totals"]["cubic_yards_mid"] = round(corrected_sum, 1)
        result_data["totals"]["cubic_yards_low"] = round(corrected_sum * 0.85, 1)
        result_data["totals"]["cubic_yards_high"] = round(corrected_sum * 1.15, 1)
        result_data["total_cubic_yards"] = round(corrected_sum, 1)
        notes.append("Small-job guardrail reduced grouped bag/container volumes to field-calibrated per-item ranges.")

    if notes:
        existing_notes = str(result_data.get("notes", "") or "").strip()
        result_data["notes"] = (existing_notes + "\n" if existing_notes else "") + " ".join(notes)
    return result_data, notes


def normalize_curbside_mixed_item_labels(result_data: dict, room_labels: list[str]) -> tuple[dict, list[str]]:
    items = result_data.get("items", []) or []
    if not isinstance(items, list) or not items:
        return result_data, []

    context = _scene_context_text(result_data, room_labels)
    outdoor_like = any(k in context for k in ("outdoor", "curb", "curbside", "driveway"))
    if not outdoor_like:
        return result_data, []

    notes: list[str] = []
    has_broken_wood_furniture = any(
        isinstance(item, dict) and any(k in _normalized_item_name(item.get("name", "")) for k in ("broken wooden furniture", "broken wood furniture"))
        for item in items
    )
    has_wood_debris = any(
        isinstance(item, dict) and any(k in _normalized_item_name(item.get("name", "")) for k in ("lumber", "wood debris"))
        for item in items
    )

    if has_broken_wood_furniture and has_wood_debris:
        for item in items:
            if not isinstance(item, dict):
                continue
            norm_name = _normalized_item_name(item.get("name", ""))
            if "lumber" in norm_name or "wood debris" in norm_name:
                item["name"] = "wood debris pieces"
            elif "broken wooden furniture" in norm_name or "broken wood furniture" in norm_name:
                item["name"] = "broken wood furniture pieces"
        notes.append("Outdoor mixed wood pile labels were normalized to avoid over-classifying curbside junk as construction debris.")

    if notes:
        existing_notes = str(result_data.get("notes", "") or "").strip()
        result_data["notes"] = (existing_notes + "\n" if existing_notes else "") + " ".join(notes)
    return result_data, notes


def normalize_special_fee_items(result_data: dict) -> tuple[dict, list[str]]:
    items = result_data.get("items", []) or []
    if not isinstance(items, list) or not items:
        return result_data, []

    notes: list[str] = []
    adjusted = False
    for item in items:
        if not isinstance(item, dict):
            continue
        norm_name = _normalized_item_name(item.get("name", ""))
        if not item.get("is_special"):
            continue

        generic_bucket = (
            ("bucket" in norm_name or "5-gallon" in norm_name or "5 gallon" in norm_name)
            and "paint can" not in norm_name
            and "paint cans" not in norm_name
            and "residue" not in norm_name
            and "full" not in norm_name
            and "hazard" not in norm_name
        )
        if generic_bucket:
            item["is_special"] = False
            adjusted = True

    if adjusted:
        notes.append("Generic buckets were not flagged for special disposal unless visible paint cans or paint residue were clearly identified.")

    if notes:
        existing_notes = str(result_data.get("notes", "") or "").strip()
        result_data["notes"] = (existing_notes + "\n" if existing_notes else "") + " ".join(notes)
    return result_data, notes


def _normalize_room_labels(photo_data: list[dict]) -> list[str]:
    out = []
    for pd in photo_data:
        room = str(pd.get("room", "") or "").strip()
        if room:
            out.append(room)
    return out


def infer_capture_scene_hint(room_labels: list[str], truck_load_pct: Optional[float] = None) -> str:
    labels = " ".join(room_labels).lower()
    if truck_load_pct is not None or "truck load" in labels:
        return "truck_load"
    if any(k in labels for k in ("garage",)):
        return "garage_clutter"
    if any(k in labels for k in ("basement", "attic", "shed", "storage")):
        return "storage_overflow"
    if any(k in labels for k in ("outdoor", "yard", "curb", "driveway")):
        return "curbside_mixed_junk"
    if any(k in labels for k in ("living room", "bedroom", "kitchen", "bathroom", "office", "dining")):
        return "room_interior_furniture"
    return ""


def build_scene_prompt_hint(scene_hint: str) -> str:
    hints = {
        "truck_load": "Scene hint: this is a truck-load style estimate. Use truck-load context if visible, but still estimate from the actual visible items.",
        "garage_clutter": "Scene hint: this appears to be garage clutter. Expect mixed household items, shelving, tools, bins, and stacked storage.",
        "storage_overflow": "Scene hint: this appears to be a basement, attic, shed, or storage-overflow job. Watch for dense clutter and occluded items.",
        "curbside_mixed_junk": "Scene hint: this appears to be an outdoor or curbside mixed-junk pile. Use visible pile boundaries and outdoor context carefully.",
        "room_interior_furniture": "Scene hint: this appears to be an interior room furniture cleanout. Expect bulky items with air gaps.",
    }
    return hints.get(scene_hint, "")


def classify_scene_type(
    result_data: dict,
    room_labels: list[str],
    truck_load_pct: Optional[float] = None,
) -> str:
    context = _scene_context_text(result_data, room_labels)
    labels = " ".join(room_labels).lower()
    items = result_data.get("items", []) or []
    names = " ".join(str(it.get("name", "") or "").lower() for it in items)
    job_type = str(result_data.get("job_type", "") or "").lower()
    conditions = {str(c).lower() for c in (result_data.get("conditions", []) or [])}
    outdoor_like = any(k in context for k in ("outdoor", "yard", "curb", "driveway"))

    if truck_load_pct is not None or job_type == "truck_load" or "truck load" in context:
        return "truck_load"

    yard_keywords = ("branch", "brush", "yard", "tree", "limb", "leaves", "mulch", "stump")
    construction_keywords = (
        "drywall", "sheetrock", "tile", "lumber", "countertop",
        "demolition", "construction", "debris", "framing", "fence", "railroad tie",
        "shingle", "roofing", "carpet", "pad", "plywood", "osb",
    )
    bag_keywords = ("bag", "trash bag", "garbage bag", "clothes", "clothing", "linen", "soft", "box", "bin")
    furniture_keywords = (
        "couch", "sofa", "loveseat", "sectional", "recliner", "chair", "table",
        "dresser", "desk", "bed", "mattress", "box spring", "nightstand", "bookshelf",
    )

    if any(k in names for k in yard_keywords) and outdoor_like:
        return "yard_waste_outdoor_pile"
    construction_hits = sum(1 for k in construction_keywords if k in names)
    furniture_hits = sum(1 for k in furniture_keywords if k in names)
    if any(k in context for k in ("garage",)) and construction_hits < 2:
        return "garage_clutter"
    if outdoor_like and furniture_hits > 0 and construction_hits < 3:
        return "curbside_mixed_junk"
    if any(k in names for k in construction_keywords):
        return "construction_debris"
    if job_type == "hoarder" or "hoarder" in conditions:
        return "storage_overflow" if any(k in context for k in ("basement", "attic", "shed", "storage")) else "bagged_trash_soft_goods"
    if sum(1 for it in items if any(k in str(it.get("name", "")).lower() for k in bag_keywords)) >= 3:
        return "bagged_trash_soft_goods"
    if any(k in context for k in ("garage",)):
        return "garage_clutter"
    if any(k in context for k in ("basement", "attic", "shed", "storage")):
        return "storage_overflow"
    if outdoor_like:
        return "curbside_mixed_junk"
    if any(k in names for k in furniture_keywords) or any(k in context for k in ("living room", "bedroom", "kitchen", "bathroom", "office", "dining")):
        return "room_interior_furniture"
    return "mixed_junk"


def apply_scene_confidence_policy(
    result_data: dict,
    photo_quality: dict,
    scene_type: str,
    room_labels: list[str],
) -> tuple[str, list[str], int]:
    confidence_bucket = str(photo_quality.get("confidence_bucket", "medium") or "medium")
    reasons = list(photo_quality.get("reasons", []) or [])
    confidence = int(result_data.get("confidence", 75) or 75)
    labels = " ".join(room_labels).lower()
    job_type = str(result_data.get("job_type", "") or "").lower()

    if scene_type in {"garage_clutter", "storage_overflow", "bagged_trash_soft_goods"} and confidence_bucket == "high":
        confidence_bucket = "medium"
        reasons.append(f"Scene classified as {SCENE_DISPLAY_NAMES.get(scene_type, scene_type).lower()}, which usually hides some items.")

    if scene_type == "construction_debris":
        reasons.append("Scene classified as construction debris, which can stack tighter than its footprint suggests.")
    elif scene_type == "room_interior_furniture":
        reasons.append("Scene classified as interior furniture, where bulky items create air gaps and occlusion.")
    elif scene_type == "curbside_mixed_junk":
        reasons.append("Scene classified as curbside mixed junk based on outdoor pile context.")
    elif scene_type == "truck_load":
        reasons.append("Scene classified as truck load from capture context and job type.")

    if job_type in {"hoarder", "truck_load"} and confidence_bucket == "high":
        confidence_bucket = "medium"
        reasons.append(f"Job type {job_type} increases estimate uncertainty.")

    if "garage" in labels and scene_type != "garage_clutter":
        reasons.append("Room labels include garage context.")

    if confidence_bucket == "high":
        confidence = max(confidence, 80)
    elif confidence_bucket == "medium":
        confidence = min(max(confidence, 64), 79)
    else:
        confidence = min(confidence, 64)

    deduped = []
    for reason in reasons:
        if reason and reason not in deduped:
            deduped.append(reason)
    return confidence_bucket, deduped[:4], confidence


def apply_job_label_guardrails(result_data: dict, scene_type: str, room_labels: list[str]) -> tuple[dict, str]:
    labels = " ".join(room_labels).lower()
    item_sum = _sync_result_totals_to_items(result_data)
    job_type = str(result_data.get("job_type", "") or "").lower()
    conditions = [str(c).lower() for c in (result_data.get("conditions", []) or [])]
    items = result_data.get("items", []) or []
    names = " ".join(_normalized_item_name(it.get("name", "")) for it in items if isinstance(it, dict))
    bag_like_count = sum(
        1 for it in items
        if isinstance(it, dict) and any(k in _normalized_item_name(it.get("name", "")) for k in ("bag", "bucket", "bin", "crate"))
    )

    if job_type == "hoarder" and item_sum < 12:
        result_data["job_type"] = "standard"
    if "hoarder" in conditions and item_sum < 12:
        result_data["conditions"] = [c for c in conditions if c != "hoarder"]

    if scene_type == "construction_debris":
        construction_hits = sum(1 for k in ("drywall", "sheetrock", "tile", "lumber", "demolition", "construction", "debris", "framing", "shingle", "roofing", "plywood", "osb") if k in names)
        if any(k in labels for k in ("garage", "basement", "storage")) and construction_hits < 2:
            scene_type = "garage_clutter" if bag_like_count < 3 else "bagged_trash_soft_goods"

    if item_sum < 8 and result_data.get("job_type") == "truck_load":
        result_data["job_type"] = "standard"

    _sync_result_totals_to_items(result_data)
    return result_data, scene_type


def widen_price_range_for_confidence(
    price_low: float,
    price_high: float,
    min_charge: float,
    confidence_bucket: str,
    scene_type: str,
) -> tuple[float, float, bool]:
    extra_pct = 0.0
    if confidence_bucket == "medium":
        extra_pct += 0.08
    elif confidence_bucket == "low":
        extra_pct += 0.15

    if scene_type in {"garage_clutter", "storage_overflow", "room_interior_furniture"}:
        extra_pct += 0.03
    elif scene_type in {"construction_debris", "bagged_trash_soft_goods"}:
        extra_pct += 0.02

    if extra_pct <= 0:
        return price_low, price_high, False

    mid = (price_low + price_high) / 2.0
    half = max((price_high - price_low) / 2.0, max(mid * 0.05, 10.0))
    widened_half = half * (1.0 + extra_pct)
    widened_low = max(min_charge, round(mid - widened_half, 2))
    widened_high = max(widened_low, round(mid + widened_half, 2))
    return widened_low, widened_high, widened_low != price_low or widened_high != price_high


def _model_uncertainty_pct(confidence_bucket: str, scene_type: str, num_photos: int) -> float:
    uncertain_scene = scene_type in {
        "garage_clutter",
        "storage_overflow",
        "construction_debris",
        "mixed_junk",
        "bagged_trash_soft_goods",
    }
    if confidence_bucket in {"low", "medium"} or num_photos <= 2 or uncertain_scene:
        return 0.25
    return 0.15


def _expand_model_range(price_low: float, price_high: float, min_charge: float, pct: float) -> tuple[float, float]:
    low = max(min_charge, round(price_low * (1.0 - pct), 2))
    high = max(low, round(price_high * (1.0 + pct), 2))
    return low, high


def _price_overlap(a_low: float, a_high: float, b_low: float, b_high: float) -> tuple[float, float, bool]:
    low = max(a_low, b_low)
    high = min(a_high, b_high)
    return low, high, high >= low


async def _get_lightweight_price_calibration(scene_type: str, capture_mode: str) -> Optional[dict]:
    st = str(scene_type or "").strip().lower()
    if not st:
        return None
    cm = normalize_capture_mode(capture_mode)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=180)
    async with AsyncSessionLocal() as db:
        query = (
            select(Estimate.price_low, Estimate.price_high, Estimate.actual_price)
            .where(
                Estimate.scene_type == st,
                Estimate.actual_price.isnot(None),
                Estimate.price_low.isnot(None),
                Estimate.price_high.isnot(None),
                Estimate.created_at >= cutoff,
            )
            .order_by(Estimate.created_at.desc())
            .limit(250)
        )
        if cm:
            query = query.where(Estimate.capture_mode == cm)
        rows = (await db.execute(query)).all()

    ratios = []
    for low, high, actual in rows:
        try:
            mid = (float(low or 0) + float(high or 0)) / 2.0
            act = float(actual or 0)
        except Exception as e:
            continue
        if mid <= 0 or act <= 0:
            continue
        ratio = act / mid
        # Trim severe outliers to keep this lightweight and stable.
        if 0.70 <= ratio <= 1.80:
            ratios.append(ratio)
    if len(ratios) < 10:
        return None
    med = float(statistics.median(ratios))
    if med <= 1.05:
        return None
    factor = min(1.15, max(1.03, med))
    return {
        "factor": round(factor, 3),
        "sample_size": len(ratios),
        "median_ratio": round(med, 3),
    }


def _parse_spatial_total_from_notes(notes: str) -> Optional[float]:
    if not notes or not str(notes).strip():
        return None
    text = str(notes)
    eq_matches = re.findall(
        r"=\s*(\d+(?:\.\d+)?)\s*(?:CY|cubic\s*yards?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if eq_matches:
        try:
            return float(eq_matches[-1])
        except ValueError:
            return None
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(?:CY|cubic\s*yards?)\b", text, flags=re.IGNORECASE)
    if matches:
        try:
            return float(matches[-1])
        except ValueError:
            return None
    return None


def evaluate_geometry_sanity(
    result_data: dict,
    scene_type: str,
    room_labels: list[str],
    truck_load_pct: Optional[float],
    truck_capacity_cy: float,
) -> dict:
    items = result_data.get("items", []) or []
    item_sum = 0.0
    bulky_count = 0
    for item in items:
        try:
            item_cy = float(item.get("cubic_yards") or 0.0) * max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            item_cy = 0.0
        item_sum += max(0.0, item_cy)
        if item_cy >= 1.0:
            bulky_count += 1

    spatial_total = _parse_spatial_total_from_notes(result_data.get("notes", ""))
    truck_total = None
    if truck_load_pct is not None and truck_capacity_cy:
        truck_total = round((truck_load_pct / 100.0) * truck_capacity_cy, 2)

    occupancy_class = "balanced"
    flags: list[str] = []
    summary_parts: list[str] = []

    if item_sum > 0:
        if bulky_count >= 3 and item_sum / max(bulky_count, 1) > 0.9:
            occupancy_class = "bulky"
        elif scene_type in {"construction_debris", "bagged_trash_soft_goods"} and bulky_count <= 1:
            occupancy_class = "tight"
        elif scene_type in {"garage_clutter", "storage_overflow"} and item_sum < 4:
            occupancy_class = "sparse"

    if spatial_total and item_sum > 0:
        ratio = spatial_total / item_sum if item_sum else 1.0
        if ratio >= 1.9:
            flags.append("spatial_above_items")
            summary_parts.append(f"Spatial note suggests {spatial_total:.1f} CY while identified items sum to {item_sum:.1f} CY.")
        elif ratio <= 0.65:
            flags.append("items_above_spatial")
            summary_parts.append(f"Identified items sum to {item_sum:.1f} CY while note math suggests {spatial_total:.1f} CY.")

    if truck_total and item_sum > 0:
        ratio = item_sum / truck_total if truck_total else 1.0
        if ratio < 0.6:
            flags.append("items_below_truck_hint")
            summary_parts.append(f"Truck-load hint implies about {truck_total:.1f} CY but visible items only total {item_sum:.1f} CY.")
        elif ratio > 1.45:
            flags.append("items_above_truck_hint")
            summary_parts.append(f"Visible items total {item_sum:.1f} CY against a truck-load hint of about {truck_total:.1f} CY.")

    adjusted_total = round(item_sum, 2)
    applied_adjustment = False
    if truck_total and scene_type == "truck_load" and item_sum > 0:
        if item_sum < truck_total * 0.6:
            adjusted_total = round(max(item_sum, truck_total * 0.7), 2)
            flags.append("raised_toward_truck_hint")
            applied_adjustment = adjusted_total > item_sum
        elif item_sum > truck_total * 1.45:
            adjusted_total = round(min(item_sum, truck_total * 1.2), 2)
            flags.append("trimmed_toward_truck_hint")
            applied_adjustment = adjusted_total < item_sum

    deduped_flags: list[str] = []
    for flag in flags:
        if flag not in deduped_flags:
            deduped_flags.append(flag)

    summary = " ".join(summary_parts[:2]).strip()
    if not summary and item_sum > 0:
        summary = f"Scene appears {occupancy_class} with {item_sum:.1f} CY across identified items."

    return {
        "item_sum": round(item_sum, 2),
        "spatial_total": round(spatial_total, 2) if spatial_total else None,
        "truck_total": truck_total,
        "occupancy_class": occupancy_class,
        "sanity_flags": deduped_flags,
        "geometry_summary": summary,
        "adjusted_total": adjusted_total,
        "applied_adjustment": applied_adjustment,
    }


def parse_clarification_answers(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug("Fallback handled: %s", e)
    return {}


def _has_truthy_answer(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        cleaned = value.strip().lower()
        return cleaned not in {"", "unknown", "not sure", "unsure", "skip", "none", "n/a"}
    if isinstance(value, list):
        return any(_has_truthy_answer(v) for v in value)
    if isinstance(value, dict):
        return any(_has_truthy_answer(v) for v in value.values())
    return True


def apply_fail_safe_estimate_rules(
    result_data: dict,
    scene_type: str,
    room_labels: list[str],
    num_photos: int,
    truck_load_pct: Optional[float],
) -> tuple[dict, list[str]]:
    notes: list[str] = []
    totals = result_data.get("totals", {}) or {}
    item_sum = _sync_result_totals_to_items(result_data)
    cy_mid = float(totals.get("cubic_yards_mid", item_sum) or item_sum)
    labels = " ".join(room_labels or []).lower()
    scene_context = _scene_context_text(result_data, room_labels)
    garage_like = any(k in labels for k in ("garage", "basement", "storage", "shed")) or any(
        k in scene_context for k in ("garage", "basement", "storage", "shed")
    )
    bag_qty = 0
    for item in result_data.get("items", []) or []:
        nm = _normalized_item_name(item.get("name", ""))
        if any(k in nm for k in ("trash bag", "bags", "contractor bag", "garbage bag")):
            try:
                bag_qty += max(1, int(item.get("quantity") or 1))
            except Exception as e:
                bag_qty += 1

    # Hard cap impossible totals for small/partial garage storage views without truck context.
    if garage_like and num_photos <= 3 and truck_load_pct is None and cy_mid > 10.0:
        capped = 10.0
        result_data["totals"]["cubic_yards_mid"] = round(capped, 1)
        result_data["totals"]["cubic_yards_low"] = round(capped * 0.85, 1)
        result_data["totals"]["cubic_yards_high"] = round(capped * 1.15, 1)
        result_data["total_cubic_yards"] = round(capped, 1)
        notes.append("Applied fail-safe cap for partial garage/storage visibility.")

    # Bag-heavy scenes should stay in a realistic range relative to visible bag count.
    if bag_qty > 0:
        bag_cap = max(3.0, bag_qty * 0.7 + 2.0)
        current_mid = float(result_data.get("totals", {}).get("cubic_yards_mid", 0) or 0)
        if current_mid > bag_cap and scene_type in {"garage_clutter", "bagged_trash_soft_goods", "mixed_junk"}:
            result_data["totals"]["cubic_yards_mid"] = round(bag_cap, 1)
            result_data["totals"]["cubic_yards_low"] = round(bag_cap * 0.85, 1)
            result_data["totals"]["cubic_yards_high"] = round(bag_cap * 1.15, 1)
            result_data["total_cubic_yards"] = round(bag_cap, 1)
            notes.append("Applied bag-count consistency cap before pricing.")

    # Never keep aggressive labels unless clear threshold conditions are met.
    job_type = str(result_data.get("job_type", "") or "").lower()
    if job_type in {"hoarder", "whole_house", "whole house"}:
        if truck_load_pct is None and (num_photos < 6 or _sync_result_totals_to_items(result_data) < 14.0):
            result_data["job_type"] = "standard"
            notes.append("Downgraded aggressive job label pending stronger evidence.")
    return result_data, notes


def build_required_clarification_questions(
    result_data: dict,
    scene_type: str,
    actionable_duplicates: list[dict],
    sanity_flags: list[str],
) -> list[dict]:
    questions: list[dict] = []
    if actionable_duplicates:
        questions.append({
            "id": "duplicate_items",
            "question": "Are these duplicate-tagged items separate pieces or the same items from different angles?",
            "type": "single_choice",
            "options": ["same_items", "separate_items", "not_sure"],
        })

    fixture_like = any(
        isinstance(it, dict) and any(k in _normalized_item_name(it.get("name", "")) for k in ("shelf", "shelving", "cabinet", "rack", "storage unit"))
        for it in (result_data.get("items", []) or [])
    )
    if fixture_like and scene_type in {"garage_clutter", "storage_overflow"}:
        questions.append({
            "id": "fixtures_included",
            "question": "Should built-in or background shelving/storage fixtures be included for removal?",
            "type": "single_choice",
            "options": ["exclude_background_fixtures", "include_all_visible", "not_sure"],
        })

    if any(flag in sanity_flags for flag in ("spatial_above_items", "items_above_spatial", "items_above_truck_hint", "items_below_truck_hint")):
        questions.append({
            "id": "scope_confirmation",
            "question": "Does this photo set show everything being removed, or only part of the job?",
            "type": "single_choice",
            "options": ["full_scope_shown", "partial_scope_only", "not_sure"],
        })

    return questions[:3]


@router_public.post("/api/public/estimate/{slug}")
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
    clarification_answers_json: str = Form(default=""),
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
            free_remaining = max(0, 5 - (cu.free_trial_used or 0))
            is_admin = cu.is_admin or (cu.estimates_limit or 0) >= 999
            if (cu.credit_balance or 0) <= 0 and free_remaining <= 0 and not is_admin:
                raise HTTPException(status_code=403, detail="This estimator is temporarily unavailable. Please contact the company directly.")
            company_user = cu

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service unavailable")

    photo_data, image_content, stored_photos, photo_quality = await prepare_estimate_photos(
        files,
        rooms,
        max_files=10,
        default_room="Main",
    )
    if photo_quality["retry_needed"]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "retry_needed",
                "message": photo_quality["retry_message"],
                "photo_quality": photo_quality,
            },
        )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT id, email, is_admin, estimates_limit, credit_balance, free_trial_used, company_name, company_phone FROM users WHERE id = :uid FOR UPDATE"),
            {"uid": company_user.id}
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")
        free_remaining = max(0, 5 - (row["free_trial_used"] or 0))
        is_admin = row["is_admin"] or (row["estimates_limit"] or 0) >= 999
        if (row["credit_balance"] or 0) <= 0 and free_remaining <= 0 and not is_admin:
            raise HTTPException(status_code=403, detail="This estimator is temporarily unavailable. Please contact the company directly.")
        if free_remaining > 0 and (row["credit_balance"] or 0) <= 0 and not is_admin:
            await db.execute(
                text("UPDATE users SET free_trial_used = COALESCE(free_trial_used, 0) + 1, estimates_used = COALESCE(estimates_used, 0) + 1 WHERE id = :uid"),
                {"uid": company_user.id}
            )
            new_trial_count = free_remaining - 1
            txn = CreditTransaction(
                user_id=company_user.id,
                transaction_type="free_trial",
                credits=-1,
                balance_after=row["credit_balance"] or 0,
                description=f"Free Trial Widget Estimate #{new_trial_count + 1}",
            )
        else:
            await db.execute(
                text("UPDATE users SET credit_balance = credit_balance - 1, credits_used_total = COALESCE(credits_used_total, 0) + 1, estimates_used = COALESCE(estimates_used, 0) + 1 WHERE id = :uid"),
                {"uid": company_user.id}
            )
            new_balance = (row["credit_balance"] or 0) - 1
            txn = CreditTransaction(
                user_id=company_user.id,
                transaction_type="usage",
                credits=-1,
                balance_after=new_balance,
                description="Widget Estimate",
            )
        db.add(txn)
        await db.commit()

    _check_user_rate_limit(company_user.id)
    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": company_user.id,
        "estimate_name": f"Customer: {_sanitize_customer_input(customer_name) or 'Walk-in'}",
        "customer_name": _sanitize_customer_input(customer_name),
        "customer_email": _sanitize_customer_input(customer_email, max_length=254),
        "customer_phone": _sanitize_customer_input(customer_phone, max_length=30),
        "preferred_contact": _sanitize_customer_input(preferred_contact, max_length=20),
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "stored_photos": stored_photos,
        "company_email": company_user.email,
        "company_name": company_user.company_name or "Junk Removal Company",
        "company_phone": company_user.company_phone or "",
        "capture_mode": "remote",
        "clarification_answers": parse_clarification_answers(clarification_answers_json),
        "photo_quality": photo_quality,
        "room_labels": _normalize_room_labels(photo_data),
        "truck_load_pct": None,
        "review_mode": "self_serve_clarify",
        "used_free_trial": free_remaining > 0 and (row["credit_balance"] or 0) <= 0,
    }
    await _upsert_job_to_db(job_id, company_user.id, 0, "analyzing")
    _record_user_estimate(company_user.id)

    asyncio.create_task(run_estimate(
        job_id=job_id,
        user=company_user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


@router_public.get("/api/public/estimate/status/{job_id}")
@limiter.limit("120/minute")
async def public_estimate_status(request: Request, job_id: str):
    """Public status check — no auth, but limited response."""
    job = estimate_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    if job["status"] in {"complete", "needs_review"} and job.get("result"):
        r = job["result"]
        if job["status"] == "needs_review":
            return {
                "status": "needs_review",
                "message": job["message"],
                "result": {
                    "id": r.get("id"),
                    "scene_label": r.get("scene_label", ""),
                    "confidence_bucket": r.get("confidence_bucket", "low"),
                    "confidence_reasons": r.get("confidence_reasons", []),
                    "clarification_questions": r.get("clarification_questions", []),
                },
            }

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
                "confidence_bucket": r.get("confidence_bucket", ""),
                "confidence_reasons": r.get("confidence_reasons", []),
                "photo_quality_flags": r.get("photo_quality_flags", []),
                "photo_guidance": r.get("photo_guidance", []),
                "capture_mode": r.get("capture_mode", "remote"),
                "scene_type": r.get("scene_type", ""),
                "scene_label": r.get("scene_label", ""),
                "range_widened": r.get("range_widened", False),
                "occupancy_class": r.get("occupancy_class", ""),
                "sanity_flags": r.get("sanity_flags", []),
                "geometry_summary": r.get("geometry_summary", ""),
                "review_status": r.get("review_status", "auto_approved"),
                "review_reason": r.get("review_reason", ""),
                "special_items": r.get("special_items", []),
                "min_charge_applied": r.get("min_charge_applied", False),
                "potential_duplicates": r.get("potential_duplicates", []),
                "photos": stored_photos,
            }
        }
    if job["status"] == "retry_needed":
        msg = job["message"]
        del estimate_jobs[job_id]
        await _delete_job_from_db(job_id)
        return {"status": "retry_needed", "message": msg, "result": None}
    return {"status": job["status"], "message": job["message"], "result": None}


@router_public.post("/api/public/appointment-request")
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
        est.appointment_requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
            except Exception as e:
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
            except Exception as e:
                pass  # Don't fail if email fails

    return {"ok": True, "message": "Appointment request submitted"}


@router_auth.post("/api/auth/signup")
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
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
        )
        db.add(sess)
        await db.commit()

    send_email(
        email,
        "Welcome to WhatShouldICharge!",
        f"<h2>Welcome, {company_name or 'there'}!</h2>"
        "<p>You have <strong>5 free estimates</strong> to try out the platform.</p>"
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


@router_auth.post("/api/auth/login")
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
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
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


@router_auth.post("/api/auth/forgot-password")
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
            return JSONResponse({"success": True, "message": "If an account with that email exists, a reset link has been sent."})

        reset_token = secrets.token_urlsafe(32)
        token_hash = await asyncio.to_thread(
            lambda: bcrypt.hashpw(reset_token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        )

        db.add(PasswordReset(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        ))
        await db.commit()

        app_url = os.environ.get("APP_URL", "https://whatshouldicharge.app")
        reset_link = f"{app_url}/reset-password?token={reset_token}&email={email}"

        send_email(
            email,
            "Reset Your WhatShouldICharge Password",
            f"<h2>Password Reset Request</h2>"
            f"<p>We received a request to reset your password. Click the link below to set a new password:</p>"
            f"<div style='margin:24px 0;text-align:center;'>"
            f"<a href='{reset_link}' style='background:#2563eb;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;'>Reset Password</a>"
            f"</div>"
            f"<p style='color:#666;font-size:14px;'>Or copy this link: {reset_link}</p>"
            f"<p style='color:#666;font-size:14px;'>This link will expire in 1 hour. If you did not request this reset, you can safely ignore this email.</p>"
            f"<p>— The WhatShouldICharge Team</p>"
        )

    return JSONResponse({"success": True, "message": "If an account with that email exists, a reset link has been sent."})


@router_auth.post("/api/auth/logout")
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


@router_auth.post("/api/auth/reset-password")
@limiter.limit("10/15minutes")
async def auth_reset_password(request: Request):
    body = await request.json()
    token = body.get("token", "").strip()
    email = body.get("email", "").strip().lower()
    new_password = body.get("new_password", "").strip()

    if not token or not email or not new_password:
        raise HTTPException(status_code=400, detail="Token, email, and new password are required.")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

        reset_result = await db.execute(
            select(PasswordReset)
            .where(PasswordReset.user_id == user.id)
            .where(PasswordReset.used_at == None)
            .where(PasswordReset.expires_at > datetime.now(timezone.utc).replace(tzinfo=None))
            .order_by(PasswordReset.created_at.desc())
        )
        reset_entry = reset_result.scalars().first()
        if not reset_entry:
            raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

        token_valid = await asyncio.to_thread(
            lambda: bcrypt.checkpw(token.encode("utf-8"), reset_entry.token_hash.encode("utf-8"))
        )
        if not token_valid:
            raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

        new_hash = await asyncio.to_thread(
            lambda: bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        )
        user.password_hash = new_hash
        reset_entry.used_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()

    return JSONResponse({"success": True, "message": "Password has been reset. You can now log in with your new password."})


@router_auth.get("/api/auth/me")
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
        "free_trial_remaining": max(0, 5 - (getattr(user, 'free_trial_used', 0) or 0)),
    }


@router_settings.get("/api/settings")
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
        "free_trial_remaining": max(0, 5 - (getattr(user, 'free_trial_used', 0) or 0)),
    }
    cache_set(cache_key, data, ttl=60)
    return data


@router_settings.put("/api/settings")
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


@router_settings.post("/api/settings/check-market-rates")
async def check_market_rates(request: Request):
    """On-demand market rate lookup via Tavily — not used in estimates."""
    user = await require_user(request)
    rates = await get_market_rates(user.company_city, user.company_state)
    return rates


@router_settings.put("/api/settings/password")
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


@router_settings.post("/api/settings/logout-all")
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
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
        ))
        await db.commit()

    response = JSONResponse({"ok": True, "message": "All other sessions have been revoked."})
    response.set_cookie(
        "session_token", new_token, httponly=True, samesite="lax",
        secure=True, max_age=30 * 24 * 3600, path="/"
    )
    return response


@router_library.get("/api/library")
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


@router_library.get("/api/library/search")
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


@router_library.post("/api/library/add")
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


@router_library.put("/api/library/{item_id}")
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
        item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        cache_invalidate("library")
        return {"success": True}


@router_library.get("/api/library/stats")
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



def compress_image(image_bytes: bytes, max_size_kb: int = 500) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    try:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        max_dim = 1280
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        output = io.BytesIO()
        quality = 85
        img.save(output, format="JPEG", quality=quality)

        if output.tell() <= max_size_kb * 1024:
            return output.getvalue()

        quality = 50
        img.save(output, format="JPEG", quality=quality)
        if output.tell() <= max_size_kb * 1024:
            return output.getvalue()

        quality = 30
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
    except Exception as e:
        logger.debug("Fallback handled: %s", e)

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


OPENROUTER_PRICING_PER_MILLION = {
    "qwen/qwen2.5-vl-72b-instruct": (0.80, 0.80),
    "mistralai/pixtral-large-2411": (2.00, 6.00),
    "z-ai/glm-5v-turbo": (1.20, 4.00),
    "openai/gpt-4.1": (2.00, 8.00),
}


def _claude_response_usage(resp) -> tuple[int, int, str]:
    """(input_tokens, output_tokens, model) from an Anthropic messages response."""
    try:
        u = getattr(resp, "usage", None)
        inp = int(getattr(u, "input_tokens", 0) or 0) if u else 0
        out = int(getattr(u, "output_tokens", 0) or 0) if u else 0
        mod = str(getattr(resp, "model", "") or "")
        return inp, out, mod
    except Exception as e:
        return 0, 0, ""


def estimate_anthropic_cost_cents(input_tokens: int, output_tokens: int, model_name: str) -> int:
    """Approximate Claude API cost in US cents."""
    rates = ANTHROPIC_PRICING_PER_MILLION.get(model_name or "", (3.0, 15.0))
    cost_dollars = (input_tokens / 1_000_000.0) * rates[0] + (output_tokens / 1_000_000.0) * rates[1]
    return int(round(cost_dollars * 100))


def estimate_openrouter_cost_cents(input_tokens: int, output_tokens: int, model_name: str) -> int:
    rates = OPENROUTER_PRICING_PER_MILLION.get(model_name or "")
    if not rates:
        return 0
    cost_dollars = (input_tokens / 1_000_000.0) * rates[0] + (output_tokens / 1_000_000.0) * rates[1]
    return int(round(cost_dollars * 100))


def parse_ai_json(raw_text: str) -> dict:
    import re
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    raw_text = raw_text.strip()
    raw_text = raw_text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'\{[\s\S]*\}', raw_text)
    candidate = json_match.group(0) if json_match else (raw_text if raw_text.startswith("{") else "")
    if not candidate:
        raise ValueError(f"Could not parse AI JSON response: {raw_text[:500]}")
    candidate = candidate.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    candidate = re.sub(r'(\d)"([A-Za-z(])', r'\1in\2', candidate)
    candidate = re.sub(r'(\d)"(\s)', r'\1in\2', candidate)
    candidate = re.sub(r'(\d)"$', r'\1in', candidate, flags=re.MULTILINE)
    candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
    candidate = re.sub(r'[\x00-\x1f]', ' ', candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    open_brackets = candidate.count("[") - candidate.count("]")
    open_braces = candidate.count("{") - candidate.count("}")
    if open_brackets > 0 or open_braces > 0:
        patched = candidate + "]" * open_brackets + "}" * open_braces
        try:
            return json.loads(patched)
        except json.JSONDecodeError:
            pass
    last_complete_item = candidate.rfind("},")
    if last_complete_item > 0:
        truncated = candidate[:last_complete_item + 1]
        t_brackets = truncated.count("[") - truncated.count("]")
        t_braces = truncated.count("{") - truncated.count("}")
        for _ in range(t_brackets):
            truncated += "]"
        truncated += "}" * t_braces
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse AI JSON response: {raw_text[:500]}")


def validate_estimate_schema(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if not isinstance(result.get("items"), list):
        return False
    totals = result.get("totals")
    if not isinstance(totals, dict) or "cubic_yards_mid" not in totals:
        return False
    if not isinstance(result.get("job_type"), str):
        return False
    return True


def validate_spotting_schema(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if not isinstance(result.get("items"), list):
        return False
    if len(result.get("items", [])) == 0:
        return False
    return True


def _clean_string_list(values) -> list[str]:
    cleaned = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def normalize_verification_result(result: dict) -> dict:
    if not isinstance(result, dict):
        return result
    result["verification_notes"] = _clean_string_list(result.get("verification_notes"))
    result["confirmed_items"] = _clean_string_list(result.get("confirmed_items"))
    result["uncertain_items"] = _clean_string_list(result.get("uncertain_items"))
    result["removed_items"] = _clean_string_list(result.get("removed_items"))
    return result


def normalize_model_eval_models(raw_models) -> list[str]:
    allowed = set(MODEL_EVAL_SUPPORTED_MODELS)
    out = []
    for model in raw_models or []:
        model_name = str(model or "").strip()
        if model_name in allowed and model_name not in out:
            out.append(model_name)
    return out or list(MODEL_EVAL_DEFAULT_MODELS)


def cleanup_expired_model_eval_jobs():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired = [
        job_id for job_id, job in model_eval_jobs.items()
        if (now - job.get("created_at", now)).total_seconds() > MODEL_EVAL_TTL_SECONDS
    ]
    for job_id in expired:
        workspace = Path(model_eval_jobs[job_id].get("workspace", "") or "")
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        model_eval_jobs.pop(job_id, None)


def _safe_eval_filename(filename: str, index: int) -> str:
    suffix = Path(filename or "").suffix.lower() or ".jpg"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "").stem).strip("._") or f"image_{index+1}"
    return f"{index+1:03d}_{base}{suffix}"


def _model_eval_price_mid(low: float, high: float) -> float:
    try:
        return round((float(low or 0) + float(high or 0)) / 2.0, 2)
    except Exception as e:
        return 0.0


def _build_eval_image_content(image_b64: str, media_type: str) -> list[dict]:
    return [
        {"type": "text", "text": "Photo 1 (Room: Main):"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
        },
    ]


def _model_eval_data_uri(image_b64: str, media_type: str) -> str:
    return f"data:{media_type};base64,{image_b64}"


async def run_claude_model_eval(image_b64: str, media_type: str, system_prompt: str, api_key: str) -> tuple[dict, dict]:
    client = anthropic.Anthropic(api_key=api_key, timeout=90.0)

    def _run():
        return client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            temperature=0,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": _build_eval_image_content(image_b64, media_type) + [{
                    "type": "text",
                    "text": "Analyze these junk removal photos and provide your estimate as JSON."
                }]
            }]
        )

    response = await asyncio.to_thread(_run)
    raw_text = response.content[0].text
    parsed = parse_ai_json(raw_text)
    in_tok, out_tok, used_model = _claude_response_usage(response)
    meta = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "model_used": used_model or "claude-sonnet-4-20250514",
        "api_cost_cents": estimate_anthropic_cost_cents(in_tok, out_tok, used_model or "claude-sonnet-4-20250514"),
    }
    return parsed, meta


async def run_openrouter_model_eval(image_b64: str, media_type: str, system_prompt: str, api_key: str, model_name: str) -> tuple[dict, dict]:
    payload = {
        "model": model_name,
        "temperature": 0,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Photo 1 (Room: Main):"},
                    {"type": "image_url", "image_url": {"url": _model_eval_data_uri(image_b64, media_type)}},
                    {"type": "text", "text": "Analyze these junk removal photos and provide your estimate as JSON."},
                ],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://whatshouldicharge.app",
        "X-Title": "WhatShouldICharge Admin Eval",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        if response.status_code >= 400:
            raw_text = (response.text or "").strip()
            detail = ""
            try:
                err_json = response.json()
                err_obj = err_json.get("error") if isinstance(err_json, dict) else None
                if isinstance(err_obj, dict):
                    detail = str(err_obj.get("message") or "").strip()
                if not detail and isinstance(err_json, dict):
                    detail = str(err_json.get("message") or "").strip()
            except Exception as e:
                logger.debug("Fallback handled: %s", e)
            snippet = detail or raw_text[:300] or response.reason_phrase
            raise RuntimeError(f"OpenRouter {response.status_code}: {snippet}")
        data = response.json()
    choice = (((data.get("choices") or [{}])[0]).get("message") or {})
    raw_text = choice.get("content") or ""
    parsed = parse_ai_json(raw_text)
    usage = data.get("usage") or {}
    meta = {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "model_used": str(data.get("model") or model_name),
        "api_cost_cents": estimate_openrouter_cost_cents(
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
            str(data.get("model") or model_name),
        ),
    }
    return parsed, meta


def _openrouter_content_from_anthropic_blocks(image_content: list[dict]) -> list[dict]:
    out: list[dict] = []
    for block in image_content or []:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype == "text":
            text = str(block.get("text") or "").strip()
            if text:
                out.append({"type": "text", "text": text})
        elif btype == "image":
            source = block.get("source") or {}
            media_type = str(source.get("media_type") or "image/jpeg").strip() or "image/jpeg"
            data = str(source.get("data") or "").strip()
            if data:
                out.append({"type": "image_url", "image_url": {"url": _model_eval_data_uri(data, media_type)}})
    return out


async def run_openrouter_estimate(
    image_content: list[dict],
    system_prompt: str,
    api_key: str,
    model_name: str,
    *,
    title: str = "WhatShouldICharge Estimate",
) -> tuple[dict, dict]:
    content_blocks = _openrouter_content_from_anthropic_blocks(image_content)
    content_blocks.append({"type": "text", "text": "Analyze these junk removal photos and provide your estimate as JSON."})
    payload = {
        "model": model_name,
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_blocks},
        ],
        "provider": {
            "sort": "throughput",
            "preferred_max_latency": {"p90": 60},
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://whatshouldicharge.app",
        "X-Title": title,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        if response.status_code >= 400:
            raw_text = (response.text or "").strip()
            detail = ""
            try:
                err_json = response.json()
                err_obj = err_json.get("error") if isinstance(err_json, dict) else None
                if isinstance(err_obj, dict):
                    detail = str(err_obj.get("message") or "").strip()
                if not detail and isinstance(err_json, dict):
                    detail = str(err_json.get("message") or "").strip()
            except Exception as e:
                logger.debug("Fallback handled: %s", e)
            snippet = detail or raw_text[:300] or response.reason_phrase
            raise RuntimeError(f"OpenRouter {response.status_code}: {snippet}")
        data = response.json()
    choice = (((data.get("choices") or [{}])[0]).get("message") or {})
    raw_text = choice.get("content") or ""
    parsed = parse_ai_json(raw_text)
    usage = data.get("usage") or {}
    meta = {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "model_used": str(data.get("model") or model_name),
        "api_cost_cents": estimate_openrouter_cost_cents(
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
            str(data.get("model") or model_name),
        ),
    }
    return parsed, meta


def finalize_model_eval_result(result_data: dict, room_labels: Optional[list[str]] = None) -> dict:
    labels = list(room_labels or ["Main"])
    result_data = validate_estimate(result_data)
    _sync_result_totals_to_items(result_data)
    result_data, _ = normalize_curbside_mixed_item_labels(result_data, labels)
    result_data, _ = normalize_special_fee_items(result_data)
    _sync_result_totals_to_items(result_data)
    scene_type = classify_scene_type(result_data, labels, None)
    result_data, _ = apply_small_job_volume_guardrails(result_data, scene_type, labels)
    _sync_result_totals_to_items(result_data)
    scene_type = classify_scene_type(result_data, labels, None)
    result_data, scene_type = apply_job_label_guardrails(result_data, scene_type, labels)
    _sync_result_totals_to_items(result_data)
    price_low, price_high, cy_mid, special_items, min_charge_applied = calculate_price(
        result_data,
        rate_low=35.0,
        rate_high=40.0,
        rate_premium=55.0,
        min_charge=75.0,
        market_rates=None,
    )
    return {
        "result_data": result_data,
        "scene_type": scene_type,
        "scene_label": SCENE_DISPLAY_NAMES.get(scene_type, scene_type.replace("_", " ").title() if scene_type else ""),
        "price_low": price_low,
        "price_high": price_high,
        "price_mid": _model_eval_price_mid(price_low, price_high),
        "cy_estimate": cy_mid,
        "special_items": special_items,
        "min_charge_applied": min_charge_applied,
        "item_count": len(result_data.get("items", []) or []),
        "items_summary": "; ".join(
            f"{it.get('name','')} x{it.get('quantity',1)} ({float(it.get('cubic_yards',0) or 0):.2f} CY)"
            for it in (result_data.get("items", []) or [])[:10]
            if isinstance(it, dict)
        ),
        "notes": str(result_data.get("notes", "") or "")[:500],
        "confidence": int(result_data.get("confidence", 0) or 0),
    }


def generate_model_eval_csv(job: dict, output_path: Path) -> None:
    fieldnames = [
        "filename", "model", "parse_ok", "cy_estimate", "price_low", "price_high", "price_mid",
        "scene_type", "scene_label", "confidence", "item_count", "special_item_count",
        "items_summary", "notes", "error", "comparison_cy_delta", "comparison_price_mid_delta", "comparison_scene_match"
    ]
    rows = []
    comparisons = job.get("comparisons", {}) or {}
    for image in job.get("images", []) or []:
        image_name = image.get("filename", "")
        cmp = comparisons.get(image_name, {})
        for result in image.get("results", []) or []:
            model_name = result.get("model", "")
            model_cmp = cmp.get(model_name, {}) if isinstance(cmp, dict) else {}
            rows.append({
                "filename": image_name,
                "model": model_name,
                "parse_ok": result.get("parse_ok", False),
                "cy_estimate": result.get("cy_estimate", ""),
                "price_low": result.get("price_low", ""),
                "price_high": result.get("price_high", ""),
                "price_mid": result.get("price_mid", ""),
                "scene_type": result.get("scene_type", ""),
                "scene_label": result.get("scene_label", ""),
                "confidence": result.get("confidence", ""),
                "item_count": result.get("item_count", ""),
                "special_item_count": len(result.get("special_items", []) or []),
                "items_summary": result.get("items_summary", ""),
                "notes": result.get("notes", ""),
                "error": result.get("error", ""),
                "comparison_cy_delta": model_cmp.get("cy_delta", ""),
                "comparison_price_mid_delta": model_cmp.get("price_mid_delta", ""),
                "comparison_scene_match": model_cmp.get("scene_match", ""),
            })
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate_model_eval_html(job: dict, output_path: Path) -> None:
    images = job.get("images", []) or []
    comparisons = job.get("comparisons", {}) or {}
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>WSIC Model Eval Report</title>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e8eaf0;padding:24px;}h1,h2{margin:0 0 12px;} .meta{color:#7b82a0;margin-bottom:20px;} .card{background:#1a1d27;border:1px solid #2e3352;border-radius:14px;padding:18px;margin-bottom:18px;} .grid{display:grid;grid-template-columns:280px 1fr;gap:16px;} img{max-width:100%;border-radius:10px;border:1px solid #2e3352;} table{width:100%;border-collapse:collapse;margin-top:12px;} th,td{border-top:1px solid #2e3352;padding:8px 10px;text-align:left;vertical-align:top;} th{color:#7b82a0;font-size:12px;text-transform:uppercase;letter-spacing:.06em;} .ok{color:#22c55e;} .bad{color:#ef4444;} .muted{color:#7b82a0;} .pill{display:inline-block;background:#22263a;border-radius:999px;padding:3px 8px;font-size:12px;margin-right:6px;}</style></head><body>",
        f"<h1>WSIC Model Eval Report</h1><div class='meta'>Created {html.escape(str(job.get('created_at_label','')))} • {len(images)} images • models: {html.escape(', '.join(job.get('models', [])))}</div>",
    ]
    for image in images:
        filename = image.get("filename", "")
        cmp = comparisons.get(filename, {})
        parts.append("<div class='card'>")
        parts.append(f"<h2>{html.escape(filename)}</h2>")
        parts.append("<div class='grid'>")
        parts.append(f"<div><img src='{image.get('data_url','')}' alt='{html.escape(filename)}'></div>")
        parts.append("<div>")
        baseline_lines = []
        if isinstance(cmp, dict) and cmp:
            for model_name, model_cmp in cmp.items():
                baseline_lines.append(
                    f"{html.escape(str(model_name))}: CY Δ {html.escape(str((model_cmp or {}).get('cy_delta','--')))} • "
                    f"Price Δ {html.escape(str((model_cmp or {}).get('price_mid_delta','--')))} • "
                    f"Scene match {html.escape(str((model_cmp or {}).get('scene_match','--')))}"
                )
        if baseline_lines:
            parts.append("<div class='muted'>vs Claude baseline: " + " | ".join(baseline_lines) + "</div>")
        else:
            parts.append("<div class='muted'>No baseline comparison available.</div>")
        parts.append("<table><thead><tr><th>Model</th><th>Parse</th><th>CY</th><th>Price</th><th>Scene</th><th>Confidence</th><th>Items</th><th>Notes</th></tr></thead><tbody>")
        for result in image.get("results", []) or []:
            parse_ok = bool(result.get("parse_ok"))
            parts.append(
                "<tr>"
                f"<td>{html.escape(result.get('model',''))}</td>"
                f"<td class='{'ok' if parse_ok else 'bad'}'>{'ok' if parse_ok else 'error'}</td>"
                f"<td>{html.escape(str(result.get('cy_estimate','--')))}</td>"
                f"<td>${html.escape(str(result.get('price_low','--')))} – ${html.escape(str(result.get('price_high','--')))}</td>"
                f"<td>{html.escape(result.get('scene_label','') or result.get('scene_type',''))}</td>"
                f"<td>{html.escape(str(result.get('confidence','--')))}</td>"
                f"<td>{html.escape(result.get('items_summary',''))}</td>"
                f"<td>{html.escape(result.get('error','') or result.get('notes',''))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table></div></div></div>")
    parts.append("</body></html>")
    output_path.write_text("".join(parts), encoding="utf-8")


async def lookup_item_dimensions(item_name: str, api_key: str) -> dict:
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key or not (api_key or "").strip():
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

    except Exception as e:
        logger.debug("Fallback handled: %s", e)

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
                existing_items[name].updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
JOB_TTL_SECONDS = 600
MAX_CONCURRENT_JOBS = 10

_processed_webhook_events: set[str] = set()

model_eval_jobs = {}
MODEL_EVAL_TTL_SECONDS = 6 * 60 * 60
MODEL_EVAL_ROOT = Path(tempfile.gettempdir()) / "wsic_model_evals"
MODEL_EVAL_ROOT.mkdir(parents=True, exist_ok=True)
MODEL_EVAL_DEFAULT_MODELS = ("claude-sonnet-4-20250514", "openai/gpt-4.1")
MODEL_EVAL_PER_RUN_TIMEOUT_SECONDS = 150
PROD_PRIMARY_MODEL = "qwen/qwen2.5-vl-72b-instruct"
PROD_SIZING_MODEL = "mistralai/pixtral-large-2411"
PROD_VERIFIER_MODEL = PROD_SIZING_MODEL
MODEL_EVAL_SUPPORTED_MODELS = (
    "claude-sonnet-4-20250514",
    "openai/gpt-4.1",
    "qwen/qwen2.5-vl-72b-instruct",
    "mistralai/pixtral-large-2411",
    "z-ai/glm-5v-turbo",
)


def count_active_jobs() -> int:
    return sum(1 for j in estimate_jobs.values() if j.get("status") in ("analyzing", "looking_up"))


def check_concurrent_limit():
    if count_active_jobs() >= MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail="Server is busy processing other estimates. Please try again in a minute.")


_user_estimate_timestamps: dict[int, list[datetime]] = {}
_ESTIMATES_PER_USER_WINDOW = 10
_ESTIMATES_USER_WINDOW_SECONDS = 60


def _check_user_rate_limit(user_id: int):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if user_id not in _user_estimate_timestamps:
        _user_estimate_timestamps[user_id] = []
    times = _user_estimate_timestamps[user_id]
    cutoff = datetime.fromtimestamp(now.timestamp() - _ESTIMATES_USER_WINDOW_SECONDS, tz=timezone.utc)
    times[:] = [t for t in times if t > cutoff]
    if len(times) >= _ESTIMATES_PER_USER_WINDOW:
        raise HTTPException(
            status_code=429,
            detail="Too many estimate requests. Please wait before creating another estimate.",
        )
    times.append(now)


def _record_user_estimate(user_id: int):
    if user_id not in _user_estimate_timestamps:
        _user_estimate_timestamps[user_id] = []
    _user_estimate_timestamps[user_id].append(datetime.now(timezone.utc).replace(tzinfo=None))


def cleanup_expired_jobs():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired = [k for k, v in estimate_jobs.items()
               if v.get("status") in ("error", "retry_needed")
               or (v.get("status") not in ("analyzing", "looking_up")
                   and (now - v.get("created_at", now)).total_seconds() > JOB_TTL_SECONDS)]
    for k in expired:
        del estimate_jobs[k]
        asyncio.create_task(_delete_job_from_db(k))


async def _upsert_job_to_db(job_id: str, user_id: int, team_member_id: int, status: str, result_json: str = "", error_message: str = "", completed_at=None):
    try:
        async with AsyncSessionLocal() as db:
            from models import Job
            result = await db.execute(select(Job).where(Job.id == job_id))
            existing = result.scalar_one_or_none()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if existing:
                existing.status = status
                existing.result_json = result_json
                existing.error_message = error_message
                existing.updated_at = now
                if completed_at:
                    existing.completed_at = completed_at
            else:
                db.add(Job(
                    id=job_id,
                    user_id=user_id,
                    team_member_id=team_member_id,
                    status=status,
                    result_json=result_json,
                    error_message=error_message,
                    created_at=now,
                    updated_at=now,
                    completed_at=completed_at,
                ))
            await db.commit()
    except Exception:
        pass


async def _delete_job_from_db(job_id: str):
    try:
        async with AsyncSessionLocal() as db:
            from models import Job
            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                await db.delete(job)
                await db.commit()
    except Exception:
        pass


def _build_model_eval_comparisons(job: dict) -> dict:
    comparisons = {}
    baseline_model = "claude-sonnet-4-20250514"
    for image in job.get("images", []) or []:
        results = image.get("results", []) or []
        by_model = {r.get("model"): r for r in results if isinstance(r, dict)}
        baseline = by_model.get(baseline_model)
        per_model: dict[str, dict] = {}
        for model_name, row in by_model.items():
            if model_name == baseline_model:
                continue
            if not baseline or not baseline.get("parse_ok") or not row.get("parse_ok"):
                per_model[model_name] = {"cy_delta": "", "price_mid_delta": "", "scene_match": ""}
                continue
            cy_delta = round(abs(float(baseline.get("cy_estimate", 0) or 0) - float(row.get("cy_estimate", 0) or 0)), 2)
            price_mid_delta = round(abs(float(baseline.get("price_mid", 0) or 0) - float(row.get("price_mid", 0) or 0)), 2)
            scene_match = "yes" if (baseline.get("scene_type") or "") == (row.get("scene_type") or "") else "no"
            per_model[model_name] = {
                "cy_delta": cy_delta,
                "price_mid_delta": price_mid_delta,
                "scene_match": scene_match,
            }
        comparisons[image.get("filename", "")] = per_model
    return comparisons


async def run_model_eval_job(job_id: str, extraction_prompt: str, anthropic_key: str, openrouter_key: str):
    job = model_eval_jobs[job_id]
    try:
        for image in job.get("images", []) or []:
            image["results"] = []
            for model_name in job.get("models", []):
                job["current_step"] = f"{image.get('filename', 'image')} • {model_name}"
                job["message"] = f"Running model comparison... {int(job.get('completed_runs', 0) or 0)}/{int(job.get('total_runs', 0) or 0)} model runs completed."
                job["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
                result_row = {
                    "model": model_name,
                    "parse_ok": False,
                    "cy_estimate": "",
                    "price_low": "",
                    "price_high": "",
                    "price_mid": "",
                    "scene_type": "",
                    "scene_label": "",
                    "confidence": "",
                    "item_count": "",
                    "items_summary": "",
                    "notes": "",
                    "special_items": [],
                    "error": "",
                }
                try:
                    if model_name == "claude-sonnet-4-20250514":
                        raw_result, meta = await asyncio.wait_for(
                            run_claude_model_eval(image["b64"], image["media_type"], extraction_prompt, anthropic_key),
                            timeout=MODEL_EVAL_PER_RUN_TIMEOUT_SECONDS,
                        )
                    elif model_name in MODEL_EVAL_SUPPORTED_MODELS:
                        raw_result, meta = await asyncio.wait_for(
                            run_openrouter_model_eval(image["b64"], image["media_type"], extraction_prompt, openrouter_key, model_name),
                            timeout=MODEL_EVAL_PER_RUN_TIMEOUT_SECONDS,
                        )
                    else:
                        raise RuntimeError(f"Unsupported model: {model_name}")

                    if not validate_estimate_schema(raw_result):
                        raise ValueError("Model returned invalid estimate schema")

                    finalized = finalize_model_eval_result(raw_result, ["Main"])
                    result_row.update({
                        "parse_ok": True,
                        "cy_estimate": finalized["cy_estimate"],
                        "price_low": finalized["price_low"],
                        "price_high": finalized["price_high"],
                        "price_mid": finalized["price_mid"],
                        "scene_type": finalized["scene_type"],
                        "scene_label": finalized["scene_label"],
                        "confidence": finalized["confidence"],
                        "item_count": finalized["item_count"],
                        "items_summary": finalized["items_summary"],
                        "notes": finalized["notes"],
                        "special_items": finalized["special_items"],
                        "meta": meta,
                    })
                except Exception as err:
                    result_row["error"] = f"{type(err).__name__}: {err}"
                image["results"].append(result_row)
                job["completed_runs"] = int(job.get("completed_runs", 0) or 0) + 1
                job["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            job["completed_images"] = int(job.get("completed_images", 0) or 0) + 1
            job["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)

        job["comparisons"] = _build_model_eval_comparisons(job)
        workspace = Path(job["workspace"])
        csv_path = workspace / "results.csv"
        html_path = workspace / "report.html"
        generate_model_eval_csv(job, csv_path)
        generate_model_eval_html(job, html_path)
        job["csv_path"] = str(csv_path)
        job["html_path"] = str(html_path)
        total_rows = 0
        success_rows = 0
        errors = []
        for image in job.get("images", []) or []:
            for result in image.get("results", []) or []:
                total_rows += 1
                if result.get("parse_ok"):
                    success_rows += 1
                elif result.get("error"):
                    errors.append(str(result.get("error")))
        job["status"] = "complete"
        job["current_step"] = ""
        job["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        if success_rows == total_rows:
            job["message"] = "Model eval complete."
        elif success_rows > 0:
            first_error = errors[0] if errors else "Unknown model error"
            job["message"] = f"Model eval complete with partial errors. First error: {first_error[:220]}"
        else:
            first_error = errors[0] if errors else "Unknown model error"
            job["message"] = f"Model eval failed for all model runs. First error: {first_error[:220]}"
    except Exception as err:
        job["status"] = "error"
        job["current_step"] = ""
        job["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        job["message"] = f"Model eval failed: {type(err).__name__}: {err}"


@router_admin.post("/api/admin/model-evals")
async def admin_create_model_eval(
    request: Request,
    files: list[UploadFile] = File(...),
    models: str = Form(default='["claude-sonnet-4-20250514","openai/gpt-4.1"]'),
):
    await require_admin(request)
    cleanup_expired_model_eval_jobs()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    try:
        parsed_models = json.loads(models or "[]")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid models payload")
    requested_models = normalize_model_eval_models(parsed_models)
    if "claude-sonnet-4-20250514" in requested_models and not anthropic_key:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY is not configured")
    needs_openrouter = any(model != "claude-sonnet-4-20250514" for model in requested_models)
    if needs_openrouter and not openrouter_key:
        raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY is not configured")
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one image")

    job_id = secrets.token_hex(8)
    workspace = MODEL_EVAL_ROOT / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    images = []
    skipped_files = []
    allowed_image_exts = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif"}
    for idx, upload in enumerate(files):
        raw = await upload.read()
        if not raw:
            continue
        original_name = upload.filename or f"image_{idx+1}.jpg"
        lower_name = original_name.lower()
        content_type = str(upload.content_type or "").lower()
        ext = Path(lower_name).suffix

        # Browser folder uploads often include metadata files like .DS_Store.
        if lower_name in {".ds_store", "thumbs.db"} or lower_name.startswith("._"):
            skipped_files.append(original_name)
            continue
        if not (content_type.startswith("image/") or ext in allowed_image_exts):
            skipped_files.append(original_name)
            continue

        safe_name = _safe_eval_filename(original_name, idx)
        file_path = workspace / safe_name
        try:
            file_path.write_bytes(raw)
            compressed = compress_image(raw)
        except UnidentifiedImageError:
            skipped_files.append(original_name)
            try:
                file_path.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("Fallback handled: %s", e)
            continue
        except Exception as err:
            shutil.rmtree(workspace, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Image processing failed for {safe_name}: {type(err).__name__}: {err}")
        b64 = base64.standard_b64encode(compressed).decode("utf-8")
        media_type = "image/jpeg"
        images.append({
            "filename": safe_name,
            "path": str(file_path),
            "media_type": media_type,
            "b64": b64,
            "data_url": _model_eval_data_uri(b64, media_type),
            "results": [],
        })
    if not images:
        shutil.rmtree(workspace, ignore_errors=True)
        skipped_preview = ", ".join(skipped_files[:5]) if skipped_files else ""
        msg = "No usable images were uploaded"
        if skipped_preview:
            msg += f". Skipped files: {skipped_preview}"
        raise HTTPException(status_code=400, detail=msg)

    model_eval_jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "message": (
            f"Running model comparison... Skipped {len(skipped_files)} non-image file(s)."
            if skipped_files else "Running model comparison..."
        ),
        "created_at": created_at,
        "updated_at": created_at,
        "created_at_label": created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "workspace": str(workspace),
        "models": requested_models,
        "images": images,
        "skipped_files": skipped_files,
        "completed_images": 0,
        "completed_runs": 0,
        "total_runs": len(images) * len(requested_models),
        "current_step": "",
        "csv_path": "",
        "html_path": "",
        "comparisons": {},
    }

    extraction_prompt = get_extraction_prompt("junk_removal")
    try:
        library_context = await get_library_context()
    except Exception as e:
        library_context = ""
    if library_context:
        extraction_prompt += "\n" + library_context

    asyncio.create_task(run_model_eval_job(job_id, extraction_prompt, anthropic_key, openrouter_key))
    return {
        "job_id": job_id,
        "skipped_count": len(skipped_files),
        "skipped_files": skipped_files[:20],
    }


@router_admin.get("/api/admin/model-evals")
async def admin_list_model_evals(request: Request):
    await require_admin(request)
    cleanup_expired_model_eval_jobs()
    jobs = []
    for job in sorted(model_eval_jobs.values(), key=lambda j: j.get("created_at") or datetime.now(timezone.utc).replace(tzinfo=None), reverse=True):
        jobs.append({
            "id": job["id"],
            "status": job.get("status", ""),
            "message": job.get("message", ""),
            "created_at": job.get("created_at").isoformat() if job.get("created_at") else None,
            "updated_at": job.get("updated_at").isoformat() if job.get("updated_at") else None,
            "models": job.get("models", []),
            "image_count": len(job.get("images", []) or []),
            "skipped_count": len(job.get("skipped_files", []) or []),
            "completed_images": int(job.get("completed_images", 0) or 0),
            "completed_runs": int(job.get("completed_runs", 0) or 0),
            "total_runs": int(job.get("total_runs", 0) or 0),
            "current_step": job.get("current_step", ""),
            "has_csv": bool(job.get("csv_path")),
            "has_html": bool(job.get("html_path")),
        })
    return {"jobs": jobs}


@router_admin.get("/api/admin/model-evals/{job_id}")
async def admin_model_eval_detail(request: Request, job_id: str):
    await require_admin(request)
    cleanup_expired_model_eval_jobs()
    job = model_eval_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Model eval not found")
    return {
        "id": job["id"],
        "status": job.get("status", ""),
        "message": job.get("message", ""),
        "created_at": job.get("created_at").isoformat() if job.get("created_at") else None,
        "updated_at": job.get("updated_at").isoformat() if job.get("updated_at") else None,
        "models": job.get("models", []),
        "image_count": len(job.get("images", []) or []),
        "skipped_count": len(job.get("skipped_files", []) or []),
        "skipped_files": job.get("skipped_files", []),
        "completed_images": int(job.get("completed_images", 0) or 0),
        "completed_runs": int(job.get("completed_runs", 0) or 0),
        "total_runs": int(job.get("total_runs", 0) or 0),
        "current_step": job.get("current_step", ""),
        "csv_download_url": f"/api/admin/model-evals/{job_id}/download/csv" if job.get("csv_path") else "",
        "html_download_url": f"/api/admin/model-evals/{job_id}/download/html" if job.get("html_path") else "",
        "images": [
            {
                "filename": image.get("filename", ""),
                "data_url": image.get("data_url", ""),
                "results": image.get("results", []),
                "comparison": (job.get("comparisons", {}) or {}).get(image.get("filename", ""), {}),
            }
            for image in job.get("images", []) or []
        ],
    }


@router_admin.get("/api/admin/model-evals/{job_id}/download/{kind}")
async def admin_download_model_eval(request: Request, job_id: str, kind: str):
    await require_admin(request)
    cleanup_expired_model_eval_jobs()
    job = model_eval_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Model eval not found")
    if kind == "csv":
        path = job.get("csv_path", "")
        media_type = "text/csv"
        filename = f"wsic-model-eval-{job_id}.csv"
    elif kind == "html":
        path = job.get("html_path", "")
        media_type = "text/html"
        filename = f"wsic-model-eval-{job_id}.html"
    else:
        raise HTTPException(status_code=404, detail="Unknown download type")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Report not ready")
    return FileResponse(path, media_type=media_type, filename=filename)


@router_admin.delete("/api/admin/model-evals/{job_id}")
async def admin_delete_model_eval(request: Request, job_id: str):
    await require_admin(request)
    job = model_eval_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Model eval not found")
    workspace = Path(job.get("workspace", "") or "")
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    model_eval_jobs.pop(job_id, None)
    return {"ok": True}


@router_estimates.post("/api/estimate")
async def create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
    estimate_name: str = Form(default=""),
    capture_mode: str = Form(default="remote"),
    clarification_answers_json: str = Form(default=""),
):
    user = await require_user(request)
    cleanup_expired_jobs()
    check_concurrent_limit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        fresh_user = result.scalar_one_or_none()
        if not fresh_user:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        free_remaining = max(0, 5 - (fresh_user.free_trial_used or 0))
        is_admin = fresh_user.is_admin or (fresh_user.estimates_limit or 0) >= 999
        if (fresh_user.credit_balance or 0) <= 0 and free_remaining <= 0 and not is_admin:
            return JSONResponse(status_code=402, content={
                "detail": "no_credits",
                "message": "No estimate credits remaining. Purchase a credit pack to continue."
            })
        user = fresh_user

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service is not configured. Please contact support.")

    capture_mode = normalize_capture_mode(capture_mode)
    photo_data, image_content, stored_photos, photo_quality = await prepare_estimate_photos(
        files,
        rooms,
        max_files=20,
        default_room="Unknown",
        capture_mode=capture_mode,
    )
    if photo_quality["retry_needed"]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "retry_needed",
                "message": photo_quality["retry_message"],
                "photo_quality": photo_quality,
            },
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
        fresh_user = result.scalar_one_or_none()
        if not fresh_user:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        free_remaining = max(0, 5 - (fresh_user.free_trial_used or 0))
        is_admin = fresh_user.is_admin or (fresh_user.estimates_limit or 0) >= 999
        if (fresh_user.credit_balance or 0) <= 0 and free_remaining <= 0 and not is_admin:
            return JSONResponse(status_code=402, content={
                "detail": "no_credits",
                "message": "No estimate credits remaining. Purchase a credit pack to continue."
            })
        if free_remaining > 0 and (fresh_user.credit_balance or 0) <= 0 and not is_admin:
            fresh_user.free_trial_used = (fresh_user.free_trial_used or 0) + 1
            txn = CreditTransaction(
                user_id=fresh_user.id,
                transaction_type="free_trial",
                credits=-1,
                balance_after=fresh_user.credit_balance or 0,
                description=f"Free Trial Estimate #{fresh_user.free_trial_used}",
            )
        else:
            fresh_user.credit_balance = (fresh_user.credit_balance or 0) - 1
            fresh_user.credits_used_total = (fresh_user.credits_used_total or 0) + 1
            txn = CreditTransaction(
                user_id=fresh_user.id,
                transaction_type="usage",
                credits=-1,
                balance_after=fresh_user.credit_balance,
                description="Estimate",
            )
        fresh_user.estimates_used = (fresh_user.estimates_used or 0) + 1
        db.add(txn)
        await db.commit()
        user = fresh_user

    truck_cap = user.truck_capacity_cy or 16.0
    if capture_mode == "operator_assist":
        image_content.append({
            "type": "text",
            "text": (
                "\nCapture mode: operator_assist. These photos should cover the same pile with a wide shot, "
                "left angle, and right angle. Prefer visible floor edges and avoid duplicate angles."
            )
        })
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

    _check_user_rate_limit(user.id)
    job_id = secrets.token_hex(8)
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": user.id,
        "estimate_name": estimate_name.strip(),
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "stored_photos": stored_photos,
        "capture_mode": capture_mode,
        "clarification_answers": parse_clarification_answers(clarification_answers_json),
        "photo_quality": photo_quality,
        "room_labels": _normalize_room_labels(photo_data),
        "truck_load_pct": truck_load_pct,
        "review_mode": "self_serve_clarify",
        "used_free_trial": free_remaining > 0 and (fresh_user.credit_balance or 0) <= 0,
    }
    await _upsert_job_to_db(job_id, user.id, 0, "analyzing")
    _record_user_estimate(user.id)

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
    pass2_json_str = ""
    lookups_json_str = ""
    photo_quality = job.get("photo_quality") or {}
    confidence_bucket = photo_quality.get("confidence_bucket", "")
    confidence_reasons = photo_quality.get("reasons", [])
    photo_quality_flags = photo_quality.get("flags", [])
    scene_type = ""
    occupancy_class = ""
    sanity_flags: list[str] = []
    geometry_summary = ""
    review_status = "auto_approved"
    review_reason = ""
    review_reason_flags: list[str] = []
    review_mode = str(job.get("review_mode") or "self_serve_clarify").strip().lower()
    allow_manual_review = review_mode == "company_manual_review"

    try:
        library_context = await get_library_context()
        industry_id = getattr(user, "industry", "junk_removal") or "junk_removal"
        extraction_prompt = get_extraction_prompt(industry_id)
        verification_prompt = get_verification_prompt(industry_id)
        if library_context:
            extraction_prompt += "\n" + library_context
            verification_prompt += "\n" + library_context
        room_labels = list(job.get("room_labels", []) or [])
        capture_scene_hint = infer_capture_scene_hint(room_labels, job.get("truck_load_pct"))
        scene_prompt_hint = build_scene_prompt_hint(capture_scene_hint)
        if scene_prompt_hint:
            extraction_prompt += "\n\n" + scene_prompt_hint
            verification_prompt += "\n\n" + scene_prompt_hint

        job["status"] = "analyzing"
        job["message"] = "Analyzing photos..."

        import logging
        logger = logging.getLogger("wsic.estimate")

        total_input_tokens = 0
        total_output_tokens = 0
        total_api_cost_cents = 0
        model_name = "parallel_pipeline"

        try:
            from services.estimation_pipeline import run_parallel_estimate, VARIANCE_FLAG_THRESHOLD
            job["message"] = "Identifying items and estimating sizes..."
            result_data, pipeline_meta = await run_parallel_estimate(image_content, extraction_prompt)

            total_input_tokens = int(pipeline_meta.get("input_tokens", 0) or 0)
            total_output_tokens = int(pipeline_meta.get("output_tokens", 0) or 0)
            total_api_cost_cents = int(pipeline_meta.get("cost_cents", 0) or 0)
            model_name = "|".join(pipeline_meta.get("provider_models", ["unknown"]))

            logger.info(
                f"[run_estimate] Pipeline completed for job {job_id}: "
                f"{len(result_data.get('items', []))} items, "
                f"providers={pipeline_meta.get('providers_used')}, "
                f"variance_flagged={pipeline_meta.get('variance_flagged')}"
            )

            if pipeline_meta.get("variance_flagged"):
                confidence_reasons.append(
                    f"Model estimates varied by >{int(VARIANCE_FLAG_THRESHOLD * 100)}% for some items. "
                    f"Using averaged values."
                )
            if pipeline_meta.get("single_provider"):
                confidence_reasons.append("Only one vision provider was available for this estimate.")

        except Exception as api_err:
            import traceback
            logger.error(f"[run_estimate] Pipeline error for job {job_id}, user {user.id}: {type(api_err).__name__}: {api_err}")
            logger.error(f"[run_estimate] Traceback: {traceback.format_exc()}")
            job["status"] = "error"
            job["message"] = f"We couldn't process your estimate. {type(api_err).__name__}: {api_err}. Check logs for provider failures."
            job["result"] = None
            return

        pass1_json_str = ""
        pass2_json_str = json.dumps(result_data)
        verifier_result = None

        job["message"] = "Calculating volume..."

        result_data, _guardrail_notes = apply_visual_estimate_guardrails(result_data, room_labels)

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

        # Sanity check: cap single items and enforce per-category maximums
        PER_ITEM_MAX_CY = {
            "trash bag": 0.4, "garbage bag": 0.4, "yard bag": 0.4, "leaf bag": 0.4,
            "contractor bag": 0.5, "duffel": 0.25, "suitcase": 0.35,
            "box": 0.25, "cardboard box": 0.25,
            "papers": 0.5, "documents": 0.5, "loose paper": 0.5,
            "clothing": 0.4, "clothes": 0.4, "textiles": 0.4,
            "books": 0.15, "magazines": 0.1,
            "shoes": 0.05, "lamp": 0.2, "pillow": 0.15,
            "end table": 0.4, "side table": 0.4, "nightstand": 0.4, "night stand": 0.4,
            "shelf": 0.5, "shelv": 0.5, "stool": 0.2, "footstool": 0.2,
            "small table": 0.4, "plant": 0.25, "pot": 0.15,
            "wood piece": 0.5, "wood furniture": 0.5, "wooden piece": 0.5,
            "wooden furniture": 0.35, "broken furniture": 0.35, "furniture pieces": 0.35,
            "wooden boards": 0.15, "lumber pieces": 0.15, "wood scraps": 0.15,
            "scrap wood": 0.15, "boards and lumber": 0.15, "wood pieces": 0.2,
            "broken wood": 0.2, "wood debris": 0.2,
            "miscellaneous": 0.5, "misc items": 0.5, "small items": 0.3,
            "household items": 0.5, "small appliance": 0.4,
            "small debris": 0.3, "debris": 0.4,
            "plastic bags": 0.35, "black bags": 0.35, "garbage bags": 0.35,
            "chair frame": 0.25, "metal framework": 0.3, "metal pieces": 0.3,
            "appliance parts": 0.25, "frame": 0.3,
        }
        CATEGORY_MAX_TOTAL_CY = {
            "paper": 0.5, "document": 0.5, "clothing": 1.0, "clothes": 1.0, "book": 1.0,
        }
        for it in items:
            name_lower = (it.get("name") or "").lower()
            cy = it.get("cubic_yards", 0)
            for keyword, max_cy in PER_ITEM_MAX_CY.items():
                if keyword in name_lower and cy > max_cy:
                    logger.info(f"[run_estimate] Capping '{name_lower}' from {cy} to {max_cy} CY")
                    it["cubic_yards"] = max_cy
                    break
            if it.get("cubic_yards", 0) > 16.0:
                it["cubic_yards"] = min(it["cubic_yards"], 16.0)

        # ── Pile compression/expansion adjustment ──
        # Rigid items (wood, metal, furniture) nest in piles — sum overestimates truck volume
        # Compressible items (clothes, bags, bedding) fluff out when loaded — sum underestimates
        RIGID_KEYWORDS = {
            "wood", "wooden", "lumber", "board", "plank", "plywood", "drywall",
            "furniture", "frame", "metal", "iron", "steel", "appliance", "cabinet",
            "door", "window", "concrete", "brick", "tile", "stone", "glass",
            "shelf", "shelving", "table", "desk", "dresser", "chest", "drawer",
            "chair", "couch", "sofa", "mattress", "box spring", "bed frame",
            "refrigerator", "freezer", "washer", "dryer", "stove", "dishwasher",
            "tv", "television", "monitor", "lumber", "railroad", "tire",
        }
        COMPRESSIBLE_KEYWORDS = {
            "cloth", "clothes", "clothing", "fabric", "textile", "linen",
            "bag", "trash", "garbage", "waste", "bedding", "pillow", "blanket",
            "quilt", "comforter", "carpet", "rug", "pad", "foam", "cushion",
            "stuffed", "soft", "towel", "curtain", "drape", "sleeping bag",
            "duffel", "suitcase", "backpack", "diaper",
        }
        rigid_total = 0.0
        compressible_total = 0.0
        neutral_total = 0.0
        for it in items:
            cy = float(it.get("cubic_yards", 0) or 0) * int(it.get("quantity", 1) or 1)
            if cy <= 0:
                continue
            name_lower = (it.get("name") or "").lower()
            is_rigid = any(kw in name_lower for kw in RIGID_KEYWORDS)
            is_compressible = any(kw in name_lower for kw in COMPRESSIBLE_KEYWORDS)
            if is_rigid and not is_compressible:
                rigid_total += cy
            elif is_compressible and not is_rigid:
                compressible_total += cy
            else:
                neutral_total += cy

        RIGID_FACTOR = 0.85
        COMPRESSIBLE_FACTOR = 1.15
        adjusted_total = round(
            rigid_total * RIGID_FACTOR
            + compressible_total * COMPRESSIBLE_FACTOR
            + neutral_total,
            1,
        )
        raw_item_sum = round(rigid_total + compressible_total + neutral_total, 1)
        if raw_item_sum > 0 and abs(adjusted_total - raw_item_sum) >= 0.2:
            logger.info(
                f"[run_estimate] Job {job_id}: pile adjustment "
                f"rigid={rigid_total:.1f}×{RIGID_FACTOR} "
                f"compressible={compressible_total:.1f}×{COMPRESSIBLE_FACTOR} "
                f"neutral={neutral_total:.1f} => "
                f"{raw_item_sum:.1f} CY → {adjusted_total:.1f} CY"
            )
            totals = result_data.get("totals", {})
            totals["cubic_yards_mid"] = adjusted_total
            totals["cubic_yards_low"] = round(adjusted_total * 0.85, 1)
            totals["cubic_yards_high"] = round(adjusted_total * 1.15, 1)
            result_data["totals"] = totals
            result_data["total_cubic_yards"] = adjusted_total

        items_needing_lookup = result_data.get("items_needing_lookup", [])
        lookups_done = []
        if items_needing_lookup:
            job["status"] = "looking_up"
            job["message"] = f"Looking up {len(items_needing_lookup)} unknown items..."

            lookup_tasks = [
                lookup_item_dimensions(item_name, os.environ.get("ANTHROPIC_API_KEY", ""))
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
                            total_api_cost_cents += estimate_anthropic_cost_cents(
                                int(tu.get("input", 0) or 0),
                                int(tu.get("output", 0) or 0),
                                str(tu.get("model") or ""),
                            )

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
        result_data, pile_notes = apply_pile_adjustment(result_data)
        if pile_notes:
            confidence_reasons.extend(pile_notes)
        heavy_materials = detect_heavy_materials(result_data)
        if heavy_materials:
            confidence_reasons.append(
                f"Heavy materials detected ({heavy_materials[0]}). Job classified as premium."
            )
        _sync_result_totals_to_items(result_data)
        result_data, _curbside_label_notes = normalize_curbside_mixed_item_labels(result_data, room_labels)
        result_data, _special_fee_notes = normalize_special_fee_items(result_data)
        _sync_result_totals_to_items(result_data)
        scene_type = classify_scene_type(result_data, room_labels, job.get("truck_load_pct"))
        result_data, _small_job_notes = apply_small_job_volume_guardrails(result_data, scene_type, room_labels)
        _sync_result_totals_to_items(result_data)
        scene_type = classify_scene_type(result_data, room_labels, job.get("truck_load_pct"))
        result_data, scene_type = apply_job_label_guardrails(result_data, scene_type, room_labels)
        result_data, fail_safe_notes = apply_fail_safe_estimate_rules(
            result_data,
            scene_type,
            room_labels,
            num_photos,
            job.get("truck_load_pct"),
        )
        if fail_safe_notes:
            confidence_reasons.extend(fail_safe_notes)
        confidence_bucket, confidence_reasons, final_confidence = apply_scene_confidence_policy(
            result_data,
            photo_quality,
            scene_type,
            room_labels,
        )
        result_data["confidence"] = final_confidence
        result_data.setdefault("conditions", [])
        if scene_type and scene_type not in result_data["conditions"] and scene_type in {"truck_load"}:
            result_data["conditions"].append(scene_type)
        sanity = evaluate_geometry_sanity(
            result_data,
            scene_type,
            room_labels,
            job.get("truck_load_pct"),
            getattr(user, "truck_capacity_cy", 16.0) or 16.0,
        )
        occupancy_class = sanity["occupancy_class"]
        sanity_flags = list(sanity["sanity_flags"] or [])
        geometry_summary = sanity["geometry_summary"]
        if sanity_flags:
            confidence_reasons.append(geometry_summary)
            if any(flag in sanity_flags for flag in ("spatial_above_items", "items_above_spatial", "items_below_truck_hint", "items_above_truck_hint")):
                if confidence_bucket == "high":
                    confidence_bucket = "medium"
                result_data["confidence"] = min(int(result_data.get("confidence", 75) or 75), 78)
            if any(flag in sanity_flags for flag in ("raised_toward_truck_hint", "trimmed_toward_truck_hint")):
                adj_total = float(sanity["adjusted_total"] or 0.0)
                if adj_total > 0:
                    result_data["totals"]["cubic_yards_mid"] = round(adj_total, 1)
                    result_data["totals"]["cubic_yards_low"] = round(adj_total * 0.85, 1)
                    result_data["totals"]["cubic_yards_high"] = round(adj_total * 1.15, 1)
                    result_data["total_cubic_yards"] = round(adj_total, 1)

        final_item_sum = _sync_result_totals_to_items(result_data)
        scene_context = _scene_context_text(result_data, room_labels)
        broad_coverage = num_photos >= 4 or any(k in scene_context for k in ("living room", "bedroom", "kitchen", "bathroom", "office", "dining"))
        if (
            scene_type in {"garage_clutter", "bagged_trash_soft_goods", "mixed_junk", "storage_overflow"}
            or any(k in scene_context for k in ("garage", "basement", "storage", "shed"))
        ) and job.get("truck_load_pct") is None:
            max_reasonable_total = 8.0
            if final_item_sum > max_reasonable_total:
                result_data["totals"]["cubic_yards_mid"] = round(max_reasonable_total, 1)
                result_data["totals"]["cubic_yards_low"] = round(max_reasonable_total * 0.85, 1)
                result_data["totals"]["cubic_yards_high"] = round(max_reasonable_total * 1.15, 1)
                result_data["total_cubic_yards"] = round(max_reasonable_total, 1)
                confidence_bucket = "medium"
                if not broad_coverage:
                    confidence_reasons.append("Small visible garage/storage pickups are capped unless truck-load context or broader room coverage is confirmed.")

        actionable_duplicates = filter_actionable_duplicates(result_data)
        clarification_questions = build_required_clarification_questions(
            result_data,
            scene_type,
            actionable_duplicates,
            sanity_flags,
        )
        clarification_answers = job.get("clarification_answers") or {}
        unresolved_question_ids = [
            q.get("id", "")
            for q in clarification_questions
            if q.get("id") and not _has_truthy_answer(clarification_answers.get(q.get("id", "")))
        ]

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

        min_charge = user.min_charge or 75.0
        range_widened = False

        if verifier_result:
            verifier_data = validate_estimate(verifier_result)
            verifier_data, _verifier_pile_notes = apply_pile_adjustment(verifier_data)
            _sync_result_totals_to_items(verifier_data)
            verifier_data, _ = normalize_curbside_mixed_item_labels(verifier_data, room_labels)
            verifier_data, _ = normalize_special_fee_items(verifier_data)
            _sync_result_totals_to_items(verifier_data)
            verifier_scene_type = classify_scene_type(verifier_data, room_labels, job.get("truck_load_pct"))
            verifier_data, _ = apply_small_job_volume_guardrails(verifier_data, verifier_scene_type, room_labels)
            _sync_result_totals_to_items(verifier_data)
            verifier_scene_type = classify_scene_type(verifier_data, room_labels, job.get("truck_load_pct"))
            verifier_data, verifier_scene_type = apply_job_label_guardrails(verifier_data, verifier_scene_type, room_labels)
            _sync_result_totals_to_items(verifier_data)
            verifier_conf_bucket, _verifier_reasons, _verifier_conf = apply_scene_confidence_policy(
                verifier_data,
                photo_quality,
                verifier_scene_type,
                room_labels,
            )

            if custom_standard and custom_heavy:
                verifier_totals = verifier_data.get("totals", {})
                verifier_cy_mid = float(verifier_totals.get("cubic_yards_mid", verifier_totals.get("cubic_yards_low", 2.0)))
                verifier_job_type = verifier_data.get("job_type", "standard")
                verifier_conditions = verifier_data.get("conditions", [])
                verifier_heavy = (
                    verifier_job_type in ("premium", "hoarder", "truck_load")
                    or "stairs" in verifier_conditions
                    or "heavy_items" in verifier_conditions
                    or "hoarder" in verifier_conditions
                    or verifier_cy_mid > 10
                )
                verifier_rate = custom_heavy if verifier_heavy else custom_standard
                verifier_base = verifier_cy_mid * verifier_rate
                verifier_price_low = round(verifier_base * 0.90, 2)
                verifier_price_high = round(verifier_base * 1.20, 2)
                verifier_price_low = max(verifier_price_low, min_charge)
                verifier_price_high = max(verifier_price_high, min_charge)
                if verifier_price_high <= verifier_price_low or verifier_price_high < verifier_price_low * 1.15:
                    verifier_price_high = round(verifier_price_low * 1.5, 2)
            else:
                verifier_price_low, verifier_price_high, _v_cy, _v_special, _v_min_charge = calculate_price(
                    verifier_data,
                    rate_low=user.price_per_cy_low or 35.0,
                    rate_high=user.price_per_cy_high or 40.0,
                    rate_premium=user.price_per_cy_premium or 55.0,
                    min_charge=min_charge,
                    market_rates=None,
                )

            verifier_pct = _model_uncertainty_pct(verifier_conf_bucket, verifier_scene_type, num_photos)
            verifier_low, verifier_high = _expand_model_range(verifier_price_low, verifier_price_high, min_charge, verifier_pct)
            overlap_low, overlap_high, has_overlap = _price_overlap(primary_low, primary_high, verifier_low, verifier_high)

            if has_overlap:
                price_low, price_high = overlap_low, overlap_high
                confidence_reasons.append(
                    f"Range calibrated by Qwen+Pixtral overlap (primary ±{int(primary_pct * 100)}%, verifier ±{int(verifier_pct * 100)}%)."
                )
            else:
                review_reason_flags.append("model_disagreement_no_overlap")
                confidence_bucket = "low"
                result_data["confidence"] = min(int(result_data.get("confidence", 70) or 70), 70)
                price_low = min(primary_low, verifier_low)
                price_high = max(primary_high, verifier_high)
                if allow_manual_review:
                    review_status = "needs_review"
                    confidence_reasons.append("Two-model check did not overlap; estimate routed to manual review.")
                else:
                    confidence_reasons.append("Two-model check did not overlap; showing low-confidence range and clarification prompts.")
        else:
            # Fallback to prior widening when verifier output is unusable.
            price_low, price_high, fallback_widened = widen_price_range_for_confidence(
                price_low,
                price_high,
                min_charge,
                confidence_bucket,
                scene_type,
            )
            if fallback_widened:
                confidence_reasons.append("Price range widened slightly because this scene type carries more uncertainty.")

        calibration = await _get_lightweight_price_calibration(scene_type, job.get("capture_mode", "remote"))
        if calibration:
            factor = float(calibration.get("factor", 1.0) or 1.0)
            if factor > 1.0:
                price_low = max(min_charge, round(price_low * factor, 2))
                price_high = max(price_low, round(price_high * factor, 2))
                confidence_reasons.append(
                    f"Calibrated +{int(round((factor - 1.0) * 100))}% from recent actuals for this scene ({int(calibration.get('sample_size', 0) or 0)} jobs)."
                )

        severe_sanity = any(
            flag in sanity_flags for flag in ("spatial_above_items", "items_above_spatial", "items_below_truck_hint", "items_above_truck_hint")
        )
        if unresolved_question_ids or severe_sanity or confidence_bucket == "low" or review_status == "needs_review":
            review_status = "needs_review" if allow_manual_review else "auto_approved"
            review_reason_bits = list(review_reason_flags)
            if unresolved_question_ids:
                review_reason_bits.append(f"unresolved_clarifications:{','.join(unresolved_question_ids)}")
            if severe_sanity:
                review_reason_bits.append("sanity_conflict")
            if confidence_bucket == "low":
                review_reason_bits.append("low_confidence")
            review_reason = "; ".join(sorted(set(review_reason_bits)))[:240] if allow_manual_review else ""
            confidence_bucket = "low"
            result_data["confidence"] = min(int(result_data.get("confidence", 70) or 70), 70)
            if allow_manual_review:
                if "Estimate requires manual review before showing a customer-ready quote." not in confidence_reasons:
                    confidence_reasons.append("Estimate requires manual review before showing a customer-ready quote.")
                # Avoid narrow quotes when clarifications remain unresolved.
                widened_low = max(user.min_charge or 75.0, round(price_low * 0.82, 2))
                widened_high = max(widened_low, round(price_high * 1.35, 2))
                price_low, price_high = widened_low, widened_high
                range_widened = True
            else:
                if unresolved_question_ids:
                    qmap = {str(q.get("id", "")): str(q.get("question", "")).strip() for q in clarification_questions}
                    lines = [qmap.get(qid, "").strip() for qid in unresolved_question_ids if qmap.get(qid, "").strip()]
                    if lines:
                        clarification_note = "Clarifying questions to improve accuracy:\n- " + "\n- ".join(lines[:3])
                        existing_notes = str(result_data.get("notes", "") or "").strip()
                        result_data["notes"] = (existing_notes + "\n\n" if existing_notes else "") + clarification_note
                if "Low confidence estimate shown; answer clarification questions and retake photos if needed." not in confidence_reasons:
                    confidence_reasons.append("Low confidence estimate shown; answer clarification questions and retake photos if needed.")

        # Serialize stored photos for DB persistence
        stored_photos = job.get("stored_photos", [])
        photos_json_str = json.dumps(stored_photos) if stored_photos else ""
        logger.info(f"[run_estimate] Job {job_id}: saving {len(stored_photos)} photos ({len(photos_json_str)} bytes)")

        api_cost_cents_val = int(total_api_cost_cents or 0)
        token_input = total_input_tokens
        token_output = total_output_tokens
        token_model = (model_name or "")[:50]

        async with AsyncSessionLocal() as db:
            # First try to add the column if it doesn't exist (safety net)
            try:
                await db.execute(text("ALTER TABLE estimates ADD COLUMN IF NOT EXISTS photos_json TEXT DEFAULT ''"))
                await db.commit()
            except Exception as e:
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
                pass2_json=pass2_json_str,
                lookups_json=lookups_json_str,
                photos_json=photos_json_str,
                input_tokens=token_input,
                output_tokens=token_output,
                api_cost_cents=int(api_cost_cents_val),
                model_used=token_model,
                capture_mode=job.get("capture_mode", "remote"),
                confidence_bucket=confidence_bucket,
                confidence_reasons=json.dumps(confidence_reasons),
                photo_quality_flags=json.dumps(photo_quality_flags),
                scene_type=scene_type,
                occupancy_class=occupancy_class,
                sanity_flags=json.dumps(sanity_flags),
                geometry_summary=geometry_summary,
                review_status=review_status,
                review_reason=review_reason,
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
        except Exception as e:
            logger.warning("Operation failed: %s", e)

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
        except Exception as e:
            pass  # Don't fail the estimate if email fails

        credit_bal = getattr(user, 'credit_balance', 0) or 0
        free_left = max(0, 5 - (getattr(user, 'free_trial_used', 0) or 0))
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
            "confidence_bucket": confidence_bucket,
            "confidence_reasons": confidence_reasons,
            "photo_quality_flags": photo_quality_flags,
            "photo_guidance": photo_quality.get("guidance", []),
            "capture_mode": job.get("capture_mode", "remote"),
            "scene_type": scene_type,
            "scene_label": SCENE_DISPLAY_NAMES.get(scene_type, scene_type.replace("_", " ").title() if scene_type else ""),
            "range_widened": range_widened,
            "occupancy_class": occupancy_class,
            "sanity_flags": sanity_flags,
            "geometry_summary": geometry_summary,
            "review_status": review_status,
            "review_reason": review_reason,
            "clarification_questions": clarification_questions,
            "clarification_answers": clarification_answers,
            "unresolved_clarification_ids": unresolved_question_ids,
            "estimates_remaining": remaining,
            "special_items": special_items,
            "items_looked_up": lookups_done,
            "rate_low": user.price_per_cy_low or 35.0,
            "rate_high": user.price_per_cy_high or 40.0,
            "rate_premium": user.price_per_cy_premium or 55.0,
            "min_charge": user.min_charge or 75.0,
            "min_charge_applied": min_charge_applied,
            "potential_duplicates": actionable_duplicates,
        }

        if market_context:
            resp["market_context"] = market_context

        job["status"] = "needs_review" if review_status == "needs_review" else "complete"
        job["message"] = (
            "Estimate routed to manual review to avoid an unreliable quote."
            if review_status == "needs_review"
            else "Estimate ready!"
        )
        job["result"] = resp

    except Exception as e:
        import logging
        logging.getLogger("wsic.estimate").error(f"[run_estimate] Unhandled error for job {job_id}: {type(e).__name__}: {e}")
        job["status"] = "error"
        job["message"] = "An error occurred while processing your estimate. Please try again."
        job["result"] = None
        try:
            async with AsyncSessionLocal() as refund_db:
                refund_user = await refund_db.get(User, user.id)
                if refund_user:
                    if job.get("used_free_trial"):
                        refund_user.free_trial_used = max(0, (refund_user.free_trial_used or 1) - 1)
                    else:
                        refund_user.credit_balance = (refund_user.credit_balance or 0) + 1
                    refund_user.estimates_used = max(0, (refund_user.estimates_used or 1) - 1)
                    await refund_db.commit()
                    logging.getLogger("wsic.estimate").info(f"[run_estimate] Refunded credit for job {job_id}, user {user.id}")
        except Exception as refund_err:
            logging.getLogger("wsic.estimate").error(f"[run_estimate] Refund failed for job {job_id}: {refund_err}")
    finally:
        if job_id in estimate_jobs:
            final_job = estimate_jobs[job_id]
            await _upsert_job_to_db(
                job_id,
                final_job.get("user_id", 0),
                final_job.get("team_member_id", 0),
                final_job.get("status", "unknown"),
                json.dumps(final_job.get("result")) if final_job.get("result") else "",
                final_job.get("message", "") if final_job.get("status") == "error" else "",
                datetime.now(timezone.utc).replace(tzinfo=None) if final_job.get("status") in ("complete", "needs_review", "error") else None,
            )


@router_estimates.get("/api/estimate/status/{job_id}")
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
        await _delete_job_from_db(job_id)
    elif job["status"] == "needs_review":
        jr = job.get("result") or {}
        resp["result"] = {
            "review_status": "needs_review",
            "review_reason": jr.get("review_reason", ""),
            "clarification_questions": jr.get("clarification_questions", []),
        }
        del estimate_jobs[job_id]
        await _delete_job_from_db(job_id)
    elif job["status"] == "retry_needed":
        del estimate_jobs[job_id]
        await _delete_job_from_db(job_id)
    elif job["status"] == "error":
        del estimate_jobs[job_id]
        await _delete_job_from_db(job_id)

    return resp


@router_payments.post("/api/payments/create-checkout")
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


@router_payments.post("/api/payments/webhook")
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

    event_id = event.get("id", "")
    if event_id in _processed_webhook_events:
        return {"received": True, "duplicate": True}
    _processed_webhook_events.add(event_id)
    if len(_processed_webhook_events) > 10000:
        _processed_webhook_events.clear()

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


@router_estimates.get("/api/estimates")
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


@router_estimates.get("/api/estimates/{estimate_id}")
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
            except Exception as e:
                pass
        adjustments_data = {}
        if getattr(e, "adjustments_json", ""):
            try:
                adjustments_data = json.loads(e.adjustments_json)
            except Exception as e:
                adjustments_data = {}
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
            "capture_mode": getattr(e, "capture_mode", "remote") or "remote",
            "confidence_bucket": getattr(e, "confidence_bucket", "") or "",
            "confidence_reasons": _safe_json_loads(getattr(e, "confidence_reasons", "") or "[]", []),
            "photo_quality_flags": _safe_json_loads(getattr(e, "photo_quality_flags", "") or "[]", []),
            "scene_type": getattr(e, "scene_type", "") or "",
            "scene_label": SCENE_DISPLAY_NAMES.get(getattr(e, "scene_type", "") or "", ""),
            "occupancy_class": getattr(e, "occupancy_class", "") or "",
            "sanity_flags": _safe_json_loads(getattr(e, "sanity_flags", "") or "[]", []),
            "geometry_summary": getattr(e, "geometry_summary", "") or "",
            "review_status": getattr(e, "review_status", "auto_approved") or "auto_approved",
            "review_reason": getattr(e, "review_reason", "") or "",
            "adjustments": adjustments_data,
        }


def _normalize_adjustment_payload(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    excluded = payload.get("excluded_item_indices") or []
    qty_overrides = payload.get("quantity_overrides") or {}
    try:
        excluded_norm = sorted({int(x) for x in excluded if str(x).strip() != "" and int(x) >= 0})
    except Exception as e:
        excluded_norm = []
    qty_norm = {}
    if isinstance(qty_overrides, dict):
        for k, v in qty_overrides.items():
            try:
                idx = int(k)
                qty = int(v)
            except Exception as e:
                continue
            if idx >= 0 and qty >= 1:
                qty_norm[str(idx)] = qty
    def _as_float(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except Exception as e:
            return None

    out = {
        "excluded_item_indices": excluded_norm,
        "quantity_overrides": qty_norm,
        "adjusted_cy": _as_float(payload.get("adjusted_cy")),
        "adjusted_price_low": _as_float(payload.get("adjusted_price_low")),
        "adjusted_price_high": _as_float(payload.get("adjusted_price_high")),
    }
    out["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return out


@router_estimates.put("/api/estimates/{estimate_id}/adjustments")
async def save_estimate_adjustments(request: Request, estimate_id: int):
    user = await require_user(request)
    body = await request.json()
    normalized = _normalize_adjustment_payload(body)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Estimate).where(Estimate.id == estimate_id, Estimate.user_id == user.id))
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        e.adjustments_json = json.dumps(normalized)
        await db.commit()
    return {"ok": True, "adjustments": normalized}


@router_team.put("/api/team/estimates/{estimate_id}/adjustments")
async def team_save_estimate_adjustments(request: Request, estimate_id: int):
    member, owner = await require_team_member(request)
    body = await request.json()
    normalized = _normalize_adjustment_payload(body)
    normalized["team_member_id"] = member.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Estimate).where(
                Estimate.id == estimate_id,
                Estimate.user_id == owner.id,
                or_(Estimate.team_member_id == member.id, Estimate.team_member_id == 0),
            )
        )
        e = result.scalar_one_or_none()
        if not e:
            raise HTTPException(status_code=404, detail="Estimate not found.")
        e.adjustments_json = json.dumps(normalized)
        await db.commit()
    return {"ok": True, "adjustments": normalized}


@router_estimates.get("/api/estimates/{estimate_id}/photos")
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
        except Exception as e:
            photos = []
        return {"photos": photos, "count": len(photos)}


@router_estimates.get("/api/estimates/{estimate_id}/photo/{photo_index}")
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


@router_estimates.get("/api/usage")
async def get_usage(request: Request):
    user = await require_user(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404)
        reset_billing_cycle_if_needed(u)
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


@router_estimates.put("/api/usage/settings")
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


@router_estimates.post("/api/usage/add-funds")
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


@router_admin.get("/api/admin/analytics")
async def admin_analytics(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
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


@router_admin.get("/api/admin/api-costs")
async def admin_api_costs(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
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


@router_admin.get("/api/admin/provider-health")
async def admin_provider_health(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        since = now - timedelta(days=7)

        rows = (await db.execute(
            select(ProviderHealthEvent)
            .where(ProviderHealthEvent.created_at >= since)
            .order_by(ProviderHealthEvent.created_at.desc())
            .limit(500)
        )).scalars().all()

        summary = {}
        for row in rows:
            key = row.provider_name or "unknown"
            if key not in summary:
                summary[key] = {
                    "provider_name": key,
                    "model_name": row.model_name or "",
                    "success_count": 0,
                    "failure_count": 0,
                    "avg_latency_ms": 0,
                    "last_error_type": "",
                    "last_error_message": "",
                    "last_seen_at": row.created_at.isoformat() if row.created_at else None,
                }
            item = summary[key]
            if row.status == "success":
                item["success_count"] += 1
                if row.latency_ms:
                    prev = item["avg_latency_ms"]
                    n = item["success_count"]
                    item["avg_latency_ms"] = int(((prev * (n - 1)) + row.latency_ms) / n)
            else:
                item["failure_count"] += 1
                if not item["last_error_message"]:
                    item["last_error_type"] = row.error_type or ""
                    item["last_error_message"] = row.error_message or ""

        recent_events = [
            {
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "provider_name": row.provider_name,
                "model_name": row.model_name,
                "status": row.status,
                "error_type": row.error_type,
                "error_message": row.error_message,
                "photos_count": row.photos_count,
                "latency_ms": row.latency_ms,
            }
            for row in rows[:100]
        ]

        cfg_rows = (await db.execute(select(SiteConfig))).scalars().all()
        cfg = {str(r.config_key or ""): str(r.config_value or "") for r in cfg_rows}

        configured_order = [
            (cfg.get("estimate_provider_primary", "") or "").strip().lower(),
            (cfg.get("estimate_provider_fallback_1", "") or "").strip().lower(),
            (cfg.get("estimate_provider_fallback_2", "") or "").strip().lower(),
        ]
        allowed = {"gemini", "venice", "openrouter"}
        requested_order = []
        for p in configured_order:
            if p in allowed and p not in requested_order:
                requested_order.append(p)
        for default_p in ["gemini", "venice", "openrouter"]:
            if default_p not in requested_order:
                requested_order.append(default_p)

        key_status = {
            "gemini": bool((os.environ.get("GEMINI_API_KEY") or "").strip()),
            "venice": bool((os.environ.get("VENICE_API_KEY") or "").strip()),
            "openrouter": bool((os.environ.get("OPENROUTER_API_KEY") or "").strip()),
        }
        runtime_order = [p for p in requested_order if key_status.get(p)]

        return {
            "window": "7d",
            "providers": list(summary.values()),
            "recent_events": recent_events,
            "routing": {
                "configured_order": configured_order,
                "requested_order": requested_order,
                "runtime_order": runtime_order,
                "api_key_status": key_status,
                "models": {
                    "gemini": (cfg.get("estimate_model_gemini", "") or "").strip() or (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"),
                    "venice": (cfg.get("estimate_model_venice", "") or "").strip() or (os.environ.get("VENICE_MODEL") or "qwen3-vl-235b-a22b"),
                    "openrouter": (cfg.get("estimate_model_openrouter", "") or "").strip() or (os.environ.get("OPENROUTER_MODEL") or "qwen/qwen2.5-vl-72b-instruct"),
                },
            },
        }


@router_admin.get("/api/admin/users")
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


@router_admin.get("/api/admin/plans")
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


@router_admin.put("/api/admin/plans/{plan_id}")
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


@router_admin.get("/api/site-config")
async def public_site_config():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SiteConfig))
        configs = result.scalars().all()
        return {c.config_key: c.config_value for c in configs}


@router_admin.get("/api/admin/site-config")
async def admin_get_site_config(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SiteConfig))
        configs = result.scalars().all()
        return {c.config_key: c.config_value for c in configs}


@router_admin.put("/api/admin/site-config")
async def admin_update_site_config(request: Request):
    await require_admin(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        for key, value in body.items():
            result = await db.execute(select(SiteConfig).where(SiteConfig.config_key == key))
            config = result.scalar_one_or_none()
            if config:
                config.config_value = str(value)
                config.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                db.add(SiteConfig(config_key=key, config_value=str(value)))
        await db.commit()
        return {"success": True}


@router_admin.get("/api/admin/estimates")
async def admin_estimates(
    request: Request,
    page: int = 1,
    q: str = "",
    capture_mode: str = "",
    review_status: str = "",
):
    await require_admin(request)
    limit = 25
    offset = (page - 1) * limit
    capture_mode = normalize_capture_mode(capture_mode) if capture_mode else ""
    review_status = str(review_status or "").strip().lower()
    async with AsyncSessionLocal() as db:
        query = select(Estimate)
        count_query = select(func.count(Estimate.id))
        if capture_mode:
            query = query.where(Estimate.capture_mode == capture_mode)
            count_query = count_query.where(Estimate.capture_mode == capture_mode)
        if review_status in {"auto_approved", "needs_review"}:
            query = query.where(Estimate.review_status == review_status)
            count_query = count_query.where(Estimate.review_status == review_status)
        total = (await db.execute(count_query)).scalar() or 0
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
                 "capture_mode": getattr(e, "capture_mode", "remote") or "remote",
                 "review_status": getattr(e, "review_status", "auto_approved") or "auto_approved",
                 "review_reason": getattr(e, "review_reason", "") or "",
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in estimates
            ],
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
            "capture_mode": capture_mode or "all",
            "review_status": review_status or "all",
        }


@router_admin.get("/api/admin/estimates/{estimate_id}")
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
            except Exception as e:
                logger.debug("Fallback handled: %s", e)

        # Parse lookups
        lookups = []
        if e.lookups_json:
            try:
                lookups = json.loads(e.lookups_json)
            except Exception as e:
                logger.debug("Fallback handled: %s", e)
        adjustments = {}
        if getattr(e, "adjustments_json", ""):
            try:
                adjustments = json.loads(e.adjustments_json)
            except Exception as e:
                adjustments = {}

        # Build photos array with data URLs
        photos = []
        if e.photos_json:
            try:
                raw_photos = json.loads(e.photos_json)
                for idx, b64 in enumerate(raw_photos):
                    photos.append({"index": idx, "data_url": f"data:image/jpeg;base64,{b64}"})
            except Exception as e:
                logger.debug("Fallback handled: %s", e)

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
            "actual_truck_fraction": getattr(e, "actual_truck_fraction", None),
            "accuracy_notes": getattr(e, 'accuracy_notes', '') or "",
            "correction_reason": getattr(e, "correction_reason", "") or "",
            "company_timezone": company_timezone,
            "capture_mode": getattr(e, "capture_mode", "remote") or "remote",
            "confidence_bucket": getattr(e, "confidence_bucket", "") or "",
            "confidence_reasons": _safe_json_loads(getattr(e, "confidence_reasons", "") or "[]", []),
            "photo_quality_flags": _safe_json_loads(getattr(e, "photo_quality_flags", "") or "[]", []),
            "scene_type": getattr(e, "scene_type", "") or "",
            "scene_label": SCENE_DISPLAY_NAMES.get(getattr(e, "scene_type", "") or "", ""),
            "occupancy_class": getattr(e, "occupancy_class", "") or "",
            "sanity_flags": _safe_json_loads(getattr(e, "sanity_flags", "") or "[]", []),
            "geometry_summary": getattr(e, "geometry_summary", "") or "",
            "review_status": getattr(e, "review_status", "auto_approved") or "auto_approved",
            "review_reason": getattr(e, "review_reason", "") or "",
            "adjustments": adjustments,
        }


@router_admin.put("/api/admin/estimates/{estimate_id}/actual-price")
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
        if "actual_truck_fraction" in body:
            val = body["actual_truck_fraction"]
            if val is None or val == "":
                e.actual_truck_fraction = None
            else:
                e.actual_truck_fraction = max(0.0, min(1.0, float(val)))
        if "accuracy_notes" in body:
            e.accuracy_notes = str(body["accuracy_notes"] or "")
        if "correction_reason" in body:
            e.correction_reason = str(body["correction_reason"] or "").strip()[:40]
        await db.commit()
        return {
            "ok": True,
            "actual_price": e.actual_price,
            "actual_cy": e.actual_cy,
            "actual_truck_fraction": getattr(e, "actual_truck_fraction", None),
            "accuracy_notes": e.accuracy_notes or "",
            "correction_reason": getattr(e, "correction_reason", "") or "",
        }


@router_admin.get("/api/admin/users/{user_id}")
async def admin_user_detail(request: Request, user_id: int, page: int = 1, est_page: int = 1):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        est_limit = 20
        est_offset = (est_page - 1) * est_limit
        count_result = await db.execute(
            select(func.count(Estimate.id)).where(Estimate.user_id == user_id)
        )
        total_estimates = count_result.scalar() or 0
        est_result = await db.execute(
            select(Estimate)
            .where(Estimate.user_id == user_id)
            .order_by(Estimate.created_at.desc())
            .offset(est_offset)
            .limit(est_limit)
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
            "total_estimates": total_estimates,
            "est_page": est_page,
            "est_pages": (total_estimates + est_limit - 1) // est_limit,
            "estimates": [
                {"id": e.id, "photos_count": e.photos_count, "price_low": e.price_low,
                 "price_high": e.price_high, "cy_estimate": e.cy_estimate,
                 "created_at": e.created_at.isoformat() if e.created_at else None,
                 "actual_price": e.actual_price}
                for e in estimates
            ],
        }


@router_admin.put("/api/admin/users/{user_id}")
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


@router_admin.post("/api/admin/users/{user_id}/reset-password")
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

        send_email(
            u.email,
            "Your WhatShouldICharge Password Has Been Reset",
            f"<h2>Password Reset</h2>"
            f"<p>An administrator has reset your password. Here is your new temporary password:</p>"
            f"<div style='background:#f4f4f4;padding:16px;border-radius:8px;font-family:monospace;font-size:18px;margin:16px 0;text-align:center;'>{new_password}</div>"
            f"<p>Please log in with this password and change it immediately.</p>"
            f"<p>— The WhatShouldICharge Team</p>"
        )

    return {"ok": True, "message": "New password sent to user's email."}


# ── Admin: Credit packs (DB + Stripe) ──

@router_admin.get("/api/admin/credit-packs")
async def admin_list_credit_packs(request: Request):
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CreditPack).order_by(CreditPack.sort_order, CreditPack.id))
        rows = result.scalars().all()
    return {"packs": [_credit_pack_admin_dict(p) for p in rows]}


@router_admin.post("/api/admin/credit-packs")
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


@router_admin.put("/api/admin/credit-packs/{pack_id}")
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


@router_admin.delete("/api/admin/credit-packs/{pack_id}")
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

@router_admin.get("/api/admin/promos")
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


@router_admin.post("/api/admin/promos")
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


@router_admin.put("/api/admin/promos/{promo_id}")
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


@router_admin.delete("/api/admin/promos/{promo_id}")
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


@router_promo.post("/api/promo/validate")
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
        if p.expires_at and p.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            return {"valid": False, "reason": "Code expired"}
        if p.usage_limit > 0 and p.times_used >= p.usage_limit:
            return {"valid": False, "reason": "Code usage limit reached"}
        return {"valid": True, "discount_type": p.discount_type, "discount_value": p.discount_value}


# ── Accuracy API ──

@router_admin.get("/api/admin/accuracy")
async def admin_accuracy(request: Request, capture_mode: str = ""):
    await require_admin(request)
    capture_mode = normalize_capture_mode(capture_mode) if capture_mode else ""
    async with AsyncSessionLocal() as db:
        def _price_accuracy(e: Estimate) -> float | None:
            mid = (e.price_low + e.price_high) / 2 if e.price_low and e.price_high else 0
            if e.actual_price and mid > 0:
                return 1 - abs(mid - e.actual_price) / e.actual_price
            return None

        def _cy_accuracy(e: Estimate) -> float | None:
            if e.actual_cy and e.cy_estimate and e.actual_cy > 0:
                return 1 - abs(e.cy_estimate - e.actual_cy) / e.actual_cy
            return None

        def _avg_pct(vals: list[float]) -> float:
            return round(sum(vals) / len(vals) * 100, 1) if vals else 0

        def _build_breakdown(estimates: list[Estimate], key_fn, label_fn):
            buckets: dict[str, dict] = {}
            for e in estimates:
                key = str(key_fn(e) or "").strip() or "unknown"
                bucket = buckets.setdefault(key, {"count": 0, "price_accs": [], "cy_accs": []})
                bucket["count"] += 1
                price_acc = _price_accuracy(e)
                cy_acc = _cy_accuracy(e)
                if price_acc is not None:
                    bucket["price_accs"].append(price_acc)
                if cy_acc is not None:
                    bucket["cy_accs"].append(cy_acc)
            rows = []
            for key, data in buckets.items():
                rows.append({
                    "key": key,
                    "label": label_fn(key),
                    "count": data["count"],
                    "avg_price_accuracy": _avg_pct(data["price_accs"]),
                    "avg_cy_accuracy": _avg_pct(data["cy_accs"]),
                })
            rows.sort(key=lambda row: (-row["count"], row["label"]))
            return rows

        # Estimates with actual data
        price_query = select(Estimate).where(Estimate.actual_price.isnot(None))
        if capture_mode:
            price_query = price_query.where(Estimate.capture_mode == capture_mode)
        with_price = await db.execute(price_query)
        price_estimates = with_price.scalars().all()

        total_with_actuals = len(price_estimates)
        price_accuracies = []
        over_count = 0
        under_count = 0
        for e in price_estimates:
            acc = _price_accuracy(e)
            if acc is not None:
                price_accuracies.append(acc)
                mid = (e.price_low + e.price_high) / 2 if e.price_low and e.price_high else 0
                if mid > e.actual_price:
                    over_count += 1
                elif mid < e.actual_price:
                    under_count += 1

        avg_price_accuracy = _avg_pct(price_accuracies)

        # CY accuracy
        cy_query = select(Estimate).where(Estimate.actual_cy.isnot(None))
        if capture_mode:
            cy_query = cy_query.where(Estimate.capture_mode == capture_mode)
        with_cy = await db.execute(cy_query)
        cy_estimates = with_cy.scalars().all()
        cy_accuracies = []
        for e in cy_estimates:
            acc = _cy_accuracy(e)
            if acc is not None:
                cy_accuracies.append(acc)
        avg_cy_accuracy = _avg_pct(cy_accuracies)

        actuals_query = select(Estimate).where(
            or_(
                Estimate.actual_price.isnot(None),
                Estimate.actual_cy.isnot(None),
                Estimate.actual_truck_fraction.isnot(None),
            )
        )
        if capture_mode:
            actuals_query = actuals_query.where(Estimate.capture_mode == capture_mode)
        actuals_result = await db.execute(actuals_query)
        calibrated_estimates = actuals_result.scalars().all()

        # Needs data queue: estimates older than 7 days without actual_price
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        needs_data_query = (
            select(Estimate)
            .where(Estimate.actual_price.is_(None), Estimate.created_at < cutoff)
            .order_by(Estimate.created_at.desc())
            .limit(50)
        )
        if capture_mode:
            needs_data_query = needs_data_query.where(Estimate.capture_mode == capture_mode)
        needs_data_result = await db.execute(needs_data_query)
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
            price_acc = _price_accuracy(e)
            if price_acc is not None:
                company_map[e.user_id]["price_accs"].append(price_acc)
                company_map[e.user_id]["count"] += 1
            cy_acc = _cy_accuracy(e)
            if cy_acc is not None:
                company_map[e.user_id]["cy_accs"].append(cy_acc)

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
                "avg_price_accuracy": _avg_pct(data["price_accs"]),
                "avg_cy_accuracy": _avg_pct(data["cy_accs"]),
            })

        by_scene_type = _build_breakdown(
            calibrated_estimates,
            lambda e: getattr(e, "scene_type", "") or "",
            lambda key: SCENE_DISPLAY_NAMES.get(key, key.replace("_", " ").title() if key != "unknown" else "Unknown"),
        )
        by_confidence_bucket = _build_breakdown(
            calibrated_estimates,
            lambda e: getattr(e, "confidence_bucket", "") or "",
            lambda key: key.replace("_", " ").title() if key != "unknown" else "Unknown",
        )
        by_capture_mode = _build_breakdown(
            calibrated_estimates,
            lambda e: getattr(e, "capture_mode", "") or "remote",
            lambda key: key.replace("_", " ").title() if key != "unknown" else "Unknown",
        )
        by_review_status = _build_breakdown(
            calibrated_estimates,
            lambda e: getattr(e, "review_status", "") or "auto_approved",
            lambda key: key.replace("_", " ").title() if key != "unknown" else "Unknown",
        )

        miss_reason_counts: dict[str, int] = {}
        for e in calibrated_estimates:
            reason = (getattr(e, "correction_reason", "") or "").strip() or "unspecified"
            miss_reason_counts[reason] = miss_reason_counts.get(reason, 0) + 1
        miss_reasons = [
            {"reason": reason.replace("_", " ").title() if reason != "unspecified" else "Unspecified", "count": count}
            for reason, count in sorted(miss_reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ]

        return {
            "total_with_actuals": total_with_actuals,
            "avg_price_accuracy": avg_price_accuracy,
            "avg_cy_accuracy": avg_cy_accuracy,
            "calibrated_count": len(calibrated_estimates),
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
            "by_scene_type": by_scene_type,
            "by_confidence_bucket": by_confidence_bucket,
            "by_capture_mode": by_capture_mode,
            "by_review_status": by_review_status,
            "miss_reasons": miss_reasons,
            "capture_mode": capture_mode or "all",
        }


@router_admin.get("/api/admin/accuracy/export.csv")
async def admin_accuracy_export(
    request: Request,
    capture_mode: str = "",
    review_status: str = "",
):
    await require_admin(request)
    capture_mode = normalize_capture_mode(capture_mode) if capture_mode else ""
    review_status = str(review_status or "").strip().lower()

    async with AsyncSessionLocal() as db:
        query = select(Estimate).order_by(Estimate.created_at.desc()).limit(5000)
        if capture_mode:
            query = query.where(Estimate.capture_mode == capture_mode)
        if review_status in {"auto_approved", "needs_review"}:
            query = query.where(Estimate.review_status == review_status)
        result = await db.execute(query)
        estimates = result.scalars().all()

    header = [
        "estimate_id",
        "created_at",
        "capture_mode",
        "review_status",
        "review_reason",
        "scene_type",
        "confidence_bucket",
        "cy_estimate",
        "actual_cy",
        "cy_error_abs",
        "cy_error_pct",
        "price_low",
        "price_high",
        "price_mid",
        "actual_price",
        "price_error_abs",
        "price_error_pct",
        "correction_reason",
        "sanity_flags",
        "geometry_summary",
    ]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for e in estimates:
        price_mid = ((e.price_low or 0.0) + (e.price_high or 0.0)) / 2.0
        cy_err_abs = ""
        cy_err_pct = ""
        if e.actual_cy and e.cy_estimate is not None:
            cy_err_abs = round(abs((e.cy_estimate or 0.0) - (e.actual_cy or 0.0)), 2)
            cy_err_pct = round((cy_err_abs / max(e.actual_cy, 0.001)) * 100.0, 2)
        price_err_abs = ""
        price_err_pct = ""
        if e.actual_price and price_mid:
            price_err_abs = round(abs(price_mid - e.actual_price), 2)
            price_err_pct = round((price_err_abs / max(e.actual_price, 0.01)) * 100.0, 2)
        writer.writerow([
            e.id,
            e.created_at.isoformat() if e.created_at else "",
            getattr(e, "capture_mode", "remote") or "remote",
            getattr(e, "review_status", "auto_approved") or "auto_approved",
            getattr(e, "review_reason", "") or "",
            getattr(e, "scene_type", "") or "",
            getattr(e, "confidence_bucket", "") or "",
            e.cy_estimate if e.cy_estimate is not None else "",
            e.actual_cy if e.actual_cy is not None else "",
            cy_err_abs,
            cy_err_pct,
            e.price_low if e.price_low is not None else "",
            e.price_high if e.price_high is not None else "",
            round(price_mid, 2) if price_mid else "",
            e.actual_price if e.actual_price is not None else "",
            price_err_abs,
            price_err_pct,
            getattr(e, "correction_reason", "") or "",
            "|".join(_safe_json_loads(getattr(e, "sanity_flags", "") or "[]", [])),
            (getattr(e, "geometry_summary", "") or "").replace("\n", " ")[:300],
        ])

    filename = f"wsic-accuracy-export-{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Env Status API ──

@router_admin.get("/api/admin/env-status")
async def admin_env_status(request: Request) -> dict[str, bool]:
    await require_admin(request)
    keys = ["GEMINI_API_KEY", "VENICE_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
            "TAVILY_API_KEY", "SENDGRID_API_KEY"]
    out: dict[str, bool] = {}
    for k in keys:
        v = os.environ.get(k)
        out[k] = bool(isinstance(v, str) and v.strip())
    return out


@router_admin.get("/api/admin/error-report")
async def admin_error_report(request: Request):
    """Return recent estimate errors, provider failures, and system health metrics."""
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)

        failed_estimates_24h = (await db.execute(
            select(func.count(Estimate.id)).where(
                Estimate.created_at >= since_24h,
                Estimate.review_status == "needs_review",
            )
        )).scalar() or 0

        provider_failures_24h = (await db.execute(
            select(func.count(ProviderHealthEvent.id)).where(
                ProviderHealthEvent.created_at >= since_24h,
                ProviderHealthEvent.status != "success",
            )
        )).scalar() or 0

        recent_provider_errors = (await db.execute(
            select(ProviderHealthEvent)
            .where(ProviderHealthEvent.created_at >= since_7d, ProviderHealthEvent.status != "success")
            .order_by(ProviderHealthEvent.created_at.desc())
            .limit(20)
        )).scalars().all()

        total_estimates_7d = (await db.execute(
            select(func.count(Estimate.id)).where(Estimate.created_at >= since_7d)
        )).scalar() or 0

        success_rate_7d = 0.0
        if total_estimates_7d > 0:
            failed_7d = (await db.execute(
                select(func.count(Estimate.id)).where(
                    Estimate.created_at >= since_7d,
                    Estimate.review_status == "needs_review",
                )
            )).scalar() or 0
            success_rate_7d = round((1 - failed_7d / total_estimates_7d) * 100, 1)

        avg_latency_7d = (await db.execute(
            select(func.avg(ProviderHealthEvent.latency_ms)).where(
                ProviderHealthEvent.created_at >= since_7d,
                ProviderHealthEvent.status == "success",
                ProviderHealthEvent.latency_ms > 0,
            )
        )).scalar() or 0

        error_by_type: dict[str, int] = {}
        for ev in recent_provider_errors:
            etype = ev.error_type or "unknown"
            error_by_type[etype] = error_by_type.get(etype, 0) + 1

        return {
            "failed_estimates_24h": failed_estimates_24h,
            "provider_failures_24h": provider_failures_24h,
            "success_rate_7d": success_rate_7d,
            "total_estimates_7d": total_estimates_7d,
            "avg_latency_ms_7d": int(float(avg_latency_7d)) if avg_latency_7d else 0,
            "error_by_type": [{"type": k, "count": v} for k, v in sorted(error_by_type.items(), key=lambda x: -x[1])],
            "recent_errors": [
                {
                    "created_at": ev.created_at.isoformat() if ev.created_at else None,
                    "provider": ev.provider_name,
                    "model": ev.model_name,
                    "error_type": ev.error_type,
                    "error_message": (ev.error_message or "")[:200],
                    "estimate_id": ev.estimate_id if ev.estimate_id else None,
                }
                for ev in recent_provider_errors[:10]
            ],
        }


@router_admin.get("/api/admin/error-report")
async def admin_error_report(request: Request):
    """Return recent estimate errors, provider failures, and system health metrics."""
    await require_admin(request)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)

        failed_estimates_24h = (await db.execute(
            select(func.count(Estimate.id)).where(
                Estimate.created_at >= since_24h,
                Estimate.review_status == "needs_review",
            )
        )).scalar() or 0

        provider_failures_24h = (await db.execute(
            select(func.count(ProviderHealthEvent.id)).where(
                ProviderHealthEvent.created_at >= since_24h,
                ProviderHealthEvent.status != "success",
            )
        )).scalar() or 0

        recent_provider_errors = (await db.execute(
            select(ProviderHealthEvent)
            .where(ProviderHealthEvent.created_at >= since_7d, ProviderHealthEvent.status != "success")
            .order_by(ProviderHealthEvent.created_at.desc())
            .limit(20)
        )).scalars().all()

        total_estimates_7d = (await db.execute(
            select(func.count(Estimate.id)).where(Estimate.created_at >= since_7d)
        )).scalar() or 0

        success_rate_7d = 0.0
        if total_estimates_7d > 0:
            failed_7d = (await db.execute(
                select(func.count(Estimate.id)).where(
                    Estimate.created_at >= since_7d,
                    Estimate.review_status == "needs_review",
                )
            )).scalar() or 0
            success_rate_7d = round((1 - failed_7d / total_estimates_7d) * 100, 1)

        avg_latency_7d = (await db.execute(
            select(func.avg(ProviderHealthEvent.latency_ms)).where(
                ProviderHealthEvent.created_at >= since_7d,
                ProviderHealthEvent.status == "success",
                ProviderHealthEvent.latency_ms > 0,
            )
        )).scalar() or 0

        error_by_type: dict[str, int] = {}
        for ev in recent_provider_errors:
            etype = ev.error_type or "unknown"
            error_by_type[etype] = error_by_type.get(etype, 0) + 1

        return {
            "failed_estimates_24h": failed_estimates_24h,
            "provider_failures_24h": provider_failures_24h,
            "success_rate_7d": success_rate_7d,
            "total_estimates_7d": total_estimates_7d,
            "avg_latency_ms_7d": int(float(avg_latency_7d)) if avg_latency_7d else 0,
            "error_by_type": [{"type": k, "count": v} for k, v in sorted(error_by_type.items(), key=lambda x: -x[1])],
            "recent_errors": [
                {
                    "created_at": ev.created_at.isoformat() if ev.created_at else None,
                    "provider": ev.provider_name,
                    "model": ev.model_name,
                    "error_type": ev.error_type,
                    "error_message": (ev.error_message or "")[:200],
                    "estimate_id": ev.estimate_id if ev.estimate_id else None,
                }
                for ev in recent_provider_errors[:10]
            ],
        }


@router_admin.get("/api/admin/usage")
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


@router_admin.get("/api/admin/users/{user_id}/usage")
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


@router_team.post("/api/team/members")
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


@router_team.get("/api/team/members")
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


@router_team.put("/api/team/members/{member_id}")
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


@router_team.delete("/api/team/members/{member_id}")
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


@router_team.post("/api/team/auth")
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
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=12),
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


@router_team.get("/api/team/me")
async def team_me(request: Request):
    member, owner = await get_team_member(request)
    if not member:
        return JSONResponse({"authenticated": False})
    credit_bal = getattr(owner, 'credit_balance', 0) or 0
    free_left = max(0, 5 - (getattr(owner, 'free_trial_used', 0) or 0))
    remaining = credit_bal + free_left
    return {
        "authenticated": True,
        "name": member.name,
        "role": member.role,
        "company_name": owner.company_name,
        "estimates_remaining": remaining,
    }


@router_team.post("/api/team/estimate")
async def team_create_estimate(
    request: Request,
    files: list[UploadFile] = File(...),
    rooms: str = Form(default="[]"),
    truck_load_pct: Optional[float] = Form(default=None),
    estimate_name: str = Form(default=""),
    customer_name: str = Form(default=""),
    customer_email: str = Form(default=""),
    customer_phone: str = Form(default=""),
    capture_mode: str = Form(default="remote"),
    clarification_answers_json: str = Form(default=""),
):
    member, owner = await require_team_member(request)
    cleanup_expired_jobs()
    check_concurrent_limit()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == owner.id))
        fresh_owner = result.scalar_one_or_none()
        if not fresh_owner:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        reset_billing_cycle_if_needed(fresh_owner)
        allowed, err = check_usage_limit(fresh_owner)
        if not allowed:
            return JSONResponse(status_code=429, content=err)
        user = fresh_owner

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI service is not configured. Please contact support.")

    capture_mode = normalize_capture_mode(capture_mode)
    photo_data, image_content, stored_photos, photo_quality = await prepare_estimate_photos(
        files,
        rooms,
        max_files=20,
        default_room="Unknown",
        capture_mode=capture_mode,
    )
    if photo_quality["retry_needed"]:
        return JSONResponse(
            status_code=200,
            content={
                "status": "retry_needed",
                "message": photo_quality["retry_message"],
                "photo_quality": photo_quality,
            },
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.id == owner.id).with_for_update()
        )
        fresh_owner = result.scalar_one_or_none()
        if not fresh_owner:
            raise HTTPException(status_code=403, detail="estimate_limit_reached")
        reset_billing_cycle_if_needed(fresh_owner)
        allowed, err = check_usage_limit(fresh_owner)
        if not allowed:
            return JSONResponse(status_code=429, content=err)
        record_usage(fresh_owner)
        fresh_owner.estimates_used = (fresh_owner.estimates_used or 0) + 1
        await db.commit()
        user = fresh_owner

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if capture_mode == "operator_assist":
        image_content.append({
            "type": "text",
            "text": (
                "\nCapture mode: operator_assist. These photos should cover the same pile with a wide shot, "
                "left angle, and right angle. Prefer visible floor edges and avoid duplicate angles."
            )
        })
    _check_user_rate_limit(owner.id)
    job_id = f"team-{member.id}-{secrets.token_hex(8)}"
    estimate_jobs[job_id] = {
        "status": "analyzing",
        "message": "Analyzing photos...",
        "result": None,
        "user_id": owner.id,
        "team_member_id": member.id,
        "estimate_name": estimate_name.strip(),
        "customer_name": _sanitize_customer_input(customer_name),
        "customer_email": _sanitize_customer_input(customer_email, max_length=254),
        "customer_phone": _sanitize_customer_input(customer_phone, max_length=30),
        "created_at": now,
        "stored_photos": stored_photos,
        "capture_mode": capture_mode,
        "clarification_answers": parse_clarification_answers(clarification_answers_json),
        "photo_quality": photo_quality,
        "room_labels": _normalize_room_labels(photo_data),
        "truck_load_pct": truck_load_pct,
        "review_mode": "self_serve_clarify",
        "used_free_trial": False,
    }
    await _upsert_job_to_db(job_id, owner.id, member.id, "analyzing")
    _record_user_estimate(owner.id)

    asyncio.create_task(run_estimate(
        job_id=job_id,
        user=user,
        image_content=image_content,
        api_key=api_key,
        num_photos=len(files),
    ))

    return {"job_id": job_id}


@router_team.get("/api/team/estimate/status/{job_id}")
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
    elif job["status"] == "needs_review":
        jr = job.get("result") or {}
        resp["result"] = {
            "review_status": "needs_review",
            "review_reason": jr.get("review_reason", ""),
            "clarification_questions": jr.get("clarification_questions", []),
        }
    elif job["status"] == "retry_needed":
        pass
    elif job["status"] == "error":
        resp["error"] = job["message"]
    return resp


@router_team.get("/api/team/estimates")
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


@router_team.post("/api/team/logout")
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


@router_pdf.post("/api/estimate/{estimate_id}/pdf")
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


@router_pdf.post("/api/estimate/{estimate_id}/send")
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


app.router.lifespan_context = lifespan

app.include_router(router_health)
app.include_router(router_credits)
app.include_router(router_pages)
app.include_router(router_public)
app.include_router(router_auth)
app.include_router(router_settings)
app.include_router(router_library)
app.include_router(router_estimates)
app.include_router(router_payments)
app.include_router(router_admin)
app.include_router(router_team)
app.include_router(router_pdf)
app.include_router(router_promo)

app.mount("/static", StaticFiles(directory="static"), name="static")
