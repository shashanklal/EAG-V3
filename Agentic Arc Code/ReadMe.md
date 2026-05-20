# Goal-Iterating Agentic Architecture

## Objective

This project implements a **goal-directed autonomous agent** that decomposes user queries into bounded goals and iteratively solves them through a structured loop of observation, decision-making, and action execution. The agent routes all LLM calls through a local **LLM Gateway V3** and executes real-world actions via a **Model Context Protocol (MCP)** tool server.

The architecture separates concerns into four cognitive roles — **Memory**, **Perception**, **Decision**, and **Action** — each with well-defined boundaries and contracts enforced by Pydantic schemas.

---

## Overall Workflow

```
User Query
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│                      AGENT LOOP (agent.py)                   │
│                                                             │
│   1. Memory.read() ──── keyword search for relevant context │
│                                                             │
│   2. Perception.observe() ─── decompose/update goals        │
│          │                                                  │
│          ├── First iteration: decompose query into goals    │
│          └── Later iterations: update done flags/attachments│
│                                                             │
│   3. All goals done? ──► YES ──► return final answer        │
│          │                                                  │
│          ▼ NO                                               │
│   4. Decision.next_step() ─── for first unfinished goal     │
│          │                                                  │
│          ├── ANSWER: record in history                       │
│          └── TOOL_CALL: dispatch via Action.execute()        │
│                    │                                        │
│                    ▼                                        │
│              MCP Server (9 tools via stdio)                  │
│                    │                                        │
│                    ▼                                        │
│              Store result in Memory + History                │
│                                                             │
│   5. Loop back to step 1 (max 10 iterations)                │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
Final Answer (synthesized from answer events or action summaries)
```

---

## Architecture Components

### `schemas.py` — Data Contracts

Pydantic v2 models that define the typed contracts at every role boundary:

| Model | Purpose |
|-------|---------|
| `MemoryItem` | Stored memory entry with kind, keywords, descriptor, value, confidence |
| `Artifact` | Metadata for content-addressable binary blobs (id: `art:<sha256>`) |
| `Goal` | A single bounded goal with text, done flag, and optional artifact attachment |
| `Observation` | Output of Perception — a list of Goals |
| `ToolCall` | A tool invocation with name + arguments dict |
| `ActionResult` | Output of Action — descriptor, artifact_id, and success flag |
| `HistoryEvent` | Typed event record (action or answer) passed between all roles |
| `DecisionOutput` | Output of Decision — either an answer string OR a ToolCall |

---

### `agent.py` — Main Orchestrator

The top-level loop that coordinates all roles. Key responsibilities:

- **Goal ID management**: Assigns stable IDs to goals across iterations (reuses prior IDs by position).
- **Deduplication**: Tracks failed tool calls by hash. After 2 identical failures → blocks the call. After 3 total failures for a goal → injects an EXHAUSTED signal so Decision falls back to general knowledge.
- **Artifact attachment**: For synthesis goals (compare, recommend, evaluate), attaches ALL available artifacts from memory so Decision has full context.
- **Final answer synthesis**: Combines answer events from history. For action-only queries (file creation), produces a summary of created files.
- **Max iterations**: Hard cap at 10 to prevent infinite loops.

---

### `memory.py` — Memory Service

A typed store with keyword-search reads and LLM-classified writes.

**Persistence:** `state/memory.json`

| Method | LLM Cost | Description |
|--------|----------|-------------|
| `read(query, history)` | None | Pure keyword-overlap search (tokenize → stopword removal → score by intersection). Returns top-K hits. |
| `filter(kinds, goal_id, recent)` | None | Structured filter by kind/goal/recency. |
| `remember(raw_text, source, run_id)` | 1 call | LLM classifies the text into a `MemoryItem` with kind, keywords, descriptor, structured value. Routes via `auto_route="memory"`. |
| `record_outcome(tool_call, result_text, ...)` | 1 call | Convenience wrapper — stores a tool result as a memory item with artifact reference. |

**Memory Classifier Prompt**: Uses step-by-step classification (identify reasoning type → classify kind → extract structured value → generate keywords → write descriptor → self-check).

