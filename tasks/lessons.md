# Lessons Learned

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
