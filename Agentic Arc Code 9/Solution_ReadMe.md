# S9 Agentic Architecture — Solution Overview

## High-Level Architecture

```
User Query
    │
    ▼
┌──────────┐     ┌────────────────┐     ┌─────────────┐
│  flow.py │────▶│  SkillRegistry │────▶│  agent_config│
│ (Graph   │     │  (skills.py)   │     │  .yaml      │
│  DAG)    │     └────────────────┘     └─────────────┘
└──────────┘
    │  grows via 5 mechanisms:
    │  1. Seed plan (planner)
    │  2. Dynamic successors (skill output)
    │  3. Static successors (YAML)
    │  4. Critic auto-insertion
    │  5. Recovery re-invocation
    ▼
┌──────────────────────────────────────────────────┐
│              Skill Execution Layer                │
├──────────┬──────────┬──────────┬────────┬────────┤
│ planner  │ browser  │distiller │ critic │formatter│
│ researcher│ retriever│  coder  │sandbox │summariser│
└──────────┴──────────┴──────────┴────────┴────────┘
    │                │
    │                ▼
    │     ┌─────────────────────┐
    │     │  Browser 4-Layer    │
    │     │  Cascade            │
    │     └─────────────────────┘
    ▼
┌──────────────────────────────────────────────────┐
│  persistence.py  │  recovery.py  │  memory.py    │
│  (NetworkX graph │  (failure     │  (FAISS       │
│   serialization) │   classification)│  vector index)│
└──────────────────────────────────────────────────┘
```

---

## Core Files

| File | Role |
|------|------|
| `code/flow.py` | Orchestrator — NetworkX DAG, node scheduling, parallel execution, 60-node cap |
| `code/skills.py` | Skill registry & dispatch; browser retry logic (2 retries, fresh instance); dynamic URL resolution from upstream (`_resolve_url_from_upstream`) |
| `code/agent_config.yaml` | Skill catalog — prompts, temperatures, tools, critic flags |
| `code/decision.py` | Single-turn decision layer (answer OR tool-call) |
| `code/perception.py` | Goal decomposition & observation tracking |
| `code/recovery.py` | Failure classification (transient/validation/upstream) & replan policy |
| `code/persistence.py` | Session state → JSON; NetworkX compat shim for `node_link_data` |
| `code/memory.py` | Long-term memory with FAISS vector index |
| `code/prompts/planner.md` | Planner system prompt with browser goal splitting rules + researcher→browser→distiller chain |
| `code/prompts/researcher.md` | Researcher prompt — 3 URLs required, 5 tool-call budget, all fetched URLs in `sources` |
| `code/prompts/critic.md` | Critic prompt with PASS BIAS section (pass partial data, fail only fabrication/omission) |

---

## Browser Skill — 4-Layer Cascade

The Browser skill (`code/browser/skill.py`) is the most complex component. It escalates through 4 layers until extraction succeeds:

```
Layer 1: Extract (trafilatura)     ← HTTP GET, no browser, ~2s
    │ empty/useless?
    ▼
Layer 2a: Deterministic            ← Playwright + caller selectors
    │ no selectors / failure?
    ▼
Layer 2b: A11y Driver              ← Text-only LLM, 20 steps max, ~10-170s
    │ insufficient?
    ▼
Layer 3: Vision (Set-of-Marks)     ← Screenshot + LLM vision, 20 steps max
    │ failure?
    ▼
Return error (all layers exhausted)
```

### Key Browser Files

| File | Purpose |
|------|---------|
| `code/browser/skill.py` | Cascade orchestration, Layer 1 fetch, `_enrich_with_ratings()`, gateway-block detection |
| `code/browser/driver.py` | `A11yDriver` (Layer 2b) and `SetOfMarksDriver` (Layer 3) — shared per-turn loop |
| `code/browser/dom.py` | DOM element enumeration via JS; context enrichment (ratings, prices, availability) |
| `code/browser/highlight.py` | Set-of-Marks screenshot annotation (numbered bounding boxes) |
| `code/browser/client.py` | Playwright browser lifecycle management |

### Browser Skill Key Features

**Layer 1 — trafilatura extract:**
- Simple HTTP GET + `trafilatura.extract()` (favor_recall=True)
- `_enrich_with_ratings(html, content)` — appends star ratings from CSS classes (`class="star-rating Five"`) that trafilatura strips
- `_is_useful_extract(content, goal)` — rejects if <200 chars or goal requires interaction (click, filter, scroll)

**Layer 2b — A11y Driver:**
- Text-only LLM interactions (no screenshots)
- Element legend: `[38]<a>Sapiens: A Brief History... [rating:Five] [price:£54.23] [avail:In stock]</a>`
- Max 20 steps, 3 consecutive failure cap
- Action vocabulary: click, type, key, scroll, drag, wait, done

**Layer 3 — Vision (Set-of-Marks):**
- Screenshots annotated with numbered boxes per interactive element
- LLM sees screenshot + element legend
- Max 20 steps

