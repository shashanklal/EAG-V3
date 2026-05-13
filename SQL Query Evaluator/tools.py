"""
Three evaluation tools for SME Query Validator.

Tool 1: Metric Identification (40% weight)
Tool 2: Query Formulation / SQL Equivalence (40% weight)
Tool 3: Result Agreement via execution (20% weight)

Chain: Tool 1 output → Tool 2 input; Tool 2 output → Tool 3 input.
"""
from __future__ import annotations

import json
import re
import sqlite3
import traceback
from pathlib import Path
from typing import Any

from prompts import format_tool1_prompt, format_tool2_prompt, format_tool3_prompt
from providers import BaseProvider
from schemas import (
    FinalScore,
    MetricIdentificationResult,
    QueryFormulationResult,
    ResultAgreementResult,
)

DB_PATH = Path(__file__).parent / "Database" / "SyntheticClaims.db"

# Maximum rows to return from query execution for display / comparison
MAX_RESULT_ROWS = 200


# ── Helpers ────────────────────────────────────────────────────────────────

def _strip_sql_comments(sql: str) -> str:
    """Remove single-line (-- ...) and multi-line (/* ... */) SQL comments."""
    # Remove multi-line comments first
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # Remove single-line comments
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql.strip()

def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # Find the outermost JSON object
    brace_start = text.find("{")
    if brace_start == -1:
        raise ValueError(f"No JSON object found in response: {text[:200]}")
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[brace_start : i + 1])
    # Fallback: try parsing from brace_start
    return json.loads(text[brace_start:])


