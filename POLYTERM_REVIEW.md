# PolyTerm Docs Review

Reviewing both docs together since the refactor plan is the proposed response to the assessment.

## ASSESSMENT.md — Solid, with caveats

**Strong points:**
- The headline finding (TUI-via-subprocess is the central liability) is the right call. 186 subprocess invocations across 83 files in an in-process Python app is a real architectural smell, not a stylistic complaint.
- Layer-by-layer ratings are calibrated; calling the analytics layer the strongest and TUI the weakest matches the symptom pattern (consolidation cycles, "fix 62 TUI screens" commits).
- The acknowledgment that the differentiation strategy is sound but implementation is undermining it is the most useful framing in the doc.

**Weaknesses:**
- **No quantification of user-facing impact.** "Poor performance" is mentioned but no numbers (cold-start ms, per-screen subprocess overhead, memory). The P1 startup targets (<150ms, <300ms) appear in the refactor plan but not the assessment that justifies them.
- **The "Live Data Fragility" section is anecdotal** — git history references without specific bug counts, MTBF, or user-reported severity. This is the section most likely to be dismissed as "every project has bugs."
- **"God class" claim for db/database.py at 1078 lines is weak by itself.** Line count ≠ god class; the actual indictment would be "knows about every domain model" (which is stated, but not demonstrated with a method-list or import-graph).
- **No risk that the refactor itself causes regressions** — 660 tests sound like a lot, but if most cover core/ and the TUI tests are subprocess-based contract tests, the test surface for the new in-process path is unknown.

**Overall:** Diagnosis is correct, supporting evidence is thinner than it should be. A skeptical engineering lead would ask for more concrete numbers before signing off on a multi-week refactor.

---

## REFACTOR_PLAN.md — Right direction, several real problems

**What's good:**
- **Option 1 (handler layer) is the right choice.** Option 2 (CliRunner) is a trap — it preserves the subprocess mental model with worse debuggability. Option 3 is rejected correctly.
- **Pilot selection is sensible.** Arbitrage first is the right call: clean dataclasses, isolated core logic, textbook thin-wrapper TUI screen. Deferring `live_monitor` to Phase 2 is wise.
- **Strict handler rules (no `console.input`, no TUI imports, return data) are correct.** These are the rules that prevent the new layer from rotting into the same mess.
- **Renderers as a shared concern** (the late mitigation in §12) is the single most important detail — without it you trade subprocess duplication for table-rendering duplication.

**Problems:**

1. **The plan is structurally broken.** There are two `## 11` sections and two "Risks & Mitigations" sections (§9 and §12). The §3 code block at line 191 starts mid-function with no opening `def`. §10 has a stray closing `)` orphan. This looks like a merge of two drafts that nobody re-read. For a planning doc the user will execute against, that's a real red flag — the doc itself demonstrates the same "ship without polish" pattern the assessment criticizes.

2. **The `output_format` parameter is over-designed.** A handler shouldn't take `"data" | "table" | "json"` AND an optional `console`. Pick one model:
   - Either: handler returns data, period. CLI does its own rendering. TUI does its own rendering. Shared renderers in `tui/renderers/` for both.
   - Or: handler returns data + optional render method on the result dataclass.
   - Mixing `output_format="table"` with `console=None` is exactly the kind of "two ways to do it" that drove the original mess.

3. **"3–6 focused days for one experienced developer" for Phase 1 is optimistic.** Four commands × (handler + thin CLI + TUI screen + tests + renderer extraction) is more like 8–12 days even when the patterns are clean. The pilot is also where you'll discover Config-loading inconsistencies, hidden side-effects, and rendering-code-that-can't-be-cleanly-extracted. Plan accordingly or you'll be in a "we said 5 days, we're on day 14, do we keep going?" conversation.

4. **No rollback or feature-flag strategy.** What if the arbitrage handler ships and produces subtly different output than the subprocess path? The "compatibility test" in §7 is mentioned but not designed. Concretely: the pilot should keep the old subprocess path behind a `POLYTERM_LEGACY_TUI=1` env var for ≥1 release.

5. **The screen registry (§11/Phase 3) is the highest-leverage architectural change but is deferred furthest.** The 170-entry `SCREEN_ROUTES` dict is also called out in the assessment as a primary friction point. Introducing the registry *during* Phase 1 (so new handlers register themselves) costs almost nothing extra and avoids three more phases of touching `controller.py`. I'd move registry intro to Phase 0.

6. **Live data path (`live_monitor`, WebSockets) is correctly deferred but never gets a concrete plan.** §12 says "may need a different pattern (background tasks + callbacks)" and leaves it there. This is the *other* high-risk area called out in the assessment. The plan should at least name an owner/timeline for that workstream so it doesn't become "we refactored everything except the part that actually breaks in production."

7. **Quantitative targets in §10 are mostly self-referential.** "< 15 subprocess calls" and "≥ 40% LOC reduction in controller.py" measure the refactor's mechanical progress, not its actual benefits. Add: cold-start time delta, per-screen latency delta, bug-rate in TUI screens 30/60/90 days post-merge. Without those, you can't tell if the refactor paid off.

---

## Recommendation

**Ship the refactor, but tighten the plan first.** Specifically before kickoff:

1. Fix the structural issues (dedupe §11 and Risks sections, fix the orphan code block).
2. Decide *one* output model — handlers return data, renderers are shared. Drop `output_format` from the handler signatures.
3. Move screen registry from Phase 3 → Phase 0/1. New handlers self-register from day one.
4. Add a `POLYTERM_LEGACY_TUI` escape hatch for the pilots.
5. Re-estimate Phase 1 as 8–12 days, not 3–6.
6. Add 2–3 user-facing metrics to §10.

The diagnosis is right. The plan needs another editing pass before it's safe to execute against.