---

### `perception.py` — Perception Role (Orchestrator)

One LLM call per iteration. Observes the full state and emits updated goals.

**Provider:** Pinned to `provider="g"` (Gemini) for reliability.

**Responsibilities:**
- **Goal decomposition** (first iteration): Breaks user query into minimal bounded goals (2-3 typical).
- **Status tracking** (subsequent iterations): Updates done flags based on history evidence.
- **Artifact attachment**: Attaches artifacts to the first unfinished goal that needs them.

**Post-LLM Validation** (strict evidence-based enforcement):
- `SYNTHESIS_VERBS` — goals containing these words can only be marked done by an answer event (not an action).
- `ACTION_ONLY_VERBS` — goals starting with these verbs can only be marked done by a successful action event (not an answer).
- `MULTI_ACTION_VERBS` — goals starting with "read" or "download" + a number require that many successful actions before an answer is accepted.
- **Combined multi-action + synthesis**: If a goal is both multi-action ("Read the top 3") AND synthesis ("identify common advice"), the answer only counts as evidence once all required actions are complete.
- Sticky done flags — once done, always done.
- First-iteration guard — no goals can be done before any execution.
- HTTP 4xx/5xx detection — tool results with error status codes don't count toward action requirements.

---

### `decision.py` — Decision Role (Action Selector)

One LLM call per iteration. Receives exactly ONE goal and decides the next step.

**Routing:** `auto_route="decision"` (gateway auto-selects best model).

**Output:** Exactly one of:
- `{"answer": "..."}` — substantive plain-text answer (extraction, synthesis, analysis).
- `{"tool_call": {"name": "...", "arguments": {...}}}` — single MCP tool invocation.

**Key Rules:**
- URL in goal → must call `fetch_url`
- "Create"/"Write"/"Save" verb → must call `create_file`
- "Fetch"/"Search" verb → must use a tool
- Extraction WITH attached artifact → answer from the data
- EXHAUSTED signal in history → answer from general knowledge

**Response Parsing (no regex on LLM output):**
- Prefers native `tool_calls` from the gateway when available
- Falls back to `json.loads()` on the response text
- Searches for `{"answer"` or `{"tool_call"` markers with brace-depth counting
- Detects embedded tool_call JSON inside answer text (LLM confusion) and redirects
- No regex is applied to LLM output — parsing is pure JSON

---

### `action.py` — Action Role (MCP Dispatch)

Pure execution — **no LLM calls**. Dispatches exactly one tool call to the MCP server.

**Key Behaviors:**
- **Returns `ActionResult`**: Typed Pydantic model with `descriptor`, `artifact_id`, and `success` flag.
- **Art: guard**: Blocks artifact handles (`art:...`) from being passed as tool arguments.
- **Empty result detection**: Returns `ActionResult(success=False)` if tool returns `[]` or `{}`.
- **Artifact threshold** (4 KB): If tool output exceeds 4096 bytes, stores the payload as a content-addressable artifact and returns a handle + preview.
- **Error handling**: Catches MCP exceptions and returns `ActionResult(success=False)` with structured error descriptor.

---

### `artifacts.py` — Content-Addressable Artifact Store

Stores large binary payloads (tool results >4 KB) with SHA-256 content addressing.

**Storage:** `state/artifacts/` directory with paired `.bin` + `.json` (metadata sidecar) files.

| Method | Description |
|--------|-------------|
| `put(data, content_type, source, descriptor)` | Store bytes, returns `Artifact` with id `art:<sha256[:16]>` |
| `get_bytes(art_id)` | Retrieve raw bytes |
| `get_meta(art_id)` | Retrieve metadata (Artifact model) |
| `exists(art_id)` | Check if artifact exists |

---

### `mcp_server.py` — MCP Tool Server

A FastMCP server exposing 9 tools via stdio transport. All external HTTP calls use `verify=False` for corporate proxy compatibility.

