import os
import re
import logging
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import select, text, func

logger = logging.getLogger("wsic.db")


def _get_database_url() -> str:
    for key in ("DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = os.environ.get(key, "").strip()
        if url and url.startswith("postgres"):
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url

    pghost = os.environ.get("PGHOST", "").strip()
    pgport = os.environ.get("PGPORT", "5432").strip()
    pguser = os.environ.get("PGUSER", "").strip()
    pgpassword = os.environ.get("PGPASSWORD", "").strip()
    pgdatabase = os.environ.get("PGDATABASE", "").strip()
    if pghost and pguser and pgpassword and pgdatabase:
        return f"postgresql+asyncpg://{pguser}:{pgpassword}@{pghost}:{pgport}/{pgdatabase}"

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

_STRIPE_PACK_PRICES = {
    "single": os.environ.get("STRIPE_PRICE_SINGLE", ""),
    "10_pack": os.environ.get("STRIPE_PRICE_10PACK", ""),
    "25_pack": os.environ.get("STRIPE_PRICE_25PACK", ""),
    "50_pack": os.environ.get("STRIPE_PRICE_50PACK", ""),
    "100_pack": os.environ.get("STRIPE_PRICE_100PACK", ""),
    "250_pack": os.environ.get("STRIPE_PRICE_250PACK", ""),
}

_STRIPE_PLAN_PRICES = {
    "solo": os.environ.get("STRIPE_PRICE_SOLO", ""),
    "team": os.environ.get("STRIPE_PRICE_TEAM", ""),
    "enterprise": os.environ.get("STRIPE_PRICE_ENTERPRISE", ""),
    "starter": os.environ.get("STRIPE_PRICE_STARTER", ""),
    "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
    "agency": os.environ.get("STRIPE_PRICE_AGENCY", ""),
}

_DEFAULT_CREDIT_PACKS_SEED = {
    "single": {"name": "Single Estimate", "credits": 1, "price_cents": 1000, "discount_pct": 0, "description": "1 estimate credit", "is_featured": False},
    "10_pack": {"name": "10-Pack", "credits": 10, "price_cents": 6000, "discount_pct": 40, "description": "10 estimate credits (40% off)", "is_featured": False},
    "25_pack": {"name": "25-Pack", "credits": 25, "price_cents": 12500, "discount_pct": 50, "description": "25 estimate credits (50% off)", "is_featured": False},
    "50_pack": {"name": "50-Pack", "credits": 50, "price_cents": 20000, "discount_pct": 60, "description": "50 estimate credits (60% off)", "is_featured": True},
    "100_pack": {"name": "100-Pack", "credits": 100, "price_cents": 30000, "discount_pct": 70, "description": "100 estimate credits (70% off)", "is_featured": False},
    "250_pack": {"name": "250-Pack", "credits": 250, "price_cents": 50000, "discount_pct": 80, "description": "250 estimate credits (80% off)", "is_featured": False},
}

_PACK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,62}$")

