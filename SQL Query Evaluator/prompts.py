"""
Prompt definitions for the 3 evaluation tools.

Each prompt follows the evaluation criteria from Prompt Validator.md:
  1. Explicit Reasoning Instructions
  2. Structured Output Format (JSON)
  3. Separation of Reasoning and Tools
  4. Conversation Loop Support
  5. Instructional Framing
  6. Internal Self-Checks
  7. Reasoning Type Awareness
  8. Error Handling / Fallbacks
  9. Overall Clarity and Robustness
"""

# ── Database Schema Context (shared across all tools) ─────────────────────

SCHEMA_CONTEXT = """
DATABASE SCHEMA — SyntheticClaims SQLite Database
==================================================

Table: claims_tbl
Columns: claim_id TEXT, platform TEXT, region TEXT, lob TEXT, claim_type TEXT,
  network_ind TEXT, funding_arrangement TEXT, submission_channel TEXT,
  member_id TEXT, provider_id TEXT, group_id TEXT, policy_id TEXT,
  svc_from_date TEXT, svc_to_date TEXT, received_date TEXT, finalized_date TEXT,
  paid_date TEXT, claim_status TEXT, clean_claim_ind REAL, auto_adjudicated_ind REAL,
  total_charge_amt REAL, total_allowed_amt REAL, total_paid_amt REAL,
  denial_code TEXT, denial_desc TEXT, line_count REAL, event_count REAL, tat_days REAL

Table: claims_events_tbl
Columns: claim_id TEXT, event_id TEXT, event_seq REAL, event_type TEXT,
  event_ts TEXT, event_actor TEXT, status_after TEXT, note TEXT

Table: claims_lines_tbl
Columns: claim_id TEXT, line_id TEXT, line_num REAL, svc_from_date TEXT,
  svc_to_date TEXT, proc_code TEXT, rev_code TEXT, pos_code TEXT, dx_code TEXT,
  units REAL, charge_amt REAL, allowed_amt REAL, paid_amt REAL,
  line_status TEXT, denial_code TEXT, denial_desc TEXT

Table: claims_flat_tbl
Columns: claim_id TEXT, line_id TEXT, line_num REAL, svc_from_date TEXT,
  svc_to_date TEXT, proc_code TEXT, rev_code TEXT, pos_code TEXT, dx_code TEXT,
  units REAL, charge_amt REAL, allowed_amt REAL, paid_amt REAL,
  line_status TEXT, denial_code_x TEXT, denial_desc_x TEXT,
  platform TEXT, region TEXT, lob TEXT, claim_type TEXT, network_ind TEXT,
  funding_arrangement TEXT, submission_channel TEXT, member_id TEXT,
  provider_id TEXT, group_id TEXT, policy_id TEXT, received_date TEXT,
  finalized_date TEXT, paid_date TEXT, claim_status TEXT,
  clean_claim_ind REAL, auto_adjudicated_ind REAL,
  total_charge_amt REAL, total_allowed_amt REAL, total_paid_amt REAL,
  denial_code_y TEXT, denial_desc_y TEXT

METRIC CATALOG (GCO / MIS definitions):
GCO metrics (business outcomes):
  - Volume: COUNT(claim_id)
  - Paid $: SUM(total_paid_amt) or line-level SUM(paid_amt)
  - Denial rate: count of denied claims / total claims
  - Partial denial rate: claim_status = 'PartiallyDenied'
  - Regional/platform mix: GROUP BY platform, region

MIS metrics (operational health):
  - TAT: AVG(tat_days); SLA buckets (0-3, 4-7, 8+)
  - Clean claim rate (FPY): AVG(clean_claim_ind) * 100
  - Auto adjudication rate: AVG(auto_adjudicated_ind) * 100
  - Pend rate: derived from events where event_type = 'Pended'
  - Rework rate: claims with 'Info Received' / 'Re-validated' events
  - Complexity proxy: line_count, event_count

Grain levels:
  - Claim level: one row per claim (claims_tbl)
  - Line level: one row per service line (claims_lines_tbl)
  - Event level: one row per workflow event (claims_events_tbl)
  - Flat/denormalized: one row per line with claim attributes (claims_flat_tbl)
"""


# ── Tool 1: Metric Identification ─────────────────────────────────────────

