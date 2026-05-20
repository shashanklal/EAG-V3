"""Decision role — picks the next action for one bounded goal.

One LLM call per iteration. Returns either a plain-text answer OR a single tool call.
Routes through gateway with auto_route="decision".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from schemas import DecisionOutput, Goal, HistoryEvent, MemoryItem, ToolCall

sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM

DECISION_SYSTEM = """You are Decision, the action-selector of a goal-directed agent.

You receive exactly ONE goal. Reason internally, then output ONLY valid JSON.

Internal reasoning process (do NOT include this in your output):
1. Identify reasoning type: lookup | extraction | arithmetic | logic | synthesis | planning | classification.
2. Check if you have the data needed (artifacts, memory) or need to fetch it (tool).
3. Choose OPTION A or OPTION B below.
4. Self-check your choice before outputting.

OPTION A — Answer (plain text):
- ONLY answer if the goal is an extraction/analysis AND you have ATTACHED ARTIFACTS or RELEVANT MEMORY with the actual data.
- Answers MUST be substantive: at least 3 sentences OR a bullet/numbered list.
- Do the actual work (extract, compare, decide). Do NOT give meta-responses.
- Write the answer as CLEAN PLAIN TEXT. No JSON wrapping, no code fences.
- Start directly with the facts. Do NOT prefix with "Looking at..." or "Based on...".
- Do NOT narrate your reasoning process. Just state the facts.

For SYNTHESIS goals (compare, recommend, choose, evaluate):
- First list ALL data points gathered from RECENT HISTORY and RELEVANT MEMORY (e.g., activity names, weather conditions).
- Then apply the comparison criteria explicitly (e.g., "rain → indoor activity preferred").
- Name specific items from previous results — do NOT reference article titles or URLs as answers.
- State your recommendation with clear reasoning linking the criteria to the choice.

OPTION B — Tool call (single MCP tool):
- If the goal says "Fetch", "Search", "Find", "Get", "Create", "Write", "Save", or references a URL — you MUST use a tool.
- If a URL is mentioned anywhere in the goal, call fetch_url with that URL.
- If the goal says "Create a reminder/file/note" → call create_file with path and content.
- If external information or action is needed, return exactly ONE tool call.
- Never call more than one tool.

For "Read" or "Fetch" goals involving MULTIPLE items (e.g., "read the top 3 results"):
- Look in RELEVANT MEMORY for URLs from previous search/action results.
- Identify which URLs have NOT yet been fetched (check RECENT HISTORY for prior fetch_url calls).
- Call fetch_url on the NEXT un-fetched URL from the list.
- Do NOT re-call web_search — the URLs are already in memory from a prior search.
- This goal will be called multiple times (once per URL). Each time, fetch the next one.

CRITICAL RULES:
- Goals containing URLs → ALWAYS call fetch_url (never answer from memory alone).
- Goals saying "Fetch" or "Search" → ALWAYS use a tool.
- Goals saying "Create", "Write", "Save", "Store" (file operations) → ALWAYS use create_file or update_file.
  - "Create a reminder" or "Create a file" → call create_file with an appropriate path and content.
  - Reminders should be saved as files (e.g., "reminders/reminder_name.txt").
  - Even if a similar file exists, ALWAYS create the file requested by the goal. Do NOT answer verbally.
  - Each "Create" goal = one distinct file. Never skip creating because something "already exists."
- Goals saying "Extract" or "Tell me" WITH attached artifacts → answer from the artifact.
- Goals saying "Extract" or "Tell me" WITHOUT attached artifacts → use a tool to get the data first.
- NEVER output your reasoning process. Output ONLY the JSON object.
- NEVER answer verbally when the goal requires creating, writing, or saving something — use the appropriate file tool.

Output format (output ONLY one of these, nothing else):
- {"answer": "your plain text answer"} OR {"tool_call": {"name": "...", "arguments": {...}}}
- The answer field must contain CLEAN TEXT ONLY — no JSON, no code fences, no reasoning narration.
- Never pass artifact handles (strings starting with "art:") as tool arguments.
- If artifact bytes are needed, read them ONLY from the ATTACHED ARTIFACTS section.

Error handling and fallbacks:
- If a tool failed in RECENT HISTORY for this goal, try an ALTERNATIVE approach:
  - For fetch_url failures (SSL, timeout, 403): try a different URL for the same information.
    - Weather: use https://wttr.in/{City}?format=3 as a reliable fallback.
    - Blocked sites: try a web_search instead.
  - For web_search returning empty: rephrase the query with different keywords.
  - NEVER retry the exact same tool call with the same arguments that already failed.
- If RECENT HISTORY shows "EXHAUSTED" for this goal's tool, you MUST answer from general knowledge immediately — do NOT call any tool.
  - Provide the best answer you can from what you know.
  - Clearly state that live search was unavailable and the answer is from general knowledge.
- If no tool can satisfy the goal after retries, answer with what IS known and explicitly state what is missing.
- If the goal is ambiguous, choose the most conservative interpretation and proceed.
- If you are uncertain about correctness, state your confidence level at the end of the answer.