def _execute_sql(sql: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """
    Execute a SQL query against the SQLite database.
    Returns (results_as_list_of_dicts, error_message).
    """
    if not DB_PATH.exists():
        return None, f"Database not found at {DB_PATH}. Run convert_db.py first."

    # Basic safety: only allow SELECT statements
    trimmed = sql.strip().upper()
    if not trimmed.startswith("SELECT") and not trimmed.startswith("WITH"):
        return None, "Only SELECT / WITH (CTE) queries are allowed for safety."

    conn = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(MAX_RESULT_ROWS)
        if not rows:
            return [], None
        columns = [desc[0] for desc in cur.description]
        result = [dict(zip(columns, row)) for row in rows]
        return result, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if conn:
            conn.close()


def _results_to_str(results: list[dict] | None, error: str | None) -> str:
    """Format query results for LLM consumption."""
    if error:
        return f"EXECUTION ERROR: {error}"
    if results is None:
        return "No results"
    if len(results) == 0:
        return "Empty result set (0 rows)"
    # Truncate for LLM context
    preview = results[:50]
    out = json.dumps(preview, indent=2, default=str)
    if len(results) > 50:
        out += f"\n... ({len(results)} total rows, showing first 50)"
    return out


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safe conversion to float."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _results_are_equal(a: list[dict], b: list[dict]) -> bool:
    """Check if two query result sets are identical (order-sensitive)."""
    if len(a) != len(b):
        return False
    for row_a, row_b in zip(a, b):
        if set(row_a.keys()) != set(row_b.keys()):
            return False
        for k in row_a:
            va, vb = row_a[k], row_b[k]
            # Handle float comparison with tolerance
            if isinstance(va, float) and isinstance(vb, float):
                if abs(va - vb) > 1e-6:
                    return False
            elif va != vb:
                return False
    return True


# ── Conversation History ───────────────────────────────────────────────────

class ConversationMemory:
    """Maintains conversation history across tool invocations."""

    def __init__(self):
        self.history: list[dict[str, str]] = []
        self.tool1_output: str = ""
        self.tool2_output: str = ""

    def add_user(self, content: str):
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self.history.append({"role": "assistant", "content": content})

    def get_messages(self) -> list[dict[str, str]]:
        return list(self.history)


# ── Tool 1: Metric Identification ─────────────────────────────────────────

def run_tool1_metric_identification(
    provider: BaseProvider,
    business_query: str,
    generated_sql: str,
    sme_sql: str,
    memory: ConversationMemory,
) -> MetricIdentificationResult:
    """
    Evaluate metric identification: does the generated query select the
    correct KPIs, metric definitions, and dimensional grain?

    Returns MetricIdentificationResult with score in [0, 1].
    """
    prompt = format_tool1_prompt(business_query, generated_sql, sme_sql)
    memory.add_user(prompt)

    response = provider.chat(
        memory.get_messages(),
        max_tokens=4096,
        temperature=0.2,
    )

    raw_text = response.text
    memory.add_assistant(raw_text)
    memory.tool1_output = raw_text

    try:
        parsed = _extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        return MetricIdentificationResult(
            metric_match_score=0.0,
            reasoning=f"Failed to parse LLM response as JSON. Raw: {raw_text[:500]}",
        )

    # Build result
    metric_mappings = parsed.get("metric_mappings", [])
    total = int(parsed.get("total_metrics_in_sme", len(metric_mappings)))
    correct = int(parsed.get("correct_count", sum(1 for m in metric_mappings if m.get("correct"))))
    score = parsed.get("metric_match_score", correct / max(total, 1))

    return MetricIdentificationResult(
        correct_metric_fields=metric_mappings,
        total_metrics_in_sme=total,
        correct_count=correct,
        metric_match_score=_safe_float(score),
        grain_analysis=parsed.get("grain_analysis", {}),
        reasoning=parsed.get("summary", raw_text[:300]),
    )


# ── Tool 2: Query Formulation ─────────────────────────────────────────────

def run_tool2_query_formulation(
    provider: BaseProvider,
    business_query: str,
    generated_sql: str,
    sme_sql: str,
    memory: ConversationMemory,
) -> QueryFormulationResult:
    """
    Evaluate SQL equivalence via Jaccard similarity across semantic components.

    Returns QueryFormulationResult with score in [0, 1].
    """
    prompt = format_tool2_prompt(
        business_query, generated_sql, sme_sql, memory.tool1_output
    )
    memory.add_user(prompt)

    response = provider.chat(
        memory.get_messages(),
        max_tokens=4096,
        temperature=0.2,
    )

    raw_text = response.text
    memory.add_assistant(raw_text)
    memory.tool2_output = raw_text

    try:
        parsed = _extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        return QueryFormulationResult(
            sql_equivalence_score=0.0,
            reasoning=f"Failed to parse LLM response as JSON. Raw: {raw_text[:500]}",
        )

    scores = parsed.get("component_scores", {})

    def _get_jaccard(key):
        v = scores.get(key, {})
        return _safe_float(v.get("jaccard", 0))

    return QueryFormulationResult(
        table_similarity=_get_jaccard("tables_joins"),
        join_similarity=_get_jaccard("tables_joins"),
        filter_similarity=_get_jaccard("filters"),
        groupby_similarity=_get_jaccard("groupby"),
        aggregation_similarity=_get_jaccard("aggregations"),
        select_similarity=_get_jaccard("select_columns"),
        sql_equivalence_score=_safe_float(
            parsed.get("sql_equivalence_score", 0)
        ),
        component_details=scores,
        reasoning=parsed.get("summary", raw_text[:300]),
    )


# ── Tool 3: Result Agreement ──────────────────────────────────────────────

def run_tool3_result_agreement(
    provider: BaseProvider,
    generated_sql: str,
    sme_sql: str,
    memory: ConversationMemory,
) -> ResultAgreementResult:
    """
    Execute both queries and compare results statistically.

    Returns ResultAgreementResult with score in [0, 1].
    """
    # Execute both queries
    gen_results, gen_error = _execute_sql(generated_sql)
    gold_results, gold_error = _execute_sql(sme_sql)

    # ── Short-circuit: if results are identical, skip LLM call ──
    if (
        gen_error is None
        and gold_error is None
        and gen_results is not None
        and gold_results is not None
        and _results_are_equal(gen_results, gold_results)
    ):
        result_type = "scalar" if len(gold_results) == 1 and len(gold_results[0]) == 1 else "distribution"
        return ResultAgreementResult(
            generated_result=gen_results,
            gold_result=gold_results,
            result_type=result_type,
            agreement_score=1.0,
            method_used="Exact match (results identical)",
            execution_error=None,
            reasoning="Both queries produced identical results. Agreement score = 1.0.",
        )

    gen_str = _results_to_str(gen_results, gen_error)
    gold_str = _results_to_str(gold_results, gold_error)

    prompt = format_tool3_prompt(
        gen_str, gold_str, gen_error, gold_error,
        memory.tool1_output, memory.tool2_output,
    )
    memory.add_user(prompt)

    response = provider.chat(
        memory.get_messages(),
        max_tokens=8192,
        temperature=0.2,
    )

    raw_text = response.text
    memory.add_assistant(raw_text)

    try:
        parsed = _extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        return ResultAgreementResult(
            generated_result=gen_results,
            gold_result=gold_results,
            agreement_score=0.0,
            execution_error=gen_error or gold_error,
            reasoning=f"Failed to parse LLM response. Raw: {raw_text[:500]}",
        )

    raw_result_type = parsed.get("result_type", "scalar")
    result_type = raw_result_type if raw_result_type in ("scalar", "distribution") else "scalar"
    agreement = _safe_float(parsed.get("agreement_score", 0))

    # Determine method used
    methods = parsed.get("methods_applied", {})
    if result_type == "scalar":
        method = "Relative Error"
    else:
        used = [k for k, v in methods.items() if v is not None]
        method = ", ".join(used) if used else "Distribution comparison"

    return ResultAgreementResult(
        generated_result=gen_results,
        gold_result=gold_results,
        result_type=result_type,
        agreement_score=agreement,
        method_used=method,
        execution_error=gen_error or gold_error,
        reasoning=parsed.get("summary", raw_text[:300]),
    )


# ── Orchestrator ───────────────────────────────────────────────────────────

def run_full_comparison(
    provider: BaseProvider,
    business_query: str,
    generated_sql: str,
    sme_sql: str,
) -> FinalScore:
    """
    Run all 3 tools in sequence (chained), compute weighted final score.

    Chain: Tool1 → Tool2 (with Tool1 output) → Tool3 (with Tool1+Tool2 output)
    Weights: Tool1=40%, Tool2=40%, Tool3=20%
    """
    # Strip SQL comments before any processing
    generated_sql = _strip_sql_comments(generated_sql)
    sme_sql = _strip_sql_comments(sme_sql)

    memory = ConversationMemory()

    # Tool 1: Metric Identification (40%)
    t1 = run_tool1_metric_identification(
        provider, business_query, generated_sql, sme_sql, memory
    )

    # Tool 2: Query Formulation (40%) — receives Tool 1 output
    t2 = run_tool2_query_formulation(
        provider, business_query, generated_sql, sme_sql, memory
    )

    # Tool 3: Result Agreement (20%) — receives Tool 1 + Tool 2 output
    t3 = run_tool3_result_agreement(
        provider, generated_sql, sme_sql, memory
    )

    # Weighted final score
    w1, w2, w3 = 0.40, 0.40, 0.20
    final = (w1 * t1.metric_match_score) + (w2 * t2.sql_equivalence_score) + (w3 * t3.agreement_score)
    final = round(min(max(final, 0.0), 1.0), 4)

    pass_fail = "PASS" if final >= 0.70 else "FAIL"

    return FinalScore(
        tool1_score=round(t1.metric_match_score, 4),
        tool2_score=round(t2.sql_equivalence_score, 4),
        tool3_score=round(t3.agreement_score, 4),
        tool1_weight=w1,
        tool2_weight=w2,
        tool3_weight=w3,
        final_weighted_score=final,
        pass_fail=pass_fail,
        tool1_detail=t1,
        tool2_detail=t2,
        tool3_detail=t3,
    )
