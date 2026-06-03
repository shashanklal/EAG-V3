# Session 8 — Agentic Architecture: Solution Overview

## Overall Architecture

Session 8 implements a **growing-graph multi-agent orchestrator**. A user query enters as a single Planner node. The Planner decomposes it into a DAG of skill nodes. The orchestrator executes ready nodes in parallel, dynamically extends the graph as each node completes (adding successors, recovery branches, or critic gates), and terminates when a Formatter node emits the final answer — or when the 60-node safety cap fires.

```
User Query
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Executor (flow.py)                                     │
│                                                         │
│  1. Add Planner node to empty DiGraph                   │
│  2. Execute ready nodes in parallel (asyncio.gather)    │
│  3. On success → extend_from (add successors, critic    │
│     auto-insertion, internal_successors chain)           │
│  4. On failure → classify → skip or replan              │
│  5. Repeat until all nodes settled or cap hit           │
│  6. Extract formatter's final_answer → print            │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Skill Dispatch (skills.py)                             │
│  resolve_inputs → render_prompt → call LLM/MCP/Sandbox  │
└─────────────────────────────────────────────────────────┘
    │                         │                      │
    ▼                         ▼                      ▼
┌──────────┐         ┌──────────────┐       ┌──────────────┐
│ Gateway  │         │  MCP Server  │       │   Sandbox    │
│ (LLM V8)│         │ (tools loop) │       │ (subprocess) │
└──────────┘         └──────────────┘       └──────────────┘
```

---

## Workflow (End-to-End)

1. **Query intake** → `flow.py` creates a session, reads FAISS memory hits once.
2. **Planner** decomposes the query into a DAG of skill nodes (researcher, distiller, comparator, coder, etc.).
3. **Parallel execution** — independent nodes (e.g., 3 researchers) run concurrently via `asyncio.gather`.
4. **Dynamic graph growth** — each completed node may emit new successors; static `internal_successors` (e.g., coder→sandbox→formatter) are auto-chained.
5. **Critic gate** — when `critic: true` is set on a skill, a Critic node is auto-inserted on outgoing edges. On `fail`, recovery splices a new Planner node.
6. **Failure recovery** — transient errors are skipped; upstream failures trigger a Planner re-plan with a failure report.
7. **Formatter** (terminal node) renders the final user-facing answer from upstream outputs.
8. **Persistence** — every node's state, the full graph, and the query are written to `state/sessions/<sid>/`.

---

## File-by-File Summary

| File | Purpose |
|------|---------|
| **flow.py** | Main orchestrator. `Graph` class (NetworkX DiGraph wrapper) + `Executor` class (async run loop, parallel batch execution, failure handling, timing table). |
| **skills.py** | Skill registry (loads YAML), input resolution (`n:<label>`, `USER_QUERY`, `art:<sha>`), prompt rendering, JSON parsing, and per-node dispatch to gateway/MCP/sandbox. |
| **gateway.py** | Bridge to LLM Gateway V8 (FastAPI on port 8108). Auto-starts the gateway subprocess, re-exports `LLM()` client and `embed()` helper. |
| **mcp_runner.py** | Multi-turn tool-use loop. Opens one stdio MCP session per skill, relays tool_calls until the model emits final text. Capped at 6 hops. |
| **mcp_server.py** | FastMCP server exposing 11 tools: web_search (Tavily/DDG), fetch_url (crawl4ai/httpx), file ops (sandboxed), index_document, search_knowledge, get_time, currency_convert. |
| **persistence.py** | `SessionStore` — writes graph.json, query.txt, and per-node JSON to disk. Atomic writes with OneDrive retry. NetworkX serialization via `node_link_data`. |
| **recovery.py** | Failure classifier (`transient`/`validation_error`/`upstream_failure`) and recovery policy (`skip` or `replan`). Also handles critic-fail splicing with per-target cap. |
| **memory.py** | Vector-backed episodic memory. LLM-classified writes (fact/preference/tool_outcome/scratchpad). FAISS vector search + keyword fallback reads. |
| **vector_index.py** | FAISS `IndexFlatIP` wrapper. L2-normalizes embeddings, persists to `state/index.faiss` + `state/index_ids.json`. Rebuilds from memory on cold start. |
| **perception.py** | Goal decomposition (S7 carry-forward). Extracts goals from user query + history. Position-based identity, append-only invariant, synthesis-keyword lock. |
| **decision.py** | One-step tool/answer decision (S7 carry-forward). Given a goal + memory + history, either emits a final answer OR calls exactly one tool. |
| **action.py** | MCP tool dispatch. Executes one tool call, routes large results (>4KB) to artifact store, blocks hallucinated artifact paths. |
| **artifacts.py** | Content-addressable blob store (SHA256-keyed). Stores raw bytes + JSON metadata under `state/artifacts/`. |
| **schemas.py** | Pydantic models defining boundaries: `MemoryItem`, `Artifact`, `Goal`, `Observation`, `ToolCall`, `DecisionOutput`, `NodeSpec`, `AgentResult`, `NodeState`. |
| **sandbox.py** | Subprocess Python runner for the Coder skill. Temp directory, 30s timeout, 1MB output caps, env whitelist. Not OS-level isolation. |
| **replay.py** | Interactive session replay. Walks `state/sessions/<sid>/nodes/` in order, shows prompt/output per node via stdin-driven UI. |
| **agent_config.yaml** | Skill catalogue. Each entry defines: prompt path, tools_allowed, temperature, max_tokens, internal_successors, critic flag, description. |
| **requirements.txt** | Dependencies: networkx, pydantic, faiss-cpu, httpx, beautifulsoup4, mcp, tavily-python, crawl4ai, etc. |
| **pyproject.toml** | Package metadata (name: `agentic-arc-s8`, Python ≥3.11). |

