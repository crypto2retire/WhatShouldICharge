## Admin Model Evaluator Implementation Plan

Date: 2026-04-05
Repo: WhatShouldICharge

### Objective
Implement an admin-only temporary image-batch evaluator in WSIC that compares Claude Sonnet 4 and GPT-4.1 via OpenRouter, then generates CSV and HTML outputs for internal review.

### Milestone 1: Backend Eval Job Runner
Add backend support for temporary evaluator jobs.

Work:
- create an in-memory `model_eval_jobs` registry
- create a temp workspace per eval run
- add admin-only endpoints:
  - `POST /api/admin/model-evals`
  - `GET /api/admin/model-evals`
  - `GET /api/admin/model-evals/{job_id}`
  - `GET /api/admin/model-evals/{job_id}/download/{kind}`
  - `DELETE /api/admin/model-evals/{job_id}`

### Milestone 2: Model Execution
Implement batch execution for:
- Claude Sonnet 4
- GPT-4.1 via OpenRouter

Work:
- reuse the extraction prompt from `services/industry_config.py`
- add an OpenRouter call helper in `main.py`
- normalize all results through a shared evaluator post-processing function

### Milestone 3: CSV + HTML Generation
Generate review outputs per job.

CSV:
- one row per image/model result
- comparison columns for the pair

HTML:
- image preview
- model outputs side by side
- CY / price / scene / confidence comparison

### Milestone 4: Admin UI
Add a `Model Eval` section to `static/admin.html`.

UI elements:
- image upload area
- model selection
- start button
- running/completed state
- CSV download button
- HTML report button
- delete batch button

### Milestone 5: Cleanup
Delete temp files and remove the in-memory job when requested.

### Suggested File Order
1. `docs/plans/...`
2. `main.py`
3. `static/admin.html`
4. `tasks/lessons.md`

### Verification
Test with:
- a small 2-3 image batch
- Claude only
- GPT-4.1 only
- both models together
- download CSV and HTML
- delete batch cleanup
