"""Decision: one LLM call per turn.

Given the current goal, the relevant memory hits (descriptors only), the
recent history, and optionally the raw bytes of an artifact Perception
attached to this goal, the model picks ONE of:

  (a) answer in plain text — the answer may itself be summarisation,
      extraction, comparison, translation, or any other semantic work the
      LLM does on the attached content;
  (b) call exactly one MCP tool from the available tool list.

There is no taxonomy of "operation kinds". The model decides what it is
doing. Decision just routes the dispatch.
"""

from __future__ import annotations

import json

from gateway import LLM, ensure_gateway
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall

SYSTEM = (
    "You are the Decision layer of an agent.\n"
    "Inputs you receive: ONE current goal, the relevant memory snippets,\n"
    "recent history, and optionally the raw bytes of one attached artifact.\n\n"
    "Choose EXACTLY ONE response:\n"
    "  (a) Reply with the final answer to this goal as plain text. If the\n"
    "      goal asks you to summarise, extract, compare, or transform the\n"
    "      attached content, do that work inside your reply.\n"
    "  (b) Call exactly ONE tool from the available MCP tools when you need\n"
    "      external work (fetching, file ops, time, currency, web search).\n\n"
    "CRITICAL RULE — when ATTACHED ARTIFACTS is non-empty, you MUST choose\n"
    "option (a) — answer directly from the attached content. NEVER call a\n"
    "tool when you already have attached artifact bytes. The content you\n"
    "need is literally in front of you.\n\n"
    "Rules:\n"
    "- Never narrate. Answer or call a tool, never both.\n"
    "- Never invent a tool that is not in the tool list.\n"
    "- If the goal is already satisfied by the memory hits + history, answer\n"
    "  directly without calling a tool.\n"
    "- Artifact handles (strings starting with `art:`) are NOT file paths,\n"
    "  URLs, or tool arguments. NEVER pass an `art:...` value to read_file,\n"
    "  list_dir, fetch_url, or ANY other tool. If a goal needs the bytes of\n"
    "  an artifact, those bytes will already appear in the ATTACHED\n"
    "  ARTIFACTS section of your input — answer directly from that text.\n"
    "  WRONG:  read_file({\"path\": \"art:abc1234\"})\n"
    "  WRONG:  fetch_url({\"url\": \"art:abc1234\"})\n"
    "  RIGHT:  read the bytes already in ATTACHED ARTIFACTS and answer.\n"
    "- read_file and list_dir operate on the local sandbox/ directory, not\n"
    "  artifacts. Call them when: (a) the user asked you to read/list a file,\n"
    "  or (b) the user asks about personal info (reminders, notes, saved\n"
    "  data) and no answer is available yet from memory hits or search.\n"
    "  Personal data like reminders and notes are stored as sandbox files.\n"
    "- Answer using whatever is in front of you: memory hits, history, and\n"
    "  any attached artifact bytes. Be substantive — at least 3 sentences\n"
    "  or a list of items when the goal is to extract/list/select/compare.\n"
    "- For 'remember X', 'save X', 'set a reminder', 'note X' style goals,\n"
    "  call create_file (or update_file when re-saving) under the sandbox\n"
    "  with a filename describing the topic. Do NOT reply that you cannot\n"
    "  set reminders — create_file IS how you set them.\n"
    "- When the goal asks to make a file's or fetched content's contents\n"
    "  SEARCHABLE for later turns or runs (phrasings like 'index', 'ingest',\n"
    "  'make searchable', 'add to the knowledge base', 'load into memory'),\n"
    "  call `index_document`. `read_file` only returns the bytes once and\n"
    "  then discards them; `index_document` chunks the content and writes\n"
    "  the chunks into Memory so they survive across turns and runs. Use\n"
    "  `read_file` only for one-shot inspection of a known sandbox file.\n"
    "- When the goal asks to ANSWER a question and the MEMORY HITS already\n"
    "  contain `fact` items whose descriptors begin with `[sandbox:` or\n"
    "  `[art:` (those are previously-indexed chunks of source documents),\n"
    "  synthesise your answer DIRECTLY from the chunk text visible in\n"
    "  MEMORY HITS (shown under `chunk (source): ...`). Do NOT call\n"
    "  search_knowledge again — the relevant chunks are already in front\n"
    "  of you. If RECENT HISTORY shows search_knowledge was already called\n"
    "  AND returned useful content, you MUST answer now from that content.\n"
    "  Calling the same search twice is never useful.\n"
    "- HOWEVER: if search_knowledge returned 'No relevant knowledge found'\n"
    "  or produced no useful content, you SHOULD call web_search to find\n"
    "  the information online instead of answering with nothing.\n"
    "- If no chunk text is visible in MEMORY HITS and no search_knowledge\n"
    "  call appears in RECENT HISTORY, you may call search_knowledge ONCE\n"
    "  to retrieve the relevant chunks — but you must answer on the next\n"
    "  turn once results are available.\n"
    "- When calling search_knowledge, derive the query string from the USER\n"
    "  QUERY (shown at the top of your input), NOT from prior tool calls in\n"
    "  memory hits or history. The search query should reflect what the user\n"
    "  is actually asking about right now."
)

