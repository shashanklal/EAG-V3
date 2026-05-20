"""Perception role — the orchestrator.

One LLM call per iteration. Observes query, memory hits, history, and prior goals.
Emits an Observation with updated goal list (done flags, artifact attachments).
Pinned to provider="g" (Gemini) for reliability.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from schemas import Goal, HistoryEvent, MemoryItem, Observation

sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM

PERCEPTION_SYSTEM = """You are Perception, the orchestrator of a goal-directed agent.

Your job each iteration: observe state, reason about progress, and emit updated goals.
Reason internally, then output ONLY valid JSON matching the Observation schema.

Internal reasoning process (do NOT include in output):
1. Identify iteration type: decomposition (first iter) | status-tracking (subsequent iters).
2. Inspect history events to determine what has been accomplished.
3. Apply the rules below to update goals.
4. Self-check before outputting.

Goal Decomposition (only when prior_goals is empty):
- Decompose the user query into MINIMAL bounded goals (short imperative statements).
- CRITICAL: Use the FEWEST goals possible. Combine related extractions into ONE goal.
- A fetch/search action = one goal. ALL extractions from that fetch = one goal.
- Example: "Fetch X and tell me birth date, death date, and contributions" → EXACTLY 2 goals:
  Goal 1: "Fetch the Wikipedia page for X"
  Goal 2: "Extract birth date, death date, and three contributions"
- NEVER split extractions from the same source into separate goals.
- Typical query needs 2-3 goals, rarely more.

Goal Status Update (when prior_goals exist):
- For each existing goal, inspect the history carefully.
- A fetch/search goal is DONE when an ACTION event shows the tool was called successfully.
- An extraction/answer goal is DONE when an ANSWER event for that goal appears in history.
- Once a goal is done, it MUST remain done (sticky done flags).
- Do NOT mark a goal done unless the history explicitly shows completion.

Artifact Attachment (for the first unfinished goal only):
- If the first unfinished goal requires content from a fetched page, set attach_artifact_id.
- Use the artifact_id string directly from the AVAILABLE ARTIFACTS section.
- Only ONE goal may have an attachment at a time.
- IMPORTANT: If a prior goal created an artifact (via fetch_url/web_search), attach it to the extraction goal.

Preservation Rules:
- Do NOT reorder goals.
- Do NOT insert new goals in the middle.
- Do NOT drop goals.
- Preserve goal count and ordering from prior_goals.

Error handling and fallbacks:
- If history shows a tool failure (error or empty result), keep the goal OPEN so Decision can retry.
- If an artifact descriptor suggests failure, do NOT attach it.
- If the query is ambiguous, decompose conservatively (fewer goals).
- If history is contradictory, keep the goal OPEN.

Self-checks (internal only, do NOT output these):
- Confirm iteration type matches state (decomposition vs. status-tracking).
- Goal count is MINIMAL (2-3 for most queries).
- Goal order is stable (matches prior_goals ordering).
- Done flags are sticky (never un-done a previously done goal).
- Attachment only on first unfinished goal.
- Attachment references an artifact present in AVAILABLE ARTIFACTS.
- Output is valid JSON with "goals" array.

