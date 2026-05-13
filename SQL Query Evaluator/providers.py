"""
LLM Provider architecture following llm_gatewayV2 pattern.

BaseProvider defines the interface; DatabricksClaudeProvider implements
it for Claude Sonnet 4.6 via Databricks serving endpoints (OpenAI-compatible).

Design notes (vs llm_gatewayV2):
  - Synchronous (not async): Streamlit runs synchronously, and the Databricks
    OpenAI SDK client is also sync — matching the MCP Task pattern.
  - No stream(): This app collects full responses (no SSE needed).
  - Retains: ProviderError, _apply_reasoning, _apply_response_format,
    retry-on-failure logic, model_capabilities pattern.
"""
from __future__ import annotations
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI

from schemas import (
    CacheableSystemBlock,
    ChatRequest,
    ChatResponse,
    ResponseFormat,
    ToolCall,
    ToolDef,
)

log = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────

class ProviderError(Exception):
    """LLM provider error with status code and retry hint."""
    def __init__(self, msg: str, status: int | None = None, retryable: bool = True):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


# ── Helpers ────────────────────────────────────────────────────────────────

# Hints for models that support a reasoning_effort parameter
REASONING_MODEL_HINTS = (
    "claude-sonnet-4", "claude-4", "claude-opus",
    "gpt-oss", "qwen3-think", "deepseek-r1", "deepseek-r2",
    "qwen3", "o1", "o3", "o4", "gpt-5",
)


def _model_supports_reasoning(model: str) -> bool:
    """Check if a model name hints at extended-thinking / reasoning support."""
    m = (model or "").lower()
    return any(h in m for h in REASONING_MODEL_HINTS)


def _flatten_system(system_blocks) -> str:
    """Flatten system blocks into a single string."""
    if system_blocks is None:
        return ""
    if isinstance(system_blocks, str):
        return system_blocks
    parts = []
    for b in system_blocks:
        if isinstance(b, dict):
            parts.append(b.get("text", ""))
        elif isinstance(b, CacheableSystemBlock):
            parts.append(b.text)
        else:
            parts.append(str(b))
    return "\n".join(parts)


def _empty_result(model: str) -> ChatResponse:
    """Return a blank ChatResponse for edge cases."""
    return ChatResponse(
        text="", model=model, stop_reason="end_turn",
    )


# ── Base ───────────────────────────────────────────────────────────────────

class BaseProvider:
    """Abstract LLM provider. Subclasses implement chat().

    Follows the llm_gatewayV2 BaseProvider pattern:
      - capabilities dict declares what the provider supports
      - chat() is the unified interface
      - chat_from_request() accepts a ChatRequest schema object
    """

    name: str = ""
    capabilities: dict[str, bool] = {
        "tools": False,
        "reasoning": False,
        "structured": False,
    }

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: Optional[str | list[CacheableSystemBlock]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        model: Optional[str] = None,
        tools: Optional[list[ToolDef]] = None,
        tool_choice: Optional[str | dict[str, Any]] = None,
        reasoning: Optional[str] = None,
        response_format: Optional[ResponseFormat | dict] = None,
        cache_system: bool = False,
    ) -> ChatResponse:
        raise NotImplementedError

    def chat_from_request(self, req: ChatRequest) -> ChatResponse:
        return self.chat(
            req.messages,
            system=req.system,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            model=req.model,
            tools=req.tools,
            tool_choice=req.tool_choice,
            reasoning=req.reasoning,
            cache_system=False,
        )


# ── Databricks Claude Provider ────────────────────────────────────────────

