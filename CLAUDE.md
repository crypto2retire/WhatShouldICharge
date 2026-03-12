# CLAUDE.md — WhatShouldICharge (WSIC)
# Master instructions for Claude Code. Read this fully before doing anything.

---

## Critical Rules
- This is WhatShouldICharge (WSIC) — a junk removal photo estimator
- Live at whatshouldicharge.app on Railway
- Auto-deploys from GitHub on push to main
- Database is Railway PostgreSQL — use DATABASE_PRIVATE_URL for internal connection
- File storage is DigitalOcean Spaces bucket: hauliq-uploads SFO3
- Never commit .env to GitHub

---

## Agent Behavior Rules

### 1. Plan First
- Enter plan mode for ANY non-trivial task (3+ steps)
- Write detailed specs upfront before touching code
- Check in before starting implementation

### 2. Verification Before Done
- Never mark a task complete without proving it works
- Check logs, demonstrate correctness
- Ask yourself: "Would a staff engineer approve this?"

### 3. Autonomous Bug Fixing
- When given a bug report: just fix it
- Zero context switching required from the user
- Find root cause, no temporary fixes

### 4. Self-Improvement Loop
- After ANY correction: update tasks/lessons.md
- Review lessons.md at start of every session

### 5. Demand Elegance
- Find root causes, no temporary fixes
- Senior developer standards

---

## Task Management
- Write plan to tasks/todo.md before starting
- Mark items complete as you go
- Capture lessons in tasks/lessons.md after corrections

---

## Core Principles
- Simplicity First: minimal code changes
- No Laziness: senior developer standards

---

*Last updated: March 2026.*
