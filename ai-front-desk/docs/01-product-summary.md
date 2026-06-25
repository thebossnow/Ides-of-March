# SystIQ — Product Summary & Feature List

> Source: review of **https://systiq.co/** (content captured June 2026). This document
> summarises what SystIQ sells and enumerates every feature/capability on the site, so the
> build plan in `02-build-plan.md` has a concrete target.

## What SystIQ is

SystIQ sells a **done-for-you "AI front desk" system for service businesses in Singapore**.
The pitch is not "a chatbot" — it is an outcome: turn an existing website's traffic into
booked appointments by **capturing, qualifying, responding to, and following up on every
enquiry, 24/7, across web chat and WhatsApp**, without the business hiring another person.

It is delivered as a **productised service**: a fixed 7-day build, then an ongoing monthly
"performance programme" that measures results against a baseline taken *before* the build.

- **Tagline:** *"Your website gets visitors. It should be converting them."*
- **Positioning line:** *"Your website is already paying for traffic. Make sure it does something with it."*
- **Target customer:** Singapore service-business owners (SMEs) with consistent enquiry
  volume and a repeatable enquiry → booking process.
- **Headline promises:** Live in 7 days · baseline benchmarked before build · WhatsApp + web AI
  · under 1 hour of the owner's time.
- **Current stage:** Founding-client phase — 5 spots, 50% off activation.

### The three problems it targets

1. **Slow response** — an 8pm enquiry waits until morning; by then the prospect booked a competitor.
2. **Manual qualification** — staff manually ask every lead the same 5 questions (service, date, budget, location, requirements) while paying customers wait.
3. **Weak follow-up** — most leads don't book on first contact; the second message converts them, but follow-up depends on whoever remembers. That's where revenue leaks.

### How it positions vs. alternatives

| Need | SystIQ system | Generic chatbot tool | DIY AI tool |
|---|---|---|---|
| 24/7 lead response | Responds, qualifies, routes | Mostly FAQs | You configure & maintain it |
| Process-first setup | Built around your workflow | Template-based | You guess the process |
| Booking + follow-up automation | Enquiry → qualified → booked | Partial | Wire up separate tools yourself |
| Monthly performance visibility | Baseline + uplift report | Basic usage data | Usage data only |
| Ongoing accountability | Review, improve, adjust | Self-managed | Troubleshoot alone |
| Visitor intelligence & learning | After 30 days: what visitors ask, where they hesitate, what converts | None | Data in logs, nobody synthesises it |

### Method / principles

- **Process before technology** — map the current enquiry/booking flow before touching tooling ("AI on a broken process accelerates the failure").
- **Measure before we build** — benchmark 5 key metrics first, to prove what changed after launch.
- **Built on your business** — trained on the client's services, pricing, and FAQs (not a generic template).
- **Accountable every month** — monthly reports show what moved; if numbers aren't where they should be, SystIQ says so first and states what they're changing.

---

## Full feature list

### A. AI assistant / conversational layer
- AI chatbot on **both web and WhatsApp**.
- Trained on the client's **services, pricing, and most common questions** (per-business knowledge base, not a template).
- **Conversation design**: decides what to answer, what *not* to answer, what lead details to collect, and when to hand off to a human.
- **Guardrails for regulated topics** — will not give medical, financial, legal, or professional advice; stays within approved business info and routes to a human for sensitive/regulated questions.
- Responds to visitors in **every mode**: the late-night specific question, the comparison shopper, the almost-ready booker who needs one reassurance.

### B. Lead capture & qualification
- **24/7 lead capture** across channels.
- **Lead qualification** — collects service, date, budget, location, requirements.
- **Multi-step lead qualification** (higher tiers) — branching qualification flows.
- **Lead routing** — qualified enquiries routed to the right place/person.
- **WhatsApp lead notifications** to the business.

### C. Booking & scheduling
- **Appointment booking automation** — enquiry → qualified → booked.
- **Booking flow** integrated into the website/chat.

### D. Follow-up & retention
- **WhatsApp follow-up sequences** (the "second message" that converts).
- **No-show / cancellation tracking.**
- **AI review management** (higher tier) — solicit/manage Google reviews.
- **Google Business Profile optimisation** (mid tier).

### E. Channels & integrations
- **Web chat widget** (embeddable on the existing site).
- **WhatsApp** (Business messaging) capture, notification, and follow-up.
- **Multi-platform automation** (top tier): WhatsApp, **email**, **SMS**.
- **CRM integration** (mid/top tier).
- **Staff handover workflows** (top tier) — clean human escalation.

