"""Session 8 — growing-graph orchestrator.

The agent's loop becomes a NetworkX DiGraph. Each node is a skill; edges
carry typed AgentResult payloads. The graph GROWS at runtime via five
actors: the Planner's seed plan, dynamic successors from any skill,
static `internal_successors` from the yaml, Critic auto-insertion on
edges out of `critic:true` skills, and Planner re-invocation on node
failure (gated by `recovery.plan_recovery`). Perception's tool-blindness
contract from S7 is preserved — Planner names skills, never tools.

Persistence lives in persistence.py; skill execution in skills.py;
failure-policy in recovery.py; sandbox in sandbox.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import networkx as nx

import memory as memory_svc
from gateway import ensure_gateway
from persistence import SessionStore
from recovery import handle_critic_verdict, plan_recovery
from schemas import AgentResult, NodeState
from skills import SkillRegistry, run_skill

MAX_NODES = 60  # hard cap so a Planner loop cannot grow forever

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def _has_meaningful_answer(text: str | None) -> bool:
    if not isinstance(text, str):
        return False
    t = text.strip()
    return bool(t and t not in {"{}", "[]", "null", '""'})


def _summarize_output(skill: str, output: dict, error: str | None = None) -> str:
    if not isinstance(output, dict):
        return error or "No result was produced."

    if skill == "computer":
        task = str(output.get("task") or "computer task")
        goal = str(output.get("goal") or "")
        path = str(output.get("path") or "")
        distance = str(output.get("distance") or "")
        url = str(output.get("url") or "")
        parts = [f"Computer skill completed {task}."]
        if goal:
            parts.append(f"Goal: {goal}.")
        if distance:
            parts.append(f"Extracted result: {distance}.")
        if path:
            parts.append(f"Layer used: {path}.")
        if url:
            parts.append(f"Source: {url}.")
        return " ".join(parts)

    if skill == "browser":
        goal = str(output.get("goal") or "")
        content = str(output.get("content") or "").strip()
        if content:
            content = content[:500]
        path = str(output.get("path") or "")
        url = str(output.get("url") or "")
        parts = ["Browser skill completed."]
        if goal:
            parts.append(f"Goal: {goal}.")
        if content:
            parts.append(f"Summary: {content}")
        if path:
            parts.append(f"Layer used: {path}.")
        if url:
            parts.append(f"Source: {url}.")
        return " ".join(parts)

    for key in ("final_answer", "summary", "recommendation", "stdout"):
        val = output.get(key)
        if isinstance(val, str) and _has_meaningful_answer(val):
            return val.strip()

    if output.get("fields"):
        return json.dumps(output.get("fields"), ensure_ascii=False)

    if error:
        return error

    compact = json.dumps(output, ensure_ascii=False)
    return compact[:600] if compact else "No result was produced."


class _Tee:
    """Write to both the original stream and a log file."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        self._stream.write(data)
        self._log.write(data)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    # Delegate everything else to the original stream so libraries
    # checking for .encoding, .isatty(), etc. still work.
    def __getattr__(self, name):
        return getattr(self._stream, name)


# ── Graph ────────────────────────────────────────────────────────────────────

