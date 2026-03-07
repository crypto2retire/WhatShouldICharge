# WhatShouldICharge — AI Junk Removal Estimator

## Overview
An AI-powered junk removal job estimator. Users upload customer photos, Claude vision AI analyzes them and returns price ranges, cubic yard estimates, item breakdowns, and job type classification. Includes user auth, Stripe subscriptions, live market rate fetching, and a marketing landing page.

## Stack
- **Backend**: Python 3.11, FastAPI, SQLite (via SQLAlchemy + aiosqlite)
- **Frontend**: Single-page HTML files (no framework), dark theme
- **AI**: Anthropic Claude (`claude-sonnet-4-20250514`) with vision
- **Auth**: bcrypt password hashing, session cookies (30-day)
- **Payments**: Stripe checkout sessions + webhooks for subscription lifecycle
- **Market Data**: Tavily API for live market rate fetching
- **Server**: uvicorn on port 5000

## Routes
- `GET /` — Marketing landing page (`static/landing.html`)
- `GET /estimate` — Estimator app (`static/index.html`, requires auth)
- `GET /login` — Login page (`static/login.html`)
- `GET /signup` — Signup page (`static/signup.html`)
- `GET /upgrade` — Subscription upgrade page (`static/upgrade.html`)
- `GET /payment-success` — Payment confirmation (`static/payment-success.html`)
- `POST /api/estimate` — Submit photos for AI estimate (auth required)
- `GET /api/estimates` — Fetch estimate history (auth required)
- `POST /api/auth/signup` — Create account
- `POST /api/auth/login` — Log in
- `POST /api/auth/logout` — Log out
- `GET /api/auth/me` — Current user info
- `POST /api/stripe/create-checkout` — Create Stripe checkout session
- `POST /api/stripe/webhook` — Stripe webhook handler

## Files
- `main.py` — FastAPI backend, DB models, auth, Stripe, pricing logic, Claude API
- `static/index.html` — Estimator UI (auth-aware navbar, upload, room labels, truck load, results, market context display)
- `static/landing.html` — Marketing landing page (hero, features, pricing, FAQ, scroll animations)
- `static/login.html` — Login form
- `static/signup.html` — Signup form (email, password, company name, city, state)
- `static/upgrade.html` — Subscription tier selection (Starter/Pro/Agency)
- `static/payment-success.html` — Post-payment confirmation
- `estimates.db` — SQLite database (auto-created on startup)

## Database Tables
- **User** — id, email, password_hash, company_name, company_city, company_state, subscription_tier, stripe_customer_id, estimates_used, pricing fields
- **Session** — id, token, user_id, expires_at
- **Estimate** — id, user_id, cubic_yards, price_low, price_high, job_type, confidence, notes, items_json, created_at

## Pricing Logic
- Standard rate: $35/CY (low) – $40/CY (high), customizable per user
- Premium rate: $55/CY for hoarder, heavy items, stairs, truck load, or >10 CY
- Special item surcharge: +$25 each for TVs, mattresses, tires, propane
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
- `TAVILY_API_KEY` — Optional. For live market rate fetching.
