# WhatShouldICharge — AI Junk Removal Estimator

## Overview
An AI-powered junk removal job estimator. Customers upload photos of their junk, Claude vision AI analyzes them, and returns a price range, cubic yard estimate, and item breakdown.

## Stack
- **Backend**: Python 3.11, FastAPI, SQLite (via SQLAlchemy + aiosqlite)
- **Frontend**: Single-page HTML (no framework)
- **AI**: Anthropic Claude (`claude-sonnet-4-20250514`) with vision
- **Server**: uvicorn on port 5000

## Routes
- `GET /` — Marketing landing page (`static/landing.html`)
- `GET /estimate` — Estimator app (`static/index.html`)
- `POST /api/estimate` — Submit photos for AI estimate
- `GET /api/estimates` — Fetch estimate history (last 50)

## Files
- `main.py` — FastAPI backend, DB models, pricing logic, Claude API call
- `static/index.html` — Estimator UI (upload, room labels, truck load, results)
- `static/landing.html` — Marketing landing page (hero, features, pricing, FAQ)
- `estimates.db` — SQLite database (auto-created on startup)

## Pricing Logic
- Standard rate: $35/CY (low) – $40/CY (high)
- Premium rate: $55/CY for hoarder, heavy items, stairs, truck load, or >10 CY
- Special item surcharge: +$25 each for TVs, mattresses, tires
- Minimum charge: $75

## Environment Variables
- `ANTHROPIC_API_KEY` — Required. Set in Replit Secrets.
