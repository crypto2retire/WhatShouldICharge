# WhatShouldICharge — AI Junk Removal Estimator

## Overview
An AI-powered junk removal job estimator. Users upload customer photos, Claude vision AI analyzes them using a two-pass estimation system with a growing reference library. Returns price ranges, cubic yard estimates, item breakdowns, and job type classification. Includes user auth, Stripe subscriptions, live market rate fetching, and a marketing landing page.

## Stack
- **Backend**: Python 3.11, FastAPI, SQLite (via SQLAlchemy + aiosqlite)
- **Frontend**: Single-page HTML files (no framework), dark theme
- **AI**: Anthropic Claude (`claude-sonnet-4-20250514`) with vision, two-pass estimation
- **Auth**: bcrypt password hashing, session cookies (30-day)
- **Payments**: Stripe checkout sessions + webhooks for subscription lifecycle
- **Market Data**: Tavily API for live market rate fetching + item dimension lookups
- **Server**: uvicorn on port 5000

## Two-Pass Estimation Engine
1. **Pass 1**: Claude analyzes photos with the reference library injected into the system prompt. Identifies items, estimates CY, classifies job type.
2. **Pass 2**: A second Claude call (no photos) acts as a skeptical senior reviewer. Verifies CY values against the library, flags misidentifications, checks totals, produces verification_notes.
3. **Pass 3 (Web Lookup)**: For items flagged as `items_needing_lookup`, parallel Tavily searches find real-world dimensions. Claude extracts CY from search results. New items are saved to the reference library.
4. **Library Learning**: After each estimate, all identified items update the reference library (increment times_seen or add new AI-learned items).

## Estimation Flow
- `POST /api/estimate` returns `{ job_id }` immediately
- Background task runs two-pass estimation
- Frontend polls `GET /api/estimate/status/{job_id}` every second
- Status progresses: analyzing → verifying → looking_up → complete
- If Pass 2 or lookups fail, silently falls back to Pass 1 result

## Routes
- `GET /` — Marketing landing page (`static/landing.html`)
- `GET /estimate` — Estimator app (`static/index.html`, requires auth)
- `GET /library` — Reference library viewer (`static/library.html`, requires auth)
- `GET /login` — Login page (`static/login.html`)
- `GET /signup` — Signup page (`static/signup.html`)
- `GET /upgrade` — Subscription upgrade page (`static/upgrade.html`)
- `GET /payment-success` — Payment confirmation (`static/payment-success.html`)
- `POST /api/estimate` — Submit photos, returns job_id (auth required)
- `GET /api/estimate/status/{job_id}` — Poll estimation progress (auth required)
- `GET /api/estimates` — Fetch estimate history (auth required)
- `GET /api/library` — All reference library items
- `GET /api/library/search?q=` — Search library by name
- `POST /api/library/add` — Add item to library
- `PUT /api/library/{id}` — Update library item
- `GET /api/library/stats` — Library statistics
- `POST /api/auth/signup` — Create account
- `POST /api/auth/login` — Log in
- `POST /api/auth/logout` — Log out
- `GET /api/auth/me` — Current user info
- `POST /api/payments/create-checkout` — Create Stripe checkout session
- `POST /api/payments/webhook` — Stripe webhook handler

## Files
- `main.py` — FastAPI backend, DB models, auth, Stripe, two-pass estimation, reference library, pricing logic
- `static/index.html` — Estimator UI (auth-aware navbar, upload, room labels, truck load, polling progress, two-pass results with verification badges)
- `static/library.html` — Reference library viewer (searchable table, sort by seen/name/recent, source badges, stats)
- `static/landing.html` — Marketing landing page (hero, features, pricing, FAQ, scroll animations)
- `static/login.html` — Login form
- `static/signup.html` — Signup form (email, password, company name, city, state)
- `static/upgrade.html` — Subscription tier selection (Starter/Pro/Agency)
- `static/payment-success.html` — Post-payment confirmation
- `estimates.db` — SQLite database (auto-created on startup)

## Database Tables
- **User** — id, email, password_hash, company_name, company_city, company_state, subscription_tier, stripe_customer_id, estimates_used, pricing fields
- **Session** — id, token, user_id, expires_at
- **Estimate** — id, user_id, photos_count, result_json, price_low, price_high, cy_estimate, pass1_json, pass2_json, lookups_json, created_at
- **ItemReferenceLibrary** — id, item_name (unique), item_category, cubic_yards, is_special, special_fee, confidence, source (builtin|ai_learned|web_search|manual), search_query_used, times_seen, created_at, updated_at

## Seed Data
86 built-in items across categories: furniture, appliance, electronics, debris, outdoor, sports, medical, hazardous. Seeded on first startup.

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

## Environment Variables
- `ANTHROPIC_API_KEY` — Required. Claude vision API.
- `STRIPE_SECRET_KEY` — Stripe API key for payments.
- `STRIPE_WEBHOOK_SECRET` — Stripe webhook signature verification.
- `TAVILY_API_KEY` — Optional. For live market rate fetching and item dimension lookups.
