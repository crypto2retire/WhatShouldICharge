import os
import re
import time
import logging

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import text
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("wsic.db")

DATABASE_URL = None
_is_postgres = False
engine = None
AsyncSessionLocal = None
Base = declarative_base()


def _get_database_url() -> str:
    for key in ("DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = os.environ.get(key, "").strip()
        if url and url.startswith("postgres"):
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            logger.info(f"[db] Using {key} (PostgreSQL)")
            return url

    pghost = os.environ.get("PGHOST", "").strip()
    pgport = os.environ.get("PGPORT", "5432").strip()
    pguser = os.environ.get("PGUSER", "").strip()
    pgpassword = os.environ.get("PGPASSWORD", "").strip()
    pgdatabase = os.environ.get("PGDATABASE", "").strip()
    if pghost and pguser and pgpassword and pgdatabase:
        url = f"postgresql+asyncpg://{pguser}:{pgpassword}@{pghost}:{pgport}/{pgdatabase}"
        logger.info(f"[db] Built URL from PG* env vars (host={pghost})")
        return url

    logger.warning("[db] No PostgreSQL config found — falling back to SQLite")
    return "sqlite+aiosqlite:///./estimates.db"


def init_engine():
    global DATABASE_URL, _is_postgres, engine, AsyncSessionLocal
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
