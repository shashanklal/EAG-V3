# Agentic Architecture — Session 7 Solution

## Overview

This is a **goal-driven, multi-iteration agentic system** that decomposes natural language queries into sub-goals, executes them via MCP (Model Context Protocol) tools, and synthesises final answers. It uses a four-layer typed architecture with persistent vector memory (FAISS), an artifact store, and an LLM gateway for inference and embeddings.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER QUERY                                  │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       agent7.py (Orchestrator)                      │
│                                                                     │
│  for each iteration (max 20 or 80 for indexing queries):            │
│                                                                     │
│    ┌──────────┐    ┌─────────────┐    ┌──────────┐    ┌──────────┐ │
│    │  MEMORY  │───▶│ PERCEPTION  │───▶│ DECISION │───▶│  ACTION  │ │
│    │  (read)  │    │ (goal mgmt) │    │ (LLM)    │    │ (MCP)    │ │
│    └──────────┘    └─────────────┘    └──────────┘    └──────────┘ │
│         ▲                                                  │       │
│         └──────────────── MEMORY (write) ◀─────────────────┘       │
│                                                                    │
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     MCP Server (11 Tools)                            │
│  web_search │ fetch_url │ get_time │ currency_convert │ read_file   │
│  list_dir │ create_file │ update_file │ edit_file                   │
│  index_document │ search_knowledge                                  │
└─────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  LLM Gateway V7 (localhost:8107)                     │
│         Models: llama-3.1-8b-instruct, gemma-3-12b                  │
│         Endpoints: /v1/chat, /v1/embed, /v1/routers                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Iteration Loop (Per Turn)

Each user query triggers a multi-iteration loop inside `agent7.py`:

```
1. MEMORY READ     →  Vector similarity search (FAISS) + keyword fallback
2. PERCEPTION      →  LLM decomposes/updates goal list from query + history
3. DECISION        →  LLM picks: (a) answer directly OR (b) call one MCP tool
4. ACTION          →  Execute the chosen tool via MCP stdio transport
5. MEMORY WRITE    →  Record tool outcome as an embedded fact for future recall
```

The loop repeats until all goals are marked `done` or the iteration limit is reached.

---

## Module Breakdown

### `agent7.py` — Orchestrator

| Responsibility | Details |
|---|---|
| Entry point | Accepts a natural language query from CLI |
| Loop control | Runs up to 20 iterations (80 for indexing queries) |
| MCP session | Opens a stdio connection to `mcp_server.py` |
| Layer sequencing | memory.read → perception → decision → action → memory.write |
| Stall detection | Breaks if 4 consecutive identical answers or tool-JSON in answers |

### `perception.py` — Goal Management

| Responsibility | Details |
|---|---|
| Query decomposition | Splits a user query into ordered sub-goals on first iteration |
| Goal tracking | Preserves goal order across iterations, marks goals as done |
| Goal expansion | When a `list_dir` reveals N files, expands into N per-file goals |
| Artifact attachment | Decides when a goal needs raw artifact bytes for synthesis |
| Intent-level language | Describes WHAT must happen, never WHICH tool to use |

**Key Design**: Perception speaks at the level of **intent** (e.g. "fetch the weather", "make this file searchable") — it never names tools. This separation keeps goal planning independent of the tool inventory.

### `decision.py` — Action Selection

| Responsibility | Details |
|---|---|
| Single LLM call | One inference per iteration — answer OR tool call, never both |
| Content synthesis | When artifact bytes are attached, synthesises answer from them |
| Tool selection | Maps intent to the appropriate MCP tool from the available list |
| Memory awareness | Answers directly from memory hits when data already exists |
| Reminder handling | Maps "remember X" / "set reminder" to `create_file` in sandbox |

**Critical Rule**: When ATTACHED ARTIFACTS is non-empty, Decision **must** answer directly — never call a tool when content is already provided.

### `action.py` — MCP Dispatcher

| Responsibility | Details |
|---|---|
| Pure dispatch | No LLM calls — just executes the tool via MCP |
| Artifact promotion | Results > 4 KB are stored in the artifact store, returning a handle |
| Safety guard | Blocks `art:...` handles from being passed as file paths or URLs |
| Result packaging | Returns `(descriptor_text, artifact_id_or_None)` to the loop |

### `memory.py` — Persistent Memory Service

