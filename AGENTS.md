# AGENTS.md — WhatShouldICharge (WSIC)
# Master instructions for any AI agent working on this codebase.

---

## Critical Rules
- This is WhatShouldICharge (WSIC) — a junk removal photo estimator
- Live at whatshouldicharge.app on Railway
- Auto-deploys from GitHub on push to main
- GitHub repo: crypto2retire/WhatShouldICharge
- Working copies must live on Kevin's local hard drive, not in iCloud-only or ephemeral mounted paths
- Database is Railway PostgreSQL — use DATABASE_PRIVATE_URL for internal connection
- File storage is DigitalOcean Spaces bucket: hauliq-uploads SFO3
- Never commit .env to GitHub

---

## Folder & File Locations

### Canonical Repos (local hard drive only)
All canonical repos must live on Kevin's local hard drive, outside iCloud-managed folders. The exact parent folder can vary (`~/dev/`, `~/Documents/`, etc.), but the repo itself must be a normal local git checkout with its own `.git` directory.
- **WhatShouldICharge** — THIS repo. Branch: `main`.
- **ctc-website** — CTC website repo. Branch: `master`.

### Deprecated Mount Guidance
Older Cowork mount paths and session-specific mount instructions are deprecated. Do not rely on `/sessions/.../mnt/...` or old `/mnt/...` conventions as the source of truth for this repo.

### Key WSIC Files
- `main.py` — FastAPI app, all API routes (organized into 13 APIRouter groups), `run_estimate()`, ~7700 lines.
- `database.py` — Database engine, session factory, `init_db()`, seed functions, `SEED_ITEMS`, Stripe price config.
- `models/__init__.py` — 12 SQLAlchemy models (User, Estimate, Session, PasswordReset, CreditPack, etc.).
- `auth.py` — Auth helpers (get_current_user, require_user, require_admin, team auth).
- `billing.py` — Usage limits, overage billing, plan call limits.
- `cache.py` — In-memory response cache with LRU eviction.
- `pricing.py` — `calculate_price()` function (extracted for testability).
- `sendgrid_email.py` — Email sending via SendGrid.
- `services/industry_config.py` — Industry-specific configuration and the main junk-removal estimation prompt.
- `services/volume_lookup.py` — Volume lookup table + redistribution logic.
- `alembic/` — Database migration system (Alembic).
- `tests/` — Test suite (35 tests: billing, pricing, volume lookup).
- `static/` — All frontend HTML files (admin.html, widget.js, landing.html, etc.)
- `tasks/todo.md` — Current task tracking
- `tasks/lessons.md` — Lessons learned from past corrections

---

## Development Workflow

### Primary: Local Repo → GitHub → Railway
The deploy flow is:
1. Edit files in the local hard-drive git checkout for WhatShouldICharge
2. Commit and push to GitHub
3. Railway auto-deploys from `main`

### Running Tests
```
python3 -m pytest tests/ -v
```

### Database Migrations
To create a new migration after model changes:
```
alembic revision --autogenerate -m "description of change"
```
To apply migrations:
```
alembic upgrade head
```
To mark an existing database as current (without running migrations):
```
alembic stamp head
```

### Deprecated Push Workarounds
Older Cowork-specific push workarounds such as `osascript` wrappers or GitHub Contents API uploads are deprecated. Do not treat them as the primary workflow unless a local git push is truly unavailable in the current environment.

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

## Architecture Quick Reference
- **Estimation flow:** Photo upload → dual OpenRouter vision models (primary: Qwen2.5-VL-72B, verifier: Pixtral Large 2411) with prompts from `services/industry_config.py` → `services/volume_lookup.py` validation → `calculate_price()` from `pricing.py` → response
- **Item dimension lookups:** Claude Sonnet (via Anthropic API) is used only for looking up unknown item dimensions with Tavily web search
- **Pricing:** Users set $/CY rates during onboarding. `calculate_price()` multiplies CY × rate. Min charge clamp. Asymmetric range (-10% low, +20% high).
- **Credit system:** Pay-per-use credit packs ($10/single through 250-pack). Credits checked before estimate runs. Stripe one-time payments.
- **Free trial:** New accounts currently get 2 free estimates before paid credits are required.
- **Widget:** Embedded on client sites (e.g., CTC). Lead capture mode — customer submits photos, operator gets estimate + contact info.
- **Stripe config:** All Stripe Price IDs are read from environment variables (STRIPE_PRICE_SINGLE, STRIPE_PRICE_SOLO, etc.) — never hardcoded.

---

## Core Principles
- Simplicity First: minimal code changes
- No Laziness: senior developer standards
- One repo per session: NEVER mix WSIC and CTC code

---

*Last updated: April 12, 2026.*
