# Task Tracker

## Volume lookup validation (2026-03-20)

- [x] Add `services/volume_lookup.py` with `validate_estimate`
- [x] Call `validate_estimate(result_data)` in `run_estimate` after item lookups, before pricing

## WSIC Railway Migration

- [x] Migrate from SQLite to PostgreSQL
- [x] Add Procfile for Railway
- [x] Fix thumbnail images not displaying (added blob: to CSP img-src)
- [x] Verify and expand item recognition list with dimensions (~55 new items, seed sync for existing DBs)
- [x] Fix perspective correction in photo analysis (detailed perspective rules in AI prompt)
- [x] Fix false TV detection (require 2+ positive indicators, list of commonly misidentified objects)
- [x] Add Tavily local market pricing by city (market rates now blended into price calculation)

## Security Hardening (2026-04-11)

- [x] Untrack estimates.db, add *.db to .gitignore
- [x] Remove .agents/ from git, add to .gitignore
- [x] Fix duplicate preferred_contact column in Estimate model
- [x] Replace forgot-password plaintext email with secure reset token flow
- [x] Admin password reset emails the password instead of returning in JSON

## Hardening & Cleanup (2026-04-11)

- [x] Replace bare except:pass with logged warnings in init_db()
- [x] Add as e to all remaining bare except blocks
- [x] Sync pyproject.toml with requirements.txt
- [x] Restore Procfile for Railway start command
- [x] Replace all 47 datetime.utcnow() calls with timezone-safe version

## Architecture Improvements (2026-04-11)

- [x] Add 500-entry LRU eviction and expired entry cleanup to response cache
- [x] Move all Stripe Price IDs to environment variables
- [x] Remove dead PRICE_TO_TIER dict

## Module Extraction (2026-04-11)

- [x] Create database.py (engine, session, Base, DB URL resolution)
- [x] Create models/__init__.py (12 SQLAlchemy models)
- [x] Create cache.py (in-memory cache with LRU)
- [x] Create auth.py (get_current_user, require_user, require_admin, team auth)
- [x] Create sendgrid_email.py (email sending)
- [x] Create billing.py (usage limits, overage billing)
- [x] Create pricing.py (calculate_price — extracted for testability)

## Test Suite (2026-04-11)

- [x] 35 tests passing: billing (13), pricing (12), volume_lookup (10)

## Password Reset Feature (2026-04-11)

- [x] PasswordReset model + password_resets table
- [x] POST /api/auth/reset-password endpoint
- [x] GET /reset-password page (static/reset-password.html)

## APIRouter Refactor (2026-04-12)

- [x] Split routes into 13 APIRouter groups within main.py
- [x] Move include_router to end of file (after all route decorators)
- [x] Fix site-breaking bug where routes registered before decorators

## Database Extraction (2026-04-12)

- [x] Move init_db() and all seed functions from main.py to database.py
- [x] Move SEED_ITEMS, Stripe price constants to database.py
- [x] main.py reduced from ~8300 to ~7700 lines

## AI Model Fix (2026-04-12)

- [x] Replace dead verifier model (llama-3.2-90b-vision, removed from OpenRouter) with Pixtral Large 2411
- [x] Improve error logging to show actual exception details

## Alembic Migrations (2026-04-12)

- [x] Set up Alembic with async PostgreSQL support
- [x] Configure env.py to read DATABASE_PRIVATE_URL
- [x] Generate initial migration from current SQLAlchemy models
- [x] Add alembic to requirements.txt and pyproject.toml
