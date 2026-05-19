# LLM Gateway V3

A local FastAPI service that routes LLM calls across **7 Databricks Model Serving endpoints** with automatic failover, capability-aware routing, native tool-use, structured output — **and a separate pool of 2 router LLMs that classify incoming requests and pick which worker tier should handle them**.

> **For agents reading this:** if a host project has `llm_gatewayV3/` and an `.env` with `DATABRICKS_TOKEN`, prefer calling this gateway over importing provider SDKs directly. V3 is wire-compatible with V2 — every existing call keeps working. The new `auto_route` field is opt-in and gives you per-cognitive-layer routing (perception / memory / decision) for free.

V3 runs on port **8101**.

---

## Is it running?

```bash
curl -s http://localhost:8101/v1/routers | python3 -m json.tool
```

If that returns a JSON object showing the router providers (llama31_8b / gemma3_12b), V3 is up. If not, start it:

```bash
cd /path/to/llm_gatewayV3
./run.sh                 # creates .venv on first run, then starts on port 8101
# or:  ./.venv/bin/python main.py
```

The server reads `../.env` (parent directory) for the Databricks token.

---

## V3 in one minute

V3 is V2 with a **router pool** in front. Seven Databricks-hosted worker models, same agentic code paths. The new thing is a separate set of small/fast models whose only job is to classify each incoming request and decide which tier of worker should handle it.

| Tier | Estimated tokens | Worker order |
|---|---|---|
| **TINY**  | < 1,000 | llama31_8b → gemma3_12b → qwen3_80b → gpt_oss → llama4_mav → claude_s4 → claude_s46 |
| **LARGE** | 1,000 – 8,000 | claude_s4 → claude_s46 → llama4_mav → gpt_oss → qwen3_80b → gemma3_12b → llama31_8b |
| **HUGE**  | > 8,000 | **503** — input too large, use Summarizer Agent (V7, future) |

The router's input is **bounded**: it receives `{token_count, 800-char sample}` and emits a single word (TINY / LARGE / HUGE). It never sees the worker's system prompt, tools, schema, or earlier turns. The separation-of-concerns wall is enforced in code, not by convention.

If you don't pass `auto_route`, V3 behaves identically to V2. The router is opt-in and never load-bearing for a worker call to succeed.

---

