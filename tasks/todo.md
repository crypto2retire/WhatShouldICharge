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
