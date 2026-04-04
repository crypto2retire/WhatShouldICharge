# Lessons Learned

## Estimate retries should not burn credits — 2026-04-03

### 1. Run quality gating before charging usage
If WSIC asks the user to retry because the photo set is too dark, blurry, unreadable, or duplicate-heavy, do not consume credits or team usage for that attempt. Quality validation must run before usage deduction so retry-needed cases are free to resubmit.

### 2. Shared upload prep logic prevents drift
The signed-in, public, and team estimate endpoints all process photos. Keep upload validation, compression, and quality analysis in a shared helper so one flow does not silently drift from the others.

## Repo workflow docs drift — 2026-04-03

### 1. Keep AGENTS.md aligned with the real deploy path
WSIC source-of-truth workflow is local hard-drive repo -> GitHub -> Railway. Do not leave old Cowork mount paths, PAT upload instructions, or other environment-specific workarounds in `AGENTS.md` as if they are still primary.

### 2. Product limits must stay consistent across code and marketing copy
Free-trial counts are easy to drift because they appear in backend logic, onboarding emails, and frontend pages. When the limit changes, search the repo and update every user-facing reference in the same pass.

## Cowork folder confusion — 2026-03-24

### 1. Know which mounted folder is which
Cowork mounts TWO folders:
- `/mnt/WhatShouldICharge/` — THIS IS THE WSIC REPO. All code edits here.
- `/mnt/ctc-website/` — CTC website. SEPARATE project. Do NOT touch when working on WSIC.
Always check which folder you're in before making changes.

### 2. GitHub API push is the primary workflow
Cowork cannot `git push` (no credentials in VM). Use GitHub Contents API with Python + PAT token. ALWAYS use Python `base64.b64encode()` for file content. JavaScript `atob()` corrupts UTF-8 multi-byte characters (em-dashes become garbage).

### 3. Don't ask Kevin for things you already have
The GitHub PAT exists and has been used before. Don't ask Kevin to paste it or explain what it is. Just use it.

## Spatial redistribution inflation — 2026-03-23

### 1. Three-layer enforcement caused phantom items
The AI prompt, main.py scaling, and volume_lookup.py redistribution ALL tried to force items to sum to the spatial bounding box. For sparse scenes (shelving, scattered items), this inflated 1.5 CY of actual items to 8.0 CY by creating phantom "miscellaneous small items."
Fix: Added occupancy assessment to AI prompt, 2x inflation cap in main.py, sparse-scene cap in volume_lookup.py, phantom misc removal.

### 2. Price range collapses at minimum charge
When both price_low and price_high fall below min_charge, both clamp to the same value ($100-$100). Fix: ensure price_high is at least 1.5x price_low when min_charge is applied.

## Admin dashboard JS — 2026-03-21

### 1. Escaped backticks break the whole page
In `static/admin.html`, a timezone block used `\`` and `\${` (literal backslash + backtick / dollar) instead of real template literals. That is a **syntax error**: the browser never runs the script, so **no** `addEventListener` hooks or global functions (`loadClients`, etc.) exist and every control appears dead. After edits, run `new Function(extractedScript)` or open DevTools console for parse errors.

## Volume lookup validation — 2026-03-20

### 1. Where to call `validate_estimate` in WSIC
This repo runs the AI pipeline in `main.py` (`run_estimate`), not `wsic_ai_router.py`. Call validation **after** optional `items_needing_lookup` adjustments (so Tavily-updated CY is included) and **before** `calculate_price`, so stored `result_json` and prices match reconciled volumes.

## Railway Deployment — 2026-03-12

### 1. Railway PostgreSQL Networking
Always use `DATABASE_PRIVATE_URL` for Railway internal postgres networking. Never rely on `DATABASE_URL` alone — it may point to an external URL with higher latency.

### 2. Never Use os.getenv() Directly in Routers
Always use config settings (e.g., `from config import settings`). Using `os.getenv("DATABASE_URL_SYNC")` directly can pick up stale or missing env vars.

### 3. Always Add a Procfile
Always add a `Procfile` to Railway projects explicitly. Do not rely on Railway's auto-detection. Makes deployment reproducible.

### 4. Redis Must Be Optional
Railway does not have Redis by default. Any code depending on Redis must have graceful fallback. Never let missing Redis crash startup.

### 5. init_db() Needs Retry Logic
Railway PostgreSQL may not be ready when the app starts. Use retry logic with exponential backoff (5 attempts: 2s, 4s, 6s, 8s delays).

### 6. Verify Tables After First Deploy
Tables are not auto-created unless `init_db()` runs successfully. Always verify tables exist after first deploy.

### 7. Cookie Domain Must Be Explicit
Set cookie domain explicitly (e.g., `.whatshouldicharge.app`). Never leave as default or it may scope to the wrong domain.

### 8. DATABASE_URL_SYNC Derivation
`DATABASE_URL_SYNC` must be derived from `DATABASE_PUBLIC_URL` on Railway, not hardcoded or derived from private URL.

### 9. SQLite Does Not Work on Railway
Railway uses ephemeral filesystem — SQLite data is lost on every deploy. Must migrate to PostgreSQL.

### 10. Check for Missing Columns
Schema mismatches cause silent failures. Always use `ADD COLUMN IF NOT EXISTS` and verify column names match between code and database.