TOOL1_METRIC_IDENTIFICATION_PROMPT = """
You are a Healthcare Claims Analytics Expert performing metric identification analysis.

ROLE: Evaluate whether a generated SQL query correctly identifies the right KPI definitions,
metrics, and dimensional grain compared to an SME's gold-standard reference query.

## REASONING TYPE: Analytical Comparison (metric-mapping + grain verification)

## TASK
Given:
  1. A business question (natural language)
  2. A generated SQL query (from a self-service tool)
  3. A gold-standard SME SQL query (the reference)

Determine whether the generated query maps to the correct metrics and grain.

## STEP-BY-STEP REASONING (Chain of Thought)

Follow these steps IN ORDER. Show your reasoning for each step.

### Step 1: IDENTIFY METRICS IN THE SME QUERY
- Parse the SME (gold) SQL query
- List every metric/KPI being computed (e.g., COUNT, SUM, AVG, CASE expressions)
- For each metric, map it to the standard catalog:
  - GCO metrics: Volume (COUNT), Paid $ (SUM paid), Denial rate, etc.
  - MIS metrics: TAT (AVG tat_days), Clean claim rate, Auto rate, Pend rate, etc.
- For non-scalar queries, list all SELECT columns and their purpose

### Step 2: IDENTIFY METRICS IN THE GENERATED QUERY
- Parse the generated SQL query the same way
- List every metric/KPI being computed
- Map each to the catalog

### Step 3: COMPARE METRIC MAPPINGS
- For each metric in the SME query, check if the generated query has an equivalent
- A metric is "correct" if:
  a) It uses the same column(s) or equivalent logic
  b) It applies the same aggregation function
  c) It does not alter the semantic meaning
- For non-scalar: compare column selections and computed expressions

### Step 4: VERIFY DIMENSIONAL GRAIN
- What grain does the SME query operate at? (claim vs line vs event vs flat)
- What grain does the generated query operate at?
- Are they the same? If not, does the grain difference change the metric semantics?

### Step 5: CALCULATE METRIC MATCH SCORE
- Formula: Metric Match Score = (# of correct metric mappings) / (total metrics in SME query)
- For non-scalar queries with column mappings:
  Score = (# of correctly mapped columns) / (total columns in SME SELECT)

### Step 6: SELF-CHECK
- Re-verify each mapping. Did you count correctly?
- Is the grain assessment consistent with the tables used?
- Are there any edge cases (e.g., CASE WHEN logic that changes meaning)?

## ERROR HANDLING
- Before analyzing, strip all SQL comments: single-line (-- ...) and multi-line (/* ... */). Only evaluate the executable SQL logic.
- If the generated SQL is syntactically invalid, set score to 0.0 and explain why
- If the business question is ambiguous, note the ambiguity but still score based on SME alignment
- If you are uncertain about a mapping, mark it as "uncertain" and count it as 0.5

{schema_context}

## INPUT
Business Question:
{business_query}

Generated SQL (Self-Service Tool):
{generated_sql}

Gold Standard SQL (SME Reference):
{sme_sql}

## OUTPUT FORMAT
Respond ONLY with valid JSON (no markdown fencing, no prose outside JSON):
{{
  "reasoning_steps": [
    {{"step": 1, "description": "Identify SME metrics", "findings": "..."}},
    {{"step": 2, "description": "Identify generated metrics", "findings": "..."}},
    {{"step": 3, "description": "Compare mappings", "findings": "..."}},
    {{"step": 4, "description": "Verify grain", "findings": "..."}},
    {{"step": 5, "description": "Calculate score", "findings": "..."}},
    {{"step": 6, "description": "Self-check", "findings": "..."}}
  ],
  "metric_mappings": [
    {{
      "sme_metric": "<metric name or expression>",
      "generated_metric": "<corresponding metric or 'MISSING'>",
      "correct": true,
      "reasoning_type": "arithmetic|logic|lookup",
      "notes": "..."
    }}
  ],
  "grain_analysis": {{
    "sme_grain": "<claim|line|event|flat>",
    "generated_grain": "<claim|line|event|flat>",
    "grain_match": true,
    "notes": "..."
  }},
  "total_metrics_in_sme": 0,
  "correct_count": 0,
  "metric_match_score": 0.0,
  "summary": "One-line summary of the analysis"
}}
"""


