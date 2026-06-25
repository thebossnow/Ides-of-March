# How I'd Build a SystIQ-style AI Front-Desk Platform

This is the engineering plan for building the product summarised in `01-product-summary.md`:
a multi-tenant **AI front desk** that captures, qualifies, books, and follows up on leads
across **web chat + WhatsApp**, with **baseline benchmarking** and **monthly performance
reporting**.

The plan separates two things SystIQ deliberately couples:

1. **The platform** — the software an agency operates to run many client front desks.
2. **The per-client onboarding pipeline** — the productised "7-day build + monthly programme".

A starter scaffold for the core chat/qualification/webhook pieces ships alongside this doc
(see `../README.md`).

---

## 1. Product requirements (derived from the site)

**Must-have (Foundation tier):**
- Web + WhatsApp AI assistant grounded on per-tenant knowledge (services, pricing, FAQs).
- Lead capture + qualification (service, date, budget, location, requirements).
- Appointment booking automation.
- WhatsApp lead notifications to the business.
- Monthly performance report + a baseline captured before go-live.
- Regulated-topic guardrails + human handoff.

**Tier 2 (Active):** follow-up sequences, multi-step qualification, CRM integration, Google
Business Profile optimisation, strategy call.

**Tier 3 (Intelligent):** custom workflow design, multi-platform (WA/email/SMS), AI review
management, staff-handover workflows.

**Cross-cutting:** multi-tenant (one agency → many client businesses), per-tenant config &
feature flags by tier, PDPA-compliant data handling (Singapore), clean data export on
off-boarding, observability of "what visitors ask / where they hesitate".

---

## 2. Architecture overview

```
                         ┌────────────────────────────────────────────┐
   Website visitor ──▶   │  Embeddable web widget (widget.js)          │
                         └───────────────┬────────────────────────────┘
   WhatsApp user  ──▶  Meta Cloud API ──▶│                             │
                                         ▼                             │
                         ┌────────────────────────────────────────────┐
                         │  App / API  (Next.js on Vercel)             │
                         │  • /api/chat        (web channel)           │
                         │  • /api/webhooks/whatsapp                   │
                         │  • Conversation orchestrator + tool loop    │
                         │  • Admin dashboard + client report viewer   │
                         └───┬───────────────┬───────────────┬─────────┘
                             │               │               │
                ┌────────────▼───┐   ┌───────▼────────┐  ┌───▼─────────────┐
                │ Postgres +     │   │ Claude API     │  │ Job queue       │
                │ pgvector       │   │ (Anthropic SDK)│  │ (BullMQ/Inngest)│
                │ (Supabase)     │   │ chat + tools   │  │ follow-ups,     │
                │ tenants, leads,│   │ + RAG grounding│  │ reports, reviews│
                │ convos, metrics│   └────────────────┘  └───┬─────────────┘
                └────────────────┘                           │
                         ▲                ┌──────────────────▼──────────────────┐
                         │                │ Integrations: WhatsApp Cloud API,    │
                         └────────────────┤ Cal.com/Google Calendar, CRM         │
                                          │ (HubSpot/Pipedrive), Google Business │
                                          │ Profile, email (Resend), SMS (Twilio)│
                                          └──────────────────────────────────────┘
```

### Tech stack (and why)

