## Admin Model Evaluator Design

Date: 2026-04-05
Repo: WhatShouldICharge

### Goal
Build an admin-only temporary model evaluator that can run a batch of uploaded images through multiple models, compare results, and generate:
- a CSV file
- an HTML visual report

This evaluator is for internal testing only. Customers must never see the test results.

### Scope
V1 supports:
- image upload from the admin dashboard
- temporary storage only
- two models:
  - Claude Sonnet 4
  - GPT-4.1 via OpenRouter
- CSV export
- HTML report with linked/embedded image references
- admin-only status, results, and cleanup

V1 does not include:
- customer-facing model comparison
- persistent eval history
- live shadow evaluation on production estimates
- DB-based saved-estimate selection yet

### Product Shape
Add a new `Model Eval` section to the admin dashboard.

Flow:
1. Admin uploads a batch of images
2. Admin chooses which models to run
3. A background job processes each image against each selected model
4. The system generates:
   - one CSV
   - one HTML visual report
5. Admin can open/download those files
6. Admin can delete the batch, and temp files are removed

### Temporary Workspace
Each eval run gets a temporary workspace folder under a server temp directory.

Workspace contents:
- uploaded images
- per-image normalized results JSON
- CSV output
- HTML report

These files are temporary and should be removable manually from admin. They may also be auto-cleaned after a TTL later.

### Model Execution
Each model should receive:
- the uploaded image
- the current junk-removal extraction prompt

For comparability, both outputs should go through the same local post-processing:
- JSON parse
- schema validation
- volume lookup validation
- item total sync
- scene classification
- special-fee normalization
- small-job guardrails where applicable
- pricing using default evaluator rates

This keeps the comparison focused on model output quality rather than downstream pipeline drift.

### Output Fields
Per image per model:
- filename
- model
- parse success
- CY
- low price
- high price
- scene type
- confidence
- item count
- special item count
- items summary
- notes
- raw error if failed

Comparison summary per image:
- Claude CY
- GPT-4.1 CY
- CY delta
- price midpoint delta
- scene match / mismatch
- parse status by model

### Admin UI
The admin page should show:
- upload form
- model checkboxes
- run button
- current job status
- completed job summary
- links/buttons for CSV and HTML report
- delete batch button

### Security
All evaluator endpoints must require admin auth.
No public or customer route should expose evaluator state or files.

### Recommendation
Ship the admin upload-based evaluator first. Use it to compare Claude vs GPT-4.1 on real test batches before deciding whether to add dual-model production review.