# ── Tool 2: Query Formulation ─────────────────────────────────────────────

TOOL2_QUERY_FORMULATION_PROMPT = """
You are a SQL Analysis Expert performing semantic SQL equivalence scoring.

ROLE: Compare a generated SQL query to a gold-standard SME query by decomposing
both into semantic components and computing Jaccard similarity for each component.

## REASONING TYPE: Mathematical (Jaccard Similarity) + Structural Decomposition

## PRIOR CONTEXT FROM TOOL 1
The Metric Identification analysis has already been performed with the following results:
{tool1_output}

Use this context to inform your analysis — the metric mappings and grain analysis
should be consistent with your SQL component comparison.

## TASK
Given:
  1. A business question (natural language)
  2. A generated SQL query
  3. A gold-standard SME SQL query

Compute SQL Equivalence Score using weighted Jaccard similarity across semantic components.

## MATHEMATICAL FRAMEWORK

### Jaccard Similarity
For two sets A and B:
  J(A, B) = |A ∩ B| / |A ∪ B|

Where:
- |A ∩ B| = number of elements in common
- |A ∪ B| = number of unique elements across both sets
- J ranges from 0.0 (no overlap) to 1.0 (identical)

### Component Weights
  - Tables/Joins: 20%
  - Filters & Time Windows: 20%
  - GROUP BY grain: 20%
  - Aggregations: 20%
  - SELECT columns: 20%

### Final Score
  SQL_Equivalence = Σ (weight_i × J_i) for all components

## STEP-BY-STEP REASONING (Chain of Thought)

### Sub-task 1: EXTRACT TABLES AND JOINS
- From SME query: list all tables referenced and join conditions
- From generated query: list all tables referenced and join conditions
- Normalize table names (case-insensitive, strip aliases)
- Compute: J_tables = |SME_tables ∩ Gen_tables| / |SME_tables ∪ Gen_tables|
- For joins: compare ON conditions semantically

### Sub-task 2: EXTRACT FILTERS AND TIME WINDOWS
- From SME query: list all WHERE/HAVING conditions
- From generated query: list all WHERE/HAVING conditions
- Normalize: convert date literals to a canonical form, normalize operators
- Compute: J_filters = |SME_filters ∩ Gen_filters| / |SME_filters ∪ Gen_filters|
- If neither query has filters, J_filters = 1.0

### Sub-task 3: EXTRACT GROUP BY GRAIN
- From SME query: list all GROUP BY columns
- From generated query: list all GROUP BY columns
- Normalize column names (case-insensitive, resolve aliases)
- Compute: J_groupby = |SME_groups ∩ Gen_groups| / |SME_groups ∪ Gen_groups|
- If neither query has GROUP BY, J_groupby = 1.0

### Sub-task 4: EXTRACT AGGREGATIONS
- From SME query: list all aggregate functions (COUNT, SUM, AVG, MIN, MAX, CASE)
- From generated query: list the same
- Normalize: SUM(total_paid_amt) and SUM(paid_amt) should be compared as aggregation patterns
- Compute: J_agg = |SME_aggs ∩ Gen_aggs| / |SME_aggs ∪ Gen_aggs|

### Sub-task 5: EXTRACT SELECT COLUMNS
- From SME query: list all output columns/expressions
- From generated query: list the same
- Compute: J_select = |SME_cols ∩ Gen_cols| / |SME_cols ∪ Gen_cols|

### Sub-task 6: COMPUTE WEIGHTED SQL EQUIVALENCE
- SQL_Equivalence = 0.20*J_tables + 0.20*J_filters + 0.20*J_groupby + 0.20*J_agg + 0.20*J_select

### Sub-task 7: SELF-CHECK
- Verify each set extraction is complete
- Re-check Jaccard computations (intersection / union)
- Confirm the weighted sum adds up correctly
- Flag any component where semantic equivalence differs from syntactic equivalence

## ERROR HANDLING
- Before analyzing, strip all SQL comments: single-line (-- ...) and multi-line (/* ... */). Only evaluate the executable SQL logic.
- If a query uses CTEs, expand them before extraction
- If a query uses subqueries, flatten them for comparison
- If columns use different aliases for the same data, treat them as equivalent
- If uncertain about equivalence, note the uncertainty and score conservatively (0.5)

{schema_context}

## INPUT
Business Question:
{business_query}

Generated SQL:
{generated_sql}

Gold Standard SQL (SME):
{sme_sql}

## OUTPUT FORMAT
Respond ONLY with valid JSON (no markdown fencing, no prose outside JSON):
{{
  "reasoning_steps": [
    {{"sub_task": 1, "component": "Tables/Joins", "sme_set": [...], "gen_set": [...], "intersection": [...], "union": [...], "jaccard": 0.0}},
    {{"sub_task": 2, "component": "Filters", "sme_set": [...], "gen_set": [...], "intersection": [...], "union": [...], "jaccard": 0.0}},
    {{"sub_task": 3, "component": "GroupBy", "sme_set": [...], "gen_set": [...], "intersection": [...], "union": [...], "jaccard": 0.0}},
    {{"sub_task": 4, "component": "Aggregations", "sme_set": [...], "gen_set": [...], "intersection": [...], "union": [...], "jaccard": 0.0}},
    {{"sub_task": 5, "component": "Select", "sme_set": [...], "gen_set": [...], "intersection": [...], "union": [...], "jaccard": 0.0}},
    {{"sub_task": 6, "component": "Weighted Score", "calculation": "...", "result": 0.0}},
    {{"sub_task": 7, "component": "Self-Check", "verified": true, "notes": "..."}}
  ],
  "component_scores": {{
    "tables_joins": {{"jaccard": 0.0, "weight": 0.20, "weighted": 0.0}},
    "filters": {{"jaccard": 0.0, "weight": 0.20, "weighted": 0.0}},
    "groupby": {{"jaccard": 0.0, "weight": 0.20, "weighted": 0.0}},
    "aggregations": {{"jaccard": 0.0, "weight": 0.20, "weighted": 0.0}},
    "select_columns": {{"jaccard": 0.0, "weight": 0.20, "weighted": 0.0}}
  }},
  "sql_equivalence_score": 0.0,
  "summary": "One-line summary"
}}
"""


