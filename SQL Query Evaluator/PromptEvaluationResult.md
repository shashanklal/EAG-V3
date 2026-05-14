# Prompt Evaluation Results

Evaluation of the 3 tool prompts in `prompts.py` against the criteria defined in `Prompt Validator.md`.

---

## Tool 1: Metric Identification (`TOOL1_METRIC_IDENTIFICATION_PROMPT`)

```json
{
  "explicit_reasoning": true,
  "structured_output": true,
  "tool_separation": true,
  "conversation_loop": true,
  "instructional_framing": true,
  "internal_self_checks": true,
  "reasoning_type_awareness": true,
  "fallbacks": true,
  "overall_clarity": "Excellent. All 9 criteria are satisfied. The prompt uses a clearly labeled 'STEP-BY-STEP REASONING (Chain of Thought)' section with 6 ordered steps, enforces strict JSON output with a detailed schema, separates reasoning from scoring, supports chaining into Tool 2 via its output, provides instructional framing with role definition and catalog references, includes an explicit Step 6 'SELF-CHECK', tags reasoning type at the prompt level ('Analytical Comparison') and per-metric in the output schema ('reasoning_type' field), and has a dedicated ERROR HANDLING section covering invalid SQL, ambiguous questions, and uncertainty (scored as 0.5). Highly robust and well-structured."
}
```

### Detailed Assessment

| # | Criterion | Pass | Evidence |
|---|-----------|------|----------|
| 1 | Explicit Reasoning Instructions | ✅ | "Follow these steps IN ORDER. Show your reasoning for each step." Six numbered steps under "STEP-BY-STEP REASONING (Chain of Thought)". |
| 2 | Structured Output Format | ✅ | JSON schema enforced with "Respond ONLY with valid JSON (no markdown fencing, no prose outside JSON)". Includes `reasoning_steps`, `metric_mappings`, `grain_analysis`, and scalar scores. |
| 3 | Separation of Reasoning and Tools | ✅ | Reasoning steps (Steps 1–6) are clearly separated from the metric catalog lookup references and from the final JSON output. Each step has a distinct purpose (parse → compare → score → verify). |
| 4 | Conversation Loop Support | ✅ | Designed to produce structured JSON output that is consumed by Tool 2 via `{tool1_output}`. The schema context and input variables are parameterized for multi-turn use. |
| 5 | Instructional Framing | ✅ | Role definition ("Healthcare Claims Analytics Expert"), explicit task description, metric catalog with GCO/MIS definitions, and a complete JSON output template serve as format examples. |
| 6 | Internal Self-Checks | ✅ | Step 6 is an explicit "SELF-CHECK": "Re-verify each mapping. Did you count correctly? Is the grain assessment consistent with the tables used? Are there any edge cases?" |
| 7 | Reasoning Type Awareness | ✅ | Prompt-level tag: "REASONING TYPE: Analytical Comparison (metric-mapping + grain verification)". Output schema includes per-metric `reasoning_type` field with allowed values: `arithmetic|logic|lookup`. |
| 8 | Error Handling / Fallbacks | ✅ | Dedicated "ERROR HANDLING" section: strips SQL comments before analysis, sets score to 0.0 for invalid SQL, notes ambiguous questions, treats uncertain mappings as 0.5. |
| 9 | Overall Clarity and Robustness | ✅ | Highly clear with logical flow. Explicit instructions minimize hallucination risk. JSON-only output constraint prevents drift. |

---

## Tool 2: Query Formulation (`TOOL2_QUERY_FORMULATION_PROMPT`)

```json
{
  "explicit_reasoning": true,
  "structured_output": true,
  "tool_separation": true,
  "conversation_loop": true,
  "instructional_framing": true,
  "internal_self_checks": true,
  "reasoning_type_awareness": true,
  "fallbacks": true,
  "overall_clarity": "Excellent. All 9 criteria are satisfied. The prompt provides a rigorous mathematical framework (Jaccard Similarity) with explicit formulas, 7 sub-tasks in a chain-of-thought structure, strict JSON output, consumes Tool 1 output for conversation continuity, includes a 'SELF-CHECK' sub-task, declares its reasoning type ('Mathematical + Structural Decomposition'), and has a comprehensive ERROR HANDLING section for CTEs, subqueries, aliases, and uncertainty. The weighted scoring model (5 components × 20%) is clearly defined and auditable."
}
```

### Detailed Assessment

| # | Criterion | Pass | Evidence |
|---|-----------|------|----------|
| 1 | Explicit Reasoning Instructions | ✅ | "STEP-BY-STEP REASONING (Chain of Thought)" with 7 sub-tasks. Each sub-task includes explicit extraction, normalization, and computation instructions. |
| 2 | Structured Output Format | ✅ | JSON-only output enforced. Schema includes `reasoning_steps` (per sub-task with sets, intersection, union, jaccard), `component_scores` (5 weighted components), and `sql_equivalence_score`. |
| 3 | Separation of Reasoning and Tools | ✅ | Mathematical framework (Jaccard formula, component weights) is clearly separated from the step-by-step reasoning process and the final scored output. Extraction → computation → verification flow. |
| 4 | Conversation Loop Support | ✅ | Explicitly consumes Tool 1 output: "PRIOR CONTEXT FROM TOOL 1: {tool1_output}". Instruction to maintain consistency: "metric mappings and grain analysis should be consistent with your SQL component comparison." Produces output for Tool 3. |
| 5 | Instructional Framing | ✅ | Mathematical formulas provided (Jaccard formula, weighted scoring), role definition ("SQL Analysis Expert"), component weight table (20% each), and complete JSON output template as format example. |
| 6 | Internal Self-Checks | ✅ | Sub-task 7 is "SELF-CHECK": "Verify each set extraction is complete. Re-check Jaccard computations. Confirm weighted sum adds up correctly. Flag semantic vs. syntactic differences." |
| 7 | Reasoning Type Awareness | ✅ | Prompt-level tag: "REASONING TYPE: Mathematical (Jaccard Similarity) + Structural Decomposition". Each sub-task focuses on a specific decomposition component. |
| 8 | Error Handling / Fallbacks | ✅ | Dedicated section: strips SQL comments, expands CTEs before extraction, flattens subqueries, treats different aliases for same data as equivalent, scores conservatively at 0.5 when uncertain. |
| 9 | Overall Clarity and Robustness | ✅ | Very well structured. The mathematical grounding (Jaccard) provides objectivity. Equal weighting across 5 components is transparent and auditable. JSON-only output prevents drift. |

