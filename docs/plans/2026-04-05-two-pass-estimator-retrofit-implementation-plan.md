## Two-Pass Estimator Retrofit Implementation Plan

Date: 2026-04-05
Repo: WhatShouldICharge

### Objective
Implement a two-pass estimator retrofit in the current WSIC codebase without breaking the live single-pass path. The first release should improve accuracy and conversion before any deeper agent or mobile work.

### Milestone 1: Prompt Split And Orchestration
Goal: separate the current single prompt into explicit extraction and verification stages.

Work:
- add separate prompt builders in `services/industry_config.py`
  - extraction prompt
  - verification prompt
- keep the current prompt available as fallback during rollout
- update `run_estimate()` in `main.py` to:
  - build reusable photo payloads once
  - run Pass 1 extraction
  - run Pass 2 verification using the same photos plus Pass 1 JSON
  - store both raw results in memory for comparison

Success criteria:
- Pass 1 and Pass 2 both run on the same estimate job
- Pass 2 can silently fall back to Pass 1 on failure
- no frontend changes required yet

### Milestone 2: Structured Verification Output
Goal: force Pass 2 to return actionable correction fields instead of a generic second opinion.

Work:
- extend the Pass 2 schema to include:
  - `confirmed_items`
  - `uncertain_items`
  - `removed_items`
  - `verification_notes`
  - corrected `potential_duplicates`
- add strict parser validation in `main.py`
- reject malformed Pass 2 output and fall back safely

Success criteria:
- backend can tell exactly what Pass 2 changed
- false positives can be removed before pricing

### Milestone 3: Clarification Question Engine
Goal: ask bounded follow-up questions only when needed.

Work:
- add a helper in `main.py` that converts uncertainty patterns into short question sets
- initial question categories:
  - duplicate item across photos
  - included vs excluded background items
  - all bags included vs some bags excluded
  - hidden area included vs excluded
- add estimate job state for:
  - `clarification_needed`
  - `clarification_questions`
  - `clarification_answers`
- add API support for submitting clarification answers and resuming estimate finalization

Success criteria:
- estimate can pause before final pricing
- answers are structured and reusable by code

### Milestone 4: Frontend Pending-Question State
Goal: support clarification in all estimate surfaces.

Work:
- update:
  - `static/index.html`
  - `static/team.html`
  - `static/customer-estimate.html`
  - inline public estimate script in `main.py`
- add UI for:
  - pending clarification state
  - short multiple-choice questions
  - resume estimate after answers
- keep wording different if needed, but logic shared across all flows

Success criteria:
- customer and internal users can answer the same bounded clarification questions
- UI does not treat clarification as an error

### Milestone 5: Deterministic Finalization Consolidation
Goal: make code the final authority after model extraction and verification.

Work:
- centralize finalization order in `run_estimate()`:
  - Pass 1
  - Pass 2
  - clarification
  - `validate_estimate()`
  - item total sync
  - scene classification
  - volume guardrails
  - geometry sanity
  - confidence policy
  - pricing
- ensure hard rules remain code-owned:
  - bag/bucket/paint caps
  - background shelving exclusion
  - small-job caps
  - scene/job label suppression

Success criteria:
- bad model labels cannot directly force bad prices
- final totals come from verified items plus deterministic rules

### Milestone 6: Admin Visibility And Tuning
Goal: make two-pass behavior inspectable.

Work:
- persist selected intermediate fields on `Estimate`
  - pass summaries, not full raw prompts unless needed
  - verification notes
  - clarification question/answer data
  - pass disagreement flags
- update admin review UI to show:
  - Pass 1 vs Pass 2 differences
  - whether clarification was asked
  - whether clarification improved final confidence

Success criteria:
- estimate drift is visible in admin
- tuning does not depend on guessing from final output only

### Suggested File-Level Work Order
1. `services/industry_config.py`
2. `main.py`
3. `static/index.html`
4. `static/team.html`
5. `static/customer-estimate.html`
6. inline public estimate script in `main.py`
7. `tasks/lessons.md`

### Recommended First Build Slice
Build only Milestones 1 and 2 first.

Why:
- highest backend leverage
- no forced UI churn yet
- lets you compare single-pass vs two-pass correction quality before exposing clarifications to users

That first slice should:
- run Pass 2 with the original photos
- use Pass 2 as the final model output when valid
- preserve single-pass fallback
- log or store verification notes for admin analysis

### Risks
- doubled model cost per estimate
- longer estimate latency
- schema drift if Pass 2 output is not tightly constrained
- frontend complexity once clarification becomes user-facing

### Mitigations
- keep Pass 2 prompt narrow
- cap Pass 2 max tokens aggressively
- fall back silently to Pass 1
- enable clarification only for medium/low-confidence jobs
- keep hard pricing and scene rules in code

### Verification Plan
Before full rollout, test against known bad examples:
- small garage bag jobs
- repeated items across two angles
- TVs vs screens
- paint cans / buckets
- background shelving
- mixed curbside piles

For each test job compare:
- single-pass result
- two-pass result
- final priced result after guardrails

### Recommendation
Implement the backend two-pass retrofit first, compare outcomes in admin, then add clarification UI. Do not spend more time tuning the current one-shot prompt until this split architecture is in place.