# ── Tool 3: Result Agreement ──────────────────────────────────────────────

TOOL3_RESULT_AGREEMENT_PROMPT = """
You are a Data Validation Expert performing result agreement analysis.

ROLE: Given the execution results of both the generated SQL and the SME's gold-standard SQL,
compute a Result Agreement Score using statistical methods.

## REASONING TYPE: Statistical / Mathematical Comparison

## PRIOR CONTEXT
Tool 1 (Metric Identification) results:
{tool1_output}

Tool 2 (Query Formulation) results:
{tool2_output}

Use this context to understand what metrics are being compared and the structural differences
identified. This informs whether result differences are due to structural SQL issues or data issues.

## TASK
Given:
  1. The execution results of the generated SQL query
  2. The execution results of the gold-standard SME SQL query
  3. Any execution errors encountered

Compute a Result Agreement Score.

## MATHEMATICAL FRAMEWORK

### For Scalar Results (single number outputs):
  Result Agreement = 1 − min( |Generated − Gold| / |Gold|, 1 )

  Where:
  - |Generated − Gold| = absolute difference between the two results
  - |Gold| = absolute value of the gold standard result
  - If Gold = 0 and Generated = 0, Agreement = 1.0
  - If Gold = 0 and Generated ≠ 0, Agreement = 0.0

### For Distribution Results (multiple rows / tabular output):
  Use THREE complementary methods and take the average:

  Method 1: Top-K Overlap
  - Sort both results by the primary metric column (descending)
  - Take top-K rows (K = min(10, total rows))
  - Overlap = |Top-K_Gen ∩ Top-K_Gold| / K

  Method 2: Correlation (Pearson)
  - For numeric columns present in both results
  - Compute Pearson correlation coefficient r
  - Score = max(0, r)  (clamp negative correlations to 0)

  Method 3: Value-Level Agreement
  - For each matching group key, compute relative error per metric
  - Agreement = 1 - AVG(min(|gen_i - gold_i| / |gold_i|, 1)) across all rows

  Final Distribution Score = (TopK + Correlation + ValueLevel) / 3

## STEP-BY-STEP REASONING (Chain of Thought)

### Step 1: CLASSIFY RESULT TYPE
- Examine both result sets
- Are they scalar (single value) or distribution (multiple rows/columns)?
- Determine the appropriate scoring method

### Step 2: STRUCTURAL CORRECTNESS CHECK
- Do both queries return the same column structure?
- If not, which columns can be aligned?
- Are there missing/extra columns in the generated result?

### Step 3: FUNCTIONAL CORRECTNESS CHECK
- For each aligned column, compare the actual values
- Note any systematic differences (off-by-one, rounding, missing rows)

### Step 4: APPLY SCORING METHOD
- For scalar: compute relative error formula
- For distribution: compute all three methods (Top-K, Correlation, Value-Level)

### Step 5: COMPUTE FINAL AGREEMENT SCORE
- Apply the appropriate formula from the mathematical framework

### Step 6: SELF-CHECK AND DIAGNOSIS
- Does the agreement score make sense given Tool 1 and Tool 2 results?
- If structural equivalence is high but result agreement is low, flag potential data issues
- If structural equivalence is low, the low result agreement is expected
- Verify your arithmetic

## ERROR HANDLING
- Note: SQL comments (-- ... and /* ... */) have already been stripped before execution. Focus only on the result data.
- If one or both queries fail to execute, set agreement to 0.0 and report the error
- If results have incompatible schemas, attempt partial alignment and note limitations
- If results are empty on both sides, agreement = 1.0 (both correctly return nothing)
- If only one result is empty, agreement = 0.0

## INPUT
Generated SQL Result:
{generated_result}

Gold Standard SQL Result (SME):
{gold_result}

Generated SQL Execution Error (if any):
{generated_error}

Gold Standard SQL Execution Error (if any):
{gold_error}

## OUTPUT FORMAT
Respond ONLY with valid JSON (no markdown fencing, no prose outside JSON):
{{
  "reasoning_steps": [
    {{"step": 1, "description": "Classify result type", "findings": "...", "reasoning_type": "classification"}},
    {{"step": 2, "description": "Structural correctness", "findings": "...", "reasoning_type": "comparison"}},
    {{"step": 3, "description": "Functional correctness", "findings": "...", "reasoning_type": "arithmetic"}},
    {{"step": 4, "description": "Apply scoring", "findings": "...", "reasoning_type": "arithmetic"}},
    {{"step": 5, "description": "Final score", "findings": "...", "reasoning_type": "arithmetic"}},
    {{"step": 6, "description": "Self-check", "findings": "...", "reasoning_type": "verification"}}
  ],
  "result_type": "scalar|distribution",
  "structural_match": true,
  "methods_applied": {{
    "relative_error": null,
    "top_k_overlap": null,
    "correlation": null,
    "value_level_agreement": null
  }},
  "agreement_score": 0.0,
  "diagnosis": "Explanation of why the score is what it is",
  "summary": "One-line summary"
}}
"""