| Responsibility | Details |
|---|---|
| Vector search | FAISS cosine similarity over embedded descriptors (primary path) |
| Keyword fallback | Token overlap scoring when vector search returns nothing |
| Embedding | Calls `gateway.embed()` at write time for facts/preferences/outcomes |
| Classification | LLM classifies free-form text into `fact`, `preference`, or `scratchpad` |
| Batch indexing | `add_facts_batch()` for concurrent multi-chunk embedding |
| Persistence | `state/memory.json` (items) + `state/index.faiss` + `state/index_ids.json` |

**Memory Kinds**:
- `fact` — durable knowledge (indexed chunks, stored data)
- `preference` — user preferences
- `tool_outcome` — results of tool executions
- `scratchpad` — run-scoped intermediate work (not embedded)

### `vector_index.py` — FAISS Wrapper

| Responsibility | Details |
|---|---|
| Index type | `IndexFlatIP` (inner product on L2-normalized vectors = cosine sim) |
| ID mapping | Parallel `list[str]` maps FAISS integer positions → MemoryItem IDs |
| Persistence | `state/index.faiss` + `state/index_ids.json` |
| Auto-rebuild | Rebuilt from `memory.json` on cold start if index files are missing |

### `artifacts.py` — Content-Addressable Blob Store

| Responsibility | Details |
|---|---|
| Deduplication | SHA-256 content hash as key |
| Storage | `state/artifacts/{hash}.bin` (bytes) + `{hash}.json` (metadata) |
| Separation | Raw bytes never enter Memory or Decision context unless explicitly attached |
| Size threshold | Defined in `action.py` at 4 KB |

### `gateway.py` — LLM Gateway Bridge

| Responsibility | Details |
|---|---|
| Auto-start | Launches the V7 gateway if not already running on port 8107 |
| Health check | Polls `/v1/routers` with 2s timeout |
| Client import | Dynamically loads `client.py` from the gateway directory |
| Embed helper | `embed(text, task_type)` → calls `POST /v1/embed` |

### `mcp_server.py` — Tool Server (11 Tools)

| Tool | Purpose |
|---|---|
| `web_search` | Tavily primary (950/mo cap), DuckDuckGo fallback |
| `fetch_url` | crawl4ai (JS-rendered) with httpx+BS4 fallback |
| `get_time` | Current time in any IANA timezone |
| `currency_convert` | ISO-3 currency conversion via frankfurter.dev |
| `read_file` | Read a UTF-8 file from `sandbox/` |
| `list_dir` | List directory contents in `sandbox/` |
| `create_file` | Create a new file in `sandbox/` |
| `update_file` | Overwrite an existing file in `sandbox/` |
| `edit_file` | Find-and-replace within a sandbox file |
| `index_document` | Chunk a file/directory and embed into Memory (FAISS) |
| `search_knowledge` | Vector search + ID-scan + status aggregation over indexed facts |

**Security**: All file operations are sandboxed — path traversal beyond `sandbox/` is blocked by `_safe()`.

### `schemas.py` — Typed Contracts

All inter-layer communication uses Pydantic models:

- `MemoryItem` — one record in memory (with optional embedding vector)
- `Artifact` — metadata for a stored blob
- `Goal` — a single sub-goal with done/attach state
- `Observation` — the goal list emitted by Perception
- `ToolCall` — name + arguments for an MCP tool invocation
- `DecisionOutput` — either an `answer` (str) or a `tool_call`

---

## Data Flow Example: Multi-Step Query

**Query**: *"Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which one is most appropriate."*

```
Iteration 1:
  Memory: (empty — fresh run)
  Perception: Creates 3 goals:
    [1] "Search for family-friendly activities in Tokyo this weekend"
    [2] "Check Saturday's weather forecast for Tokyo"
    [3] "Recommend which activity is most appropriate given the weather"
  Decision: Calls web_search("family-friendly activities Tokyo this weekend")
  Action: Returns 5 search results
  Memory: Records tool_outcome with embedding

Iteration 2:
  Memory: Returns the search results from iter 1
  Perception: Goal 1 still open (needs page content), Goal 2-3 pending
  Decision: Calls fetch_url(top_result_url)
  Action: Fetches page → stored as artifact (>4KB)

Iteration 3:
  Perception: Attaches artifact to Goal 1
  Decision: Answers Goal 1 from attached content (3 activities listed)
  Memory: Records answer

Iteration 4:
  Perception: Goal 1 ✓, Goal 2 next
  Decision: Calls web_search("Tokyo weather forecast Saturday")
  Action: Returns weather results

Iteration 5:
  Decision: Calls fetch_url(weather_page)
  Action: Returns weather data

Iteration 6:
  Perception: Attaches weather artifact to Goal 2
  Decision: Answers Goal 2 (weather summary)

Iteration 7:
  Perception: Goals 1-2 ✓, Goal 3 (synthesis)
  Decision: Synthesises recommendation from history
  → FINAL ANSWER emitted
```