Output must be valid JSON matching the Observation schema. Do NOT output reasoning or explanation."""


def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[HistoryEvent],
    prior_goals: list[Goal],
    run_id: str,
) -> Observation:
    """Run Perception: emit updated goal list with done flags and attachments."""
    llm = LLM()

    # Build indexed artifact list for safe referencing
    artifact_index = []
    for i, hit in enumerate(hits):
        if hit.artifact_id:
            artifact_index.append({
                "artifact_index": i,
                "artifact_id": hit.artifact_id,
                "descriptor": hit.descriptor,
            })

    # Build user message
    user_parts = [
        f"USER QUERY: {query}",
        "",
        f"MEMORY HITS ({len(hits)} items):",
    ]
    for i, hit in enumerate(hits):
        art_note = f" [has artifact: index={i}, id={hit.artifact_id}]" if hit.artifact_id else ""
        user_parts.append(f"  [{i}] ({hit.kind}) {hit.descriptor}{art_note}")

    user_parts.append("")
    user_parts.append(f"HISTORY ({len(history)} events):")
    for event in history[-10:]:
        if event.kind == "answer":
            user_parts.append(f"  [iter {event.iter}] ANSWER for goal {event.goal_id}: {(event.text or '')[:200]}")
        elif event.kind == "action":
            user_parts.append(f"  [iter {event.iter}] ACTION for goal {event.goal_id}: {event.tool}({event.arguments or {}}) → {(event.result_descriptor or '')[:100]}")

    user_parts.append("")
    if prior_goals:
        user_parts.append(f"PRIOR GOALS ({len(prior_goals)} goals):")
        for g in prior_goals:
            status = "DONE" if g.done else "OPEN"
            user_parts.append(f"  [{status}] id={g.id}: {g.text}")
    else:
        user_parts.append("PRIOR GOALS: (none — first iteration, decompose the query into goals)")

    if artifact_index:
        user_parts.append("")
        user_parts.append("AVAILABLE ARTIFACTS:")
        for a in artifact_index:
            user_parts.append(f"  index={a['artifact_index']}: {a['artifact_id']} — {a['descriptor']}")

    user_parts.append("")
    user_parts.append("Now emit the Observation JSON with the updated goal list.")

    user_message = "\n".join(user_parts)

    # Schema for structured output
    goal_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "done": {"type": "boolean"},
            "attach_artifact_id": {"type": ["string", "null"]},
        },
        "required": ["id", "text", "done", "attach_artifact_id"],
        "additionalProperties": False,
    }
    observation_schema = {
        "type": "object",
        "properties": {
            "goals": {"type": "array", "items": goal_schema},
        },
        "required": ["goals"],
        "additionalProperties": False,
    }

    response = llm.chat(
        prompt=user_message,
        system=PERCEPTION_SYSTEM,
        provider="g",
        temperature=1.0,
        max_tokens=2048,
        response_format={"type": "json_schema", "schema": observation_schema, "name": "Observation", "strict": True},
    )

    parsed = response.get("parsed") or json.loads(response.get("text", '{"goals": []}'))

    goals = []
    for g in parsed.get("goals", []):
        # Map artifact_index to real artifact_id if needed
        attach_id = g.get("attach_artifact_id")
        if attach_id and attach_id.isdigit():
            idx = int(attach_id)
            if idx < len(artifact_index):
                attach_id = artifact_index[idx]["artifact_id"]
            else:
                attach_id = None
        goals.append(Goal(
            id=g.get("id", ""),
            text=g.get("text", ""),
            done=g.get("done", False),
            attach_artifact_id=attach_id,
        ))

    # Enforce sticky done flags from prior_goals
    if prior_goals:
        for i, pg in enumerate(prior_goals):
            if pg.done and i < len(goals):
                goals[i].done = True

    # Safety: on first iteration (no prior_goals), no goals can be done
    # because nothing has been executed yet
    if not prior_goals:
        for g in goals:
            g.done = False

    # Strict done validation: override LLM done flags with history evidence.
    # A goal can only be done if:
    #   1. It was already done in prior_goals (handled above), OR
    #   2. There's a matching history event for this goal_id
    # This prevents the LLM from prematurely marking goals done.
    import re as _re
    SYNTHESIS_VERBS = {"determine", "choose", "recommend", "analyze", "compare", "evaluate", "which", "select", "decide", "identify", "summarize", "explain", "tell", "give", "list", "describe", "based", "extract"}
    MULTI_ACTION_VERBS = {"read", "download"}
    # Action goals can ONLY be done by an action event (not an answer)
    # Includes file-creation verbs — "Create a file" must be confirmed by a tool action, not a verbal answer
    ACTION_ONLY_VERBS = {"fetch", "search", "check", "download", "get", "create", "write", "save", "store"}
    for i, g in enumerate(goals):
        # Skip goals already confirmed done from prior_goals
        if prior_goals and i < len(prior_goals) and prior_goals[i].done:
            continue
        # Check if this is a synthesis goal (requires answer, not just action)
        goal_words = set(g.text.lower().split())
        is_synthesis = bool(goal_words & SYNTHESIS_VERBS)
        # Check if this is an action-only goal (can only be done by a tool action, not an answer)
        # Only flag as action-only if the goal STARTS with an action verb
        # (e.g., "Fetch the page" is action-only, but "Extract info from the search" is NOT)
        first_word = g.text.lower().split()[0] if g.text else ""
        is_action_only = first_word in ACTION_ONLY_VERBS
        # Check if this is a multi-action goal (e.g., "read top 3 results")
        # Only apply if the goal STARTS with a multi-action verb
        required_actions = 1
        if first_word in MULTI_ACTION_VERBS:
            num_match = _re.search(r'\b(\d+)\b', g.text)
            if num_match:
                required_actions = int(num_match.group(1))
        # Check if history supports this goal being done
        has_evidence = False
        success_action_count = 0
        for event in history:
            if event.goal_id == g.id:
                if event.kind == "action":
                    # Synthesis goals cannot be satisfied by actions alone
                    # BUT we still count successful actions for multi-action tracking
                    rd = event.result_descriptor or ""
                    # Exclude failed/empty/blocked results from counting as evidence
                    if (rd.startswith("Tool '") or rd.startswith("ERROR:") or
                        rd.startswith("BLOCKED:") or "Error executing tool" in rd or
                        "error" in rd.lower()[:30]):
                        continue
                    # Exclude HTTP 4xx/5xx responses (e.g., 404 Not Found)
                    if _re.search(r'"status":\s*(4|5)\d{2}', rd):
                        continue
                    # EXHAUSTED means the tool gave up — goal should stay OPEN
                    if rd.startswith("EXHAUSTED:"):
                        continue
                    success_action_count += 1
                    # For non-synthesis goals, actions alone can satisfy
                    if not is_synthesis and success_action_count >= required_actions:
                        has_evidence = True
                        break
                elif event.kind == "answer":
                    # Action-only goals cannot be satisfied by answers
                    if is_action_only:
                        continue
                    # For multi-action + synthesis goals, answer only counts
                    # if required actions have been completed first
                    if required_actions > 1 and success_action_count < required_actions:
                        continue
                    has_evidence = True
                    break
        # If no evidence, force it open regardless of what the LLM said
        if not has_evidence:
            g.done = False
        else:
            g.done = True

    # Force-attach safety net: if first unfinished goal has no attachment but
    # a valid artifact (art:...) exists in hits, attach it automatically.
    # ONLY attach to extraction/synthesis goals — NOT to fetch/search/action goals.
    ACTION_GOAL_VERBS = {"fetch", "search", "find", "get", "check", "download", "look"}
    first_open = next((g for g in goals if not g.done), None)
    if first_open and not first_open.attach_artifact_id and artifact_index:
        # Check if this goal is an action goal (should NOT receive artifacts)
        first_open_words = set(first_open.text.lower().split())
        is_action_goal = bool(first_open_words & ACTION_GOAL_VERBS)
        if not is_action_goal:
            # Only attach real artifact IDs (start with "art:")
            valid_arts = [a for a in artifact_index if a["artifact_id"].startswith("art:")]
            if valid_arts:
                # For synthesis goals, attach the most recent artifact (last in list)
                first_open.attach_artifact_id = valid_arts[-1]["artifact_id"]

    # Strip attachments from action goals (they need tools, not artifacts)
    for g in goals:
        if g.attach_artifact_id:
            g_words = set(g.text.lower().split())
            if g_words & ACTION_GOAL_VERBS:
                g.attach_artifact_id = None

    # Strip any attach_artifact_id that isn't a real artifact handle
    for g in goals:
        if g.attach_artifact_id and not g.attach_artifact_id.startswith("art:"):
            g.attach_artifact_id = None

    return Observation(goals=goals)
