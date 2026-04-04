# WSIC Estimation Upgrades Implementation Plan

Date: 2026-04-03
Project: WhatShouldICharge
Status: Planning only
Depends on: `docs/plans/2026-04-03-estimation-upgrades-design.md`

## Objective

Translate the approved estimation-upgrades design into a concrete delivery plan for the current WSIC codebase, which is centered in `main.py` with supporting logic in `services/industry_config.py`, `services/volume_lookup.py`, and `static/`.

This plan intentionally avoids native mobile work. It focuses on improving the existing web product first.

## Current Code Touchpoints

The current implementation already has useful hooks for this work:

- `main.py`
  - `Estimate` model already stores `photos_json`, `actual_price`, `actual_cy`, and `accuracy_notes`
  - public and authenticated estimate endpoints already exist
  - `run_estimate()` is the main orchestration point
  - `calculate_price()` already converts estimate output into pricing
  - admin accuracy reporting already exists
- `services/industry_config.py`
  - current Anthropic prompt configuration for junk removal
- `services/volume_lookup.py`
  - current estimate validation logic
- `static/`
  - current upload and estimate UI
  - existing admin views that can absorb more calibration fields

This means the implementation should extend the existing pipeline rather than rewrite it.

## Delivery Strategy

Implement in four shipping milestones and two later milestones:

1. Photo quality checks and confidence plumbing
2. Scene classification and confidence-aware UX
3. Geometry sanity validation
4. Calibration data capture and admin review improvements
5. Operator assist mode later
6. Native iPhone/LiDAR later

The first two milestones should be prioritized because they improve outcomes quickly without heavy architectural risk.

## Milestone 1: Photo Quality Checks and Confidence Plumbing

### Goal

Catch clearly weak photo sets before the estimate runs and introduce internal confidence metadata that later milestones can build on.

### Scope

- Add lightweight photo quality analysis
- Add confidence-related estimate fields
- Add retry/warning decisions before the main estimate pipeline
- Preserve the current estimate flow for good photo sets

### Backend Work

Primary file: `main.py`

- Add a small pre-estimate analysis stage before `run_estimate()` starts expensive model work
- Create helper functions for:
  - blur detection
  - low-light detection
  - over-close / low-context framing detection
  - duplicate-angle heuristics
- Return structured metadata such as:
  - `photo_quality_flags`
  - `photo_quality_summary`
  - `confidence_reasons`
  - `recommended_retry`

### Suggested Data Model Additions

Add columns to `Estimate`:

- `capture_mode` text default `'remote'`
- `confidence_bucket` text default `''`
- `confidence_reasons` text default `''`
- `photo_quality_flags` text default `''`
- `scene_type` text default `''`

These can be JSON-encoded strings in the current architecture to stay consistent with the rest of `main.py`.

### API Work

Update these flows:

- `/api/estimate`
- `/api/public/estimate/{slug}`
- `/api/team/estimate`

Behavior:

- if the photo set is clearly unusable, return a structured retry response
- if usable but weak, continue and mark lower confidence
- if healthy, continue with no friction

### Frontend Work

Files likely affected:

- estimate page HTML generated from `main.py`
- `static/index.html`
- public estimate page flow in `main.py`

Add lightweight UI messaging only:

- "Add one wider shot"
- "Photo too dark"
- "Need the full pile in frame"

Do not add a rigid multi-step wizard in this milestone.

### Testing

- unit tests for image-quality thresholds
- regression tests for good photos not being incorrectly blocked
- endpoint tests for retry vs continue behavior

## Milestone 2: Scene Classification and Confidence-Aware UX

### Goal

Classify the job scene and make confidence visible in the estimate response and UI.

### Scope

- scene classification
- confidence bucket rules
- scene-aware price range widening
- prompt context updates

### Backend Work

Primary files:

- `main.py`
- `services/industry_config.py`

Add a classification step before the final prompt call or as part of a cheap initial call. Initial scene types:

- curbside mixed junk
- garage clutter
- room interior furniture
- bagged trash / soft goods
- construction debris
- yard waste / outdoor pile
- storage / attic / basement overflow

Use scene type to influence:

- prompt instructions
- occupancy assumptions
- acceptable tolerance bands
- confidence bucket logic

### UX Changes

Show estimate confidence in a restrained way:

- high confidence: no extra treatment
- medium confidence: slightly wider range and short note
- low confidence: request another photo or show clearly wider provisional range

This should be informative, not alarming.

### Testing

- scene classification mapping tests
- confidence bucket tests
- regression tests for estimate response payload shape

## Milestone 3: Geometry Sanity Validation

### Goal

Add a conservative post-model validation layer that catches obvious overestimates and underestimates without replacing the current AI pipeline.

### Scope

- estimate a basic geometry envelope from visible context
- classify occupancy pattern
- compare itemized output vs scene envelope
- adjust confidence first, numbers second

### Backend Work

Primary files:

