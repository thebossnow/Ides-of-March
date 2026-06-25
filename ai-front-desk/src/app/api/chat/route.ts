import type Anthropic from "@anthropic-ai/sdk";
import { runAssistantTurn, type TenantContext, type ToolHandlers } from "@/lib/assistant";

/**
 * Web chat channel. POST { tenantId, visitorRef, message, history? }.
 * In production: load the tenant + conversation from the DB, stream the reply (messages.stream),
 * and persist the updated history. This handler shows the orchestration shape.
 */
export async function POST(req: Request) {
  const { tenantId, message, history = [] } = (await req.json()) as {
    tenantId: string;
    visitorRef: string;
    message: string;
    history?: Anthropic.MessageParam[];
  };

  // TODO: load real tenant context + business summary from DB (kept byte-stable for prompt caching).
  const tenant: TenantContext = {
    id: tenantId,
    name: "Demo Clinic",
    vertical: "aesthetics",
    timezone: "Asia/Singapore",
    businessSummary: "Services: facials, laser, consultations. Hours: Mon–Sat 10:00–19:00.",
  };

  // TODO: wire these to the DB / Cal.com / CRM / WhatsApp-notify. Stubbed for the scaffold.
  const handlers: ToolHandlers = {
    async search_knowledge({ query }) {
      return `No KB results for "${query}" yet — connect the RAG store (pgvector).`;
    },
    async capture_lead(input) {
      return `Lead updated: ${JSON.stringify(input)}`;
    },
    async propose_booking_slots({ service }) {
      return `Available ${service} slots: [connect Cal.com] e.g. Tue 11:00, Wed 15:00.`;
    },
    async book_appointment({ slotId }) {
      return `Booked slot ${slotId} (stub). Confirmation sent.`;
    },
    async escalate_to_human({ reason }) {
      return `Escalated to staff: ${reason}. A human will follow up.`;
    },
    async log_unanswered_question({ question }) {
      return `Logged unanswered question: "${question}".`;
    },
  };

  const conv: Anthropic.MessageParam[] = [...history, { role: "user", content: message }];
  const { text, history: updated } = await runAssistantTurn(tenant, conv, handlers);

  return Response.json({ reply: text, history: updated });
}
