# WSIC Estimation Upgrades Design

Date: 2026-04-03
Project: WhatShouldICharge
Status: Approved design, implementation not started

## Goal

Improve cubic-yard estimate accuracy in the existing WSIC web product without increasing customer friction enough to hurt conversion. Defer the native iPhone/LiDAR app until the product has stronger customer traction.

The product must support two real-world workflows:

- Remote customer-submitted estimates, where the customer is often walking a pile during a bid and cannot follow a strict multi-step capture routine.
- Operator-assisted estimates later, but only after the current web product has been strengthened.

## Non-Goals

- No native iPhone app in this phase
- No Android app in this phase
- No full geometry-first computer vision rebuild
- No major rewrite of the current prompt-centric estimation pipeline
- No requirement that customers perform a strict guided capture sequence before submitting

## Problem Statement

WSIC currently produces estimates from customer-uploaded photos using Anthropic Claude analysis, prompt rules, itemization, volume validation, and pricing logic. The current pipeline works, but it is constrained by inconsistent photo quality and by the limits of estimating cluttered 3D scenes from ordinary phone photos.

The biggest near-term opportunity is not a full model rewrite. It is reducing bad inputs, making low-confidence cases explicit, improving scene-aware validation, and storing real-world correction data so the system can be tuned against completed jobs.

## Product Strategy

WSIC should remain a web-first product with one primary public workflow:

- Remote Estimate: customer uploads ordinary phone photos and receives an instant estimate.

Inside that workflow, the system should become more selective and more explicit:

- It should detect obviously poor photo inputs before estimation.
- It should ask for an additional photo only when confidence is materially harmed.
- It should classify the scene type and use different validation rules by job type.
- It should preserve fast turnaround for good submissions.

An operator-focused higher-discipline capture mode can be added later inside the web app before any native mobile investment.

## Recommended Approach

Use a staged upgrade path:

1. Improve input quality and capture guidance
2. Make confidence affect user-facing behavior
3. Add scene classification and job-type-specific estimation rules
4. Add a geometry sanity layer after model output
5. Build a real-world calibration loop from completed jobs
6. Add operator assist mode later

This approach is preferred over a prompt-only refresh because prompt tuning alone has a limited ceiling. It is preferred over a computer-vision rebuild because the product does not yet justify the cost and complexity.

## Target Architecture

Current logical flow:

1. Customer uploads photos
2. Anthropic Claude analyzes photos and returns structured estimate output
3. `services/volume_lookup.py` validates item volumes
4. `calculate_price()` converts CY to pricing
5. Estimate is stored and returned

Proposed logical flow:

1. Customer uploads photos
2. Pre-estimate input-quality checks run on the photo set
3. System decides:
   - proceed normally
   - warn and continue with wider range
   - request one additional photo
4. Scene classification runs
5. Anthropic Claude analyzes the photos with scene-aware prompt context
6. Post-model geometry and occupancy sanity checks run
7. Confidence policy determines:
   - standard range
   - widened range
   - manual-review-style messaging or retry request
8. Estimate is stored with richer metadata for calibration

## Phase 1: Capture-Quality Upgrades

### Objective

Reduce clearly bad inputs before they enter the main estimation pipeline.

### Features

- Blur detection
- Too-dark image detection
- Too-close framing detection
- Missing floor or missing pile-context detection
- Near-duplicate angle detection
- Lightweight customer guidance in upload UI

### UX Requirements

Guidance must stay lightweight. The public flow should not become a mandatory guided scan. The system should prefer short messages such as:

- "Include the full pile and some floor around it."
- "This photo is too dark to estimate reliably."
- "Add one wider shot so the full pile is visible."

### Decision Rules

- If one photo is weak but the set is usable, continue.
- If the whole set lacks usable context, request one more photo.
- Avoid blocking good submissions with overly strict checks.

## Phase 2: Confidence and Retry Flow

### Objective

Make low confidence visible and actionable instead of forcing the same estimate behavior for every submission.

### Features

- Estimate confidence bucket
- Confidence-aware price range width
- Retry requests with specific reason labels
- Internal metadata on what caused low confidence

### Confidence Buckets

- High confidence: normal estimate range
- Medium confidence: slightly wider range and note that visibility was limited
- Low confidence: request one more photo or return a clearly wider provisional range

### Retry Triggers

- Full pile not visible
- Height cannot be inferred
- Images too dark or blurry
- Multiple piles merged ambiguously
- Dense clutter with insufficient angles

## Phase 3: Scene Classification and Job-Type Rules

### Objective

Stop treating every junk-removal scene as if the same assumptions apply.

### Initial Scene Types

- Curbside mixed junk
- Garage clutter
- Room furniture / interior cleanout
- Bagged trash / soft goods
- Construction debris
- Yard waste / outdoor pile
- Storage / shed / attic / basement overflow

### Why This Matters

Different scenes require different occupancy assumptions, anchor priorities, and tolerance bands:

- Construction debris is dense and can justify tighter volume assumptions.
- Furniture piles have more air and more shape ambiguity.
- Bagged trash and hoarder-style soft goods may require upward packing adjustments.
- Outdoor piles and curbside jobs often provide better boundary visibility than indoor rooms.

### System Impact

