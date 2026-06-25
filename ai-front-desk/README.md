# AI Front Desk

A SystIQ-style **AI front-desk** for service businesses: capture, qualify, book, and follow up
on every web + WhatsApp enquiry 24/7, with baseline benchmarking and monthly performance
reporting.

This folder is a **design + starter scaffold** produced from a review of
[systiq.co](https://systiq.co/):

- [`docs/01-product-summary.md`](docs/01-product-summary.md) — what SystIQ is + the full feature list.
- [`docs/02-build-plan.md`](docs/02-build-plan.md) — architecture, stack, data model, and delivery roadmap.
- `prisma/schema.prisma` — multi-tenant data model.
- `src/` — a lean, runnable slice: the Claude-powered chat orchestrator with tool use, a web
  chat API route, and the WhatsApp Cloud API webhook.
- `public/widget.js` — embeddable web chat widget loader.

> Status: scaffold. It illustrates the core conversation engine described in the build plan;
> integrations (Cal.com, CRM, report generation, follow-up jobs) are stubbed with clear TODOs.

## Quick start

```bash
cp .env.example .env.local   # fill in ANTHROPIC_API_KEY, DATABASE_URL, WhatsApp creds
npm install
npx prisma migrate dev       # create the schema
npm run dev                  # Next.js on http://localhost:3000
```

- Web chat:    `POST /api/chat`            — streams a Claude reply, runs the qualify/book/handoff tool loop.
- WhatsApp:    `GET/POST /api/webhooks/whatsapp` — Meta Cloud API verify + inbound handler.

## How the assistant works

The conversation engine (`src/lib/assistant.ts`) drives Claude with a small tool surface —
`search_knowledge`, `capture_lead`, `propose_booking_slots`, `book_appointment`,
`escalate_to_human`, `log_unanswered_question` — grounded by per-tenant RAG and bounded by
regulated-topic guardrails. See `docs/02-build-plan.md` §4 for the model-selection tradeoff
(defaults to `claude-opus-4-8`; route Haiku/Sonnet per task once you have volume).
