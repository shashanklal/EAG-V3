"""Provider adapters for llm_gatewayV3 — Databricks Model Serving edition.

All 7 models are served via Azure Databricks Model Serving endpoints. The
gateway uses a single base URL (the Databricks workspace) and a PAT token for
auth. Each "provider" is a logical name mapped to a specific deployed model.

Each provider implements:
  async chat(messages, *, max_tokens, temperature, model, tools, tool_choice,
             reasoning, response_format, system_blocks) -> dict

The returned dict is normalised:
  {
    "text": str,
    "tool_calls": [ {"id","name","arguments"} ],
    "input_tokens": int, "output_tokens": int,
    "cache_creation_input_tokens": int, "cache_read_input_tokens": int,
    "stop_reason": "tool_use"|"end_turn"|"max_tokens",
    "model": str,
    "tool_call_dialect": "native"|"prompted_fallback"|"none",
    "reasoning_applied": bool,
  }

`messages` may include role="tool" entries with `tool_call_id` and `content`;
each adapter translates them to its native shape.
"""
from __future__ import annotations
import os, json, uuid, hashlib, re
from typing import AsyncIterator, Optional, Any
import httpx


# ────────────────────────────────────────────────────────────────────────────
# Databricks workspace configuration
# ────────────────────────────────────────────────────────────────────────────
DATABRICKS_HOST = os.getenv(
    "DATABRICKS_HOST",
    "https://adb-2177732704131972.12.azuredatabricks.net",
)
DATABRICKS_TOKEN = os.getenv("DATABRICKS_API_KEY", "")


class ProviderError(Exception):
    def __init__(self, msg, status=None, retryable=True):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _flatten_system(system_blocks) -> tuple[str, list[dict], bool]:
    """Returns (joined_text, raw_blocks, has_cache_marker)."""
    if system_blocks is None:
        return "", [], False
    if isinstance(system_blocks, str):
        return system_blocks, [{"text": system_blocks, "cache": False}], False
    blocks = []
    has_cache = False
    parts = []
    for b in system_blocks:
        if isinstance(b, dict):
            t = b.get("text", "")
            c = bool(b.get("cache", False))
        else:
            t = getattr(b, "text", "")
            c = bool(getattr(b, "cache", False))
        blocks.append({"text": t, "cache": c})
        parts.append(t)
        if c:
            has_cache = True
    return "\n".join(parts), blocks, has_cache


def _empty_result(model: str) -> dict:
    return {
        "text": "", "tool_calls": [],
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "stop_reason": "end_turn", "model": model,
        "tool_call_dialect": "none", "reasoning_applied": False,
    }


# ────────────────────────────────────────────────────────────────────────────
# Base
# ────────────────────────────────────────────────────────────────────────────

class BaseProvider:
    name: str = ""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False) -> dict:
        raise NotImplementedError

    async def stream(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                     tools=None, tool_choice=None, reasoning=None, response_format=None,
                     system_blocks=None, cache_system=False) -> AsyncIterator[str]:
        # Default fallback: do non-streaming and yield once.
        result = await self.chat(messages, max_tokens=max_tokens, temperature=temperature,
                                 model=model, tools=tools, tool_choice=tool_choice,
                                 reasoning=reasoning, response_format=response_format,
                                 system_blocks=system_blocks, cache_system=cache_system)
        if result["text"]:
            yield result["text"]


# ────────────────────────────────────────────────────────────────────────────
# Databricks Model Serving Provider (OpenAI-compatible)
# ────────────────────────────────────────────────────────────────────────────

REASONING_MODEL_HINTS = ("gpt-oss", "qwen3", "claude-sonnet-4", "claude-sonnet-4-6")


def _model_supports_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in REASONING_MODEL_HINTS)


