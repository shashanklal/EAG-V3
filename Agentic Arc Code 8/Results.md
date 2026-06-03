# Session 8 — Agentic Architecture Results

## Overview

Session 8 implements a **growing-graph multi-agent orchestrator** where a planner emits a DAG of skill nodes executed in topological order with parallelism. Key features tested:

- **Parallel fan-out** — multiple researcher nodes execute concurrently
- **Critic gate** — validates outputs against user constraints; triggers recovery on fail
- **Recovery splice** — on critic fail or upstream failure, a new planner node is inserted to retry

---

## 1. Parallel Fan-Out (3 Researchers)

**Session:** `s8-2bf2ac11`  
**Query:** *"For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest."*

### DAG Structure
```
planner → researcher₁ (Lagos)
        → researcher₂ (Cairo)      → formatter
        → researcher₃ (Kinshasa)
```

### Timing
| Node | Skill      | Elapsed |
|------|-----------|---------|
| n:1  | planner    | 6.19 s  |
| n:2  | researcher | 46.99 s |
| n:3  | researcher | 53.68 s |
| n:4  | researcher | 45.37 s |
| n:5  | formatter  | 6.06 s  |

- **Wall-clock (parallel):** 6.19 + 53.68 + 6.06 = **65.93 s**
- **Serial sum:** 158.29 s
- **Speedup:** **2.40×**

### Output
> Lagos, with a growth rate between 2.48% and 3.78%, is growing faster than Cairo, which has a growth rate of 1.99%. Kinshasa's growth rate is not available for comparison. The current populations are approximately 16.5–17.8 million for Lagos, 22.6 million for Cairo, and 7.8 million for Kinshasa.

---

## 2. Healthcare Parallel Fan-Out (3 Researchers)

**Session:** `s8-8f3a26c0`  
**Query:** *"Find the average hospitalization cost, top cause of readmission, and 30-day readmission rate for heart failure, pneumonia, and hip replacement surgery in US hospitals."*

### Timing
| Node | Skill      | Elapsed |
|------|-----------|---------|
| n:1  | planner    | 6.84 s  |
| n:2  | researcher | 76.33 s |
| n:3  | researcher | 60.29 s |
| n:4  | researcher | 47.14 s |
| n:5  | formatter  | 10.49 s |

- **Wall-clock (parallel):** 6.84 + 76.33 + 10.49 = **93.66 s**
- **Serial sum:** 201.09 s
- **Speedup:** **2.15×**

### Output
> The average cost per 30-day readmission to Medicare was $13,200. The top cause of readmission is medication non-compliance (38%). 30-day readmission rates: Heart failure 19.8%, Pneumonia 12.1–21.4%, Hip replacement monitored under HRRP.

---

## 3. Critic PASS Verdict

**Session:** `s8-ba02f53a`  
**Query:** *"Research the CMS Hospital Readmissions Reduction Program and list the key penalty mechanisms. Each bullet must be 15 words or fewer. The critic must verify all bullets are ≤ 15 words."*

### Critic Output (node n:4)
```json
{
  "verdict": "pass",
  "rationale": "All fields are directly supported by the input rationale, which cites specific text from the page without fabrication or unsupported claims."
}
```

### Flow
```
planner → researcher → summariser → critic (PASS) → formatter
```

No recovery triggered. The summariser successfully kept all bullets under the 15-word limit.

---

## 4. Critic FAIL Verdict + Recovery Loop

**Session:** `s8-f5cbab4e`  
**Query:** *"Read the paper sandbox/papers/attention.md and produce exactly 4 bullet points. Each bullet must contain EXACTLY 8 words — not 7, not 9, exactly 8. The critic must count each bullet's words and FAIL if any bullet does not have exactly 8 words."*

### First Critic Output (node n:4)
```json
{
  "verdict": "fail",
  "rationale": "Bullet_3 has only 7 words: 'Multi-head attention runs parallel attention layers simultaneously.' — 'Multi-head' is one word, so total is 7, not 8."
}
```

### Recovery Cascade
The critic fail triggered a recovery planner splice:
```
[n:4] critic             → critic-fail recovery: planner node n:6 for n:3
[n:9] critic             → critic-fail recovery: planner node n:13 for n:8
[n:16] critic            → critic-fail recovery: planner node n:25 for n:15
[n:20] critic            → critic-fail recovery: planner node n:31 for n:19
[n:29] critic            → critic-fail recovery: planner node n:48 for n:28
[n:34] critic            → critic-fail recovery
[n:38] critic            → critic-fail recovery
[n:43] critic            → critic-fail recovery
[flow] node cap 60 hit at 52; stopping
```

The exact 8-word constraint was so strict that the LLM could not consistently satisfy it. The system kept retrying via recovery splices until the **60-node safety cap** halted execution.

### Timing
- **Wall-clock:** 187.57 s
- **Serial sum:** 524.64 s
- **Speedup:** 2.80× (due to parallel recovery branches)
- **Nodes executed:** 52 (cap: 60)

---

## 5. Bug Fixes Applied

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `TypeError: edges` in `node_link_data` | NetworkX 3.3 renamed parameter | Changed `edges=` → `link=` in persistence.py |
| `UnicodeEncodeError` writing JSON | Windows cp1252 default encoding | Added `encoding="utf-8"` to `_atomic_write` |
| `crawl4ai` TargetClosedError | Corporate security kills headless Chromium | Added `httpx + BeautifulSoup` fallback in mcp_server.py |
| `PermissionError` on `os.replace` | OneDrive holds brief file locks | Added retry loop (3 attempts, 100–200 ms delay) |

---

## 6. Architecture Summary

```
flow.py (Executor)
  ├── perception.py     → intent/entity extraction
  ├── decision.py       → DAG topology from planner
  ├── skills.py         → dispatch to LLM via gateway
  │     └── mcp_runner.py → tool-use loop (fetch_url, tavily, sandbox)
  ├── persistence.py    → session state on disk
  ├── recovery.py       → critic-fail & upstream-failure recovery
  ├── memory.py         → FAISS episodic memory
  └── vector_index.py   → embedding + ANN search
```

**Key Config:** `agent_config.yaml` — skills, tools_allowed, critic flag, extend_from relationships.

---

## Environment

- Python 3.11.10, Windows 11
- LLM Gateway V8 (FastAPI on port 8108)
- NetworkX 3.3, FAISS-cpu, httpx, BeautifulSoup4
- Tavily API for web search