# ── Prompt formatting helpers ──────────────────────────────────────────────

def format_tool1_prompt(
    business_query: str,
    generated_sql: str,
    sme_sql: str,
) -> str:
    return TOOL1_METRIC_IDENTIFICATION_PROMPT.format(
        schema_context=SCHEMA_CONTEXT,
        business_query=business_query,
        generated_sql=generated_sql,
        sme_sql=sme_sql,
    )


def format_tool2_prompt(
    business_query: str,
    generated_sql: str,
    sme_sql: str,
    tool1_output: str,
) -> str:
    return TOOL2_QUERY_FORMULATION_PROMPT.format(
        schema_context=SCHEMA_CONTEXT,
        business_query=business_query,
        generated_sql=generated_sql,
        sme_sql=sme_sql,
        tool1_output=tool1_output,
    )


def format_tool3_prompt(
    generated_result: str,
    gold_result: str,
    generated_error: str,
    gold_error: str,
    tool1_output: str,
    tool2_output: str,
) -> str:
    return TOOL3_RESULT_AGREEMENT_PROMPT.format(
        generated_result=generated_result,
        gold_result=gold_result,
        generated_error=generated_error or "None",
        gold_error=gold_error or "None",
        tool1_output=tool1_output,
        tool2_output=tool2_output,
    )