SEED_ITEMS = [
    ("king mattress", "furniture", 1.50, True, 25.0, "76×80×11 in"),
    ("queen mattress", "furniture", 1.25, True, 25.0, "60×80×11 in"),
    ("full mattress", "furniture", 1.00, True, 25.0, "54×75×11 in"),
    ("twin mattress", "furniture", 0.75, True, 25.0, "38×75×11 in"),
    ("box spring", "furniture", 1.00, True, 25.0, "60×80×9 in"),
    ("large sectional sofa", "furniture", 5.50, False, 0, "120×90×36 in"),
    ("sofa", "furniture", 2.00, False, 0, "84×36×34 in"),
    ("loveseat", "furniture", 1.50, False, 0, "60×36×34 in"),
    ("recliner", "furniture", 1.25, False, 0, "36×38×40 in"),
    ("armchair", "furniture", 0.75, False, 0, "32×34×34 in"),
    ("king bed frame", "furniture", 1.50, False, 0, "80×76×14 in"),
    ("queen bed frame", "furniture", 1.25, False, 0, "80×60×14 in"),
    ("twin bed frame", "furniture", 0.75, False, 0, "75×38×14 in"),
    ("large dresser", "furniture", 1.25, False, 0, "60×18×34 in"),
    ("small dresser", "furniture", 0.75, False, 0, "36×18×30 in"),
    ("nightstand", "furniture", 0.25, False, 0, "24×16×26 in"),
    ("coffee table", "furniture", 0.50, False, 0, "48×24×18 in"),
    ("dining table large", "furniture", 1.75, False, 0, "72×42×30 in"),
    ("dining table small", "furniture", 0.75, False, 0, "48×30×30 in"),
    ("dining chair", "furniture", 0.25, False, 0, "18×20×38 in"),
    ("large workbench", "furniture", 2.50, False, 0, "96×30×36 in"),
    ("small workbench", "furniture", 1.25, False, 0, "60×24×34 in"),
    ("bookshelf large", "furniture", 0.75, False, 0, "36×12×72 in"),
    ("bookshelf small", "furniture", 0.35, False, 0, "30×10×48 in"),
    ("desk large", "furniture", 1.25, False, 0, "60×30×30 in"),
    ("desk small", "furniture", 0.75, False, 0, "42×24×30 in"),
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
    ("large flat screen tv 55+", "electronics", 0.50, True, 25.0, "49×4×29 in"),
    ("medium flat screen tv 32-54", "electronics", 0.35, True, 25.0, "37×3×22 in"),
    ("small flat screen tv under 32", "electronics", 0.20, True, 25.0, "28×3×17 in"),
    ("crt television", "electronics", 0.50, True, 25.0, "24×20×20 in"),
    ("desktop computer tower", "electronics", 0.15, False, 0, "18×8×17 in"),
    ("monitor", "electronics", 0.20, False, 0, "24×8×18 in"),
    ("printer large", "electronics", 0.20, False, 0, "20×18×14 in"),
    ("large cardboard box", "debris", 0.15, False, 0, "24×18×18 in"),
    ("medium cardboard box", "debris", 0.10, False, 0, "18×14×14 in"),
    ("small cardboard box", "debris", 0.05, False, 0, "12×12×12 in"),
    ("large plastic tote with lid", "debris", 0.20, False, 0, "30×20×16 in"),
    ("small plastic tote", "debris", 0.10, False, 0, "22×16×12 in"),
    ("contractor trash bag full", "debris", 0.30, False, 0, "24×24×30 in"),
    ("standard trash bag full 33 gal", "debris", 0.15, False, 0, "22×20×26 in"),
    ("small trash bag full", "debris", 0.10, False, 0, "18×18×24 in"),
    ("plastic outdoor chair", "outdoor", 0.25, False, 0, "22×24×34 in"),
    ("metal outdoor chair", "outdoor", 0.35, False, 0, "22×24×34 in"),
    ("outdoor dining set 4 chairs table", "outdoor", 2.50, False, 0, "48×48×30 in + 4 chairs"),
    ("plastic outdoor table", "outdoor", 0.60, False, 0, "36×36×28 in"),
    ("riding lawn mower", "outdoor", 3.00, False, 0, "66×42×44 in"),
    ("push lawn mower", "outdoor", 1.00, False, 0, "56×22×42 in"),
    ("gas grill large", "outdoor", 1.25, False, 0, "56×22×44 in"),
    ("gas grill small", "outdoor", 0.75, False, 0, "40×18×38 in"),
    ("trampoline", "outdoor", 4.00, False, 0, "144 in diameter × 36 in tall"),
    ("swing set", "outdoor", 6.00, False, 0, "144×96×84 in"),
    ("hot tub", "outdoor", 6.00, False, 0, "84×84×36 in"),
    ("above ground pool", "outdoor", 8.00, False, 0, "180 in diameter × 52 in tall"),
    ("4 drawer file cabinet", "other", 0.75, False, 0, "15×25×52 in"),
    ("2 drawer file cabinet", "other", 0.40, False, 0, "15×25×29 in"),
    ("lateral file cabinet", "other", 0.75, False, 0, "36×18×28 in"),
    ("treadmill", "sports", 2.50, False, 0, "72×34×56 in"),
    ("elliptical", "sports", 2.50, False, 0, "70×28×64 in"),
    ("stationary bike", "sports", 1.00, False, 0, "42×22×48 in"),
    ("weight bench", "sports", 1.25, False, 0, "56×26×46 in"),
    ("weight set with rack", "sports", 2.00, False, 0, "48×24×52 in"),
    ("ping pong table", "sports", 2.50, False, 0, "108×60×30 in"),
    ("pool table", "sports", 5.00, False, 0, "100×56×32 in"),
    ("wheelchair", "medical", 0.50, False, 0, "26×16×36 in"),
    ("hospital bed", "medical", 2.50, False, 0, "84×36×24 in"),
    ("walker", "medical", 0.25, False, 0, "22×18×34 in"),
    ("propane tank large", "hazardous", 0.25, True, 50.0, "12×12×48 in"),
    ("propane tank small", "hazardous", 0.10, True, 25.0, "12×12×18 in"),
    ("paint cans box", "hazardous", 0.15, True, 25.0, "18×12×12 in"),
    ("car battery", "hazardous", 0.10, True, 15.0, "10×7×8 in"),
    ("tire car", "hazardous", 0.25, True, 15.0, "26 in diameter × 8 in wide"),
    ("tire truck", "hazardous", 0.35, True, 25.0, "34 in diameter × 12 in wide"),
    ("lumber pile small", "debris", 0.75, False, 0, "48×24×24 in"),
    ("lumber pile large", "debris", 1.75, False, 0, "96×24×36 in"),
    ("drywall sheets", "debris", 0.15, False, 0, "96×48×0.5 in per sheet"),
    ("carpet room", "debris", 1.25, False, 0, "rolled: 12 ft × 18 in diameter"),
]