class DatabricksClaudeProvider(BaseProvider):
    """
    Claude Sonnet 4.6 via Databricks serving endpoints.
    Uses the OpenAI-compatible API provided by Databricks.

    Supports:
      - Extended context window (200k tokens)
      - Tool use (function calling)
      - Structured JSON output (json_schema / json_object)
      - Reasoning effort (extended thinking) when model supports it

    Retry logic (matching llm_gatewayV2 pattern):
      - If the endpoint rejects reasoning_effort → retry without it
      - If the endpoint rejects json_schema → downgrade to json_object and retry
    """

    name = "databricks_claude"
    capabilities = {
        "tools": True,
        "reasoning": True,
        "structured": True,
    }

    def __init__(self, api_key: str, model: str, base_url: str):
        super().__init__(api_key, model, base_url)
        self._client = OpenAI(
            api_key=api_key,
            base_url=f"{base_url}/serving-endpoints",
        )

    # ── message translation ──

    def _translate_tools(self, tools: list[ToolDef] | None) -> list[dict] | None:
        if not tools:
            return None
        out = []
        for t in tools:
            d = t.model_dump() if hasattr(t, "model_dump") else t
            out.append({
                "type": "function",
                "function": {
                    "name": d["name"],
                    "description": d.get("description", ""),
                    "parameters": d.get("input_schema") or {
                        "type": "object",
                        "properties": {},
                    },
                },
            })
        return out

    def _build_messages(
        self,
        messages: list[dict[str, Any]],
        system_text: str,
    ) -> list[dict]:
        out = []
        if system_text:
            out.append({"role": "system", "content": system_text})
        for m in messages:
            role = m.get("role")
            if role == "system":
                if not system_text:
                    out.append({"role": "system", "content": m.get("content", "")})
                continue
            if role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or m.get("id") or "",
                    "content": (
                        m.get("content", "")
                        if isinstance(m.get("content"), str)
                        else json.dumps(m.get("content"))
                    ),
                })
                continue
            if role == "assistant" and m.get("tool_calls"):
                tcs = []
                for tc in m["tool_calls"]:
                    tcs.append({
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments") or {}),
                        },
                    })
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": tcs,
                })
                continue
            out.append({"role": role, "content": m.get("content", "")})
        return out

    # ── response format & reasoning (matching gatewayV2 pattern) ──

    def _apply_response_format(
        self, kwargs: dict, response_format: ResponseFormat | dict | None,
    ) -> None:
        """Inject response_format into the API kwargs."""
        if not response_format:
            return
        rf = (
            response_format
            if isinstance(response_format, dict)
            else response_format.model_dump(by_alias=True)
        )
        if rf.get("type") == "json_schema" and rf.get("schema"):
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": rf.get("name", "out"),
                    "schema": rf["schema"],
                    "strict": bool(rf.get("strict", True)),
                },
            }
        elif rf.get("type") == "json_object":
            kwargs["response_format"] = {"type": "json_object"}

    def _apply_reasoning(
        self, kwargs: dict, reasoning: str | None, model: str,
    ) -> bool:
        """Set reasoning_effort if the model supports it. Returns True if applied."""
        if not reasoning or reasoning == "off":
            return False
        if not _model_supports_reasoning(model):
            return False
        kwargs["reasoning_effort"] = reasoning
        return True

    # ── main chat ──

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: Optional[str | list[CacheableSystemBlock]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        model: Optional[str] = None,
        tools: Optional[list[ToolDef]] = None,
        tool_choice: Optional[str | dict[str, Any]] = None,
        reasoning: Optional[str] = None,
        response_format: Optional[ResponseFormat | dict] = None,
        cache_system: bool = False,
    ) -> ChatResponse:
        # Note: cache_system is accepted for API compatibility with
        # llm_gatewayV2's BaseProvider signature. Databricks' OpenAI-compat
        # endpoint does not expose an explicit prompt-caching API, so the
        # parameter is a no-op here. If a future provider (e.g. native
        # Anthropic or Gemini) is added, implement caching logic in its
        # subclass — see GeminiProvider in llm_gatewayV2 for reference.
        m = model or self.model
        system_text = _flatten_system(system)
        api_messages = self._build_messages(messages, system_text)
        _t0 = time.perf_counter()

        kwargs: dict[str, Any] = {
            "model": m,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        translated_tools = self._translate_tools(tools)
        if translated_tools:
            kwargs["tools"] = translated_tools
            if tool_choice:
                kwargs["tool_choice"] = (
                    tool_choice
                    if isinstance(tool_choice, (str, dict))
                    else "auto"
                )

        self._apply_response_format(kwargs, response_format)
        reasoning_applied = self._apply_reasoning(kwargs, reasoning, m)

        # ── Call with retry logic (matching llm_gatewayV2) ──
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as first_err:
            retried = False
            # Retry 1: strip reasoning_effort if rejected
            if reasoning_applied and "reasoning_effort" in str(first_err):
                kwargs.pop("reasoning_effort", None)
                reasoning_applied = False
                retried = True
                log.info("Retrying without reasoning_effort")
            # Retry 2: downgrade json_schema → json_object if rejected
            rf = kwargs.get("response_format") or {}
            if not retried and isinstance(rf, dict) and rf.get("type") == "json_schema":
                kwargs["response_format"] = {"type": "json_object"}
                retried = True
                log.info("Retrying with json_object instead of json_schema")
            if retried:
                try:
                    response = self._client.chat.completions.create(**kwargs)
                except Exception as retry_err:
                    raise ProviderError(
                        f"{self.name}: {retry_err}",
                        retryable=False,
                    ) from retry_err
            else:
                raise ProviderError(
                    f"{self.name}: {first_err}",
                    retryable=True,
                ) from first_err

        choice = response.choices[0] if response.choices else None
        _elapsed_ms = int((time.perf_counter() - _t0) * 1000)
        if not choice:
            return ChatResponse(provider=self.name, text="", model=m, stop_reason="error", latency_ms=_elapsed_ms)

        msg = choice.message
        text = msg.content or ""

        tool_calls_out = []
        for tc in msg.tool_calls or []:
            fn = tc.function
            args_str = fn.arguments or "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except Exception:
                args = {"_raw": args_str}
            tool_calls_out.append(
                ToolCall(
                    id=tc.id or f"call_{uuid.uuid4().hex[:8]}",
                    name=fn.name or "",
                    arguments=args,
                )
            )

        usage = response.usage
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0

        stop = choice.finish_reason or "stop"
        stop_norm = (
            "tool_use" if tool_calls_out
            else ("max_tokens" if stop == "length" else "end_turn")
        )

        # Auto-parse JSON if text looks like JSON
        parsed = None
        if text and text.strip().startswith("{"):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                pass

        return ChatResponse(
            provider=self.name,
            text=text,
            tool_calls=tool_calls_out,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=stop_norm,
            model=m,
            latency_ms=_elapsed_ms,
            reasoning_applied=reasoning_applied,
            parsed=parsed,
        )


# ── Factory ────────────────────────────────────────────────────────────────

def build_provider() -> DatabricksClaudeProvider:
    """Build the Databricks Claude provider from environment variables."""
    # Load .env from MCP\Task (as specified in instructions)
    mcp_env = os.path.join(
        os.path.dirname(__file__), "..", "..", "MCP", "Task", ".env"
    )
    if os.path.exists(mcp_env):
        load_dotenv(mcp_env)

    # Also try local .env
    local_env = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(local_env):
        load_dotenv(local_env, override=True)

    host = os.getenv("DATABRICKS_HOST")
    model = os.getenv("DATABRICKS_MODEL") or os.getenv("DATABRICKS_ENDPOINT")
    api_key = os.getenv("DATABRICKS_API_KEY")

    if not all([host, model, api_key]):
        raise RuntimeError(
            "Missing env vars. Ensure .env has DATABRICKS_HOST, "
            "DATABRICKS_MODEL (or DATABRICKS_ENDPOINT), DATABRICKS_API_KEY."
        )

    return DatabricksClaudeProvider(
        api_key=api_key,
        model=model,
        base_url=host,
    )