# How much attached content to send to the model per turn. Most LARGE-tier
# workers handle 30 KB comfortably; truncate above that and let the model
# work with a head-and-tail window.
ATTACH_HEAD = 20_000
ATTACH_TAIL = 10_000


def _format_hits(hits: list[MemoryItem]) -> str:
    # Surface enough of each hit's `value` for Decision to anchor on it.
    # NOTES_RUNS §6 (2) handled the classifier-fact case (`value.raw` such
    # as the birthday date). NOTES_FIX §3 extends this to indexed-chunk
    # facts: when a hit carries `value.chunk` (an indexed slice of a
    # document), the chunk body IS the answer material, and stripping it
    # leaves Decision unable to synthesise — it sees that chunks exist but
    # cannot read them, so it loops on `search_knowledge`. We render a
    # short chunk preview here so Decision can answer directly from the
    # memory-hit list when search_knowledge has already populated it.
    if not hits:
        return "  (none)"
    out = []
    for h in hits[:10]:
        line = f"  - [{h.kind}] {h.descriptor}"
        val = h.value or {}
        if val:
            raw = val.get("raw")
            chunk = val.get("chunk")
            if isinstance(raw, str) and raw.strip():
                line += f"\n      raw: {raw[:200]}"
            elif isinstance(chunk, str) and chunk.strip():
                src = val.get("source") or ""
                # Strip markdown link syntax and common HTML noise to
                # surface the actual textual content in the preview.
                import re
                clean = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', chunk)
                clean = re.sub(r'[#*_|>!]', '', clean)
                clean = re.sub(r'\s+', ' ', clean).strip()
                # Skip arXiv page boilerplate — jump to title or abstract.
                for marker in ('Title:', 'Abstract:', 'abstract:'):
                    mi = clean.find(marker)
                    if mi > 0:
                        clean = clean[mi:]
                        break
                preview = clean[:800]
                more = "…" if len(clean) > 800 else ""
                line += f"\n      chunk ({src}): {preview}{more}"
            else:
                # For tool_outcome items, show result_preview (up to 800 chars)
                rp = val.get("result_preview")
                if isinstance(rp, str) and rp.strip():
                    line += f"\n      result: {rp[:800]}"
                else:
                    compact = {
                        k: v for k, v in val.items()
                        if k != "chunk" and not (isinstance(v, str) and len(v) > 200)
                    }
                    if compact:
                        line += f"\n      value: {json.dumps(compact)[:240]}"
        out.append(line)
    return "\n".join(out)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "  (empty)"
    lines = []
    for h in history[-6:]:
        kind = h.get("kind", "?")
        if kind == "answer":
            lines.append(f"  - iter {h.get('iter')}: ANSWER → {(h.get('text') or '')[:140]}")
        elif kind == "action":
            tool = h.get("tool")
            # agent7.py stores up to 1500 chars of result_descriptor so
            # Decision can see full web_search/search_knowledge responses
            # for synthesis goals.
            desc = h.get("result_descriptor", "")[:1500]
            art = f" (artifact {h['artifact_id']})" if h.get("artifact_id") else ""
            lines.append(f"  - iter {h.get('iter')}: {tool}{art} → {desc}")
        else:
            lines.append(f"  - iter {h.get('iter')}: {kind} {h}")
    return "\n".join(lines)