| Tool | Description |
|------|-------------|
| `web_search(query, max_results)` | Tavily primary (REST API), DuckDuckGo fallback. Capped at 5 results. Usage tracked monthly. |
| `fetch_url(url, timeout)` | httpx + BeautifulSoup HTML→text extraction. Strips scripts/styles/nav. |
| `get_time(timezone)` | Current time in any IANA timezone. |
| `currency_convert(amount, from, to)` | Live conversion via frankfurter.dev API. |
| `read_file(path)` | Read UTF-8 file from sandbox. |
| `list_dir(path)` | List sandbox directory contents. |
| `create_file(path, content)` | Create new file (errors if exists). Auto-creates parent dirs. |
| `update_file(path, content)` | Overwrite existing file. |
| `edit_file(path, find, replace)` | Find-and-replace within a file. |

**Security:** All file tools are sandboxed under `./sandbox/` with path traversal protection.

---

### `llm_gatewayV3/client.py` — LLM Gateway Client

Thin HTTP client that routes all LLM calls through the local gateway at `localhost:8101`.

| Parameter | Purpose |
|-----------|---------|
| `provider` | Pin to specific model (e.g., `"g"` for Gemini) |
| `auto_route` | Let gateway choose model by task type (`"memory"`, `"decision"`) |
| `response_format` | JSON schema for structured output enforcement |
| `temperature` | Controls randomness (0.3 for Memory, 1.0 for Perception) |

---

## Interaction Flow Between Components

```
agent.py
  │
  ├── Memory.read(query) ──────────────── returns relevant MemoryItems
  │
  ├── Perception.observe(query, hits, history, prior_goals)
  │       │
  │       └── LLM call (provider="g") ──► returns Observation (goal list)
  │                                        + post-validation enforcement
  │
  ├── Decision.next_step(goal, hits, attached, history, tools)
  │       │
  │       └── LLM call (auto_route="decision") ──► returns DecisionOutput
  │                                                  (answer OR tool_call)
  │
  ├── Action.execute(session, tool_call, artifacts)
  │       │
  │       └── MCP session.call_tool() ──► MCP Server (stdio)
  │               │                           │
  │               │                           ├── web_search → Tavily/DDG
  │               │                           ├── fetch_url → httpx+BS4
  │               │                           ├── create_file → sandbox/
  │               │                           └── ... (9 tools)
  │               │
  │               └── Returns ActionResult (Pydantic model)
  │                     - descriptor: text summary or artifact preview
  │                     - artifact_id: handle if >4KB (or None)
  │                     - success: bool for failure tracking
  │
  └── Memory.record_outcome() ──────────── stores tool result for future reads

All inter-role communication uses typed Pydantic models:
- History: list[HistoryEvent] (not list[dict])
- Action results: ActionResult (not tuple)
- Goals/Observations/Decisions: all Pydantic BaseModel subclasses
```

---

## State & Persistence

| Path | Contents |
|------|----------|
| `state/memory.json` | All MemoryItems (keyword-searchable store) |
| `state/artifacts/*.bin` | Raw artifact bytes (content-addressed) |
| `state/artifacts/*.json` | Artifact metadata sidecars |
| `sandbox/` | File tool sandbox (reminders, notes, etc.) |
| `usage.json` | Monthly Tavily/DDG usage counters |

---

## Running the Agent

```bash
# Prerequisites: LLM Gateway V3 running at localhost:8101
# Environment: .env file with TAVILY_API_KEY

# Install dependencies via uv (no manual virtualenv activation)
uv sync

# Run with preset test queries
uv run python agent.py A    # Fetch + Extract
uv run python agent.py B    # Search + Weather + Synthesis
uv run python agent.py C1   # Memory Store + File Creation
uv run python agent.py C2   # Memory Recall (run after C1)
uv run python agent.py D    # Search + Read + Summarize

# Run with custom query
uv run python agent.py "Your custom query here"

# State management flags
uv run python agent.py --clean A     # Force clean state before running
uv run python agent.py --no-clean C2 # Preserve state (e.g., after C1)
```

---

## Dependencies

Managed via `pyproject.toml` + **uv** (no manual virtualenv activation):