### F. Measurement, reporting & accountability
- **Process review** of the current enquiry journey (discovery).
- **Baseline benchmarking** across **5 key metrics** taken *before* the build, fixed as the comparison point.
- **Monthly performance report** covering:
  - **Lead flow:** enquiries captured per week; lead-to-call conversion rate; booking conversion rate.
  - **Operational efficiency:** average response time (minutes); % of enquiries handled automatically (no staff); no-show & cancellation rate.
  - **Trust signals:** Google review count and average rating.
- **Visitor intelligence (after 30 days):** what visitors ask, where they hesitate, what converts; common questions, **unanswered questions**, objection themes, recommended improvements.
- **Accountability loop:** if numbers move, the report shows it vs baseline; if they don't, SystIQ explains why and what they're changing — "the programme should justify itself every month."
- **Monthly strategy call** (mid tier, 30 min) / **quarterly strategy deep-dive** (top tier).
- **Custom LLM workflow design** (top tier).

### G. Advanced / intelligent (top tier)
- Custom LLM workflow design.
- Multi-platform automation (WhatsApp, email, SMS).
- AI review management.
- Staff handover workflows.

### H. Vertical solution packages (target industries)
Aesthetics & Wellness (clinics, spas, medi-aesthetics) · Real Estate (agencies & independent agents)
· Education & Training (tuition centres, academies) · Home Services (renovation, cleaning, maintenance)
· Gyms & Personal Trainers · Coaches & Practitioners · Insurance & Financial (IFAs, insurance agencies).

### I. Delivery model

**Part 1 — The 7-day build**
- **D1 Discovery** — process review & baseline (5 metrics).
- **D2 Planning** — system architecture: knowledge base drafted, booking flow mapped, WhatsApp sequences designed.
- **D3–5 Build** — chatbot config, booking integration, WhatsApp flows, testing. Client does nothing; gets WhatsApp updates, not meetings.
- **D6 Review** — 30-minute walkthrough & sign-off (the client's one committed hour).
- **D7 Live** — system goes live across channels.

**Part 2 — Post-launch monthly programme (weeks 1–4)**
- W1 live monitoring (edge cases, response-quality tuning, stabilisation).
- W2 lead-quality check (qualification & routing at target rate?).
- W3 booking-conversion review + adjustments.
- W4 month-1 performance report (baseline vs current across all 5 metrics).

### J. Commercial model
- **No lock-in** — cancel with 30 days' notice; cancellation period is used to off-board and hand over all data (conversation data, visitor insights, lead patterns, performance history, documentation) in a structured format. "The learning your system built belongs to your business."
- **Tiers (SGD):**
  - **Foundation — Front Desk Starter:** S$3,500 activation + S$299/mo. Process review & baseline; AI chatbot (web + WhatsApp); lead capture & qualification; appointment booking automation; WhatsApp lead notifications; monthly performance report.
  - **Active System — Complete Front Desk (recommended):** S$5,500 + S$499/mo. Everything in Foundation + WhatsApp follow-up sequences; multi-step qualification; CRM integration; Google Business Profile optimisation; monthly 30-min strategy call; priority support.
  - **Intelligent System — Full Front Desk:** S$8,500 + S$799/mo. Everything in Active + custom LLM workflow design; multi-platform automation (WA, email, SMS); AI review management; staff handover workflows; quarterly strategy deep-dive.
- Activation fee paid once at project start; retainer billed monthly.
- **Founding Client Programme:** 5 spots; 50% off activation (any tier); full monthly rate; hands-on involvement; first access to new automations. In exchange: genuine engagement, permission to publish an approved case study, honest feedback.

### K. FAQ-derived capabilities & constraints
- Works as an **intelligence layer on an existing site** if the site is clear/fast/usable; otherwise a rebuild may be recommended (assessed honestly after review).
- **Low owner effort** — SystIQ researches, structures, drafts content; owner reviews/confirms.
- **Post-launch** the system is monitored and improved from real visitor behaviour (not a one-time project).
- **Data handover on exit** in a structured format; the learning belongs to the business.
- Good fit when the business depends on enquiries/appointments/consultations/quotes/trials/viewings/intake, especially with repeated questions, manual responses, after-hours leads, or paid traffic with unknown post-click outcomes.
