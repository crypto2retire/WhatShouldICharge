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
            "img-src 'self' data: https:; "
            "connect-src 'self' https://api.stripe.com; "
            "frame-src https://js.stripe.com; "
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

DATABASE_URL = "sqlite+aiosqlite:///./estimates.db"
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
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
    async with engine.begin() as conn:
        alter_statements = [
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN team_member_id INTEGER",
            "ALTER TABLE estimates ADD COLUMN customer_name TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_email TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN customer_phone TEXT DEFAULT ''",
        ]
        for stmt in alter_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass


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
            return
        defaults = {
            "hero_title": "Junk Removal Pricing.",
            "hero_subtitle": "Instant AI Estimates.",
            "hero_description": "Upload customer photos. Get an instant AI price estimate with cubic yard calculations. Close more jobs — without the guesswork.",
            "cta_primary": "Try It Free →",
            "cta_secondary": "See How It Works",
            "feature_1_title": "Upload Photos",
            "feature_1_desc": "Snap photos of the items. Our AI handles the rest.",
            "feature_2_title": "Get Instant Pricing",
            "feature_2_desc": "AI calculates cubic yards and gives you a price range in seconds.",
            "feature_3_title": "Close More Jobs",
            "feature_3_desc": "Send professional estimates to customers. Win more bids.",
            "faq_1_q": "How accurate are the estimates?",
            "faq_1_a": "Our AI achieves 85-95% accuracy by analyzing items, calculating cubic yards, and applying current market rates for your area.",
            "faq_2_q": "What types of junk can it estimate?",
            "faq_2_a": "Furniture, appliances, electronics, yard waste, construction debris, and more. Over 90 item types in our reference library.",
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
        "<p>Upload customer photos and get instant AI-powered pricing.</p>"
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
        "is_admin": bool(user.is_admin),
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

    price_low = max(price_low, min_charge)
    price_high = max(price_high, min_charge)

    special_items = [
        {"name": item.get("name", "Unknown"), "quantity": int(item.get("quantity", 1))}
        for item in items if item.get("is_special")
    ]

    return round(price_low, 2), round(price_high, 2), round(cy_mid, 1), special_items


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
  "reference_points": [
    {
      "name": "item used as reference",
      "known_dimensions": "3ft x 2ft x 3ft",
      "cubic_yards": 0.5,
      "location_in_photo": "left foreground"
    }
  ],
  "items": [
    {
      "name": "specific item name",
      "quantity": 1,
      "category": "furniture|appliance|electronics|debris|hazardous|other",
      "cubic_yards": 0.5,
      "is_special": false
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

SPATIAL REASONING METHOD (THIS IS HOW YOU ESTIMATE — FOLLOW EXACTLY):

Step 1 — FIND REFERENCE POINTS: Scan the photo for 5-10 items you recognize from the KNOWN ITEM REFERENCE LIBRARY below. These are your spatial anchors. Pick items spread across the photo — foreground, middle, background, left, right — to establish scale from multiple vantage points.

Step 2 — ESTABLISH SCALE AND DEPTH: Use the known real-world dimensions of those 5-10 reference items to calibrate the photo's perspective. A couch you know is 7ft long tells you how big everything near it is. A refrigerator in the background tells you the scale of that area. A trash bag in the corner reveals depth. By triangulating between multiple known objects at different depths, you can determine the true 3D volume of the entire scene.

Step 3 — MEASURE UNKNOWN ITEMS: Now that you understand the photo's scale and depth, estimate the cubic yards of every other item by comparing its apparent size to your reference points. An unknown box sitting next to a known chair can be sized accurately because you know how big the chair really is.

Step 4 — CALCULATE TOTALS: Sum all items for total cubic yards. Use reference-calibrated measurements, not guesses.

List your reference points in the "reference_points" array so the estimate is explainable and auditable.

If fewer than 5 reference items are visible, note this in "notes" and lower your confidence score. The fewer reference points, the less accurate the spatial calibration.

MULTI-PHOTO DEDUPLICATION (CRITICAL):
- Multiple photos may show the SAME room from different angles
- Photos labeled with the same room name are different views of ONE space
- If the same item is visible in multiple photos of the same room, count it ONLY ONCE
- Use visual cues (position, color, size, surroundings) to identify duplicate items across angles
- When uncertain if an item in two photos is the same, assume it IS the same item and count once
- Only count an item multiple times if it is clearly a DIFFERENT item (e.g., two distinct chairs in different positions)
- Use reference points from multiple angles to improve spatial accuracy — seeing the same reference item from two angles gives better depth calibration

ITEM IDENTIFICATION RULES:
- Identify every visible item individually, do not group unless identical
- Assign cubic_yards to each item based on its size RELATIVE TO YOUR REFERENCE POINTS — not generic guesses
- Look specifically along walls, in corners, behind other items
- FLAT SCREEN TVs vs WINDOW SCREENS: Dark rectangular objects leaning against walls may be TVs OR window screens — distinguish carefully. A TV will have a visible stand base, port connections on the back/side, a brand logo, a glossy screen surface, or a thick plastic bezel. A window screen has a thin metal or wooden frame with mesh visible through it. When uncertain, add "possible TV or window screen, verify on site" in the notes field for the crew to check.
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
    lines = [
        "\nKNOWN ITEM REFERENCE LIBRARY — USE THESE AS SPATIAL REFERENCE POINTS:",
        "Find 5-10 of these items in the photo to establish scale and depth.",
        "Their known cubic yard volumes tell you the real-world size of the scene.",
        "",
    ]
    for item in items:
        line = f"- {item.item_name}: {item.cubic_yards} CY"
        if item.is_special:
            line += " [SPECIAL ITEM - flag for recycling/disposal]"
        lines.append(line)
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

        price_low, price_high, cy_mid, special_items = calculate_price(
            result_data,
            rate_low=user.price_per_cy_low or 35.0,
            rate_high=user.price_per_cy_high or 40.0,
            rate_premium=user.price_per_cy_premium or 55.0,
            min_charge=user.min_charge or 75.0,
        )

        async with AsyncSessionLocal() as db:
            est = Estimate(
                user_id=user.id,
                team_member_id=job.get("team_member_id", 0),
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
            "price_low": price_low,
            "price_high": price_high,
            "cy_estimate": cy_mid,
            "items": result_data.get("items", []),
            "job_type": result_data.get("job_type", "standard"),
            "conditions": result_data.get("conditions", []),
            "notes": result_data.get("notes", ""),
            "confidence": result_data.get("confidence", 75),
            "estimates_remaining": remaining,
            "special_items": special_items,
            "items_looked_up": lookups_done,
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
