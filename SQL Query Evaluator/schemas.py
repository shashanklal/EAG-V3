"""Pydantic models for the SME Query Validator."""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


# ── LLM layer ──────────────────────────────────────────────────────────────

class ToolDef(BaseModel):
    """Canonical tool definition passed to LLM."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CacheableSystemBlock(BaseModel):
    text: str
    cache: bool = False


class ResponseFormat(BaseModel):
    """Structured output format (matching llm_gatewayV2 pattern)."""
    type: Literal["json_schema", "json_object"] = "json_schema"
    schema_: Optional[dict[str, Any]] = Field(default=None, alias="schema")
    name: str = "out"
    strict: bool = True

    model_config = ConfigDict(populate_by_name=True)


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    system: Optional[str | list[CacheableSystemBlock]] = None
    model: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.3
    tools: Optional[list[ToolDef]] = None
    tool_choice: Optional[str | dict[str, Any]] = None
    reasoning: Optional[Literal["off", "low", "medium", "high"]] = None
    response_format: Optional[ResponseFormat] = None
    cache_system: Optional[bool] = None


class ChatResponse(BaseModel):
    provider: str = ""
    model: str = ""
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: Literal["tool_use", "end_turn", "max_tokens", "error"] = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_call_dialect: Literal["native", "prompted_fallback", "none"] = "native"
    reasoning_applied: bool = False
    parsed: Optional[dict[str, Any]] = None  # auto-parsed JSON when response_format used


# ── Tool results ───────────────────────────────────────────────────────────

class MetricIdentificationResult(BaseModel):
    """Output of Tool 1: Metric Identification."""
    correct_metric_fields: list[dict[str, Any]] = Field(default_factory=list)
    total_metrics_in_sme: int = 0
    correct_count: int = 0
    metric_match_score: float = 0.0
    grain_analysis: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""


class QueryFormulationResult(BaseModel):
    """Output of Tool 2: SQL Equivalence via Jaccard similarity."""
    table_similarity: float = 0.0
    join_similarity: float = 0.0
    filter_similarity: float = 0.0
    groupby_similarity: float = 0.0
    aggregation_similarity: float = 0.0
    select_similarity: float = 0.0
    sql_equivalence_score: float = 0.0
    component_details: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""


class ResultAgreementResult(BaseModel):
    """Output of Tool 3: Result Agreement via execution."""
    generated_result: Any = None
    gold_result: Any = None
    result_type: Literal["scalar", "distribution"] = "scalar"
    agreement_score: float = 0.0
    method_used: str = ""
    execution_error: Optional[str] = None
    reasoning: str = ""


class FinalScore(BaseModel):
    """Combined weighted score from all 3 tools."""
    tool1_score: float = 0.0
    tool2_score: float = 0.0
    tool3_score: float = 0.0
    tool1_weight: float = 0.40
    tool2_weight: float = 0.40
    tool3_weight: float = 0.20
    final_weighted_score: float = 0.0
    pass_fail: str = "FAIL"
    tool1_detail: Optional[MetricIdentificationResult] = None
    tool2_detail: Optional[QueryFormulationResult] = None
    tool3_detail: Optional[ResultAgreementResult] = None
