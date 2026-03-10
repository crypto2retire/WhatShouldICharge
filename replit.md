# WhatShouldICharge — AI Junk Removal Estimator

## Overview
An AI-powered junk removal job estimator. Users upload customer photos, Claude vision AI analyzes them using a two-pass estimation system with a growing reference library. Returns price ranges, cubic yard estimates, item breakdowns, and job type classification. Includes user auth, Stripe subscriptions, live market rate fetching, marketing landing page, admin dashboard, team portal with PIN-based auth, and PDF estimate generation with email delivery.

## Stack
- **Backend**: Python 3.11, FastAPI, SQLite (via SQLAlchemy + aiosqlite)
- **Frontend**: Single-page HTML files (no framework), dark theme
- **AI**: Anthropic Claude (`claude-sonnet-4-20250514`) with vision, single-pass estimation
- **Auth**: bcrypt password hashing (async via executor), secure session cookies (30-day, httponly, samesite, secure)
- **Payments**: Stripe checkout sessions + webhooks with server-side tier validation from price_id
- **Market Data**: Tavily API for live market rate fetching + item dimension lookups
- **PDF**: ReportLab for professional PDF estimate generation
- **Email**: SendGrid for estimate delivery
- **Server**: uvicorn on port 5000

## Single-Pass Estimation Engine
1. **Pass 1**: Claude analyzes photos with the reference library injected into the system prompt. Identifies items, estimates CY, classifies job type. Special items (TVs, mattresses, tires, etc.) are flagged with `is_special: true` but NO fees are calculated.
2. **Web Lookup**: For items flagged as `items_needing_lookup`, parallel Tavily searches find real-world dimensions. Claude extracts CY from search results. New items are saved to the reference library.
3. **Library Learning**: After each estimate, all identified items update the reference library (increment times_seen or add new AI-learned items).

## Multi-Photo Per Room
- Users can upload up to 30 photos total, with multiple photos per room
- Photos are grouped by room label in the preview UI (visual grouping with room headers)
- Backend groups photos by room before sending to Claude, with explicit per-group headers
- AI prompt includes dedicated MULTI-PHOTO DEDUPLICATION section: same-room photos are treated as different angles of one space, items visible in multiple angles counted only once
- When uncertain, AI defaults to assuming items ARE the same (avoids over-counting)

## Special Item Handling
- Special items are flagged but NEVER added to price totals
- Price is based ONLY on cubic yards and job type
- Special items shown in a separate amber section with recycling fee notice
- Two disclaimers always shown on every estimate: recycling fees disclaimer and photo-based estimate disclaimer

## Estimation Flow
- `POST /api/estimate` returns `{ job_id }` immediately
- Background task runs single-pass estimation
- Frontend polls `GET /api/estimate/status/{job_id}` every second
- Status progresses: analyzing → looking_up → complete
- If lookups fail, silently falls back to base result

## Admin Dashboard
- Route: `GET /admin` (requires admin user)
- Tabs: Analytics, Users, Plans, Site Config, Estimates, Team
- Analytics: key metrics (total users, estimates today/week/month, revenue), usage stats
- Users: searchable/paginated user list with subscription info
- Plans: edit tier display names, prices, estimate limits, features, active status
- Site Config: edit landing page content (hero text, feature descriptions, CTA text)
- Estimates: filterable table of all estimates across users
- Team: create/edit/deactivate team members, manage PINs
- Admin user: kevin@cleartheclutter.net (is_admin=True, seeded on startup)

## Team Portal
- PIN-based authentication for team members (field estimators)
- Route: `GET /team` (login), `GET /team/app` (dashboard)
- Team login: company email/name + 4-digit PIN on mobile-friendly numpad
- Team members belong to an owner (admin) user; they use the owner's subscription quota
- Mobile-first estimate flow: simplified photo upload, room selection, customer info capture
- Results include PDF download and email-to-customer buttons
- Team session: 12-hour token via cookie

## PDF Estimate Generation
- Professional PDF generated via ReportLab
- Includes: company branding, date, estimate ID, price range, CY estimate, item breakdown table, special items, conditions, disclaimer
- `POST /api/estimate/{id}/pdf` — generates and downloads PDF
- `POST /api/estimate/{id}/send` — emails PDF to customer via SendGrid
- Available from both main estimator (index.html) and team portal (team.html)

## Site Config System
- Key-value pairs stored in SiteConfig table
- Editable from Admin Dashboard → Site Config tab
- Public API: `GET /api/site-config` returns all config
- Landing page loads config via JS and updates elements with `data-config` attributes
- Configurable fields: hero_title, hero_subtitle, hero_description, cta_primary, cta_secondary, feature_1_title, feature_1_desc, feature_2_title, feature_2_desc, feature_3_title, feature_3_desc