async def init_db():
    from models import User, PlanConfig, CreditPack, SiteConfig, ItemReferenceLibrary

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
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS photos_json TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_standard DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS price_per_cy_heavy DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS actual_price DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS actual_cy DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS actual_truck_fraction DOUBLE PRECISION DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS accuracy_notes TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS correction_reason VARCHAR(40) DEFAULT ''",
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
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS capture_mode VARCHAR(30) DEFAULT 'remote'",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS confidence_bucket VARCHAR(20) DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS confidence_reasons TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS photo_quality_flags TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS scene_type VARCHAR(50) DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS occupancy_class VARCHAR(30) DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS sanity_flags TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS geometry_summary TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS review_status VARCHAR(30) DEFAULT 'auto_approved'",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS review_reason TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_requested BOOLEAN DEFAULT FALSE",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_contact_method VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_preferred_day VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_preferred_time VARCHAR DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS appointment_requested_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS additional_items_text TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS adjustments_json TEXT DEFAULT ''",
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
            "ALTER TABLE estimates ADD COLUMN actual_truck_fraction REAL DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN accuracy_notes TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN correction_reason TEXT DEFAULT ''",
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
            "ALTER TABLE estimates ADD COLUMN capture_mode TEXT DEFAULT 'remote'",
            "ALTER TABLE estimates ADD COLUMN confidence_bucket TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN confidence_reasons TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN photo_quality_flags TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN scene_type TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN occupancy_class TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN sanity_flags TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN geometry_summary TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN review_status TEXT DEFAULT 'auto_approved'",
            "ALTER TABLE estimates ADD COLUMN review_reason TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_requested BOOLEAN DEFAULT 0",
            "ALTER TABLE estimates ADD COLUMN appointment_contact_method TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_preferred_day TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_preferred_time TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN appointment_requested_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE estimates ADD COLUMN additional_items_text TEXT DEFAULT ''",
            "ALTER TABLE estimates ADD COLUMN adjustments_json TEXT DEFAULT ''",
        ]

    async with engine.begin() as conn:
        for stmt in alter_statements:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.debug("Migration skip: %s — %s", stmt[:80], e)

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
        except Exception as e:
            logger.debug("credit_transactions table creation skip: %s", e)

    async with engine.begin() as conn:
        try:
            if _is_postgres:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS password_resets (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        token_hash VARCHAR NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        expires_at TIMESTAMP NOT NULL,
                        used_at TIMESTAMP DEFAULT NULL
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_password_resets_token_hash ON password_resets(token_hash)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_password_resets_user_id ON password_resets(user_id)"))
            else:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS password_resets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        token_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL,
                        used_at TIMESTAMP DEFAULT NULL
                    )
                """))
        except Exception as e:
            logger.debug("password_resets table creation skip: %s", e)

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
            except Exception as e:
                logger.debug("Backfill skip: %s — %s", stmt[:60], e)

    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "UPDATE users SET credit_balance = 999 WHERE is_admin = true AND (credit_balance IS NULL OR credit_balance < 999)"
            ))
        except Exception as e:
            logger.debug("Admin credit grant skip: %s", e)

    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "UPDATE users SET price_per_cy_standard = 35.0, price_per_cy_heavy = 50.0 "
                "WHERE company_slug = 'clear-the-clutter' AND price_per_cy_standard IS NULL"
            ))
        except Exception as e:
            logger.debug("CTC rates backfill skip: %s", e)
    logger.info("Database migrations and backfills complete")


async def seed_reference_library():
    from models import ItemReferenceLibrary
    dims_map = {name: dims for name, _, _, _, _, dims in SEED_ITEMS}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ItemReferenceLibrary).limit(1))
        existing = result.scalar_one_or_none()
        if existing:
            all_result = await db.execute(
                select(ItemReferenceLibrary).where(
                    (ItemReferenceLibrary.dimensions == None) | (ItemReferenceLibrary.dimensions == "")
                )
            )
            empty_dims = all_result.scalars().all()
            for item in empty_dims:
                if item.item_name in dims_map:
                    item.dimensions = dims_map[item.item_name]
            name_result = await db.execute(select(ItemReferenceLibrary.item_name))
            existing_names = {row[0] for row in name_result.fetchall()}
            added = 0
            for name, cat, cy, special, fee, dims in SEED_ITEMS:
                if name not in existing_names:
                    db.add(ItemReferenceLibrary(
                        item_name=name, item_category=cat, cubic_yards=cy,
                        dimensions=dims, is_special=special, special_fee=fee,
                        confidence=1.0, source="builtin", times_seen=0,
                    ))
                    added += 1
            if empty_dims or added > 0:
                await db.commit()
            return
        for name, cat, cy, special, fee, dims in SEED_ITEMS:
            db.add(ItemReferenceLibrary(
                item_name=name, item_category=cat, cubic_yards=cy,
                dimensions=dims, is_special=special, special_fee=fee,
                confidence=1.0, source="builtin", times_seen=0,
            ))
        await db.commit()


async def seed_plan_configs():
    from models import PlanConfig
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
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("solo", ""), is_active=True),
            PlanConfig(tier_name="team", display_name="Team", price_cents=29900, estimate_limit=999,
                       features_json='["Up to 3 users","Everything in Solo","Truck load calculator","Custom rate settings","Priority support"]',
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("team", ""), is_active=True),
            PlanConfig(tier_name="enterprise", display_name="Enterprise", price_cents=49900, estimate_limit=999,
                       features_json='["Unlimited users","Everything in Team","API access","Dedicated onboarding","Phone support"]',
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("enterprise", ""), is_active=True),
            PlanConfig(tier_name="custom", display_name="Custom", price_cents=99900, estimate_limit=999,
                       features_json='["Fully customized solution","Custom integrations","White-label options","Dedicated support"]',
                       stripe_price_id="", is_active=True),
            PlanConfig(tier_name="starter", display_name="Starter (Legacy)", price_cents=2900, estimate_limit=20,
                       features_json='["Legacy plan"]',
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("starter", ""), is_active=False),
            PlanConfig(tier_name="pro", display_name="Pro (Legacy)", price_cents=5900, estimate_limit=40,
                       features_json='["Legacy plan"]',
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("pro", ""), is_active=False),
            PlanConfig(tier_name="agency", display_name="Agency (Legacy)", price_cents=9900, estimate_limit=999,
                       features_json='["Legacy plan"]',
                       stripe_price_id=_STRIPE_PLAN_PRICES.get("agency", ""), is_active=False),
        ]
        for p in plans:
            db.add(p)
        await db.commit()


async def seed_credit_packs():
    from models import CreditPack
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count(CreditPack.id)))).scalar() or 0
        if n > 0:
            logger.info("[seed_credit_packs] credit_packs already populated (%s rows)", n)
            return
        order = 0
        for pack_key, v in _DEFAULT_CREDIT_PACKS_SEED.items():
            db.add(CreditPack(
                pack_key=pack_key, name=v["name"], credits=int(v["credits"]),
                price_cents=int(v["price_cents"]), discount_pct=int(v.get("discount_pct", 0)),
                description=v.get("description", "") or "", stripe_product_id="",
                stripe_price_id=_STRIPE_PACK_PRICES.get(pack_key, ""),
                is_active=True, is_featured=bool(v.get("is_featured", False)), sort_order=order,
            ))
            order += 10
        await db.commit()
        logger.info("[seed_credit_packs] Inserted %s default credit packs", len(_DEFAULT_CREDIT_PACKS_SEED))


async def seed_site_config():
    from models import SiteConfig
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
    from models import User
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
            if user.estimates_limit < 999:
                user.estimates_limit = 999
                user.estimates_used = 0
                changed = True
            if not getattr(user, 'timezone', None):
                user.timezone = "America/Chicago"
                changed = True
            if changed:
                await db.commit()