**Navigation Fix (`_dispatch` in driver.py):**
- Clicks on `<a>` tags use `page.expect_navigation(wait_until="domcontentloaded")` + `wait_for_load_state("load")`
- Prevents "Execution context was destroyed" race condition

**DOM Context Enrichment (dom.py):**
- JS enumeration walks parent containers for:
  - Star ratings: `.star-rating {One|Two|Three|Four|Five}`
  - Price: `.price_color`, `.amount`, `[itemprop=price]`
  - Availability: `.instock`, `.availability`
- Appends `[rating:X] [price:X] [avail:X]` to element names

**Gateway-Block Detection:**
- Checks for captcha, hCaptcha, reCAPTCHA, Cloudflare markers
- Returns `error_code="gateway_blocked"` → triggers replan

**Retry Logic (skills.py):**
- `_BROWSER_MAX_RETRIES = 2`
- Fresh `BrowserSkill` instance per retry
- Progressive backoff: 2s, 4s

---

## Planner — Browser Goal Splitting

The planner prompt (`code/prompts/planner.md`) enforces:

1. **Single-page-per-session rule** — Never ask one browser node to visit multiple pages
2. **Pattern (site-specific queries):**
   - Browser node 1: List/identify targets on a single page
   - Distiller: Extract structured data (titles, URLs)
   - Browser nodes 2–N: One per detail page (if listing lacks required fields)
3. **Pattern (open-ended queries — STANDARD FLOW):**
   - `researcher` → discovers relevant URLs + brief overview
   - Per-item `browser` nodes → each uses `url_from_input: true` + `url_index: N` to resolve the Nth URL from the researcher's `sources` array (no intermediate distiller needed for URL extraction)
   - `distiller` → structures browser outputs into uniform fields
   - `formatter` → renders the comparison table
4. **Direct URLs** — Use full absolute URLs for category/detail pages (not base URL + interactive navigation)
5. **Step budget** — Each browser session has ~20 actions; multi-page tasks will fail

---

## Dynamic URL Resolution (`skills.py`)

When a browser node has `metadata.url_from_input: true`, the orchestrator resolves the actual URL from upstream output before dispatching. Resolution strategies (tried in order):

| Strategy | Source | Typical Upstream |
|----------|--------|------------------|
| 0 | `output.sources[]` array (url/href fields or plain strings) | researcher |
| 1 | `output.fields.urls` / `detail_urls` / `book_urls` / `items` lists | distiller |
| 2 | Numbered URL fields in `output.fields` (`book_1_url`, `url_1`, etc.) | distiller |
| 3 | Any string value starting with `http` in `output.fields` | fallback |

If no URL is found at the requested `url_index`, a `ValueError("url_from_input: no URL at index N")` is raised. This is classified as `validation_error` by recovery.py (skip, not replan) — the upstream had fewer items than planned.

---

## Recovery & Critic System

**Failure Classification (`recovery.py`):**
| Reason | Markers | Action |
|--------|---------|--------|
| transient | 503, 502, 504, timeout, connection errors | skip (gateway already retried) |
| validation_error | malformed, ValidationError | skip (prompt bug) |
| validation_error | `"no code in upstream coder output"` | skip (sandbox has nothing to run) |
| validation_error | `"url_from_input: no url at index"` | skip (upstream had fewer URLs than planned) |
| upstream_failure | everything else (including empty error) | replan (new Planner node with prior results) |

**Critic Auto-Insertion:**
- Skills with `critic: true` in YAML (e.g., distiller) auto-get Critic nodes on outgoing edges
- **Skip logic:** Critic nodes are NOT inserted before `browser`, `researcher`, or `distiller` children — only before terminal consumers (e.g., `formatter`). This prevents critics from blocking the URL→browser chain unnecessarily.
- On `verdict: fail` → skip child, insert recovery Planner for target
- Per-target cap: 1 recovery attempt (prevents infinite critic loops)

**Critic PASS BIAS (`prompts/critic.md`):**
- PASS if upstream extracted what was available (even if some fields are "N/A")
- PASS if extra bonus fields are included beyond what was asked
- PASS if partial data matches what the inputs contained (e.g. 2 of 3 items)
- FAIL only for: fabricated data, clearly-present data completely omitted, or empty `{}` output

**Researcher Prompt (`prompts/researcher.md`):**
- Must return at least N distinct source URLs when query asks to "compare N items"
- Every fetched URL MUST appear in the `sources` array (downstream browser nodes discover URLs from this)
- Tool budget: 5 calls max (1 web_search + up to 4 fetch_url)

**Distiller Config:**
- `max_tokens: 4000` (raised from 1200 to prevent JSON truncation on multi-item structured output)

---

## Execution Model

- **Parallelism:** Independent nodes run concurrently (2.24x speedup observed)
- **Node Cap:** 60 nodes max (orchestrator hard stop)
- **Logging:** 8-point summary (BROWSER SUMMARY + TURN COUNT & COST SUMMARY per session)
- **Persistence:** Full session state serialized to `state/sessions/<session-id>/`