---

## Skill Catalogue

| Skill | Role | Tools | Key Behaviour |
|-------|------|-------|---------------|
| **planner** | Decomposes query into DAG; emits recovery subgraphs on failure | None | Temp 0.4, decides which skills to invoke and how to wire them |
| **researcher** | Multi-step web research | web_search, fetch_url | Temp 0.7, up to 6 tool hops per invocation |
| **retriever** | Searches FAISS memory index | search_knowledge | Temp 0.2, used when memory already covers the query |
| **distiller** | Extracts structured fields from raw text | None | Temp 0.1, `critic: true` (auto-inserts critic on outgoing edges) |
| **summariser** | Condenses long content | None | Temp 0.3 |
| **comparator** | Side-by-side comparison of 2+ entities | None | Temp 0.2, outputs structured table with rankings |
| **critic** | Pass/fail verdict on upstream output | None | Temp 0.0, deterministic; fail triggers recovery |
| **coder** | Emits Python code for computation | None | Temp 0.2, `internal_successors: [sandbox_executor, formatter]` |
| **sandbox_executor** | Runs coder's Python in subprocess | None | Temp 0.0, bypasses LLM — direct `run_python()` call |
| **formatter** | Renders final user-facing answer | None | Temp 0.3, terminal node — its `final_answer` is the output |

---

## Key Mechanisms

### Parallel Fan-Out
When the Planner emits multiple independent nodes (e.g., 3 researchers), the orchestrator runs them concurrently via `asyncio.gather`. Demonstrated 2.3–2.8× speedup.

### Critic Gate + Recovery
Skills with `critic: true` auto-insert a Critic node on outgoing edges. If the Critic emits `fail`, the orchestrator:
1. Marks the downstream child as `skipped`
2. Queues a Planner recovery node with the failure rationale
3. Caps at 1 recovery per target per run to prevent infinite loops

### Failure Classification
| Category | Examples | Action |
|----------|----------|--------|
| Transient | 503, timeout, connection reset | Skip (gateway already retried) |
| Validation | Malformed NodeSpec, no code in coder output | Skip (prompt bug) |
| Upstream | File not found, API returned nothing | Replan (new Planner node) |

### Internal Successors Chain
`coder` has `internal_successors: [sandbox_executor, formatter]`. When coder completes, the orchestrator auto-chains: coder → sandbox → formatter (each depends on the previous).

### Memory Contract
FAISS-ranked memory hits are read once at session start and fed into every skill's prompt. If hits are relevant, the Planner skips web research and routes through the retriever instead.

---

## Where This Framework Works Well

| Scenario | Why |
|----------|-----|
| **Multi-step research with parallel sources** | Fan-out researchers, merge in comparator/formatter |
| **Structured data extraction from web** | Researcher → Distiller → Critic validation pipeline |
| **Computation from researched data** | Researcher → Coder → Sandbox → Formatter chain |
| **Comparative analysis** | Parallel researchers → Comparator → Formatter with ranking |
| **Quality-gated outputs** | Critic auto-insertion ensures format/content compliance |
| **Recoverable workflows** | Failures trigger replanning rather than hard crashes |
| **Auditable AI pipelines** | Full session persistence + replay for every node |
| **Iterative refinement** | Critic fail → recovery → corrected answer loop |

---

## Where This Framework May Not Work Properly

| Scenario | Limitation |
|----------|------------|
| **Real-time / low-latency requirements** | Each node is an LLM call (3–80s); total latency 30–180s per session |
| **Highly interactive multi-turn conversations** | Designed for single-query DAG execution, not chat-style back-and-forth |
| **Tasks requiring persistent state across sessions** | Memory persists, but the DAG is per-session; no cross-session continuity of execution state |
| **Large-scale data processing** | Sandbox is a single Python subprocess; no distributed compute, no GPU |
| **Adversarial or untrusted inputs** | Sandbox is a usability boundary, not a security boundary (no chroot/container) |
| **Deterministic outputs** | LLM non-determinism means identical queries may produce different DAGs or different final answers |
| **Very long documents (>100 pages)** | Input resolution caps at 20KB per upstream; large artifacts get truncated |
| **Tasks requiring visual/multimodal understanding** | Text-only pipeline; no image/video/audio processing |
| **Complex iterative computation** | Coder emits one-shot scripts; no REPL-style iteration or debugging loop |
| **Offline / air-gapped environments** | Requires LLM Gateway (API calls) + optional web search (Tavily) |
| **When coder JSON parsing fails intermittently** | LLMs sometimes produce malformed JSON that `parse_skill_json` cannot recover; causes wasted recovery cycles |

---

## Environment

- Python 3.11.10, Windows 11
- LLM Gateway V8 (FastAPI, port 8108)
- NetworkX 3.3, FAISS-cpu, httpx, BeautifulSoup4
- Tavily API for web search (950 calls/month soft cap)
- MCP (Model Context Protocol) for tool dispatch