| Layer | Choice | Why |
|---|---|---|
| Web app / API | **Next.js (App Router) + TypeScript** | One deployable for marketing site, embeddable widget host, API routes, admin dashboard, and client report viewer. Streams Claude responses natively. |
| LLM | **Claude via `@anthropic-ai/sdk`** | Tool use for slot-filling/booking/handoff; streaming for low-latency chat; strong instruction-following for guardrails. Model choice per route — see §4. |
| Database | **Postgres + `pgvector` (Supabase)** | Relational core (tenants, leads, bookings, metrics) + vector store for RAG in one place. Row-level security for tenant isolation. |
| Background jobs | **Inngest** (or BullMQ + Upstash Redis) | Scheduled follow-up sequences, monthly report generation, review requests, metric rollups. Inngest's durable cron/step model fits "send message 2 at +24h". |
| WhatsApp | **Meta WhatsApp Cloud API** | First-party, template-message support for proactive follow-ups (24h window rules). Twilio is the fallback adapter. |
| Booking | **Cal.com (self-host/API)** or **Google Calendar API** | Cal.com is open-source and handles availability/timezones; GCal direct for simpler cases. |
| CRM | **HubSpot / Pipedrive adapters** | Push qualified leads; thin adapter interface so tiers can add CRMs. |
| Email / SMS | **Resend** / **Twilio** | Tier-3 multi-platform follow-up. |
| Auth | **Auth.js (NextAuth)** or **Clerk** | Agency staff + per-client logins; org/tenant model. |
| Reports | **React-PDF** + scheduled job | Monthly PDF emailed to the client; same data in the dashboard. |
| Observability | **Sentry** + **PostHog** | Errors + product analytics; PostHog doubles as the "visitor intelligence" event store. |
| Hosting | **Vercel** (app) + **Supabase** (db) + **Upstash** (redis) | Low-ops, scales per tenant. |

---

## 3. Data model (multi-tenant)

Prisma schema lives in `../prisma/schema.prisma`. Core entities:

- **Agency** → operates many **Tenants** (the client businesses).
- **Tenant** — name, vertical, tier (`FOUNDATION|ACTIVE|INTELLIGENT`), feature flags, timezone, channels config (web/WhatsApp/email/SMS), business hours.
- **User** — agency staff or client staff, scoped to agency/tenant with a role.
- **KnowledgeDoc** + **KnowledgeChunk** (embedding `vector`) — per-tenant services/pricing/FAQ corpus for RAG.
- **Conversation** + **Message** — one per visitor thread, per channel; stores transcript + handoff state.
- **Lead** — qualification fields (service, date, budget, location, requirements), `status` (`NEW|QUALIFYING|QUALIFIED|BOOKED|LOST`), source, score.
- **Booking** — linked to Lead; calendar event id, slot, status, no-show flag.
- **FollowUpSequence** + **ScheduledMessage** — the "second message that converts", with send-at + channel + template.
- **UnansweredQuestion** — captured whenever the assistant can't answer confidently → feeds the monthly "what visitors ask / where they hesitate" report and KB improvements.
- **MetricSnapshot** — daily/weekly rollups of the 5 tracked metrics per tenant.
- **Baseline** — the pre-build fixed snapshot (the whole accountability model hinges on this).
- **MonthlyReport** — generated artifact (data + PDF url) comparing baseline vs current.

PDPA notes: store minimal PII, encrypt at rest (Supabase), per-tenant data export + delete
endpoints for the "clean handover on cancellation" promise, and consent/opt-in tracking for
WhatsApp proactive messages.

---

## 4. The assistant (Claude) design

The conversation engine is a **tool-use loop** grounded by **RAG** over the tenant's KB.

**System prompt (per tenant)** assembles: business identity, services/pricing summary,
business hours/timezone, escalation rules, and **hard guardrails** (no medical/financial/
legal/professional advice; stay within approved info; route to human when unsure). Kept
byte-stable per tenant so prompt caching works (volatile context goes later in the messages).

**Tools exposed to Claude** (`../src/lib/assistant.ts`):
- `search_knowledge(query)` — RAG retrieval over the tenant KB (answers FAQs with grounded text).
- `capture_lead(service, date, budget, location, requirements, contact)` — structured slot-filling; persists/upder the Lead.
- `propose_booking_slots(service, preferred_window)` — pulls availability from Cal.com/GCal.
- `book_appointment(slot_id, lead_id)` — creates the booking + confirmation.
- `escalate_to_human(reason)` — flags handoff; notifies staff via WhatsApp.
- `log_unanswered_question(question)` — records gaps for the monthly report + KB backlog.

