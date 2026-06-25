/**
 * WhatsApp Cloud API (Meta) webhook.
 *  - GET  verifies the subscription (hub.challenge handshake).
 *  - POST receives inbound messages → run the assistant → reply via the Cloud API.
 *
 * Proactive follow-ups (the "second message that converts") must use approved templates and
 * respect the 24-hour customer-care window — handled by the follow-up job, not this webhook.
 */

export async function GET(req: Request) {
  const url = new URL(req.url);
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode === "subscribe" && token === process.env.WHATSAPP_VERIFY_TOKEN) {
    return new Response(challenge ?? "", { status: 200 });
  }
  return new Response("Forbidden", { status: 403 });
}

export async function POST(req: Request) {
  const body = await req.json();

  try {
    const change = body?.entry?.[0]?.changes?.[0]?.value;
    const msg = change?.messages?.[0];
    if (msg?.type === "text") {
      const from: string = msg.from; // visitor phone — used as visitorRef
      const text: string = msg.text.body;

      // TODO: resolve tenant from change.metadata.phone_number_id, load conversation history,
      // run runAssistantTurn(...), persist, then send the reply below.
      const reply = `(stub) You said: ${text}`;
      await sendWhatsAppText(from, reply);
    }
  } catch (e) {
    // Always 200 so Meta doesn't retry-storm; log for investigation.
    console.error("whatsapp webhook error", e);
  }

  return new Response("ok", { status: 200 });
}

async function sendWhatsAppText(to: string, text: string) {
  const phoneNumberId = process.env.WHATSAPP_PHONE_NUMBER_ID;
  const accessToken = process.env.WHATSAPP_ACCESS_TOKEN;
  if (!phoneNumberId || !accessToken) return;

  await fetch(`https://graph.facebook.com/v21.0/${phoneNumberId}/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messaging_product: "whatsapp",
      to,
      type: "text",
      text: { body: text },
    }),
  });
}