Scene type should be stored with each estimate and passed into the estimation and validation pipeline. It should influence:

- prompt instructions
- occupancy assumptions
- sanity-check thresholds
- price-range widening rules

## Phase 4: Geometry Sanity Layer

### Objective

Add a post-model check that compares the itemized estimate against plausible scene geometry without pretending to solve full 3D reconstruction.

### Inputs

- model item list
- itemized volume total
- photo count and angle diversity
- detected scene type
- visible floor extent estimate
- visible height band estimate

### Outputs

- geometry envelope estimate
- occupancy classification
- flags for likely undercount or overcount
- adjusted confidence

### Rules

This layer should be conservative. It should not aggressively overwrite model output. Its job is to catch obvious failures:

- phantom inflation from forcing sparse scenes to fill a bounding box
- obvious undercount when large visible bulk exceeds itemized volume
- scene types where the current result violates expected density patterns

### Adjustment Policy

- Prefer confidence changes before hard numeric overrides
- Apply bounded corrections only when evidence is strong
- Log every correction reason for later review

## Phase 5: Outcome Calibration Loop

### Objective

Create the data foundation required to materially improve model quality over time.

### Data to Store

- estimated CY
- estimated price range
- final charged price
- actual truck fraction or actual CY when known
- scene type
- confidence bucket
- correction reason
- operator notes
- whether more photos would have changed the estimate

### Admin Tooling

Add lightweight admin workflows to review estimates after job completion:

- mark estimate as accurate / low / high
- record actual truck fraction or actual CY
- categorize why the estimate missed
- surface recurring miss patterns by scene type

### Why This Matters

Without real completed-job feedback, improvements will mostly be prompt intuition. With outcome data, WSIC can be tuned against actual junk-removal operations.

## Phase 6: Operator Assist Mode

### Objective

Add a higher-discipline workflow for internal users without building a native app yet.

### Features

- optional internal-only capture mode
- prompts for:
  - one wide shot
  - left angle
  - right angle
  - close-up for dense debris
- stronger confidence expectations than public customer mode

### Reason for Deferral

This brings some of the benefit of guided capture while staying in the existing web stack. It is the right stepping stone before deciding whether native iPhone + LiDAR investment is justified.

## Data Model Changes

Likely new or expanded estimate fields:

- `capture_mode`
- `scene_type`
- `confidence_bucket`
- `confidence_reasons`
- `photo_quality_flags`
- `geometry_envelope_json`
- `occupancy_class`
- `actual_cy`
- `actual_truck_fraction`
- `accuracy_notes`
- `correction_reason`

The schema should be additive and backward-compatible.

## API and Pipeline Changes

### New Processing Stages

- pre-estimate photo quality analysis
- scene classification
- post-estimate geometry sanity validation
- calibration metadata persistence

### Design Principle

Do not collapse everything into one giant prompt. Keep distinct stages where possible so failures can be inspected and tuned independently.

## UX Principles

- Keep public customer flow low-friction
- Ask for more photos only when the expected accuracy gain is meaningful
- Explain low-confidence outcomes clearly
- Avoid pretending to know more than the system actually knows
- Preserve speed for good submissions

## Error Handling

- If quality checks fail softly, continue with warning
- If quality checks fail hard, request one better photo
- If classification is ambiguous, fall back to generic rules
- If geometry sanity logic cannot form a confident envelope, do not force numeric corrections
- If the model output is malformed, preserve current fallback/error behavior

## Testing Strategy

### Unit Tests

- photo quality rule thresholds
- scene classification mapping
- confidence bucket logic
- geometry sanity policy boundaries
- calibration field persistence

### Regression Dataset

Assemble a representative set of past estimates across:

- curbside jobs
- garages
- interior furniture
- construction debris
- yard waste
- hoarder-like soft goods

Track:

- current estimate output
- upgraded estimate output
- actual outcome where known

### Success Metrics

- lower percentage of severely wrong CY estimates
- lower frequency of phantom inflation cases
- higher match rate against actual truck usage
- acceptable estimate completion rate without harming conversion
- improved trust in low-confidence messaging

## Risks

- Overly strict capture checks could reduce estimate completion
- Geometry heuristics could introduce new failure modes if too aggressive
- Scene classification errors could misroute validation logic
- Calibration data may be sparse unless operators actually enter outcomes

## Mitigations

- Start with advisory checks before hard blocking
- Keep geometry corrections bounded and inspectable
- Store reasoning metadata on every intervention
- Make outcome entry fast enough that it actually gets used

## Recommended Implementation Order

1. Capture-quality upgrades
2. Confidence and retry flow
3. Scene classification and job-type rules
4. Geometry sanity layer
5. Outcome calibration loop
6. Operator assist mode

## Later Option: Native iPhone App

If WSIC gains traction and internal usage justifies it, the next major platform bet should be an iPhone app focused on fast on-site operator capture. That app should optimize for speed and clear accuracy gains, potentially using LiDAR depth assist during photo capture. It should not be started in this phase.

## Decision Summary

WSIC should improve the current web product first, not jump to native mobile. The recommended path is a low-friction accuracy upgrade: better input quality checks, confidence-aware UX, scene-aware rules, conservative geometry sanity checks, and a real calibration loop from completed jobs.