## SEO
- Landing page has full meta tags: description, canonical, OG, Twitter Card, robots, theme-color
- JSON-LD structured data: SoftwareApplication (with pricing offers), FAQPage (5 questions), Organization
- External CSS for browser caching (`landing.css`)
- SVG favicon with green $ icon
- robots.txt blocks auth-gated pages, admin, team routes; allows landing page
- sitemap.xml lists indexable pages
- Routes: `GET /robots.txt`, `GET /sitemap.xml`

## Routes
- `GET /` — Marketing landing page (`static/landing.html`)
- `GET /estimate` — Estimator app (`static/index.html`, requires auth)
- `GET /library` — Reference library viewer (`static/library.html`, requires auth)
- `GET /login` — Login page (`static/login.html`)
- `GET /signup` — Signup page (`static/signup.html`)
- `GET /upgrade` — Subscription upgrade page (`static/upgrade.html`)
- `GET /payment-success` — Payment confirmation (`static/payment-success.html`)
- `GET /admin` — Admin dashboard (`static/admin.html`, requires admin)
- `GET /team` — Team login page (`static/team-login.html`)
- `GET /team/app` — Team estimate dashboard (`static/team.html`, requires team auth)
- `POST /api/estimate` — Submit photos, returns job_id (auth required)
- `GET /api/estimate/status/{job_id}` — Poll estimation progress (auth required)
- `GET /api/estimates` — Fetch estimate history (auth required)
- `POST /api/estimate/{id}/pdf` — Generate PDF estimate
- `POST /api/estimate/{id}/send` — Email PDF to customer
- `GET /api/library` — All reference library items
- `GET /api/library/search?q=` — Search library by name
- `POST /api/library/add` — Add item to library
- `PUT /api/library/{id}` — Update library item
- `GET /api/library/stats` — Library statistics
- `POST /api/auth/signup` — Create account
- `POST /api/auth/login` — Log in
- `POST /api/auth/logout` — Log out
- `POST /api/auth/forgot-password` — Reset password and email new one to user
- `GET /api/auth/me` — Current user info (includes is_admin flag)
- `POST /api/payments/create-checkout` — Create Stripe checkout session
- `POST /api/payments/webhook` — Stripe webhook handler
- `GET /api/site-config` — Public site config for landing page
- `GET /api/admin/analytics` — Admin analytics data
- `GET /api/admin/users` — Admin user list
- `GET /api/admin/plans` — Admin plan configs
- `PUT /api/admin/plans/{id}` — Update plan config
- `GET /api/admin/site-config` — Admin site config
- `PUT /api/admin/site-config` — Update site config
- `GET /api/admin/estimates` — All estimates (admin)
- `POST /api/team/members` — Create team member
- `GET /api/team/members` — List team members
- `PUT /api/team/members/{id}` — Update team member
- `DELETE /api/team/members/{id}` — Deactivate team member
- `POST /api/team/auth` — Team PIN login
- `GET /api/team/me` — Current team member info
- `POST /api/team/estimate` — Submit team estimate
- `GET /api/team/estimate/status/{job_id}` — Poll team estimate
- `GET /api/team/estimates` — Team estimate history

## Files
- `main.py` — FastAPI backend, DB models, auth, Stripe, estimation engine, reference library, pricing logic, admin API, team API, PDF generation
- `static/index.html` — Estimator UI (auth-aware navbar, upload, room labels, truck load, polling progress, results with PDF/send buttons)
- `static/library.html` — Reference library viewer (searchable table, sort by seen/name/recent, source badges, stats)
- `static/landing.html` — Marketing landing page (hero, features, pricing, FAQ, scroll animations, SEO-optimized, dynamic site config)
- `static/landing.css` — External CSS for landing page (cacheable)
- `static/login.html` — Login form (with team portal link)
- `static/signup.html` — Signup form (email, password, company name, city, state)
- `static/upgrade.html` — Subscription tier selection (Starter/Pro/Agency)
- `static/payment-success.html` — Post-payment confirmation
- `static/admin.html` — Admin dashboard (analytics, users, plans, site config, estimates, team management)
- `static/team-login.html` — Team PIN login (mobile-first numpad)
- `static/team.html` — Team estimate dashboard (mobile-first, photo upload, results, PDF/send)
- `static/robots.txt` — Search engine crawl directives (blocks auth-gated, admin, team pages)
- `static/sitemap.xml` — XML sitemap for search engine indexing
- `static/favicon.svg` — SVG favicon (green $ icon)
- `estimates.db` — SQLite database (auto-created on startup)

