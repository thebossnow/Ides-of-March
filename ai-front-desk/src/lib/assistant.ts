import type Anthropic from "@anthropic-ai/sdk";
import { anthropic, MODELS } from "./anthropic";

/**
 * The AI front-desk conversation engine: a tool-use loop grounded by per-tenant RAG and
 * bounded by regulated-topic guardrails. See docs/02-build-plan.md §4.
 */

export interface TenantContext {
  id: string;
  name: string;
  vertical: string;
  timezone: string;
  /** Compact, byte-stable summary of services + pricing + hours (kept stable for prompt caching). */
  businessSummary: string;
}

export function buildSystemPrompt(t: TenantContext): string {
  return [
    `You are the AI front desk for "${t.name}", a ${t.vertical} business (timezone ${t.timezone}).`,
    `Your job: greet visitors, answer questions from approved business information, qualify`,
    `enquiries (service, date, budget, location, requirements), and move them toward a booking.`,
    ``,
    `Business information you may rely on:`,
    t.businessSummary,
    ``,
    `Rules:`,
    `- Use search_knowledge before answering anything factual about services, pricing, or policy.`,
    `- Collect lead details progressively with capture_lead; don't interrogate — ask one or two at a time.`,
    `- When the visitor is ready, use propose_booking_slots then book_appointment.`,
    `- GUARDRAILS: never give medical, financial, legal, or other professional advice. Stay within`,
    `  approved business information. For regulated or sensitive questions, use escalate_to_human.`,
    `- If you cannot answer confidently from approved info, call log_unanswered_question and offer a human follow-up.`,
    `- Be concise and warm. Never invent prices, availability, or guarantees.`,
  ].join("\n");
}

export const TOOLS: Anthropic.Tool[] = [
  {
    name: "search_knowledge",
    description:
      "Retrieve approved business information (services, pricing, FAQs, policies) to ground an answer. Call this before answering any factual question.",
    input_schema: {
      type: "object",
      properties: { query: { type: "string", description: "What to look up" } },
      required: ["query"],
    },
  },
  {
    name: "capture_lead",
    description:
      "Persist or update the qualification details for this visitor. Call whenever you learn a new detail.",
    input_schema: {
      type: "object",
      properties: {
        service: { type: "string" },
        date: { type: "string", description: "Preferred date/time in ISO or natural language" },
        budget: { type: "string" },
        location: { type: "string" },
        requirements: { type: "string" },
        contactName: { type: "string" },
        contactPhone: { type: "string" },
        contactEmail: { type: "string" },
      },
    },
  },
  {
    name: "propose_booking_slots",
    description: "Fetch available appointment slots for a service and a preferred window.",
    input_schema: {
      type: "object",
      properties: {
        service: { type: "string" },
        preferredWindow: { type: "string", description: "e.g. 'next week mornings'" },
      },
      required: ["service"],
    },
  },
  {
    name: "book_appointment",
    description: "Book a specific slot for the qualified lead and send a confirmation.",
    input_schema: {
      type: "object",
      properties: { slotId: { type: "string" } },
      required: ["slotId"],
    },
  },
  {
    name: "escalate_to_human",
    description:
      "Hand off to a human (regulated/sensitive topic, complaint, or anything outside approved info). Notifies staff.",
    input_schema: {
      type: "object",
      properties: { reason: { type: "string" } },
      required: ["reason"],
    },
  },
  {
    name: "log_unanswered_question",
    description:
      "Record a question the assistant could not answer confidently, for the monthly report and knowledge-base backlog.",
    input_schema: {
      type: "object",
      properties: { question: { type: "string" } },
      required: ["question"],
    },
  },
];

/** Implement these against your DB / integrations (Cal.com, CRM, WhatsApp notify, etc.). */
export interface ToolHandlers {
  search_knowledge(i: { query: string }): Promise<string>;
  capture_lead(i: Record<string, unknown>): Promise<string>;
  propose_booking_slots(i: { service: string; preferredWindow?: string }): Promise<string>;
  book_appointment(i: { slotId: string }): Promise<string>;
  escalate_to_human(i: { reason: string }): Promise<string>;
  log_unanswered_question(i: { question: string }): Promise<string>;
}

/**
 * Run one assistant turn: a manual tool-use loop. Returns the final assistant text.
 * `history` is the running Anthropic message list for the conversation (persist it).
 */
export async function runAssistantTurn(
  tenant: TenantContext,
  history: Anthropic.MessageParam[],
  handlers: ToolHandlers,
): Promise<{ text: string; history: Anthropic.MessageParam[] }> {
  const system = buildSystemPrompt(tenant);

  // Loop until Claude stops requesting tools.
  // (Streaming is used in the web route; here we keep the loop simple and robust.)
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const res = await anthropic.messages.create({
      model: MODELS.conversation,
      max_tokens: 1024,
      system,
      tools: TOOLS,
      messages: history,
    });

    history.push({ role: "assistant", content: res.content });

    if (res.stop_reason === "refusal") {
      return { text: "I'm sorry, I can't help with that. Let me connect you with a person.", history };
    }
    if (res.stop_reason !== "tool_use") {
      const text = res.content
        .filter((b): b is Anthropic.TextBlock => b.type === "text")
        .map((b) => b.text)
        .join("");
      return { text, history };
    }

    const toolResults: Anthropic.ToolResultBlockParam[] = [];
    for (const block of res.content) {
      if (block.type !== "tool_use") continue;
      const name = block.name as keyof ToolHandlers;
      let out: string;
      try {
        // block.input is already parsed JSON — never string-match the serialized form.
        out = await handlers[name](block.input as never);
      } catch (e) {
        toolResults.push({
          type: "tool_result",
          tool_use_id: block.id,
          content: `Error: ${(e as Error).message}`,
          is_error: true,
        });
        continue;
      }
      toolResults.push({ type: "tool_result", tool_use_id: block.id, content: out });
    }
    history.push({ role: "user", content: toolResults });
  }
}