- Python 3.11+
- `pydantic>=2.6` — typed schema validation (Pydantic v2)
- `httpx>=0.27` — HTTP client (all external calls)
- `beautifulsoup4>=4.12` — HTML parsing for fetch_url
- `duckduckgo-search>=6.0` — fallback web search
- `mcp>=1.0` — Model Context Protocol client/server
- `python-dotenv>=1.0` — environment variable loading
- `tavily-python>=0.3` — primary web search API
- LLM Gateway V3 (local service at port 8101)

**Dev dependencies:** `pytest>=8.0`, `pytest-asyncio>=0.23`




# Results — Agent Execution Logs

Date: May 20, 2026

---

## Query A
**Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.**

```
[clean] state/ and sandbox/ cleared
Query: Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.

[memory.remember] storing user query
[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'get_time', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

--- iter 1 ----------------------------------------
[memory.read]   1 hits
[perception]    [open] Fetch the Wikipedia page for Claude Shannon
                [open] Extract birth date, death date, and three key contributions to information theory
[decision]      TOOL_CALL: fetch_url({'url': 'https://en.wikipedia.org/wiki/Claude_Shannon', 'timeout': 20})
[action]        → [artifact art:2ccfa31da6ec67eb, 82221 bytes] preview: {   "status": 200,   "content_type": "text/pla

--- iter 2 ----------------------------------------
[memory.read]   2 hits
[perception]    [done] Fetch the Wikipedia page for Claude Shannon
                [open] Extract birth date, death date, and three key contributions to information theory
                  attach=art:2ccfa31da6ec67eb
[attach]        art:2ccfa31da6ec67eb (82221 bytes)
[decision]      ANSWER: **Birth Date:** April 30, 1916 (born in Petoskey, Michigan)  **Death Date:** February 24, 2001 (died in Medford, Massachusetts, aged 84)  **Three Key ...

--- iter 3 ----------------------------------------
[memory.read]   2 hits
[perception]    [done] Fetch the Wikipedia page for Claude Shannon
                [done] Extract birth date, death date, and three key contributions to information theory

[done] all 2 goals satisfied

FINAL: **Birth Date:** April 30, 1916 (born in Petoskey, Michigan)

**Death Date:** February 24, 2001 (died in Medford, Massachusetts, aged 84)

**Three Key Contributions to Information Theory:**

1. **"A Mathematical Theory of Communication" (1948)** - This foundational paper laid the groundwork for the entire field of information theory and has been called the "blueprint for the digital era" and "the Magna Carta of the Information Age."

2. **Introduction of the concept of "bit"** - Shannon formally introduced this fundamental unit of information, which became essential to all digital communications and computing.

3. **Development of entropy in information theory** - He established the mathematical framework for measuring information content and uncertainty, providing the theoretical foundation for data compression, error correction, and efficient communication systems.

These contributions established Shannon as the "father of information theory" and laid the foundations for the Information Age, influencing everything from the development of the Internet to mobile telephony and digital storage systems.
```


---

## Query B
**Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which one is most appropriate.**