class Graph:
    """NetworkX DiGraph wrapper. Nodes are str ids `n:<i>`; each node carries
    `skill`, `inputs` (list of str), and `status`."""

    def __init__(self):
        self.g = nx.DiGraph()
        self._counter = 0

    def add_node(self, skill: str, inputs: list[str], metadata: dict | None = None) -> str:
        self._counter += 1
        nid = f"n:{self._counter}"
        self.g.add_node(nid, skill=skill, inputs=list(inputs),
                        metadata=dict(metadata or {}), status="pending")
        for inp in inputs:
            if inp.startswith("n:") and inp in self.g.nodes:
                self.g.add_edge(inp, nid)
        return nid

    def mark(self, nid: str, status: str) -> None:
        self.g.nodes[nid]["status"] = status

    def ready_nodes(self) -> list[str]:
        # A predecessor counts as "satisfied" when it is either complete or
        # skipped (the latter is how a Critic-fail removes a child from the
        # critical path without blocking unrelated branches downstream).
        out = []
        for nid, d in self.g.nodes(data=True):
            if d["status"] != "pending":
                continue
            preds = list(self.g.predecessors(nid))
            if all(self.g.nodes[p]["status"] in ("complete", "skipped") for p in preds):
                out.append(nid)
        return out

    def has_running(self) -> bool:
        return any(d["status"] == "running" for _, d in self.g.nodes(data=True))

    def extend_from(self, src_nid: str, result: AgentResult,
                    *, registry: SkillRegistry) -> list[str]:
        """Splice in dynamic successors, static internal_successors, and
        critic auto-insertion. Returns the list of new node ids.

        Resolves label-based input references (`n:<label>`) against the
        `metadata.label` of nodes added in the same batch. The Planner is
        encouraged to name its nodes by label so it can reference them
        without knowing the integer ids the orchestrator will hand out."""
        added: list[str] = []
        src_def = registry.get(self.g.nodes[src_nid]["skill"])

        # Pass 1: add the new nodes; build a label → assigned-id map.
        label_to_id: dict[str, str] = {}
        pending: list[tuple[str, list[str]]] = []
        for spec in result.successors:
            label = (spec.metadata or {}).get("label")
            new_id = self.add_node(spec.skill, inputs=[],
                                   metadata=spec.metadata)
            added.append(new_id)
            if isinstance(label, str) and label:
                label_to_id[label] = new_id
            pending.append((new_id, list(spec.inputs)))

        # Pass 2: resolve inputs now that every sibling has an id. Translate
        # `n:<label>` to `n:<assigned-id>` if the label matches; pass numeric
        # `n:<i>` references through; pass anything else through unchanged.
        # NOTE: an empty `raw_inputs` is now a legitimate Planner signal for
        # a fan-out worker scoped via `metadata.question` (see planner.md).
        # We do NOT substitute the parent in that case — doing so would dump
        # the parent's full output (which for the Planner contains every
        # sibling's question) back into the worker's INPUTS block and undo
        # the scoping. The structural parent edge is preserved separately
        # below so the graph topology is still correct.
        for new_id, raw_inputs in pending:
            resolved: list[str] = []
            for inp in raw_inputs:
                # `n:<label>` or `n:<int>` form (preferred).
                if inp.startswith("n:"):
                    suffix = inp[2:]
                    if suffix in label_to_id:
                        resolved.append(label_to_id[suffix])
                        continue
                    if suffix.isdigit() and inp in self.g.nodes:
                        resolved.append(inp)
                        continue
                # Bare label form — the Planner sometimes drops the n: prefix.
                if inp in label_to_id:
                    resolved.append(label_to_id[inp])
                    continue
                # Special literal — the user query is always available.
                if inp == "USER_QUERY":
                    resolved.append(inp)
                    continue
                # Artifact handle — pass through, the input renderer handles it.
                if inp.startswith("art:"):
                    resolved.append(inp)
                    continue
                # Unresolvable input — fall back to the parent so the child
                # has at least one upstream dependency to wait on. This still
                # leaks the parent's output into INPUTS, but only when the
                # Planner emitted a bad input name; it is not the fan-out
                # path. A future round may want to fail loudly here instead.
                resolved.append(src_nid)
            self.g.nodes[new_id]["inputs"] = resolved
            for inp in resolved:
                if inp.startswith("n:") and inp in self.g.nodes:
                    self.g.add_edge(inp, new_id)
            # Fan-out worker case: planner emitted inputs=[] on purpose. No
            # data dependency, but we still record the structural parent
            # edge so the executor's `ready_nodes` ordering and replay
            # topology stay coherent.
            if not raw_inputs:
                self.g.add_edge(src_nid, new_id)

        for child_skill in src_def.internal_successors:
            nid = self.add_node(child_skill, inputs=[src_nid])
            added.append(nid)

        # Critic auto-insertion: when a `critic: true` skill completes,
        # gate every outgoing edge (to a non-critic child) with a Critic
        # node. Covers BOTH newly-added dynamic successors AND pre-existing
        # edges from the initial Planner plan — earlier versions only saw
        # `added`, so a pre-planned distiller → formatter chain bypassed
        # the auto-critic entirely (the `critic: true` flag became a no-op
        # in the common pre-planned case). Reading the graph's actual
        # outgoing edges makes the flag load-bearing in both shapes.
        if src_def.critic:
            child_targets: list[str] = []
            for child_nid in list(self.g.successors(src_nid)):
                if self.g.nodes[child_nid].get("skill") == "critic":
                    continue  # already gated
                # Skip critic insertion for intermediate processing nodes
                # (browser, researcher, distiller). Critics should only gate
                # terminal consumers (formatter) or the final data distiller
                # that feeds a formatter. Inserting critics before browsers
                # blocks the URL→browser chain unnecessarily.
                child_skill = self.g.nodes[child_nid].get("skill", "")
                if child_skill in ("browser", "computer", "researcher", "distiller"):
                    continue
                child_targets.append(child_nid)
            for child_nid in child_targets:
                self.g.remove_edge(src_nid, child_nid)
                # Critics need USER_QUERY: without it the critic falls back
                # to MEMORY HITS for context (and stale hits from prior
                # sessions can fool the critic into believing the user
                # asked a completely different question). With USER_QUERY
                # the critic evaluates against the real ask and not against
                # whatever happens to be top-of-FAISS.
                # Pass the target's scoped question so the critic evaluates
                # against the intermediate node's actual task, not the full
                # user query (which may ask for fields that are another
                # node's responsibility downstream).
                target_question = (self.g.nodes[src_nid].get("metadata") or {}).get("question")
                critic_meta = {"target": src_nid, "child": child_nid}
                if target_question:
                    critic_meta["question"] = f"SCOPED_QUESTION: {target_question}"
                critic_nid = self.add_node(
                    "critic", inputs=["USER_QUERY", src_nid],
                    metadata=critic_meta,
                )
                self.g.add_edge(critic_nid, child_nid)
                added.append(critic_nid)

        return added


