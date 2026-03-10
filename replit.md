# WhatShouldICharge — AI Junk Removal Estimator

## Overview
WhatShouldICharge is an AI-powered junk removal job estimator. Its core purpose is to provide accurate price ranges, cubic yard estimates, item breakdowns, and job type classifications to users by analyzing customer-uploaded photos using advanced AI vision. The platform aims to streamline the estimation process for junk removal businesses, improve accuracy, and provide tools for team management, customer interaction, and business analytics. Key capabilities include user authentication, subscription management via Stripe, live market rate integration, a marketing landing page, an admin dashboard, a team portal with PIN-based authentication, and automated PDF estimate generation and email delivery. The business vision is to become the leading estimation tool in the junk removal industry, offering a scalable solution that reduces manual effort and increases conversion rates for businesses.

## User Preferences
I prefer iterative development, with a focus on delivering functional components that can be tested and refined. Please provide clear explanations of complex technical decisions. When implementing new features or making significant changes, ask for confirmation before proceeding. I value clean, readable code and robust error handling.

## System Architecture

### UI/UX Decisions
The frontend consists of single-page HTML files with a dark theme for a modern aesthetic. Key UI/UX considerations include:
- **Mobile-first design** for the team portal.
- **Visual grouping** of photos by room in the preview UI.
- **Professional PDF templates** for estimates, including company branding.
- **SEO optimization** for the landing page with full meta tags and JSON-LD structured data.
- **Accessibility features** such as semantic HTML, ARIA labels, skip links, and keyboard navigation support.

### Technical Implementations
- **Backend**: Python 3.11 with FastAPI, using SQLite via SQLAlchemy for data persistence.
- **AI**: Anthropic Claude vision AI (`claude-sonnet-4-20250514`) for image analysis.
- **Authentication**: Secure session cookies, bcrypt for password hashing, and PIN-based authentication for team members.
- **Payments**: Stripe Checkout sessions and webhooks for subscription management.
- **PDF Generation**: ReportLab for creating professional PDF estimates.
- **Email Delivery**: SendGrid for sending estimates to customers.
- **Server**: Uvicorn on port 5000.

### Feature Specifications
- **Spatial Reasoning Estimation Engine**: Uses known items with real L×W×H dimensions from the reference library as spatial anchors to calibrate photo scale. Even 1-2 recognized items provide a reliable ruler. The AI detects circled/marked items in photos and only includes those in the estimate. Users can also uncheck items from results to recalculate the price client-side.
- **Multi-Photo Per Room Handling**: Supports uploading multiple photos per room, which are then grouped and processed by the AI with explicit deduplication logic to avoid over-counting items.
- **Special Item Handling**: Identifies special items (e.g., hazardous materials) but excludes them from cubic yardage pricing, flagging them separately with recycling fee notices.
- **Asynchronous Estimation Flow**: Estimates are processed in background tasks, with frontend polling for status updates.
- **Admin Dashboard**: Comprehensive dashboard for analytics, user management, plan configuration, site content editing, and team management.
- **Team Portal**: PIN-authenticated portal for field estimators to capture customer info, upload photos, and generate/send estimates on mobile devices.
- **Site Configuration System**: Dynamic content management for the landing page and other site elements via an admin-editable key-value store.
- **Subscription Tiers**: Multiple tiers (Free, Starter, Pro, Agency) with varying estimate limits and pricing, managed through Stripe.
- **Security Hardening**: Implements security headers, rate limiting on auth endpoints, input validation, server-side validation for Stripe webhooks, HTML escaping for XSS prevention, generic error messages, and hardened CORS policies.
- **Performance Optimizations**: Includes database connection pooling, indexing, optimized library statistics and analytics queries, async programming patterns, and efficient image processing.
- **Frontend Optimization**: Focus on semantic HTML, ARIA labels, keyboard navigation, and proper error handling in `fetch()` calls.

## External Dependencies
- **Anthropic Claude API**: For AI vision capabilities and image analysis.
- **Stripe API**: For processing payments, managing subscriptions, and handling webhooks.
- **Tavily API**: Used for live market rate fetching and looking up item dimensions.
- **SendGrid API**: For sending email notifications and PDF estimates to customers.
- **ReportLab**: Python library for generating PDF documents.