---

## Tool 3: Result Agreement (`TOOL3_RESULT_AGREEMENT_PROMPT`)

```json
{
  "explicit_reasoning": true,
  "structured_output": true,
  "tool_separation": true,
  "conversation_loop": true,
  "instructional_framing": true,
  "internal_self_checks": true,
  "reasoning_type_awareness": true,
  "fallbacks": true,
  "overall_clarity": "Excellent. All 9 criteria are satisfied. The prompt provides a thorough statistical framework with separate methods for scalar vs. distribution results, a 6-step chain-of-thought process, strict JSON output, consumes both Tool 1 and Tool 2 outputs for full pipeline context, includes 'SELF-CHECK AND DIAGNOSIS' with cross-tool consistency verification, tags reasoning types per-step in the output schema, and has extensive error handling for execution failures, schema incompatibilities, and empty results. The three-method distribution scoring (Top-K, Correlation, Value-Level) is particularly robust."
}
```

### Detailed Assessment

| # | Criterion | Pass | Evidence |
|---|-----------|------|----------|
| 1 | Explicit Reasoning Instructions | ✅ | "STEP-BY-STEP REASONING (Chain of Thought)" with 6 numbered steps: Classify → Structural Check → Functional Check → Score → Final Score → Self-Check. |
| 2 | Structured Output Format | ✅ | JSON-only output enforced. Schema includes `reasoning_steps` (with `reasoning_type` per step), `result_type`, `methods_applied` (4 methods), `agreement_score`, and `diagnosis`. |
| 3 | Separation of Reasoning and Tools | ✅ | Mathematical framework (scalar formula, 3 distribution methods) is clearly separated from the reasoning steps. Classification step (Step 1) determines which mathematical path to follow. |
| 4 | Conversation Loop Support | ✅ | Consumes both Tool 1 and Tool 2 outputs: "Tool 1 (Metric Identification) results: {tool1_output}" and "Tool 2 (Query Formulation) results: {tool2_output}". Explicit instruction to use prior context for cross-tool consistency. |
| 5 | Instructional Framing | ✅ | Role definition ("Data Validation Expert"), complete mathematical formulas for scalar and distribution scoring, three complementary methods explained, and JSON output template. |
| 6 | Internal Self-Checks | ✅ | Step 6 "SELF-CHECK AND DIAGNOSIS": cross-references Tool 1/2 results, flags inconsistencies (e.g., high structural equivalence but low result agreement), and verifies arithmetic. |
| 7 | Reasoning Type Awareness | ✅ | Prompt-level tag: "REASONING TYPE: Statistical / Mathematical Comparison". Output schema includes per-step `reasoning_type` field with values like `classification`, `comparison`, `arithmetic`, `verification`. |
| 8 | Error Handling / Fallbacks | ✅ | Dedicated section: execution failures → 0.0 with error report, incompatible schemas → partial alignment with noted limitations, both empty → 1.0, one empty → 0.0. Also notes SQL comments are pre-stripped. |
| 9 | Overall Clarity and Robustness | ✅ | Excellent structure. Dual-path scoring (scalar vs. distribution) handles diverse query types. Three-method averaging for distributions reduces single-metric bias. Cross-tool diagnosis adds robustness. |

---

## Summary Comparison

| Criterion | Tool 1 | Tool 2 | Tool 3 |
|-----------|--------|--------|--------|
| 1. Explicit Reasoning | ✅ | ✅ | ✅ |
| 2. Structured Output | ✅ | ✅ | ✅ |
| 3. Tool Separation | ✅ | ✅ | ✅ |
| 4. Conversation Loop | ✅ | ✅ | ✅ |
| 5. Instructional Framing | ✅ | ✅ | ✅ |
| 6. Internal Self-Checks | ✅ | ✅ | ✅ |
| 7. Reasoning Type Awareness | ✅ | ✅ | ✅ |
| 8. Error Handling / Fallbacks | ✅ | ✅ | ✅ |
| 9. Overall Clarity | ✅ | ✅ | ✅ |
| **Score** | **9/9** | **9/9** | **9/9** |

## Overall Assessment

All three tool prompts score **9/9** against the Prompt Validator criteria. They demonstrate a consistent, high-quality design pattern:

- **Chain-of-thought reasoning** with numbered steps and explicit "show your reasoning" instructions
- **Strict JSON output** with no prose allowed outside the schema, making results parseable and validatable
- **Pipeline continuity** — Tool 1 feeds Tool 2, which feeds Tool 3, with explicit prior-context injection
- **Self-verification** built into every prompt as a dedicated final step
- **Reasoning type tagging** at both the prompt level and within output schemas
- **Comprehensive error handling** covering invalid inputs, edge cases, and uncertainty scoring

The prompts are well-engineered for structured LLM reasoning with minimal hallucination risk.