# ── Executor ─────────────────────────────────────────────────────────────────

class Executor:
    def __init__(self, registry: SkillRegistry | None = None):
        ensure_gateway()
        self.registry = registry or SkillRegistry()

    async def run(self, query: str, *, session_id: str | None = None,
                  resume: bool = False) -> str:
        sid = session_id or f"s8-{uuid.uuid4().hex[:8]}"
        store = SessionStore(sid)
        if resume:
            existing = store.read_graph()
            if existing is None:
                raise RuntimeError(f"cannot resume {sid}: no graph.pkl on disk")
            graph_obj = existing
            graph = Graph.__new__(Graph)
            graph.g = graph_obj
            graph._counter = max(
                [int(n.split(":")[1]) for n in graph.g.nodes if n.startswith("n:")] or [0]
            )
            for _, d in graph.g.nodes(data=True):
                if d["status"] == "running":
                    d["status"] = "pending"
            if not query:
                query = store.read_query()
        else:
            store.write_query(query)
            graph = Graph()
            graph.add_node("planner", inputs=["USER_QUERY"])

        # ── File logging: mirror stdout to logs/<session_id>.log ─────────
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"{sid}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        old_stdout = sys.stdout
        sys.stdout = _Tee(old_stdout, log_file)

        print(f"\n{'═' * 78}\nsession {sid}  ─  query: {query}\n{'═' * 78}")
        # Read memory ONCE at session start; the same hits flow into every
        # skill's prompt. The S7 contract is that every cognitive role sees
        # memory; carrying that forward verbatim here is what makes S7's
        # indexing investment continue to pay off in S8.
        memory_hits = memory_svc.read(query) or []
        if memory_hits:
            print(f"[memory.read] {len(memory_hits)} hit(s) visible to every skill this run")
        try:
            memory_svc.remember(query, source="user_query", run_id=sid)
        except Exception as e:
            print(f"[memory.remember] skipped: {e!r}")

        formatter_answer: str | None = None
        executed_count = 0
        run_t0 = time.time()  # wall-clock reference for the timing table
        node_timings: list[dict] = []  # {"nid", "skill", "start", "elapsed", "finish"}
        # Per-target cap for critic-fail recovery; see P1 #5 fix below.
        recovered_branches: dict[str, bool] = {}
        # NOTES_RUNS round-3 review #5: when the cap fires, the branch is
        # skipped silently and the final answer reflects missing data with
        # no flag. Track every second-or-later critic-fail here so the
        # final log can surface it.
        critic_fail_cap_hit: list[str] = []

        while True:
            ready = graph.ready_nodes()
            if not ready and not graph.has_running():
                break
            if executed_count + len(ready) > MAX_NODES:
                print(f"[flow] node cap {MAX_NODES} hit at {executed_count}; stopping")
                break

            for nid in ready:
                graph.mark(nid, "running")
            store.write_graph(graph.g)

            outcomes = await asyncio.gather(*[self._run_one(nid, graph, sid, query, store, memory_hits)
                                              for nid in ready])

            for nid, result, prompt in outcomes:
                executed_count += 1
                graph.g.nodes[nid]["result"] = result
                graph.mark(nid, "complete" if result.success else "failed")
                node_finish = time.time()
                node_start_rel = node_finish - result.elapsed_s - run_t0
                node_finish_rel = node_finish - run_t0
                node_timings.append({
                    "nid": nid, "skill": graph.g.nodes[nid]["skill"],
                    "start": max(0.0, node_start_rel),
                    "elapsed": result.elapsed_s,
                    "finish": node_finish_rel,
                })
                store.write_node(NodeState(
                    node_id=nid, skill=graph.g.nodes[nid]["skill"],
                    status=graph.g.nodes[nid]["status"],
                    inputs=graph.g.nodes[nid]["inputs"],
                    result=result, prompt_sent=prompt,
                    started_at=time.time() - result.elapsed_s,
                    completed_at=time.time(),
                ))
                print(f"[{nid}] {graph.g.nodes[nid]['skill']:18s} "
                      f"{graph.g.nodes[nid]['status']:8s} "
                      f"({result.elapsed_s:.1f}s)"
                      + (f"  err={result.error[:80]}" if result.error else ""))
                # Inline browser path log — visible during execution
                if graph.g.nodes[nid]["skill"] == "browser" and result.output:
                    _bp = result.output.get("path", "?")
                    _bt = result.output.get("turns", 0)
                    _bu = result.output.get("url", "")
                    print(f"       browser: path={_bp}  turns={_bt}  url={_bu[:80]}")
                if graph.g.nodes[nid]["skill"] == "computer" and result.output:
                    _cp = result.output.get("path", "?")
                    _ct = result.output.get("turns", 0)
                    _ck = result.output.get("task", "")
                    print(f"       computer: path={_cp}  turns={_ct}  task={_ck[:80]}")

                if result.success:
                    if graph.g.nodes[nid]["skill"] == "critic":
                        if handle_critic_verdict(nid, result, graph,
                                                 recovered_branches,
                                                 critic_fail_cap_hit):
                            continue
                        # verdict == pass: the child is now ready to run.
                    graph.extend_from(nid, result, registry=self.registry)
                    if graph.g.nodes[nid]["skill"] == "formatter":
                        fa = result.output.get("final_answer")
                        if _has_meaningful_answer(fa):
                            formatter_answer = fa
                        elif not formatter_answer:
                            # Fallback: formatter parse failed; walk predecessors
                            # (skip critics which only have verdict/rationale)
                            visited = set()
                            queue = list(graph.g.predecessors(nid))
                            while queue and not formatter_answer:
                                pred = queue.pop(0)
                                if pred in visited:
                                    continue
                                visited.add(pred)
                                pred_skill = graph.g.nodes[pred].get("skill", "")
                                pred_res = graph.g.nodes[pred].get("result")
                                if not pred_res or not hasattr(pred_res, "output") or not isinstance(pred_res.output, dict):
                                    continue
                                if pred_skill == "critic":
                                    queue.extend(graph.g.predecessors(pred))
                                    continue
                                fb = (pred_res.output.get("recommendation")
                                      or pred_res.output.get("summary")
                                      or pred_res.output.get("final_answer")
                                      or pred_res.output.get("stdout"))
                                if _has_meaningful_answer(fb):
                                    formatter_answer = str(fb)
                                elif pred_res.output.get("fields"):
                                    formatter_answer = json.dumps(pred_res.output["fields"], indent=2, ensure_ascii=False)
                                else:
                                    queue.extend(graph.g.predecessors(pred))
                else:
                    failed_skill = graph.g.nodes[nid]["skill"]
                    decision = plan_recovery(
                        failed_skill=failed_skill,
                        error_text=result.error or "",
                        failed_node_id=nid,
                    )
                    if decision.action == "skip":
                        graph.mark(nid, "skipped")
                        print(f"  ↪ {nid} failed ({decision.reason}, "
                              f"skill={failed_skill}): {decision.note}")
                        continue
                    # action == "replan"
                    # Recovery Planner amnesia fix: pass the ids of nodes that
                    # have already completed successfully so the recovery
                    # Planner can wire them by id in its successor plan
                    # instead of re-emitting fresh fan-out siblings that
                    # duplicate work already done. Excludes planner nodes
                    # (routing context, not data) and critic nodes (verdicts,
                    # not data); their outputs are not useful upstream input
                    # for a recovery plan. planner.md teaches the recovery
                    # Planner what to do with these n:* refs.
                    prior_complete = [
                        n for n, d in graph.g.nodes(data=True)
                        if d.get("status") == "complete"
                        and d["skill"] not in ("planner", "critic")
                        and isinstance(d.get("result"), AgentResult)
                    ]
                    recovery_inputs = ["USER_QUERY"] + prior_complete
                    rec_nid = graph.add_node(
                        "planner", inputs=recovery_inputs,
                        metadata={"failure_report": decision.failure_report,
                                  "recovers": nid,
                                  "recovery_reason": decision.reason,
                                  "prior_complete": prior_complete},
                    )
                    print(f"  ↪ recovery ({decision.reason}): planner node "
                          f"{rec_nid} queued for {nid}"
                          + (f"; reusing {len(prior_complete)} prior result(s): "
                             f"{', '.join(prior_complete)}" if prior_complete else ""))

            store.write_graph(graph.g)

        if not _has_meaningful_answer(formatter_answer):
            for nid in reversed(list(graph.g.nodes)):
                d = graph.g.nodes[nid]
                if d["status"] == "complete" and isinstance(d.get("result"), AgentResult):
                    res: AgentResult = d["result"]
                    skill_name = d.get("skill", "")
                    if skill_name == "formatter":
                        fa = (res.output or {}).get("final_answer") if isinstance(res.output, dict) else None
                        if not _has_meaningful_answer(fa):
                            continue
                    candidate = _summarize_output(
                        skill_name,
                        res.output or {},
                        res.error,
                    )
                    if _has_meaningful_answer(candidate):
                        formatter_answer = candidate
                        break

        if critic_fail_cap_hit:
            # Loud surface — see review round-3 #5. Without this the cap
            # firing was invisible and the user would just see a thin
            # formatter answer with no explanation of why.
            print(f"\n[flow] WARNING: critic-fail cap hit on "
                  f"{len(critic_fail_cap_hit)} branch(es): "
                  f"{', '.join(critic_fail_cap_hit)}. "
                  f"The final answer reflects missing data from these "
                  f"branches because the Critic rejected the re-planned "
                  f"output too.")

        # ── Timing table ─────────────────────────────────────────────────
        wall_clock = time.time() - run_t0
        sum_elapsed = sum(t["elapsed"] for t in node_timings)
        speedup = sum_elapsed / wall_clock if wall_clock > 0 else 1.0
        print(f"\n{'─' * 78}")
        print(f"{'node':<6} {'skill':<20} {'start (rel)':>12} {'elapsed':>10} {'finish (rel)':>13}")
        for t in sorted(node_timings, key=lambda x: x["start"]):
            print(f"{t['nid']:<6} {t['skill']:<20} {t['start']:>9.2f} s {t['elapsed']:>7.2f} s {t['finish']:>10.2f} s")
        print()
        print(f"{'wall-clock end-to-end:':<34} {wall_clock:.2f} s")
        print(f"{'sum-of-elapsed (serial):':<34} {sum_elapsed:.2f} s")
        print(f"{'parallel speedup ratio:':<34} {speedup:.2f}x")
        print(f"{'─' * 78}")

        # ── Browser Summary ──────────────────────────────────────────────
        browser_nodes = [
            (nid, d) for nid, d in graph.g.nodes(data=True)
            if d.get("skill") == "browser"
        ]
        if browser_nodes:
            print(f"\n{'─' * 78}")
            print("BROWSER SUMMARY")
            print(f"{'─' * 78}")
            for b_nid, b_data in browser_nodes:
                b_result = b_data.get("result")
                b_status = b_data.get("status", "?")
                b_output = b_result.output if (isinstance(b_result, AgentResult) and b_result.output) else {}
                b_path = b_output.get("path", "n/a")
                b_url = b_output.get("url", "n/a")
                b_goal = b_output.get("goal", "n/a")
                b_turns = b_output.get("turns", 0)
                b_actions = b_output.get("actions") or []
                b_content = b_output.get("content") or ""
                b_final_url = b_output.get("final_url") or b_url
                b_error = b_result.error if isinstance(b_result, AgentResult) else None

                print(f"\n  [{b_nid}] status={b_status}  path={b_path}  "
                      f"turns={b_turns}  url={b_url}")
                if b_final_url and b_final_url != b_url:
                    print(f"         final_url={b_final_url}")
                if b_goal and b_goal != "n/a":
                    print(f"         goal: {b_goal}")
                if b_error:
                    print(f"         error: {b_error[:120]}")

                # 4. Browser actions taken
                if b_actions:
                    print(f"         actions ({len(b_actions)} turn(s)):")
                    for act in b_actions[:20]:  # cap to avoid log explosion
                        turn_num = act.get("turn", "?")
                        actions_list = act.get("actions", [])
                        outcome = act.get("outcome", "")
                        actions_str = ", ".join(str(a) for a in actions_list) if actions_list else "none"
                        print(f"           turn {turn_num}: {actions_str[:120]}")
                        if outcome:
                            print(f"             → {str(outcome)[:120]}")

                # 5. Screenshots / page-state logs
                browser_arts = Path(str(
                    Path(__file__).resolve().parent / "state" / "sessions" / sid / "browser"
                ))
                if browser_arts.exists():
                    # Find artifacts dirs that correspond to this browser run
                    art_dirs = sorted(browser_arts.iterdir())
                    # Map by order of browser nodes
                    b_idx = [n for n, _ in browser_nodes].index(b_nid)
                    if b_idx < len(art_dirs):
                        art_dir = art_dirs[b_idx]
                        screenshots = []
                        for layer_dir in sorted(art_dir.iterdir()):
                            if layer_dir.is_dir():
                                pngs = sorted(layer_dir.glob("*.png"))
                                if pngs:
                                    screenshots.extend(pngs)
                        if screenshots:
                            print(f"         screenshots ({len(screenshots)}):")
                            for ss in screenshots[:10]:
                                print(f"           {ss.relative_to(browser_arts.parent.parent.parent)}")
                            if len(screenshots) > 10:
                                print(f"           ... and {len(screenshots) - 10} more")
                        else:
                            print(f"         screenshots: none")
                    else:
                        print(f"         screenshots: (no artifacts dir)")

                # 6. Extracted data
                if b_content:
                    preview = b_content[:500].replace("\n", " ↵ ")
                    print(f"         extracted data ({len(b_content)} chars):")
                    print(f"           {preview}")
                    if len(b_content) > 500:
                        print(f"           ... ({len(b_content) - 500} more chars)")
                elif b_status == "complete":
                    print(f"         extracted data: (empty)")

        # ── Computer Summary ─────────────────────────────────────────────
        computer_nodes = [
            (nid, d) for nid, d in graph.g.nodes(data=True)
            if d.get("skill") == "computer"
        ]
        if computer_nodes:
            print(f"\n{'─' * 78}")
            print("COMPUTER SUMMARY")
            print(f"{'─' * 78}")
            for c_nid, c_data in computer_nodes:
                c_result = c_data.get("result")
                c_status = c_data.get("status", "?")
                c_output = c_result.output if (isinstance(c_result, AgentResult) and c_result.output) else {}
                c_path = c_output.get("path", "n/a")
                c_task = c_output.get("task", "n/a")
                c_goal = c_output.get("goal", "n/a")
                c_turns = c_output.get("turns", 0)
                c_traj = c_output.get("trajectory_dir", "")
                c_error = c_result.error if isinstance(c_result, AgentResult) else None

                print(f"\n  [{c_nid}] status={c_status}  path={c_path}  turns={c_turns}  task={c_task}")
                if c_goal and c_goal != "n/a":
                    print(f"         goal: {c_goal}")
                if c_traj:
                    print(f"         trajectory: {c_traj}")
                if c_error:
                    print(f"         error: {c_error[:120]}")

                c_actions = c_output.get("actions") or []
                if c_actions:
                    print(f"         actions ({len(c_actions)}):")
                    for act in c_actions[:20]:
                        tnum = act.get("turn", "?")
                        tool = act.get("tool", "")
                        print(f"           turn {tnum}: {tool}")

        # ── Turn count & cost summary ────────────────────────────────────
        print(f"\n{'─' * 78}")
        print("TURN COUNT & COST SUMMARY")
        print(f"{'─' * 78}")
        total_turns = 0
        total_cost = 0.0
        skill_counts: dict[str, int] = {}
        skill_costs: dict[str, float] = {}
        for nid, d in graph.g.nodes(data=True):
            sk = d.get("skill", "?")
            skill_counts[sk] = skill_counts.get(sk, 0) + 1
            res = d.get("result")
            if isinstance(res, AgentResult):
                total_cost += res.cost
                skill_costs[sk] = skill_costs.get(sk, 0.0) + res.cost
                # For browser nodes, add turns from output
                if sk in ("browser", "computer") and isinstance(res.output, dict):
                    total_turns += res.output.get("turns", 0)
        print(f"  {'skill':<20} {'nodes':>6} {'cost ($)':>10}")
        print(f"  {'─' * 38}")
        for sk in sorted(skill_counts.keys()):
            c = skill_costs.get(sk, 0.0)
            cost_str = f"${c:.4f}" if c > 0 else "—"
            print(f"  {sk:<20} {skill_counts[sk]:>6} {cost_str:>10}")
        print(f"  {'─' * 38}")
        print(f"  {'TOTAL':<20} {sum(skill_counts.values()):>6} "
              f"{'$' + f'{total_cost:.4f}' if total_cost > 0 else '—':>10}")
        print(f"\n  total browser/computer turns: {total_turns}")
        print(f"  total nodes executed: {executed_count}")
        print(f"{'─' * 78}")

        print(f"\n{'═' * 78}\nFINAL: {formatter_answer or ''}\n{'═' * 78}\n")

        # ── Close log file ───────────────────────────────────────────────
        sys.stdout = old_stdout
        log_file.close()
        print(f"[log] session log → {log_path}")

        return formatter_answer or ""

    async def _run_one(self, nid: str, graph: Graph, sid: str, query: str,
                       store: SessionStore, memory_hits: list) -> tuple[str, AgentResult, str]:
        skill_name = graph.g.nodes[nid]["skill"]
        skill = self.registry.get(skill_name)
        fr = graph.g.nodes[nid].get("metadata", {}).get("failure_report")
        store.write_node(NodeState(node_id=nid, skill=skill_name, status="running",
                                   inputs=graph.g.nodes[nid]["inputs"],
                                   started_at=time.time()))
        try:
            result, prompt = await run_skill(skill, nid, graph.g.nodes, sid, query, fr,
                                             memory_hits=memory_hits)
        except Exception as e:  # pragma: no cover - dispatcher fault path
            result = AgentResult(success=False, agent_name=skill_name,
                                 error=f"exception: {type(e).__name__}: {e}")
            prompt = "(exception before prompt-render)"
        return nid, result, prompt


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    resume_sid: str | None = None
    if args and args[0] == "--resume":
        resume_sid = args[1] if len(args) > 1 else None
        query = " ".join(args[2:])
    else:
        query = " ".join(args) or "Say hello in one short sentence."
    asyncio.run(Executor().run(query, session_id=resume_sid, resume=bool(resume_sid)))


if __name__ == "__main__":
    main()