```
[clean] state/ and sandbox/ cleared
Query: Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which one is most appropriate.

[memory.remember] storing user query
[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'get_time', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

--- iter 1 ----------------------------------------
[memory.read]   1 hits
[perception]    [open] Fetch family-friendly activities in Tokyo for this weekend
                [open] Fetch Saturday's weather forecast for Tokyo
                [open] Determine which of the fetched activities is most appropriate based on the weather forecast
[decision]      TOOL_CALL: web_search({'query': 'family-friendly activities in Tokyo this weekend', 'max_results': '5'})
[action]        → [artifact art:87cd19889f616bbd, 9593 bytes] preview: {   "title": "The best Tokyo Family-friendly ac

--- iter 2 ----------------------------------------
[memory.read]   2 hits
[perception]    [done] Fetch family-friendly activities in Tokyo for this weekend
                [open] Fetch Saturday's weather forecast for Tokyo
                [open] Determine which of the fetched activities is most appropriate based on the weather forecast
[decision]      TOOL_CALL: fetch_url({'url': 'https://wttr.in/Tokyo?format=3', 'timeout': 20})
[action]        → {
  "status": 200,
  "content_type": "text/plain",
  "length_bytes": 49,
  "text": "wttr.in — Weather

--- iter 3 ----------------------------------------
[memory.read]   3 hits
[perception]    [done] Fetch family-friendly activities in Tokyo for this weekend
                [done] Fetch Saturday's weather forecast for Tokyo
                [open] Determine which of the fetched activities is most appropriate based on the weather forecast
                  attach=art:87cd19889f616bbd
[attach]        art:87cd19889f616bbd (9593 bytes)
[decision]      ANSWER: Based on the weather forecast showing Tokyo at 24°C with partly cloudy conditions, the most appropriate activities are outdoor options...

--- iter 4 ----------------------------------------
[memory.read]   3 hits
[perception]    [done] Fetch family-friendly activities in Tokyo for this weekend
                [done] Fetch Saturday's weather forecast for Tokyo
                [done] Determine which of the fetched activities is most appropriate based on the weather forecast

[done] all 3 goals satisfied

FINAL: Based on the weather forecast showing Tokyo at 24°C with partly cloudy conditions, the most appropriate activities are outdoor options that take advantage of the pleasant weather. Yoyogi Park is the top recommendation - it offers vast open lawns perfect for children's running, ball games, and picnics, with no admission fee making it budget-friendly. The 24°C temperature is ideal for outdoor family activities, and the park's location adjacent to Harajuku provides indoor shopping and dining backup options.

Ueno Park combined with the National Museum of Nature and Science is another excellent choice, offering outdoor strolling around Shinobazu Pond plus indoor museum activities with dinosaur exhibits and hands-on science displays. The Inokashira Park Zoo at 400 yen per adult is also perfect for this mild weather.

For more active families, the Shibuya Go Karting Tour would be ideal in these pleasant conditions. Indoor backup options like the Samurai Ninja Museum experiences, ramen making workshops, or Tokyo Skytree provide flexibility if weather conditions change. The current partly cloudy, mild temperature conditions favor outdoor activities while still allowing comfortable transitions to indoor venues if needed.
```


---

## Query C1
**My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.**

```
[clean] state/ and sandbox/ cleared
Query: My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.

[memory.remember] storing user query
[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'get_time', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

--- iter 1 ----------------------------------------
[memory.read]   1 hits
[perception]    [open] Record the mom's birthday as May 15, 2026
                [open] Create a calendar reminder for two weeks before May 15, 2026
                [open] Create a calendar reminder for May 15, 2026
[decision]      ANSWER: The mom's birthday is recorded as May 15, 2026.

--- iter 2 ----------------------------------------
[memory.read]   1 hits
[perception]    [done] Record the mom's birthday as May 15, 2026
                [open] Create a calendar reminder for two weeks before May 15, 2026
                [open] Create a calendar reminder for May 15, 2026
[decision]      TOOL_CALL: create_file({'path': 'reminders/birthday_reminder.txt', 'content': "Reminder: Mom's birthday is May 15, 2026"})
[action]        → {
  "ok": true,
  "path": "reminders/birthday_reminder.txt",
  "size_bytes": 40
}

--- iter 3 ----------------------------------------
[memory.read]   2 hits
[perception]    [done] Record the mom's birthday as May 15, 2026
                [done] Create a calendar reminder for two weeks before May 15, 2026
                [open] Create a calendar reminder for May 15, 2026
[decision]      TOOL_CALL: create_file({'path': 'reminders/may_15_2026_reminder.txt', 'content': 'Reminder: Birthday is May 15, 2026'})
[action]        → {
  "ok": true,
  "path": "reminders/may_15_2026_reminder.txt",
  "size_bytes": 34
}

--- iter 4 ----------------------------------------
[memory.read]   3 hits
[perception]    [done] Record the mom's birthday as May 15, 2026
                [done] Create a calendar reminder for two weeks before May 15, 2026
                [done] Create a calendar reminder for May 15, 2026

[done] all 3 goals satisfied

FINAL: The mom's birthday is recorded as May 15, 2026.
```