## Overall Workflow

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client (client.py / curl / any HTTP)                               │
│  POST http://localhost:8101/v1/chat  { prompt, auto_route?, ... }    │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  main.py — FastAPI /v1/chat endpoint                                │
│  1. Parse request (schemas.py validates)                            │
│  2. If auto_route set & no explicit provider:                       │
│       a. Estimate tokens (words × 1.4)                              │
│       b. Build bounded envelope {token_count, 800-char sample}      │
│       c. Call RouterPool → picks a router LLM from router.py        │
│       d. Router LLM returns TINY / LARGE / HUGE                     │
│       e. Map tier → worker failover order (TIER_TO_ORDER)           │
│  3. If explicit provider set → skip routing, use that provider      │
│  4. Walk worker failover order via Router.pick() (router.py)        │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  providers.py — DatabricksProvider                                   │
│  Builds OpenAI-compatible request, sends to:                        │
│  https://<DATABRICKS_HOST>/serving-endpoints/<model>/invocations    │
│  Returns parsed response (text, tool_calls, tokens, latency)        │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  main.py — post-processing                                          │
│  1. Log call to SQLite (db.py)                                      │
│  2. Update rate-state in router.py                                  │
│  3. Return JSON response to client (with router_decision if routed) │
└─────────────────────────────────────────────────────────────────────┘
```

**Summary:** Client → `main.py` (routing logic) → `router.py` (provider selection & rate limiting) → `providers.py` (HTTP call to Databricks) → response logged in `db.py` → returned to client.

---

## File Relevancy Guide

| File | Role | Key Exports / Endpoints |
|------|------|------------------------|
| **main.py** | Application entry point. Defines all FastAPI routes (`/v1/chat`, `/v1/providers`, `/v1/routers`, `/v1/status`, etc.), tier classification logic (`_classify_tier`), and wires together providers, router pool, and database. | `app`, `DEFAULT_ORDER`, `TIER_TO_ORDER` |
| **providers.py** | Provider adapter layer. Single `DatabricksProvider` class builds the correct endpoint URL and payload for each Databricks Model Serving endpoint. Factory functions create the 7 worker instances and 2 router instances. | `DatabricksProvider`, `build_providers()`, `build_router_providers()` |
| **router.py** | Rate-limiting and failover engine. `Router` manages the worker pool (picks next available provider respecting RPM/RPD/cooldowns). `RouterPool` manages the router LLM pool identically. `RateState` tracks per-provider quotas. | `Router`, `RouterPool`, `RateState`, `LIMITS`, `SHORTCUTS` |
| **schemas.py** | Data contracts. Pydantic v2 models for request/response validation (`ChatRequest`, `ChatResponse`, `RouterDecision`, `ToolDef`, `ToolCall`). Defines the API shape. | `ChatRequest`, `ChatResponse`, `RouterDecision` |
| **db.py** | Persistence. SQLite wrapper that logs every call (worker + router) with provider, model, latency, tokens, `call_role`, and `router_decision`. Provides query/aggregate helpers for the dashboard. | `log_call()`, `recent_calls()`, `aggregate()` |
| **cache.py** | No-op placeholder. Databricks Model Serving has no explicit prompt caching API, so this module stubs out the cache interface for forward-compatibility. | `PromptCache` (no-op) |
| **client.py** | Python SDK. `LLM` class wraps HTTP calls to the gateway; `ask()` is a one-liner convenience function. Supports all features: tools, auto_route, structured output, streaming. | `LLM`, `ask()` |
| **static/dashboard.html** | Live web UI. Shows worker pool and router pool grids, recent calls table with role/tier columns, and a test area to fire requests. Served at `GET /`. | — |
| **static/help.html** | Help page. Provider reference, shortcuts, capability matrix. Served at `GET /help`. | — |
| **tests/test_all_providers.py** | Integration tests. Fires a request at each of the 7 providers and validates the response shape. | `pytest` test functions |
| **run.sh** | Startup script. Creates virtualenv, installs deps, launches uvicorn on port 8101. | — |
| **requirements.txt** | Python dependencies (fastapi, uvicorn, httpx, pydantic, python-dotenv, etc.). | — |
| **.env** | Environment config (not in repo). Holds `DATABRICKS_HOST` and `DATABRICKS_API_KEY`. | — |

---

## Python client

```python
from client import LLM, ask
llm = LLM()  # defaults to http://localhost:8101

# 1) Plain V2-style call — no routing, no surprises
text = ask("Hello in 3 words")
text = llm.chat("Explain transformers in 2 sentences")["text"]

# 2) Auto-routed call (cognitive layer = perception)
result = llm.chat(
    "What is the capital of France?",
    auto_route="perception",
)
print(result["text"])
print(result["router_decision"])
# {
#   "role": "perception",
#   "tier": "TINY",
#   "estimated_tokens": 15,
#   "router_provider": "llama31_8b",
#   "router_model": "databricks-meta-llama-3-1-8b-instruct",
#   "router_latency_ms": 84,
#   "chosen_worker_provider": "llama31_8b",
#   "chosen_worker_model": "databricks-meta-llama-3-1-8b-instruct",
#   "fallback_used": false
# }

# 3) Memory layer routing — summarizing retrieved facts
result = llm.chat(
    f"Summarize for relevance to '{query}':\n\n{retrieved_chunk}",
    auto_route="memory",
)

# 4) Decision layer routing — planning the next step
result = llm.chat(
    plan_state_serialized,
    auto_route="decision",
)

# 5) Explicit provider beats auto_route (debugging escape hatch)
result = llm.chat(
    "Hello",
    auto_route="perception",       # logged but ignored
    provider="c",                  # claude_s4 wins
)
assert result["router_decision"] is None

