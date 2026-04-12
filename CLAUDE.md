# CLAUDE.md — WhatShouldICharge (WSIC)
# Master instructions for Claude Code. Read this fully before doing anything.

---

## Critical Rules
- This is WhatShouldICharge (WSIC) — a junk removal photo estimator
- Live at whatshouldicharge.app on Railway
- Auto-deploys from GitHub on push to main
- GitHub repo: crypto2retire/WhatShouldICharge
- Database is Railway PostgreSQL — use DATABASE_PRIVATE_URL for internal connection
- File storage is DigitalOcean Spaces bucket: hauliq-uploads SFO3
- Never commit .env to GitHub

---

## Folder Locations
- **Canonical local repo:** `~/dev/WhatShouldICharge/` (NOT iCloud — avoids `.git/HEAD` locking)
- **CTC website:** `~/dev/ctc-website/` (separate project, do NOT mix)

### Key Files
- `main.py` — FastAPI app, all API routes (13 APIRouter groups), `run_estimate()`, ~7700 lines.
- `database.py` — Database engine, session factory, `init_db()`, seed functions, Stripe price config.
- `models/__init__.py` — 12 SQLAlchemy models (User, Estimate, Session, PasswordReset, etc.).
- `auth.py` — Auth helpers (get_current_user, require_user, require_admin).
- `billing.py` — Usage limits, overage billing.
- `cache.py` — In-memory response cache with LRU eviction.
- `pricing.py` — `calculate_price()` function.
- `sendgrid_email.py` — Email sending via SendGrid.
- `services/industry_config.py` — AI estimation prompts, industry config.
- `services/volume_lookup.py` — Volume lookup table + redistribution logic.
- `alembic/` — Database migration system.
- `tests/` — Test suite (35 tests).
- `static/` — All frontend HTML files.
- `tasks/todo.md` — Current task tracking
- `tasks/lessons.md` — Lessons learned from past corrections

---

## Agent Behavior Rules

### 1. Plan First
- Enter plan mode for ANY non-trivial task (3+ steps)
- Write detailed specs upfront before touching code
- Check in before starting implementation

### 2. Verification Before Done
- Never mark a task complete without proving it works
- Run tests, check syntax, verify Railway deployment
- Ask yourself: "Would a staff engineer approve this?"

### 3. Autonomous Bug Fixing
- When given a bug report: just fix it
- Zero context switching required from the user
- Find root cause, no temporary fixes

### 4. Self-Improvement Loop
- After ANY correction: update tasks/lessons.md
- Review lessons.md at start of every session

### 5. Demand Elegance
- Find root causes, no temporary fixes
- Senior developer standards

### 6. APIRouter Ordering
- `app.include_router()` MUST be called AFTER all route decorators are defined
- FastAPI only copies routes that already exist on a router at include time
- The include_router block lives at the very end of `main.py`

---

## Task Management
- Write plan to tasks/todo.md before starting
- Mark items complete as you go
- Capture lessons in tasks/lessons.md after corrections

---

## Architecture Quick Reference
- **Estimation flow:** Photo upload → dual OpenRouter vision models (primary: Qwen2.5-VL-72B, verifier: Pixtral Large 2411) → `volume_lookup.py` validation → `calculate_price()` → response
- **Stripe config:** All Price IDs from environment variables, never hardcoded.

---

## Core Principles
- Simplicity First: minimal code changes
- No Laziness: senior developer standards

---

*Last updated: April 12, 2026.*