---

## Document Indexing & RAG Flow

The system supports Retrieval-Augmented Generation through `index_document` and `search_knowledge`:

```
INDEX PHASE:
  File → _chunk_text() (sliding window) → embed each chunk → FAISS + memory.json

QUERY PHASE:
  User question → embed query → FAISS top-k → chunks in Memory Hits → Decision answers
```

**Chunking**: Sliding window with configurable size (default 400 words, overlap 80). For directories, minimum 2000 words/chunk to limit API calls.

**Special Searches in `search_knowledge`**:
1. **ID-based scan** — If query contains identifiers like `CLM202600051`, brute-force scans all chunks for exact matches
2. **Status aggregation** — For queries about claim statuses (denied, paid, pended), extracts matching rows from tabular data
3. **Vector similarity** — Standard FAISS cosine search for semantic queries

---

## Persistent State

```
state/
├── memory.json        # All MemoryItems (facts, preferences, outcomes, scratchpad)
├── index.faiss        # FAISS binary index (L2-normalized vectors)
├── index_ids.json     # Parallel ID list mapping positions → MemoryItem.id
└── artifacts/
    ├── {hash}.bin     # Raw content bytes
    └── {hash}.json    # Artifact metadata (content_type, source, descriptor)
```

State survives across runs. Clearing `state/` resets the agent to a blank slate.

---

## Sandbox Directory

```
sandbox/
├── papers/            # Research papers (markdown) for indexing
├── claims/            # Claims data files for domain-specific RAG
├── reminders/         # User reminders created via create_file
└── ...                # Any user-created files
```

All file tools (`read_file`, `create_file`, `list_dir`, etc.) operate exclusively within `sandbox/`. Path traversal is blocked.

---

## Key Design Principles

1. **Typed Layer Boundaries** — Every inter-module contract is a Pydantic model, not a free-form dict
2. **Separation of Concerns** — Perception handles WHAT (intent), Decision handles HOW (tool choice)
3. **Bytes Never in Context** — Large content lives in the artifact store; only handles + short descriptors flow through the loop
4. **Vector-First Retrieval** — FAISS cosine similarity is the primary read path; keyword search is a fallback
5. **Idempotent Memory** — Facts are deduplicated by content; re-indexing the same file produces no duplicates
6. **Sandboxed I/O** — All file operations are constrained to `sandbox/` for security
7. **Stall Recovery** — The loop detects repeated identical answers and forces a graceful exit

---

## Test Validation Results

All queries pass end-to-end:

| Query | Description | Time |
|---|---|---|
| A | Wikipedia research (web_search fallback) | 91.9s |
| B | Tokyo activities + weather + recommendation | 196.1s |
| C1 | Remember birthday + create reminders | 120.7s |
| C2 | Recall birthday from memory | 216.3s |
| D | Web search + read + synthesise asyncio advice | 252.2s |
| E | Index single paper + answer from it | 59.6s |
| F1 | Index all papers in a directory | 63.9s |
| F2 | Answer from indexed papers (RAG) | 33.1s |
| G | Cross-paper comparison from indexed knowledge | 51.8s |
| H | Multi-turn conversation continuity | 52.9s |
| CLAIMS_INDEX | Bulk index claims directory | 275.2s |
| CUSTQ1-5 | Domain-specific RAG queries over claims | 35-59s |

---

## Running the Agent

```bash
# Single query
python agent7.py "What is the current time in Tokyo?"

# Multi-step query
python agent7.py "Find 3 family-friendly things to do in Tokyo this weekend."

# Indexing + RAG
python agent7.py "Index every .md file under papers/. Confirm how many chunks were indexed."
python agent7.py "What are the key contributions of the Transformer architecture?"
```

**Prerequisites**:
- LLM Gateway V7 running on `localhost:8107`
- Python dependencies: `pydantic`, `faiss-cpu`, `httpx`, `mcp`, `crawl4ai`, `tavily-python`, `python-ddgs`, `beautifulsoup4`
- Environment: `.env` file with `TAVILY_API_KEY` (optional — falls back to DuckDuckGo)