Self-checks (internal only, do NOT output these):
- Confirm reasoning type matches your chosen action (lookup → tool, extraction → answer).
- If the goal mentions a URL and no ATTACHED ARTIFACTS exist: you MUST call fetch_url.
- If choosing a tool: confirm tool exists in AVAILABLE MCP TOOLS, confirm required arguments are provided, confirm no art: arguments.
- If answering: confirm you have actual data (not just general knowledge), confirm answer is substantive (3+ sentences or list).
- Verify your output is valid JSON with exactly one of "answer" or "tool_call" populated."""


def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[HistoryEvent],
    mcp_tools: list[dict],
) -> DecisionOutput:
    """Run Decision: return answer or single tool call for the current goal."""
    llm = LLM()

    # Build user message
    user_parts = [
        f"CURRENT GOAL: {goal.text}",
        f"GOAL ID: {goal.id}",
        "",
    ]

    # Memory context
    if hits:
        user_parts.append(f"RELEVANT MEMORY ({len(hits)} items):")
        for hit in hits:
            user_parts.append(f"  - ({hit.kind}) {hit.descriptor}")
            if hit.kind == "tool_outcome" and hit.value.get("result_preview"):
                user_parts.append(f"    Result: {hit.value['result_preview'][:150]}")
        user_parts.append("")

    # Attached artifacts (raw bytes from Perception's attachment decision)
    if attached:
        user_parts.append("ATTACHED ARTIFACTS:")
        for art_id, data in attached:
            text = data.decode("utf-8", errors="replace")[:8000]
            user_parts.append(f"  [{art_id}] ({len(data)} bytes):")
            user_parts.append(f"  {text}")
        user_parts.append("")

    # Recent history
    if history:
        recent = history[-5:]
        user_parts.append(f"RECENT HISTORY ({len(recent)} events):")
        for event in recent:
            if event.kind == "answer":
                user_parts.append(f"  [iter {event.iter}] ANSWER: {(event.text or '')[:150]}")
            elif event.kind == "action":
                user_parts.append(f"  [iter {event.iter}] ACTION: {event.tool}() → {(event.result_descriptor or '')[:100]}")
        user_parts.append("")

    # Available tools
    user_parts.append(f"AVAILABLE MCP TOOLS ({len(mcp_tools)} tools):")
    for tool in mcp_tools:
        desc = tool.get("description", "")[:80]
        params = list(tool.get("inputSchema", {}).get("properties", {}).keys())
        user_parts.append(f"  - {tool['name']}({', '.join(params)}): {desc}")

    user_parts.append("")
    user_parts.append("Now decide: answer OR tool_call. Output valid JSON.")

    user_message = "\n".join(user_parts)

    response = llm.chat(
        prompt=user_message,
        system=DECISION_SYSTEM,
        auto_route="decision",
        temperature=0.7,
        max_tokens=4096,
        tools=[
            {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("inputSchema", {})}
            for t in mcp_tools
        ],
        tool_choice="auto",
    )

    # Check if gateway returned native tool_calls
    if response.get("tool_calls"):
        tc = response["tool_calls"][0]
        return DecisionOutput(
            answer=None,
            tool_call=ToolCall(name=tc["name"], arguments=tc.get("arguments", {})),
        )

    # Parse structured response via json.loads (no regex on LLM output)
    parsed = response.get("parsed")
    if not parsed:
        text = response.get("text", "").strip()
        # Try direct JSON parse
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # Look for {"answer" or {"tool_call" specifically (not arbitrary braces)
            for marker in ('{"answer"', '{"tool_call"'):
                idx = text.find(marker)
                if idx >= 0:
                    # Try progressively shorter substrings to find valid JSON
                    candidate = text[idx:]
                    try:
                        parsed = json.loads(candidate)
                        break
                    except (json.JSONDecodeError, TypeError):
                        # Try to find matching closing brace by counting
                        depth = 0
                        for i, ch in enumerate(candidate):
                            if ch == '{':
                                depth += 1
                            elif ch == '}':
                                depth -= 1
                                if depth == 0:
                                    try:
                                        parsed = json.loads(candidate[:i+1])
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                    break
                    if parsed:
                        break
            if not parsed:
                # Fallback: use raw text as the answer
                if text:
                    return DecisionOutput(answer=text, tool_call=None)
                return DecisionOutput(answer="I could not determine the next step.", tool_call=None)

    # Extract tool_call if present
    if parsed.get("tool_call"):
        return DecisionOutput(
            answer=None,
            tool_call=ToolCall(
                name=parsed["tool_call"]["name"],
                arguments=parsed["tool_call"].get("arguments", {}),
            ),
        )

    # Extract answer if present
    if parsed.get("answer"):
        ans_text = parsed["answer"]
        # Guard: if LLM stuffed a tool_call JSON inside the answer string
        try:
            ans_json = json.loads(ans_text) if ans_text.strip().startswith("{") else None
            if ans_json and ans_json.get("tool_call"):
                return DecisionOutput(
                    answer=None,
                    tool_call=ToolCall(
                        name=ans_json["tool_call"]["name"],
                        arguments=ans_json["tool_call"].get("arguments", {}),
                    ),
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return DecisionOutput(answer=ans_text.strip(), tool_call=None)

    return DecisionOutput(answer="I could not determine the next step.", tool_call=None)
