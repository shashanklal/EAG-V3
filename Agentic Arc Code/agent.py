"""Main agent loop — goal-iterating agent with Memory, Perception, Decision, Action.

Usage:
    python agent.py "Your query here"

Requires:
    - MCP server running (python mcp_server.py via stdio)
    - LLM Gateway V3 running at http://localhost:8101
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import action
import perception
from artifacts import ArtifactStore
from decision import next_step
from memory import Memory
from schemas import Goal

MAX_ITERATIONS = 10


def _generate_goal_ids(goals: list[Goal], prior_goals: list[Goal]) -> list[Goal]:
    """Assign stable IDs to goals. Reuse prior IDs by position; generate new ones for new goals."""
    for i, goal in enumerate(goals):
        if i < len(prior_goals):
            goal.id = prior_goals[i].id
        elif not goal.id or goal.id == "":
            goal.id = f"g{i+1}_{uuid.uuid4().hex[:4]}"
    return goals


async def run(query: str) -> str:
    """Execute the full agent loop for a user query."""
    run_id = uuid.uuid4().hex[:8]
    history: list[dict] = []
    prior_goals: list[Goal] = []

    # Initialize services
    memory = Memory()
    artifacts = ArtifactStore()

    # Durable memory contract: remember the user query
    print(f"[memory.remember] storing user query")
    memory.remember(query, source="user_query", run_id=run_id)

    # Connect to MCP server
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).parent / "mcp_server.py")],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Load tool specs
            tools_result = await session.list_tools()
            mcp_tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                }
                for t in tools_result.tools
            ]
            print(f"[mcp] loaded {len(mcp_tools)} tools: {[t['name'] for t in mcp_tools]}")

            # Main loop
            failed_calls: dict[str, int] = {}  # "tool:args_hash" → retry count
            tool_fail_counts: dict[str, int] = {}  # "goal_id:tool_name" → total fail count
            for it in range(1, MAX_ITERATIONS + 1):
                print(f"\n--- iter {it} {'-'*40}")

                # 1. Read memory
                hits = memory.read(query, history)
                print(f"[memory.read]   {len(hits)} hits")

                # 2. Perception: observe and update goals
                obs = perception.observe(query, hits, history, prior_goals, run_id)
                obs.goals = _generate_goal_ids(obs.goals, prior_goals)
                prior_goals = obs.goals

                # Print goal status
                for i, g in enumerate(obs.goals):
                    status = "done" if g.done else "open"
                    prefix = "[perception]    " if i == 0 else "                "
                    attach_note = f"\n                  attach={g.attach_artifact_id}" if g.attach_artifact_id and not g.done else ""
                    print(f"{prefix}[{status}] {g.text}{attach_note}")

                # 3. Check if all goals done
                if all(g.done for g in obs.goals):
                    print(f"\n[done] all {len(obs.goals)} goals satisfied")
                    break

                # 4. Find first unfinished goal
                goal = next((g for g in obs.goals if not g.done), None)
                if not goal:
                    break

                # 5. Load attached artifact bytes if Perception attached one
                # For synthesis goals, attach ALL available artifacts so Decision has full context
                attached: list[tuple[str, bytes]] = []
                if goal.attach_artifact_id and artifacts.exists(goal.attach_artifact_id):
                    art_bytes = artifacts.get_bytes(goal.attach_artifact_id)
                    if art_bytes:
                        attached.append((goal.attach_artifact_id, art_bytes))
                        print(f"[attach]        {goal.attach_artifact_id} ({len(art_bytes)} bytes)")
                # Also attach any other artifacts from memory hits for synthesis goals
                goal_words = set(goal.text.lower().split())
                synthesis_verbs = {"determine", "choose", "recommend", "analyze", "compare", "evaluate", "which", "select", "decide"}
                if goal_words & synthesis_verbs:
                    for hit in hits:
                        if hit.artifact_id and hit.artifact_id != goal.attach_artifact_id and artifacts.exists(hit.artifact_id):
                            art_bytes = artifacts.get_bytes(hit.artifact_id)
                            if art_bytes:
                                attached.append((hit.artifact_id, art_bytes))
                                print(f"[attach]        {hit.artifact_id} ({len(art_bytes)} bytes)")

                # 6. Decision: next step for this goal
                out = next_step(goal, hits, attached, history, mcp_tools)

                # 7. Handle Decision output
                if out.answer:
                    preview = out.answer[:150].replace('\n', ' ')
                    print(f"[decision]      ANSWER: {preview}...")
                    history.append({
                        "iter": it,
                        "kind": "answer",
                        "goal_id": goal.id,
                        "text": out.answer,
                    })
                elif out.tool_call:
                    # Dedup check: detect repeated identical failing calls
                    import hashlib
                    call_key = f"{out.tool_call.name}:{hashlib.md5(str(sorted(out.tool_call.arguments.items())).encode()).hexdigest()}"
                    goal_tool_key = f"{goal.id}:{out.tool_call.name}"

                    # Check if this exact call already failed twice
                    if call_key in failed_calls and failed_calls[call_key] >= 2:
                        print(f"[dedup]         blocked repeated failing call: {out.tool_call.name}({out.tool_call.arguments})")
                        history.append({
                            "iter": it,
                            "kind": "action",
                            "goal_id": goal.id,
                            "tool": out.tool_call.name,
                            "arguments": out.tool_call.arguments,
                            "result_descriptor": f"BLOCKED: This exact call has failed {failed_calls[call_key]} times. You MUST try a different query, different URL, or alternative tool.",
                            "artifact_id": None,
                        })
                        tool_fail_counts[goal_tool_key] = tool_fail_counts.get(goal_tool_key, 0) + 1
                        # If this tool has failed 3+ times total for this goal, force the goal to answer from knowledge
                        if tool_fail_counts.get(goal_tool_key, 0) >= 3:
                            print(f"[fallback]      tool '{out.tool_call.name}' exhausted for goal '{goal.id}' — forcing answer from knowledge")
                            history.append({
                                "iter": it,
                                "kind": "action",
                                "goal_id": goal.id,
                                "tool": out.tool_call.name,
                                "arguments": {},
                                "result_descriptor": f"EXHAUSTED: Tool '{out.tool_call.name}' has failed {tool_fail_counts[goal_tool_key]} times for this goal. Answer from general knowledge or skip to next goal.",
                                "artifact_id": None,
                            })
                        continue

                    print(f"[decision]      TOOL_CALL: {out.tool_call.name}({out.tool_call.arguments})")

                    # 8. Action: execute tool call
                    result_text, art_id = await action.execute(session, out.tool_call, artifacts)
                    print(f"[action]        → {result_text[:100]}")

                    # Track failed calls for dedup
                    is_failure = (
                        result_text.startswith("Tool '") or
                        result_text.startswith("ERROR:") or
                        "Error executing tool" in result_text or
                        "error" in result_text[:50].lower()
                    )
                    if is_failure:
                        failed_calls[call_key] = failed_calls.get(call_key, 0) + 1
                        tool_fail_counts[goal_tool_key] = tool_fail_counts.get(goal_tool_key, 0) + 1
                        # If this tool has failed 3+ times total for this goal, inject exhaustion notice
                        if tool_fail_counts[goal_tool_key] >= 3:
                            print(f"[fallback]      tool '{out.tool_call.name}' exhausted for goal '{goal.id}' — forcing answer from knowledge")
                            history.append({
                                "iter": it,
                                "kind": "action",
                                "goal_id": goal.id,
                                "tool": out.tool_call.name,
                                "arguments": out.tool_call.arguments,
                                "result_descriptor": f"EXHAUSTED: Tool '{out.tool_call.name}' has failed {tool_fail_counts[goal_tool_key]} times for this goal. Answer from general knowledge or skip to next goal.",
                                "artifact_id": None,
                            })
                    else:
                        # Success — remove from failure tracker
                        failed_calls.pop(call_key, None)
                    print(f"[action]        → {result_text[:100]}")

                    # Record outcome in memory
                    memory.record_outcome(
                        tool_call=out.tool_call,
                        result_text=result_text,
                        artifact_id=art_id,
                        source=f"action:iter{it}",
                        run_id=run_id,
                        goal_id=goal.id,
                    )

                    history.append({
                        "iter": it,
                        "kind": "action",
                        "goal_id": goal.id,
                        "tool": out.tool_call.name,
                        "arguments": out.tool_call.arguments,
                        "result_descriptor": result_text[:200],
                        "artifact_id": art_id,
                    })
            else:
                print(f"\n[warn] max iterations ({MAX_ITERATIONS}) reached")

    # Extract final answer from history
    return _final_answer(history)


def _final_answer(history: list[dict]) -> str:
    """Synthesize final answer from all answer events in history."""
    answers = [e["text"] for e in history if e.get("kind") == "answer" and e.get("text", "").strip()]
    if not answers:
        # No verbal answers — summarize from actions (file creations, searches, etc.)
        actions = [e for e in history if e.get("kind") == "action"]
        if actions:
            # Try to extract file paths from create_file results
            import re as _re
            created_files = []
            for a in actions:
                desc = a.get("result_descriptor", "")
                path_match = _re.search(r'"path":\s*"([^"]+)"', desc)
                if path_match and "ok" in desc:
                    created_files.append(path_match.group(1))
            if created_files:
                unique = list(dict.fromkeys(created_files))  # deduplicate preserving order
                return "Done. Created files:\n" + "\n".join(f"  - {f}" for f in unique)
            return f"Completed {len(actions)} actions successfully."
        return "No answer was produced."

    # If there's only one answer, return it directly
    if len(answers) == 1:
        return answers[0]

    # Multiple answers: combine them (the last answer is typically the most complete)
    # Check if the last answer already contains all information
    last = answers[-1]
    if len(last) > 200:
        return last

    # Otherwise combine all unique answers
    return "\n\n".join(answers)


async def main():
    TEST_QUERIES = {
        "A": 'Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.',
        "B": 'Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday\'s weather forecast there and tell me which one is most appropriate.',
        "C1": 'My mom\'s birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.',
        "C2": 'When is mom\'s birthday?',
        "D": 'Search for \'Python asyncio best practices\', read the top 3 results, and give me a short numbered list of the advice they agree on.',
    }

    if len(sys.argv) < 2:
        print("Usage: python agent.py <A|B|C|D|\"custom query\">")
        print("\nTest Queries:")
        for key, q in TEST_QUERIES.items():
            print(f"  {key}: {q[:80]}...")
        sys.exit(1)

    arg = sys.argv[1].upper()
    if arg in TEST_QUERIES:
        query = TEST_QUERIES[arg]
    else:
        query = " ".join(sys.argv[1:])

    print(f"Query: {query}\n")

    result = await run(query)
    print(f"\nFINAL: {result}")


if __name__ == "__main__":
    asyncio.run(main())