## Database Tables
- **User** — id, email, password_hash, company_name, company_city, company_state, subscription_tier, stripe_customer_id, estimates_used, pricing fields, is_admin
- **Session** — id, token, user_id, expires_at
- **Estimate** — id, user_id, team_member_id, customer_name, customer_email, customer_phone, photos_count, result_json, price_low, price_high, cy_estimate, pass1_json, pass2_json, lookups_json, created_at
- **ItemReferenceLibrary** — id, item_name (unique), item_category, cubic_yards, is_special, special_fee, confidence, source (builtin|ai_learned|web_search|manual), search_query_used, times_seen, created_at, updated_at
- **TeamMember** — id, user_id (FK to owner/admin), name, pin_hash, role, is_active, created_at
- **TeamSession** — id, token, team_member_id, expires_at
- **SiteConfig** — id, config_key (unique), config_value, updated_at
- **PlanConfig** — id, tier_name (unique), display_name, price_cents, estimate_limit, features_json, stripe_price_id, is_active

## Seed Data
86 built-in items across categories: furniture, appliance, electronics, debris, outdoor, sports, medical, hazardous. Seeded on first startup.
Default plans seeded: free, starter, pro, agency with current pricing.
Default site config seeded with landing page content.
Admin user kevin@cleartheclutter.net seeded with is_admin=True.

## Pricing Logic
- Standard rate: $35/CY (low) – $40/CY (high), customizable per user
- Premium rate: $55/CY for hoarder, heavy items, stairs, truck load, or >10 CY
- Special item surcharges: variable per item ($15-$50), stored in reference library
- Minimum charge: $75

## Subscription Tiers
- **Free**: 3 estimates
- **Starter**: 20 estimates/month ($29/mo)
- **Pro**: 40 estimates/month ($59/mo)
- **Agency**: 999 estimates/month ($99/mo)

## Stripe Price IDs
- Starter: `price_1T7PXXAPEzwLONiqIIrAtsQZ`
- Pro: `price_1T6iUPAPEzwLONiqp31lIw9T`
- Agency: `price_1T7PXXAPEzwLONiqpQbgpgZ8`

## Security & Performance Optimizations

### Security Hardening
- Security headers middleware: X-Content-Type-Options, X-Frame-Options (DENY), X-XSS-Protection, Referrer-Policy, Permissions-Policy, Strict-Transport-Security, Content-Security-Policy
- Rate limiting on auth endpoints: 10 req/min for login/signup/team-auth, 5 req/5min for forgot-password
- Input validation: email format/length on signup, password max length (128 chars to prevent bcrypt DoS), file content-type validation (image MIME types only)
- Stripe webhook derives tier from price_id via `list_line_items` (never trusts client-supplied tier_name)
- Checkout endpoint validates price_id against server-side `PRICE_TO_TIER` mapping
- All innerHTML rendering of AI/DB data uses HTML escaping (`esc()` function) to prevent XSS
- Generic error messages returned to clients (no internal details leaked)
- Admin user configured via `ADMIN_EMAIL` environment variable (not hardcoded)
- CORS hardened with explicit methods and headers (no wildcards)
- Team estimate status endpoint checks ownership before returning results
- Session cookies set with `secure=True`, `httponly=True`, `samesite=lax`
- Upload size limit: 20MB per file, 30 files max

### Backend Performance
- Database connection pool: `pool_pre_ping=True`, `pool_recycle=3600`
- Database indexes on: Estimate.user_id, Estimate.team_member_id, Estimate.created_at, User.subscription_tier
- Library stats use SQL COUNT/GROUP BY aggregation instead of loading all items into memory
- Admin analytics uses single GROUP BY query instead of 4 separate per-tier queries
- Modern async patterns: `asyncio.to_thread()` instead of deprecated `get_event_loop().run_in_executor()`
- Lifespan context manager pattern with proper engine disposal on shutdown
- PIL Image resources properly closed with try/finally in compress_image()
- Estimate quota only incremented after successful AI processing
- Library updates use batch IN query instead of N+1 per-item SELECTs
- Expired estimate jobs cleaned up automatically (5-minute TTL)

### Frontend Optimization
- Semantic HTML: proper nav, main, section, header, footer landmarks
- Skip links on landing page and estimator
- ARIA labels on all interactive elements (drop zones, modals, loading spinners, error boxes)
- role="alert" on error containers, role="dialog" on modals, role="status" on live regions
- Keyboard navigation: Enter/Space on drop zones, Escape closes modals, focus management
- Proper form label associations across all pages
- Admin tabs use proper ARIA tab pattern (role="tab", aria-selected, aria-controls)
- URL.revokeObjectURL cleanup to prevent memory leaks
- Proper error handling in all fetch() calls

## Environment Variables
- `ANTHROPIC_API_KEY` — Required. Claude vision API.
- `STRIPE_SECRET_KEY` — Stripe API key for payments.
- `STRIPE_WEBHOOK_SECRET` — Stripe webhook signature verification.
- `TAVILY_API_KEY` — Optional. For live market rate fetching and item dimension lookups.
- `SENDGRID_API_KEY` — Optional. For emailing PDF estimates to customers.
- `ADMIN_EMAIL` — Required. Email address for the admin user (e.g., kevin@cleartheclutter.net).
