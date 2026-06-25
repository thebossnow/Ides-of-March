import Anthropic from "@anthropic-ai/sdk";

export const anthropic = new Anthropic(); // reads ANTHROPIC_API_KEY from the environment

/**
 * Model routing. Defaults to Opus 4.8 (quality-first) for the customer-facing turn.
 * For a high-volume front desk you can route cheaper models per task once you have
 * volume data — see docs/02-build-plan.md §4. Switching is the operator's call.
 */
export const MODELS = {
  conversation: "claude-opus-4-8",
  classify: "claude-haiku-4-5", // optional: intent / guardrail pre-classification
} as const;