def _format_attached(attached: list[tuple[str, bytes]]) -> str:
    if not attached:
        return ""
    parts = ["\n\nATTACHED ARTIFACTS:"]
    for art_id, data in attached:
        text = data.decode("utf-8", errors="replace")
        if len(text) > ATTACH_HEAD + ATTACH_TAIL + 50:
            text = (
                text[:ATTACH_HEAD]
                + f"\n\n...[truncated; full size {len(data)} bytes]...\n\n"
                + text[-ATTACH_TAIL:]
            )
        parts.append(f"--- {art_id} ---\n{text}")
    return "\n".join(parts)


def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
    *,
    query: str = "",
) -> DecisionOutput:
    ensure_gateway()

    prompt = (
        f"USER QUERY:\n  {query}\n\n"
        f"GOAL:\n  {goal.text}\n\n"
        f"MEMORY HITS:\n{_format_hits(hits)}\n\n"
        f"RECENT HISTORY:\n{_format_history(history)}"
        f"{_format_attached(attached)}"
    )

    # When an artifact is attached AND the goal is synthesis, the model
    # has all the content it needs to produce an answer. Remove tools to
    # force answer-only mode. Also force answer mode for synthesis goals
    # when search_knowledge was already called (prevents infinite re-search
    # loops). Bulk/action goals (index, fetch, list) keep tools even when
    # an artifact is attached — the artifact is context, not answer material.
    SYNTHESIS_KW = (
        "answer", "tell", "summarise", "summarize", "explain", "describe",
        "compare", "evaluate", "select", "recommend", "identify",
        "determine", "report", "extract", "synthes",
    )
    goal_lc = goal.text.lower()
    is_synthesis_goal = any(kw in goal_lc for kw in SYNTHESIS_KW) or "?" in goal.text
    search_already_ran = any(
        h.get("tool") == "search_knowledge" for h in history
    )
    # Only force answer if search returned actual results (not "no knowledge found")
    search_had_results = search_already_ran and any(
        h.get("tool") == "search_knowledge"
        and "No relevant knowledge found" not in (h.get("result_descriptor") or "")
        for h in history
    )
    force_answer = is_synthesis_goal and (attached or search_had_results)

    effective_tools = mcp_tools if not force_answer else []
    tool_choice = "auto" if effective_tools else "none"

    # When forced to answer (no tools), inject a directive so the model
    # doesn't attempt to pick option (b) from the SYSTEM prompt.
    if force_answer and not attached:
        prompt += (
            "\n\n--- IMPORTANT ---\n"
            "No tools are available this turn. You MUST respond with option (a) — "
            "a direct, substantive plain-text answer. If the information is not in "
            "the memory hits or history above, clearly state that the requested "
            "information is not available in the knowledge base. Do NOT output "
            "'(b) Call ...' or any tool-call syntax."
        )

    reply = LLM().chat(
        prompt=prompt,
        system=SYSTEM,
        cache_system=True,
        tools=effective_tools or None,
        tool_choice=tool_choice,
        auto_route="decision",
        temperature=0,
        max_tokens=2048,
    )

    tcs = reply.get("tool_calls") or []
    if tcs:
        tc = tcs[0]
        return DecisionOutput(
            tool_call=ToolCall(
                name=tc["name"],
                arguments=tc.get("arguments") or {},
            )
        )
    return DecisionOutput(answer=(reply.get("text") or "").strip())