class DatabricksProvider(BaseProvider):
    """OpenAI-compatible adapter for Databricks Model Serving endpoints.

    Databricks exposes /serving-endpoints/<endpoint>/invocations with an
    OpenAI-compatible chat completions interface. All 7 models use the same
    base URL and token — they differ only by endpoint/model name.
    """
    capabilities = {
        "tools": True, "caching": False, "reasoning": False,
        "structured": True, "parallel_tools": True,
    }

    def __init__(self, api_key: str, model: str, base_url: str, name: str = "databricks"):
        super().__init__(api_key, model, base_url)
        self.name = name

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _translate_tools(self, tools):
        out = []
        for t in tools or []:
            d = t if isinstance(t, dict) else t.model_dump()
            out.append({
                "type": "function",
                "function": {
                    "name": d["name"],
                    "description": d.get("description", ""),
                    "parameters": d.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    def _translate_messages(self, messages, system_text):
        """Translate canonical messages (incl role=tool) to OpenAI shape."""
        out = []
        if system_text:
            out.append({"role": "system", "content": system_text})
        for m in messages:
            r = m.get("role")
            if r == "system":
                if not system_text:
                    out.append({"role": "system", "content": m.get("content", "")})
                continue
            if r == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or m.get("id") or "",
                    "content": m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content")),
                })
                continue
            if r == "assistant" and m.get("tool_calls"):
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
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
                continue
            out.append({"role": r, "content": m.get("content", "")})
        return out

    def _apply_response_format(self, body, response_format):
        if not response_format:
            return
        rf = response_format if isinstance(response_format, dict) else response_format.model_dump(by_alias=True)
        if rf.get("type") == "json_schema" and rf.get("schema"):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": rf.get("name", "out"),
                    "schema": rf["schema"],
                    "strict": bool(rf.get("strict", True)),
                },
            }
        elif rf.get("type") == "json_object":
            body["response_format"] = {"type": "json_object"}

    def _apply_reasoning(self, body, reasoning, model):
        if not reasoning or reasoning == "off":
            return False
        if not _model_supports_reasoning(model):
            return False
        body["reasoning_effort"] = reasoning
        return True

    def _endpoint_url(self, model: str) -> str:
        """Construct the Databricks serving endpoint URL.
        Databricks Model Serving uses: <host>/serving-endpoints/<endpoint>/invocations
        """
        return f"{self.base_url}/serving-endpoints/{model}/invocations"

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, _, _ = _flatten_system(system_blocks)
        body = {
            "messages": self._translate_messages(messages, system_text),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice if isinstance(tool_choice, (str, dict)) else "auto"
        self._apply_response_format(body, response_format)
        reasoning_applied = self._apply_reasoning(body, reasoning, m)

        url = self._endpoint_url(m)
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(url, headers=self._headers(), json=body)
            if r.status_code != 200:
                txt = r.text
                if reasoning_applied and "reasoning_effort" in txt:
                    body.pop("reasoning_effort", None)
                    reasoning_applied = False
                    r = await c.post(url, headers=self._headers(), json=body)
                if r.status_code != 200 and "json_schema" in (body.get("response_format") or {}).get("type", ""):
                    body["response_format"] = {"type": "json_object"}
                    r = await c.post(url, headers=self._headers(), json=body)
                if r.status_code != 200:
                    raise ProviderError(
                        f"{self.name} HTTP {r.status_code}: {r.text[:300]}",
                        status=r.status_code,
                        retryable=(r.status_code not in (400, 401)),
                    )
            d = r.json()
            choice = (d.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            tool_calls_out = []
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {"_raw": args_str}
                tool_calls_out.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn.get("name", ""),
                    "arguments": args,
                })
            usage = d.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            cache_read = details.get("cached_tokens", 0) or 0
            stop = choice.get("finish_reason") or "stop"
            stop_norm = "tool_use" if tool_calls_out else (
                "max_tokens" if stop == "length" else "end_turn"
            )
            return {
                "text": text or "",
                "tool_calls": tool_calls_out,
                "input_tokens": usage.get("prompt_tokens", 0) or 0,
                "output_tokens": usage.get("completion_tokens", 0) or 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
                "stop_reason": stop_norm,
                "model": m,
                "tool_call_dialect": "native",
                "reasoning_applied": reasoning_applied,
            }

    async def stream(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                     tools=None, tool_choice=None, reasoning=None, response_format=None,
                     system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, _, _ = _flatten_system(system_blocks)
        body = {
            "messages": self._translate_messages(messages, system_text),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice if isinstance(tool_choice, (str, dict)) else "auto"
        self._apply_response_format(body, response_format)
        self._apply_reasoning(body, reasoning, m)

        url = self._endpoint_url(m)
        async with httpx.AsyncClient(timeout=180) as c:
            async with c.stream("POST", url, headers=self._headers(), json=body) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode("utf-8", "ignore")[:300]
                    raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", status=r.status_code)
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        return
                    try:
                        d = json.loads(payload)
                        delta = d["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                        if delta.get("tool_calls"):
                            yield "[[TOOL_CALL_DELTA]] " + json.dumps(delta["tool_calls"])
                    except Exception:
                        continue


# ────────────────────────────────────────────────────────────────────────────
# Concrete provider instances (one per Databricks endpoint)
# ────────────────────────────────────────────────────────────────────────────
# Mapping of logical provider names to Databricks endpoint model names:
#   llama31_8b  → databricks-meta-llama-3-1-8b-instruct
#   gemma3_12b  → databricks-gemma-3-12b
#   llama4_mav  → databricks-llama-4-maverick
#   qwen3_80b   → databricks-qwen3-next-80b-a3b-instruct
#   claude_s4   → databricks-claude-sonnet-4
#   gpt_oss     → databricks-gpt-oss-120b
#   claude_s46  → databricks-claude-sonnet-4-6

PROVIDER_MODELS = {
    "llama31_8b": "databricks-meta-llama-3-1-8b-instruct",
    "gemma3_12b": "databricks-gemma-3-12b",
    "llama4_mav": "databricks-llama-4-maverick",
    "qwen3_80b":  "databricks-qwen3-next-80b-a3b-instruct",
    "claude_s4":  "databricks-claude-sonnet-4",
    "gpt_oss":    "databricks-gpt-oss-120b",
    "claude_s46": "databricks-claude-sonnet-4-6",
}

# Per-provider capability overrides
PROVIDER_CAPABILITIES = {
    "llama31_8b": {"tools": True, "caching": False, "reasoning": False, "structured": True, "parallel_tools": False},
    "gemma3_12b": {"tools": True, "caching": False, "reasoning": False, "structured": True, "parallel_tools": False},
    "llama4_mav": {"tools": True, "caching": False, "reasoning": False, "structured": True, "parallel_tools": True},
    "qwen3_80b":  {"tools": True, "caching": False, "reasoning": True, "structured": True, "parallel_tools": True},
    "claude_s4":  {"tools": True, "caching": False, "reasoning": True, "structured": True, "parallel_tools": True},
    "gpt_oss":    {"tools": True, "caching": False, "reasoning": True, "structured": True, "parallel_tools": True},
    "claude_s46": {"tools": True, "caching": False, "reasoning": True, "structured": True, "parallel_tools": True},
}


# ────────────────────────────────────────────────────────────────────────────
# Per-model capability resolution
# ────────────────────────────────────────────────────────────────────────────

def model_capabilities(provider_name: str, model: str, default_caps: dict) -> dict:
    caps = dict(default_caps)
    if provider_name in PROVIDER_CAPABILITIES:
        caps.update(PROVIDER_CAPABILITIES[provider_name])
    return caps


def build_providers(cache_store=None):
    """Worker pool — 7 Databricks Model Serving endpoints.

    All endpoints share the same Databricks workspace and PAT token.
    The `cache_store` parameter is accepted for interface compatibility but unused
    (Databricks does not have Gemini-style explicit prompt caching).
    """
    token = DATABRICKS_TOKEN
    base = DATABRICKS_HOST
    if not token:
        token = os.getenv("DATABRICKS_TOKEN", "")
    if not base:
        base = os.getenv("DATABRICKS_HOST", "")

    out = {}
    for name, model in PROVIDER_MODELS.items():
        env_model = os.getenv(f"DATABRICKS_MODEL_{name.upper()}", model)
        prov = DatabricksProvider(api_key=token, model=env_model, base_url=base, name=name)
        prov.capabilities = PROVIDER_CAPABILITIES.get(name, DatabricksProvider.capabilities)
        out[name] = prov
    return out


# Router pool — uses the small/fast model (llama31_8b) for routing decisions.
ROUTER_DEFAULTS = {
    "llama31_8b": "databricks-meta-llama-3-1-8b-instruct",
    "gemma3_12b": "databricks-gemma-3-12b",
}


def build_router_providers():
    """Router pool — small/fast Databricks endpoints used for routing decisions.
    Uses llama31_8b and gemma3_12b as routers (small, fast, low cost).
    """
    token = DATABRICKS_TOKEN
    base = DATABRICKS_HOST
    if not token:
        token = os.getenv("DATABRICKS_TOKEN", "")
    if not base:
        base = os.getenv("DATABRICKS_HOST", "")

    out = {}
    for name, model in ROUTER_DEFAULTS.items():
        env_model = os.getenv(f"DATABRICKS_ROUTER_MODEL_{name.upper()}", model)
        prov = DatabricksProvider(api_key=token, model=env_model, base_url=base, name=name)
        prov.capabilities = PROVIDER_CAPABILITIES.get(name, DatabricksProvider.capabilities)
        out[name] = prov
    return out