**Model selection (documented tradeoff):** the scaffold defaults to **`claude-opus-4-8`**
for the conversation. For a high-volume, latency-sensitive front desk you may route by task:
**`claude-haiku-4-5`** for cheap intent/guardrail classification, **`claude-sonnet-4-6`** or
Opus for the customer-facing turn. Model choice is the operator's call — opus-4-8 is the
quality-first default; switch per route once you have volume data.

**Streaming + low latency:** web chat streams tokens (`messages.stream`). Guardrail refusals
are handled by checking `stop_reason` and the system-prompt rules rather than post-hoc string
matching. Tool inputs are always JSON-parsed (never string-matched).

**Channel differences:** web is real-time SSE; WhatsApp is asynchronous via webhook, with the
**24-hour customer-care window** governing when proactive (template) follow-ups are allowed.

---

## 5. Per-client onboarding pipeline (the "7-day build")

The product is as much an operational pipeline as software. Encode it as a tenant state machine:

| Day | Stage | System actions |
|---|---|---|
| D1 | `DISCOVERY` | Create tenant; run **baseline capture** wizard → write `Baseline` (5 metrics); import current enquiry flow. |
| D2 | `PLANNING` | Ingest KB (services/pricing/FAQ) → embed into `KnowledgeChunk`; draft booking flow + follow-up sequences. |
| D3–5 | `BUILD` | Configure assistant, connect WhatsApp/Calendar/CRM, run automated test conversations; status pushed to owner via WhatsApp. |
| D6 | `REVIEW` | Owner walkthrough + sign-off; apply requested edits. |
| D7 | `LIVE` | Flip channels live; start metric collection against the fixed baseline. |

Then the **monthly programme** is a recurring job: W1 monitor, W2 lead-quality check, W3
booking-conversion review, W4 **MonthlyReport** generation (baseline vs current) emailed to
the client.

---

## 6. Metrics & reporting

Track exactly the 5 the site promises, plus the trust signal:

1. Enquiries captured / week
2. Lead-to-call conversion rate
3. Booking conversion rate
4. Average response time (minutes)
5. % enquiries handled automatically (no staff)
- (+ no-show/cancellation rate, Google review count & rating)

`MetricSnapshot` rollups run nightly; the monthly job diffs current vs `Baseline` and renders
the report. The "visitor intelligence" section is built from `UnansweredQuestion` + PostHog
events (common questions, hesitation points, objection themes, recommended KB changes).

---

## 7. Delivery roadmap (building the platform)

- **Phase 0 — Foundations (wk 1–2):** Next.js app, auth, tenant model, Postgres schema, CI.
- **Phase 1 — MVP front desk (wk 3–6):** web widget + `/api/chat` with RAG + capture/qualify tools; lead store; WhatsApp inbound webhook + notifications; basic Cal.com booking. → enough to run a Foundation-tier client.
- **Phase 2 — Measurement (wk 7–8):** baseline wizard, metric rollups, monthly PDF report, admin dashboard.
- **Phase 3 — Follow-up + Active tier (wk 9–11):** scheduled WhatsApp follow-up sequences, multi-step qualification, CRM adapter, GBP optimisation hooks.
- **Phase 4 — Intelligent tier (wk 12–14):** email/SMS channels, AI review management, staff-handover workflows, custom workflow builder.
- **Phase 5 — Hardening:** PDPA export/delete, rate limiting, prompt-injection defenses on KB content, load testing, per-tenant observability.

---

## 8. Risks & decisions

- **WhatsApp policy** — proactive follow-ups need approved templates + opt-in; the 24h window constrains timing. Build the template/opt-in layer early.
- **Grounding & safety** — regulated verticals (aesthetics/medical, financial) mean guardrails are a feature, not a nicety: strict "approved info only" + human routing, and treat KB text as untrusted input to resist prompt injection.
- **Latency vs quality** — pick models per route; measure with real traffic before downgrading.
- **The baseline is the moat** — capture it *before* go-live, immutably; the entire "accountability" value prop depends on a trustworthy baseline.
- **Tenant isolation** — Postgres row-level security + per-tenant KB namespaces; never let one tenant's retrieval touch another's chunks.