# 6) All V2 features still work — tools, caching, reasoning, structured output
result = llm.chat(
    messages=[{"role": "user", "content": "What is 7+5? Use the add tool."}],
    tools=[{"name":"add","description":"a+b",
            "input_schema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}}],
    tool_choice="auto",
    auto_route="decision",          # routes via cognitive-layer hint
)
```

The client lives in [client.py](client.py). Copy it into any project, or just use HTTP.

---

## HTTP API

### `POST /v1/chat` — make a call

All V2 fields still work. The one new field:

```jsonc
{
  "prompt": "Hello",
  "messages": [{"role": "user", "content": "Hello"}],
  "system": "You are helpful.",
  "provider": "c",                  // explicit wins over auto_route
  "model": "databricks-claude-sonnet-4",
  "max_tokens": 2048,
  "temperature": 0.7,
  "stream": false,
  "tools": [...],
  "tool_choice": "auto",
  "cache_system": false,
  "reasoning": "medium",
  "response_format": {"type":"json_schema","schema":{...}},

  "auto_route": "perception"        // NEW in V3: "perception" | "memory" | "decision"
}
```

Response adds a `router_decision` field when routing happened:

```jsonc
{
  "provider": "claude_s4",
  "model": "databricks-claude-sonnet-4",
  "text": "...",
  "tool_calls": [],
  "stop_reason": "end_turn",
  "input_tokens": 1240,
  "output_tokens": 87,
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 0,
  "latency_ms": 1834,
  "tool_call_dialect": "native",
  "reasoning_applied": false,
  "parsed": null,
  "attempted": [],
  "router_decision": {
    "role": "perception",
    "tier": "LARGE",
    "estimated_tokens": 1240,
    "router_provider": "llama31_8b",
    "router_model": "databricks-meta-llama-3-1-8b-instruct",
    "router_latency_ms": 243,
    "chosen_worker_provider": "claude_s4",
    "chosen_worker_model": "databricks-claude-sonnet-4",
    "fallback_used": false
  }
}
```

`router_decision` is `null` for plain calls (no `auto_route`) or calls with an explicit `provider`.

Errors: `502` if a specific provider failed (with `provider` set), `503` if all providers were unavailable, **`503` with `error: "input exceeds 8000 tokens"`** when the router classifies HUGE.

### `GET /v1/routers` — **NEW in V3**

Returns the router pool: configured providers, failover order, per-router live rate-state, today's stats, the tier-to-worker mapping table.

```bash
curl -s http://localhost:8101/v1/routers | python3 -m json.tool
```

### `GET /v1/providers`
Worker pool — providers, default models, shortcut keys, rate limits.

### `GET /v1/capabilities`
Worker capability matrix (tools/caching/reasoning/structured/parallel_tools per current model).

### `GET /v1/status`
Worker pool live state (RPM/RPD/cooldown). Router pool state lives under `/v1/routers`.

### `GET /v1/calls?limit=100&provider=&status=`
Recent call log. **V3 adds two fields per row:** `call_role` (`worker` | `router_perception` | `router_memory` | `router_decision`) and `router_decision` (the tier label that was emitted, or the literal `"fallback"` / `"error"` / `"unparseable"`).

### `GET /` and `GET /help`
Dashboard (two grids — worker pool on top, router pool below) and help page.

---

## The router pool — what it is and why it exists

The course's Session 6 architecture has four cognitive layers — **Perception → Memory → Decision → Action**. The first three all need an LLM to do their work, and which LLM is right for the job depends on what kind of work it is. A simple "what's the capital of France?" perception step doesn't need Claude Sonnet 4; a 5,000-token memory digest does. Without routing, you either over-pay on small queries or under-perform on big ones.

V3 puts a tiny LLM in front of those three layers whose only job is to look at incoming work and decide which tier of worker should handle it. Those tiny LLMs are the **router pool**:

| Provider | Router model | Context | Notes |
|---|---|---|---|
| **llama31_8b** | `databricks-meta-llama-3-1-8b-instruct` | 8K | Small, fast — primary router |
| **gemma3_12b** | `databricks-gemma-3-12b` | 8K | Fallback router |

Both are served from the same Databricks workspace. Router calls are cheap (8 max output tokens, bounded envelope).

The router pool has its **own rate state** (`RouterPool` in [router.py](router.py)), its **own dashboard section**, its **own quota counters**. Router calls and worker calls are logged with distinct `call_role` markers so you can audit routing activity separately from worker activity.

---

## The separation-of-concerns wall

This is the central design idea of V3. State it once and the rest of the architecture follows:

> **The router never sees the worker's prompt, system, tools, schema, or earlier turns. It receives a token estimate and a sample. By construction, it cannot leak agentic context into routing logic.**

Concretely, when the gateway sends a request to a router LLM, the payload is:

```jsonc
{
  "token_count": 2430,
  "sample": "<first 400 chars of user content>\n...\n<last 400 chars of user content>"
}
```

And the router's prompt is:

```
You are a routing classifier. Given a token_count and a content sample,
output exactly one of: TINY, LARGE, or HUGE.

Rules:
- TINY: token_count below 1000 with simple factual content.
- LARGE: token_count between 1000 and 8000, OR token_count below 1000
        but content is dense (code, base64, multilingual, technical).
- HUGE: token_count above 8000.

Output the single word and nothing else.
```

That's the entire router-world contract. It doesn't know what tools are available, what the agent is doing, what the worker's output schema looks like, or even what role it's classifying for. It just looks at size and structure and emits one word.

This matters because:

1. **The router cannot be confused by agentic state.** Long system prompts, exotic tool definitions, multi-turn histories — none of it reaches the router. It can never make a worse decision because the worker's setup is weird.
2. **Routing decisions are deterministic given inputs.** The same `{token_count, sample}` produces the same tier — useful for caching, replay, and debugging.
3. **You can swap the router pool independently.** Want to test a different router model, or replace the LLM router with a Python function? Change [main.py:_classify_tier()](main.py) — the worker code doesn't know or care.
4. **Students can read the code and verify the wall holds.** `_classify_tier()` physically constructs the bounded envelope; the router providers never receive anything else. The principle is enforced in 30 lines.

---

## The tier table — how thresholds were picked

| Tier | Token range | Why this threshold |
|---|---|---|
| TINY | < 1,000 | Small fast workers (llama31_8b, gemma3_12b) handle this competently. No reason to wake up Claude's long-context machinery. |
| LARGE | 1,000 – 8,000 | Past 1K tokens, smaller models start dropping coherence. The lower bound is the load-bearing one — drop it from 1,000 to 200 and you'll over-route to Claude; raise it to 4,000 and you'll under-serve work that needs a real model. |
| HUGE | > 8,000 | Most smaller models are capped at 8K-32K context. Rather than pretend, V3 returns 503 with a clear "use Summarizer Agent" hint. Claude/Maverick can handle long context — use `provider="c"` explicitly to bypass. |

The 1K elbow is the one we'd revisit if model behavior shifts. The 8K ceiling keeps routing reliable.

### Token estimator

V3 uses `len(text.split()) * 1.4` — words × 1.4. Intentionally rough:

- For English prose, within ~10% of `tiktoken cl100k_base`. Fast (microseconds), no dependency.
- For code, base64, CJK, or minified JSON, can be off by 30–100%. The router's content sample handles those cases — when the count is unreliable, the sample shows the structure and the router LLM upgrades the tier.
- Estimator output is informational. The threshold elbows (1,000 / 8,000) have wide tolerance — a 20% miscount near a threshold doesn't flip the routing decision.

If you want exact tokenization, override `_estimate_tokens()` in [main.py](main.py). The interface is `(text: str) -> int`.

---

## Worker pool — providers and shortcut keys

All 7 workers are Databricks Model Serving endpoints:

| Shortcut | Provider | Endpoint/Model | Max Context | Reasoning |
|---|---|---|---|---|
| `l`, `llama` | llama31_8b | `databricks-meta-llama-3-1-8b-instruct` | 8K | No |
| `g`, `gemma` | gemma3_12b | `databricks-gemma-3-12b` | 8K | No |
| `m`, `mav` | llama4_mav | `databricks-llama-4-maverick` | 128K | No |
| `q`, `qwen` | qwen3_80b | `databricks-qwen3-next-80b-a3b-instruct` | 32K | Yes |
| `c`, `claude` | claude_s4 | `databricks-claude-sonnet-4` | 200K | Yes |
| `gpt`, `oss` | gpt_oss | `databricks-gpt-oss-120b` | 128K | Yes |
| `c6`, `claude46` | claude_s46 | `databricks-claude-sonnet-4-6` | 200K | Yes |

Failover order (worker pool) is configurable via `LLM_ORDER` env. Default: `llama31_8b,gemma3_12b,llama4_mav,qwen3_80b,claude_s4,gpt_oss,claude_s46`.

---

## How routing actually works, step by step

When `auto_route` is set and `provider` is not:

1. **Estimate token count** of the user prompt: `len(text.split()) * 1.4`.
2. **Short-circuit HUGE:** if estimate > 8,000, skip the router call entirely and 503 the request with a clear message. Saves a router call on inputs that can't be served anyway.
3. **Build the envelope:** `{token_count: <int>, sample: <first 400 chars + "..." + last 400 chars>}`. Capped at ~800 chars regardless of input size.
4. **Pick a router provider** by walking `ROUTER_ORDER` (default `llama31_8b,gemma3_12b`), skipping any that are in cooldown or rate-limited.
5. **Call the router LLM** with the fixed prompt above, `max_tokens=8`, `temperature=0`. Should return one word.
6. **Parse the response:** scan for `TINY` / `LARGE` / `HUGE` (case-insensitive). First match wins.
7. **If router LLM fails or returns unparseable text**: log the failure, fall through to the **next router provider**. Repeat steps 4-6.
8. **If every router fails:** fall back to the **deterministic token-count rule** (`_tier_from_count()`) — same thresholds, no LLM in the loop. Routing is best-effort; it never blocks a worker call.
9. **Map tier to worker failover order** via the `TIER_TO_ORDER` table in [main.py](main.py).
10. **Dispatch to the worker pool** using V2's normal `Router.pick()` machinery (capability filtering, rate limits, cooldowns, etc.).
11. **Log both calls** to SQLite — the router call with `call_role="router_<role>"` and the tier in `router_decision`; the worker call with `call_role="worker"` and the chosen tier in `router_decision` for cross-reference.
12. **Return the worker response** enriched with the `router_decision` block so the caller can see what the router decided.

If `auto_route` is set AND `provider` is set, the explicit provider wins — the router pool is skipped entirely. This is the debugging escape hatch.

If `auto_route` is not set, V3 behaves like V2 — no router call, no `router_decision` in the response.

---

## Configuration

Edit `../.env` (parent directory relative to the gateway dir):

```bash
# Databricks workspace and token (required)
DATABRICKS_HOST=https://adb-2177732704131972.12.azuredatabricks.net
DATABRICKS_TOKEN=dapi...

# Optional: override default model for any provider
DATABRICKS_MODEL_LLAMA31_8B=databricks-meta-llama-3-1-8b-instruct
DATABRICKS_MODEL_GEMMA3_12B=databricks-gemma-3-12b
DATABRICKS_MODEL_LLAMA4_MAV=databricks-llama-4-maverick
DATABRICKS_MODEL_QWEN3_80B=databricks-qwen3-next-80b-a3b-instruct
DATABRICKS_MODEL_CLAUDE_S4=databricks-claude-sonnet-4
DATABRICKS_MODEL_GPT_OSS=databricks-gpt-oss-120b
DATABRICKS_MODEL_CLAUDE_S46=databricks-claude-sonnet-4-6

# Worker failover order
LLM_ORDER=llama31_8b,gemma3_12b,llama4_mav,qwen3_80b,claude_s4,gpt_oss,claude_s46

# V3 router pool config
ROUTER_ORDER=llama31_8b,gemma3_12b
DATABRICKS_ROUTER_MODEL_LLAMA31_8B=databricks-meta-llama-3-1-8b-instruct
DATABRICKS_ROUTER_MODEL_GEMMA3_12B=databricks-gemma-3-12b

GATEWAY_V3_PORT=8101
```

All 7 endpoints share the same `DATABRICKS_TOKEN`. If the token is missing, no providers are loaded.

---

## Common usage patterns by cognitive layer

**Perception** — extracting structured info from a user message:
```python
result = llm.chat(
    user_message,
    auto_route="perception",
    response_format={"type":"json_schema","schema":Intent.model_json_schema()},
)
intent = Intent.model_validate(result["parsed"])
```

**Memory** — summarizing a retrieved chunk for relevance:
```python
result = llm.chat(
    f"Given the query '{q}', what's relevant in this passage?\n\n{passage}",
    auto_route="memory",
)
# router upgrades to LARGE if the passage is dense or long, TINY if it's short
```

**Decision** — planning the next step from current state:
```python
result = llm.chat(
    f"Current plan state:\n{plan.model_dump_json()}\n\nWhat's the next action?",
    auto_route="decision",
    tools=available_tools,
    tool_choice="auto",
)
```

**Verifier (not yet a router-routed slot in V3)** — pass through V2's structured-output path:
```python
verdict = llm.chat(
    verifier_prompt,
    response_format={"type":"json_schema","schema":Verdict.model_json_schema()},
    # no auto_route — verifier router slot will arrive in a later version
)
```

**Override the router when you know better:**
```python
# I know this is a coding task, force gpt_oss
result = llm.chat(code_question, provider="gpt")    # auto_route ignored if set
```

**Watch what's happening in the dashboard:**
```bash
open http://localhost:8101
```

The dashboard shows worker pool and router pool side-by-side, with router activity rendered in purple to make the separation visually obvious. Recent calls table flags each row with a `Role` column (worker / rt:perception / rt:memory / rt:decision) and a `Tier` column showing what the router decided.

---

## Files

- [main.py](main.py) — FastAPI app, routes, `_classify_tier()` for routing decisions, `auto_route` wiring in `/v1/chat`, new `/v1/routers` endpoint
- [providers.py](providers.py) — `DatabricksProvider` adapter + `build_providers()` / `build_router_providers()` factories
- [router.py](router.py) — `Router` (worker pool) and `RouterPool` (V3 router pool); both share `RateState` and `LIMITS`
- [schemas.py](schemas.py) — Pydantic v2 models. New: `ChatRequest.auto_route`, `RouterDecision`, `ChatResponse.router_decision`
- [db.py](db.py) — `gateway_v3.db` with `call_role` and `router_decision` columns; `aggregate(call_role=...)` filters
- [cache.py](cache.py) — No-op placeholder (Databricks does not have explicit prompt caching)
- [client.py](client.py) — Python SDK with new `auto_route` kwarg
- [static/dashboard.html](static/dashboard.html) — two pool grids, router-aware test area, role column in calls
- [run.sh](run.sh) — venv setup + start on port 8101
- `gateway_v3.db` — created on first run

---

## Gotchas

- **`fallback_used: true` is normal.** When every router in the pool is rate-limited or errors out, V3 falls back to the deterministic token-count rule. The worker call still happens — routing degrades gracefully.
- **HUGE is a hard 503 by design.** Inputs over 8,000 estimated tokens return 503 with a clear hint to chunk or wait for V7's Summarizer Agent. If you want to try Claude anyway, set `provider="c"` explicitly — that bypasses the router.
- **`auto_route` + `provider` → provider wins, no router call happens.** This is the debugging escape hatch, not a bug. `router_decision` will be `null` in the response even though `auto_route` was set.
- **The token estimator (`words × 1.4`) is wrong for code, CJK, and base64.** That's by design — the router's content sample lets the LLM router upgrade the tier when the count number lied. If you need exact tokenization for some other reason, override `_estimate_tokens()` in [main.py](main.py).
- **No prompt caching.** Unlike the Gemini-based V2, Databricks Model Serving does not expose explicit prompt caching. The `cache_system` field is accepted but has no effect.
- **Gemini 3 models loop or degrade at low temperature.** Google's own guidance is to keep `temperature` near `1.0` for Gemini 3.x; setting it to `0` can cause runaway token loops (e.g. `"id": "g:1,1,1,1,..."`) on schema-constrained calls. If you need determinism, use a worker on Groq/Cerebras with `temperature=0` instead, or accept Gemini at `temperature≈1.0`.
- **The four router slots in the pool all share quotas across the same API key when the worker uses the same provider.** Concretely: Groq's router (`llama-3.3-70b-versatile`) and Groq's worker (`openai/gpt-oss-120b`) are two **separate** per-model RPM buckets, so they don't compete. The Cerebras router (`llama3.1-8b`) and Cerebras worker (`zai-glm-4.7`) are also separate per-model. But the **daily token quota** on Cerebras (`tokens_per_day: 1_000_000`) is account-wide, so both router and worker draw from it.

---

## Adding a new router provider

The router pool is configurable. To add a fifth router (say, an OVHcloud anonymous endpoint):

1. **Add the provider adapter** in [providers.py](providers.py) if it's not already there (subclass `OpenAICompatProvider` — most providers are OpenAI-compatible).
2. **Add an entry in `build_router_providers()`** reading the env key and the `ROUTER_<PROVIDER>_MODEL` override.
3. **Add the provider to `ROUTER_DEFAULTS`** with the model id you want as default.
4. **Add the provider to `ROUTER_ORDER` env var** in the position where it should sit in the failover ring.
5. **Add an entry in `LIMITS`** in [router.py](router.py) if the provider isn't already in the worker pool.

No other changes needed — the dashboard, `/v1/routers`, and call logging all pick it up automatically. Mistral AI and OVHcloud were evaluated for V3 and parked for V4 — see Session 6 notes for the rationale.

---

## Adding a new worker provider

Same as V1/V2 — add adapter in `providers.py`, entry in `build_providers()`, `LIMITS` entry, shortcut keys in `SHORTCUTS`, add to `LLM_ORDER`. The router pool's `TIER_TO_ORDER` table in [main.py](main.py) determines where new workers slot into each tier's failover order — add them there too if you want them in the auto-routing flow.

---

## What V3 deliberately does NOT do

- **No verifier router slot.** Three cognitive-layer slots (perception, memory, decision), not four. The verifier needs more design work before it gets its own router — coming in a later version.
- **No content-based routing decisions.** The router emits only a tier label (`TINY` / `LARGE` / `HUGE`). It never recommends a specific provider, never picks a model, never answers questions. Worker selection from the tier is deterministic (table lookup) so routing decisions are reproducible.
- **No router-router-of-routers.** The router pool is failed-over via a static `ROUTER_ORDER` ring. There is no meta-router deciding which router to use — that would be infinite recursion.
- **No streaming on routed calls.** `auto_route` and `stream=true` aren't designed to work together yet (the router needs the full prompt before it can classify). For streaming, use explicit `provider=...`.
- **No new providers vs V2.** Mistral AI and OVHcloud were evaluated and parked for V4. Worker pool stays at 7 providers; router pool is 4 of the existing providers used with different (router-grade) model defaults.
- **No new tools / no agent loop.** Same as V2 — V3 is the substrate. The agent loop that uses `auto_route` is in Session 6's `agent6.py`, not here.

---

## Test matrix

```bash
./.venv/bin/python tests/test_all_providers.py
```

Inherits V2's per-worker matrix (basic, tools, structured, cache, reasoning) plus a routing column that checks each `auto_route` value produces a `router_decision` block with a sensible tier.

A real run (May 2026, free-tier keys):

```
provider    basic    tools     struct    cache    reasoning   routing
---------------------------------------------------------------------
ollama      OK       OK        OK        SKIP     n/a         n/a
gemini      OK       OK        OK        n/a*     n/a*        OK
nvidia      OK       OK        OK        n/a      n/a         OK
groq        OK       OK        OK        FAIL‡    n/a         OK
cerebras    FAIL§    FAIL§     FAIL§     FAIL     FAIL        OK§§
openrouter  OK       OK        OK        n/a      n/a         (skipped)
github      OK       OK        OK        n/a      n/a         OK

router pool:
- cerebras (llama3.1-8b)       — intermittent (queue_exceeded), failover works
- groq (llama-3.3-70b)          — rock solid, 220-450ms typical
- nvidia (nemotron-nano-8b)     — wired, tested ad-hoc
- github (phi-4-mini)           — wired, tested ad-hoc
```

§§ Cerebras router serves the first request after a quiet period, then queue-exceeds for a few minutes; failover to Groq handles the gap.

---

## Pedagogical notes — what to surface in class

1. **The router can be turned off.** Skip `auto_route` and V3 behaves like V2. The router is opt-in, not load-bearing. This makes the routing subsystem independently testable.
2. **The wall is enforced in code, not by convention.** Walk students through `_classify_tier()` and show them the bounded envelope being constructed. Then ask: "could you add a feature that lets the router see the tool list?" The answer is no — not without breaking the wall, which would be a visible code change.
3. **Free-tier reality forces design choices.** Cerebras `gpt-oss-120b` 404s on the test account despite the docs. The May-27 deprecation is real and looming. These aren't bugs to fix, they're constraints to design around — and the four-router pool is the design response.
4. **Token thresholds are honest, not aspirational.** The 1K elbow is set where free-tier small models actually start failing, not where their context windows nominally end. The 8K ceiling is set where free-tier Gemini quality actually starts dropping. Students should learn to test these elbows themselves with their own keys.
5. **`fallback_used` is the honest measure of routing health.** A dashboard that shows high `fallback_used` count tells you the router pool is overloaded; the gateway didn't lie about its decisions, it just couldn't make them with a real LLM. This is the kind of observability you can only build when the architecture is honest about failure modes.
