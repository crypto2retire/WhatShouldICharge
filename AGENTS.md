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

**Deprecated examples** (do NOT treat as canonical):
- `/sessions/.../mnt/dev--WhatShouldICharge/`
- `/sessions/.../mnt/ctc-website/`
- `/mnt/WhatShouldICharge/`
- `/mnt/whatshouldicharge.app/`

### Key WSIC Files
- `main.py` — THE main application file. FastAPI app, all API routes, `run_estimate()` function, `calculate_price()`, database models, everything. ~5000+ lines.
- `services/industry_config.py` — Industry-specific configuration and the main junk-removal estimation prompt used by the Anthropic/Claude pipeline.
- `services/volume_lookup.py` — Volume lookup table + redistribution logic. Overrides AI per-item volumes with known-accurate values.
- `services/__init__.py` — Service package marker.
- `static/` — All frontend HTML files (admin.html, widget.js, landing.html, etc.)
- `tasks/todo.md` — Current task tracking
- `tasks/lessons.md` — Lessons learned from past corrections

### Memory & Config
Local tool memory/config paths may exist outside the repo, but they are environment-specific and not authoritative for deploy or source-of-truth workflow decisions.

---

## Development Workflow

### Primary: Local Repo → GitHub → Railway
The deploy flow is:
1. Edit files in the local hard-drive git checkout for WhatShouldICharge
2. Commit and push to GitHub
3. Railway auto-deploys from `main`

### Secondary: Cursor / Codex
For complex multi-file refactors or when another tool is more efficient:
- Cursor IDE with AI assist
- Codex CLI

### Deprecated Push Workarounds
Older Cowork-specific push workarounds such as `osascript` wrappers or GitHub Contents API uploads are deprecated. Do not treat them as the primary workflow unless a local git push is truly unavailable in the current environment.

Preferred workflow:
- Use standard git from the local repo checkout
- Push to `origin main`
- Verify Railway deployment after push

---

## Agent Behavior Rules

### 1. Plan First
- Enter plan mode for ANY non-trivial task (3+ steps)
- Write detailed specs upfront before touching code
- Check in before starting implementation

### 2. Verification Before Done
- Never mark a task complete without proving it works
- Check logs, demonstrate correctness
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

---

## Architecture Quick Reference
- **Estimation flow:** Photo upload → Anthropic Claude vision/message analysis (prompt from `services/industry_config.py`, currently `claude-sonnet-4-20250514` in `main.py`) → `services/volume_lookup.py` validation → `calculate_price()` in `main.py` → response
- **Pricing:** Users set $/CY rates during onboarding. `calculate_price()` multiplies CY × rate. Min charge clamp. Asymmetric range (-10% low, +20% high).
- **Credit system:** Pay-per-use credit packs ($10/single through 250-pack). Credits checked before estimate runs. Stripe one-time payments.
- **Free trial:** New accounts currently get 2 free estimates before paid credits are required.
- **Widget:** Embedded on client sites (e.g., CTC). Lead capture mode — customer submits photos, operator gets estimate + contact info.

---

## Core Principles
- Simplicity First: minimal code changes
- No Laziness: senior developer standards
- One repo per session: NEVER mix WSIC and CTC code

---

*Last updated: April 3, 2026.*