---

## Query C2
**When is mom's birthday?**

```
Query: When is mom's birthday?

[memory.remember] storing user query
[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'get_time', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

--- iter 1 ----------------------------------------
[memory.read]   4 hits
[perception]    [open] Extract mom's birthday from memory
[decision]      ANSWER: Mom's birthday is May 15, 2026. This information was previously recorded and saved in reminder files in the system.

--- iter 2 ----------------------------------------
[memory.read]   4 hits
[perception]    [done] Extract mom's birthday from memory

[done] all 1 goals satisfied

FINAL: Mom's birthday is May 15, 2026. This information was previously recorded and saved in reminder files in the system.
```


---

## Query D
**Search for 'Python asyncio best practices', read the top 3 results, and give me a short numbered list of the advice they agree on.**

```
[clean] state/ and sandbox/ cleared
Query: Search for 'Python asyncio best practices', read the top 3 results, and give me a short numbered list of the advice they agree on.

[memory.remember] storing user query
[mcp] loaded 9 tools: ['web_search', 'fetch_url', 'get_time', 'currency_convert', 'read_file', 'list_dir', 'create_file', 'update_file', 'edit_file']

--- iter 1 ----------------------------------------
[memory.read]   1 hits
[perception]    [open] Search for 'Python asyncio best practices'
                [open] Read the top 3 results of the search
                [open] Give me a short numbered list of the advice they agree on.
[decision]      TOOL_CALL: web_search({'query': 'Python asyncio best practices', 'max_results': '3'})
[action]        → [artifact art:fa697f3b7673253c, 5373 bytes] preview: {   "title": "Asyncio Best Practices and Common

--- iter 2 ----------------------------------------
[memory.read]   2 hits
[perception]    [done] Search for 'Python asyncio best practices'
                [open] Read the top 3 results of the search
[decision]      TOOL_CALL: fetch_url({'url': '...'})
[action]        → Error (invalid URL attempt)

--- iter 3 ----------------------------------------
[decision]      TOOL_CALL: fetch_url({'url': '...'})
[action]        → Error (retry)

--- iter 4 ----------------------------------------
[decision]      TOOL_CALL: fetch_url({'url': 'https://...'})
[action]        → {status: 200, ...}

--- iter 5 ----------------------------------------
[decision]      TOOL_CALL: fetch_url({'url': 'https://...'})
[action]        → {status: 200, ...}

--- iter 6 ----------------------------------------
[decision]      TOOL_CALL: fetch_url({'url': 'https://...'})
[action]        → {status: 200, ...}

--- iter 7 ----------------------------------------
[perception]    [done] Search for 'Python asyncio best practices'
                [done] Read the top 3 results of the search
                [open] Give me a short numbered list of the advice they agree on.
                  attach=art:fa697f3b7673253c
[attach]        art:fa697f3b7673253c (5373 bytes)
[decision]      ANSWER: 1. Use asyncio.run() as your main entry point...

--- iter 8 ----------------------------------------
[perception]    [done] Search for 'Python asyncio best practices'
                [done] Read the top 3 results of the search
                [done] Give me a short numbered list of the advice they agree on.

[done] all 3 goals satisfied

FINAL: 1. Use asyncio.run() as your main entry point - All sources emphasize starting asyncio programs with asyncio.run() to properly set up and tear down the event loop.

2. Always await coroutines - Multiple sources stress that coroutines must be awaited or they won't execute, avoiding the common mistake of creating but never executing coroutines.

3. Use async context managers when available - Sources recommend using 'async with' statements for proper resource management in asynchronous code.

4. Avoid blocking operations in async functions - There's consensus on not using synchronous blocking calls (like time.sleep()) inside async functions as this defeats the purpose of asynchronous programming.

5. Handle cancellation gracefully - Sources agree on the importance of catching CancelledError and properly cleaning up resources in tasks that might be cancelled.

6. Use asyncio.gather() for concurrent execution - Multiple sources recommend using gather() to run multiple asynchronous tasks concurrently rather than sequentially awaiting them.
```