- `main.py`
- `services/volume_lookup.py`

Recommended implementation structure:

- keep `services/volume_lookup.py` responsible for item-level validation
- add a new helper section or module for scene-level sanity checks
- feed its output back into the stored estimate metadata

Potential extracted module later:

- `services/estimate_sanity.py`

This module could own:

- geometry envelope estimation
- occupancy class assignment
- bounded correction rules
- correction-reason logging

### Rules

- do not aggressively override model output
- prefer confidence downgrade over hard numeric rewrite
- when numeric adjustment is applied, log exact reason
- keep corrections bounded and inspectable

### Testing

- sparse-scene inflation regressions
- obvious undercount regressions
- edge cases by scene type

## Milestone 4: Calibration Data and Admin Review

### Goal

Turn completed jobs into usable tuning data.

### Scope

- expand accuracy metadata collection
- improve admin entry and review
- support reporting by scene type and confidence bucket

### Backend Work

Primary file: `main.py`

Extend admin estimate update payloads and admin estimate detail responses to include:

- `scene_type`
- `confidence_bucket`
- `correction_reason`
- `actual_truck_fraction`
- richer `accuracy_notes`

If `actual_truck_fraction` is added, include a nullable column on `Estimate`.

### Admin UI Work

Primary file:

- `static/admin.html`

Add fields and review controls for:

- actual CY
- actual truck fraction
- estimate was high / low / accurate
- miss reason
- notes

Add filtered reporting slices:

- by scene type
- by confidence bucket
- by capture mode

### Testing

- admin CRUD tests for new fields
- analytics endpoint regression tests
- migration tests for nullable columns

## Milestone 5: Operator Assist Mode

### Goal

Introduce a stricter internal capture mode without building native mobile.

### Scope

- internal-only capture hints
- stronger confidence requirements
- guided but lightweight photo order

### Work

- add `capture_mode='operator_assist'`
- expose a separate internal UI path or toggle
- require suggested sequence:
  - wide shot
  - left angle
  - right angle
  - close-up if needed

This should happen only after the earlier confidence and validation groundwork is in place.

## Schema Migration Plan

### Additive Columns Only

Use the project’s current migration style in `init_db()` with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

Expected initial additions:

- `capture_mode`
- `confidence_bucket`
- `confidence_reasons`
- `photo_quality_flags`
- `scene_type`
- `correction_reason`
- `actual_truck_fraction`

All new fields should be nullable or have safe defaults to avoid breaking existing rows.

## API Contract Changes

### Estimate Submission Responses

Add structured non-error outcomes:

- `status: retry_needed`
- `retry_reason`
- `retry_message`
- `confidence_bucket`

Avoid overloading generic error responses for recoverable capture issues.

### Estimate Result Payloads

Add optional metadata:

- `confidence_bucket`
- `confidence_reasons`
- `scene_type`
- `capture_mode`

This should be additive so current clients do not break.

## File-Level Work Order

### First Pass

- `main.py`
  - add schema fields
  - add photo quality helpers
  - add confidence plumbing
  - update estimate endpoints
  - update admin endpoints

- `static/index.html`
  - surface customer-facing retry/warning states

- public estimate page HTML in `main.py`
  - surface retry messaging for public flows

### Second Pass

- `services/industry_config.py`
  - add scene-aware prompt variants or scene hints

- `services/volume_lookup.py`
  - integrate scene-aware tolerance inputs

### Third Pass

- `static/admin.html`
  - calibration and review UI

### Optional Extraction

If `main.py` becomes too large during this work, extract helper logic into:

- `services/photo_quality.py`
- `services/scene_classifier.py`
- `services/estimate_sanity.py`

This extraction is recommended if milestone 1 or 3 starts creating hard-to-test inline logic.

## Rollout Plan

### Release 1

- internal quality checks
- no or minimal user-facing friction
- metadata stored for inspection

### Release 2

- customer-visible retry requests for only the clearest low-quality cases
- confidence-aware messaging

### Release 3

- geometry sanity layer active
- admin review fields live

Roll out conservatively and inspect outcomes between releases.

## Success Metrics

Track before and after by weekly cohort:

- percentage of estimates later marked high / low / accurate
- absolute CY error where `actual_cy` exists
- percentage of estimates requiring retry
- estimate completion rate
- public conversion rate after retry prompts
- accuracy by scene type
- accuracy by confidence bucket

## Recommended First Build Slice

Start with Milestone 1 only:

- add photo quality metadata
- add confidence bucket plumbing
- store new fields on estimates
- return retry-needed responses without changing the rest of the estimate algorithm yet

This is the smallest slice with real value. It improves bad-input handling and creates the foundation for every later milestone.

## Deferred Work

- native iPhone app
- LiDAR capture
- Android strategy
- full CV reconstruction pipeline
- heavy ML training infrastructure

These should wait until the improved web product proves demand and exposes the biggest remaining accuracy bottlenecks.
