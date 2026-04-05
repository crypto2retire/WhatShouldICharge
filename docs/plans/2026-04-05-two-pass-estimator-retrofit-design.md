## Two-Pass Estimator Retrofit Design

Date: 2026-04-05
Repo: WhatShouldICharge

### Goal
Replace the current single direct-Claude estimation path with a constrained two-pass estimator that is more accurate on real junk-removal jobs, especially small garage/storage pickups where the current model overcounts, over-classifies, and overprices.

### Problem Summary
The live estimator currently asks one Claude vision prompt to do too many things at once:
- detect visible items
- dedupe repeated items across photos
- assign cubic yards
- infer scene type
- infer job type
- explain uncertainty

This creates compounding failure modes:
- the same bad assumption appears in counts, scene labels, and totals
- duplicate-angle warnings do not reliably reduce the total
- unlabeled customer uploads lose garage/basement context
- grouped items such as trash bags and paint containers can dominate the estimate
- aggressive labels like `construction_debris` or `hoarder` make bad estimates even worse for conversion

### Recommended Architecture
Keep the current WSIC app structure, but split the estimation engine into four stages:

1. Pass 1: Visual extraction
2. Pass 2: Visual verification
3. Conditional clarification
4. Deterministic finalization in code

The key principle is simple: models should extract and verify visible facts, while code should own guardrails, scene policy, confidence policy, and pricing.

### Pass 1: Visual Extraction
Pass 1 receives the uploaded photos and returns a narrow structured output:
- visible items
- quantities
- photo sources
- possible duplicate groups
- special-fee candidates
- uncertainty flags
- short visual notes

Pass 1 should not be trusted to:
- decide final pricing
- decide final scene type
- decide final job type
- invent fallback miscellaneous volume

Pass 1 prompt goals:
- identify only visible haul-away items
- avoid background fixtures and installed shelving
- count repeated objects conservatively
- emit uncertainty rather than padding the estimate

### Pass 2: Visual Verification
Pass 2 must receive:
- the original photos again
- the Pass 1 JSON result

Pass 2 acts as a verifier, not a second creative estimator. Its job is to:
- confirm each listed item is actually visible
- remove false positives
- fix bag, box, and grouped-item counts
- confirm or reject potential duplicates
- downgrade special-fee items that are not visually certain
- return verification notes and explicit uncertain items

Pass 2 should be biased toward subtraction and correction, not inflation.

### Conditional Clarification
If meaningful ambiguity remains after Pass 2, WSIC should ask up to three bounded clarification questions before showing a final price.

This clarification step should be available in both:
- customer-facing estimate flows
- internal/team/operator flows

Clarifications should be multiple-choice whenever possible. Good question types:
- same item vs separate items across photos
- background shelving included vs excluded
- all visible bags included vs only selected bags
- hidden/behind-door/inside-closet items included vs excluded

Clarification should not become a freeform chatbot. It is a short structured intake interruption only when needed.

### Deterministic Finalization
After Pass 2 and any clarification answers, the backend should perform finalization in Python.

Code-owned responsibilities:
- volume lookup overrides
- bag, bucket, and paint-can per-item caps
- background fixture exclusion
- scene classification
- confidence bucket
- small-job caps
- pricing

Model-owned responsibilities:
- visible item extraction
- duplicate candidates
- uncertain-item reporting
- verification notes

When model output and guardrails disagree, code wins.

### Data Contract
The current estimate pipeline should be extended with structured intermediate fields:
- `pass1_result`
- `pass2_result`
- `verification_notes`
- `clarification_needed`
- `clarification_questions`
- `clarification_answers`
- `confirmed_items`
- `uncertain_items`
- `removed_items`

Clarification question structure:
- `id`
- `question`
- `type`
- `options`
- `target_items`

Example:
- `id`: `duplicate_bag_group`
- `question`: `Are these the same bags shown from another angle, or separate bags?`
- `type`: `single_choice`
- `options`: `same_items`, `separate_items`, `not_sure`

### Integration With Current Repo
Retrofitting should stay inside the existing WSIC architecture.

Primary integration points:
- `main.py`
  `run_estimate()` becomes the orchestrator for Pass 1, Pass 2, clarification, and finalization.
- `services/industry_config.py`
  Replace the single estimation prompt with separate prompt builders or prompt bodies for:
  - extraction
  - verification
  - clarification generation if needed
- `services/volume_lookup.py`
  Keep this deterministic validation layer and extend it only where repeated grouped-item overestimation remains a problem.

Frontend impact:
- signed-in estimate flow
- team/operator flow
- public/customer flow

All three need a pending-question state instead of assuming every estimate request immediately returns a finished price.

### Rollout Strategy
Roll this out behind a feature flag.

Stage 1:
- run Pass 1 and Pass 2 server-side
- do not expose clarification questions yet
- log Pass 1 vs Pass 2 differences for admin review

Stage 2:
- enable clarification on medium/low-confidence jobs only
- cap questions at 1 to 3

Stage 3:
- enable for all estimate surfaces
- keep single-pass fallback if Pass 2 fails

### Failure Policy
The retrofit must fail conservatively.

- If Pass 1 fails: return retry/error as today
- If Pass 2 fails: fall back to Pass 1 plus deterministic guardrails
- If clarification is needed but unanswered: do not show a falsely precise price
- If both passes disagree heavily on a small job: prefer the smaller visually confirmed total unless truck-load context exists

### What Not To Build Yet
Do not revive the old agent design wholesale.

Avoid for now:
- automatic AI-written reference library updates
- freeform conversational intake on every estimate
- model-owned final pricing logic
- verification without photos
- unconstrained “skeptical reviewer” prompts with loose output

### Recommendation
Adopt the two-pass estimator as a constrained extraction-and-verification system, not as a generic chat agent. Add clarification only when uncertainty is material. Keep pricing and hard guardrails deterministic in code.
