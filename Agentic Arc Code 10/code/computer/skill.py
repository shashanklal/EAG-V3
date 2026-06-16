"""Computer skill: host desktop control through cua-driver.

Five-layer architecture (aligned to CUA_DRIVER_GUIDE.md):
  Layer 1   - task planning and deterministic recipe selection
  Layer 2a  - deterministic hotkeys / scripted actions
  Layer 2b  - semantic AX interpretation via V9 /v1/chat
  Layer 3   - vision fallback via V9 /v1/vision
  Layer 4   - verify/recover envelope around each task

Every run starts `start_recording` and persists the trajectory directory in
AgentResult.output as evidence.
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from browser.client import V9Client
from browser.highlight import to_data_url
from schemas import AgentResult, NodeSpec


@dataclass
class TaskPlan:
    key: str
    title: str
    goal: str
    expected: str


class CuaDriverError(RuntimeError):
    pass


class ComputerSkill:
    NAME = "computer"

    def __init__(
        self,
        *,
        gateway_url: str = "http://localhost:8109",
        agent_tag: str = "computer",
        artifacts_root: str | None = None,
        session: str | None = None,
    ):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.session = session
        self._client = V9Client(base_url=gateway_url, agent=agent_tag, session=session)
        self._turns = 0
        self._actions: list[dict[str, Any]] = []

    async def run(self, node: NodeSpec) -> AgentResult:
        started = time.time()
        self._turns = 0
        self._actions = []

        plan = self._layer1_plan(node)
        if not plan:
            return AgentResult(
                success=False,
                agent_name=self.NAME,
                error=(
                    "computer skill needs metadata.task in "
                    "{calculator_hotkeys|vscode_csv_code|maps_distance}"
                ),
            )

        try:
            driver = self._init_backend()
            await driver.ensure_ready()
        except Exception as exc:
            return self._pack_error(
                plan,
                path="layer1",
                msg=f"computer driver unavailable: {type(exc).__name__}: {exc}",
                elapsed=time.time() - started,
            )

        task_root = self._task_root(plan.key, started)
        trajectory_dir = task_root / "trajectory"
        trajectory_dir.mkdir(parents=True, exist_ok=True)

        rec_started = False
        rec_error = ""
        try:
            await self._call_driver(driver, "start_recording", {"output_dir": str(trajectory_dir)})
            rec_started = True
        except Exception as exc:
            rec_error = f"recording start failed: {type(exc).__name__}: {exc}"

        result = None
        try:
            result = await self._layer4_verify_and_execute(driver, plan, node, task_root)
            if rec_error:
                result.output["recording_warning"] = rec_error
        except Exception as exc:
            result = self._pack_error(
                plan,
                path="layer4",
                msg=f"execution failed: {type(exc).__name__}: {exc}",
                elapsed=time.time() - started,
            )
            if rec_error:
                result.output["recording_warning"] = rec_error
        finally:
            if rec_started:
                try:
                    await self._call_driver(driver, "stop_recording", {})
                except Exception as exc:
                    if result is not None:
                        result.output["recording_stop_warning"] = str(exc)

        if result is None:
            result = self._pack_error(plan, path="layer4", msg="unknown error", elapsed=time.time() - started)

        result.output["trajectory_dir"] = str(trajectory_dir)
        result.output["actions"] = self._actions
        result.output["turns"] = self._turns
        result.elapsed_s = time.time() - started
        return result

    # Layer 1: task decomposition into deterministic plans
    def _layer1_plan(self, node: NodeSpec) -> TaskPlan | None:
        m = node.metadata or {}
        task = str(m.get("task") or "").strip().lower()
        aliases = {
            "calculator": "calculator_hotkeys",
            "calc": "calculator_hotkeys",
            "vscode": "vscode_csv_code",
            "maps": "maps_distance",
        }
        task = aliases.get(task, task)
        if task == "calculator_hotkeys":
            expr = str(m.get("expression") or "25*4")
            try:
                evaluated = self._safe_eval(expr)
                if isinstance(evaluated, float) and evaluated.is_integer():
                    expected = str(int(evaluated))
                else:
                    expected = str(evaluated)
            except Exception:
                expected = ""
            return TaskPlan(
                key=task,
                title="Calculator deterministic hotkeys",
                goal=f"Compute {expr} using deterministic keyboard actions",
                expected=expected,
            )
        if task == "vscode_csv_code":
            return TaskPlan(
                key=task,
                title="VS Code Electron task",
                goal="Open VS Code and type Python CSV-reading code",
                expected="import csv",
            )
        if task == "maps_distance":
            home, office = self._resolve_route(m)
            return TaskPlan(
                key=task,
                title="Google Maps distance",
                goal=f"Read travel distance from {home} to {office}",
                expected="distance",
            )
        return None

    # Pull "from <A> to <B>" (or "between <A> and <B>") out of a free-text
    # instruction so the planner's loosely-structured nodes still route to the
    # correct cities when it omits explicit home/office metadata.
    _ROUTE_RE = re.compile(
        r"(?:from\s+(?P<a>.+?)\s+to\s+(?P<b>.+?)"
        r"|between\s+(?P<c>.+?)\s+and\s+(?P<d>.+?))"
        r"(?:\s+(?:using|via|on|with|in)\b.*)?[.?!]?\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _resolve_route(cls, meta: dict) -> tuple[str, str]:
        """Return (home, office) for a maps_distance task.

        Precedence: explicit home/office metadata > parsed from a free-text
        field (question/query/label/goal) > New York defaults. Keeps the
        deterministic contract while tolerating the planner's free-form nodes.
        """
        home = str(meta.get("home") or "").strip()
        office = str(meta.get("office") or "").strip()
        if home and office:
            return home, office
        for field in ("question", "query", "goal", "label"):
            text = str(meta.get(field) or "").strip()
            if not text:
                continue
            match = cls._ROUTE_RE.search(text)
            if match:
                a = (match.group("a") or match.group("c") or "").strip(" .,\"'")
                b = (match.group("b") or match.group("d") or "").strip(" .,\"'")
                if a and b:
                    return home or a, office or b
        return (home or "Times Square, New York",
                office or "JFK Airport, New York")


    # Layer 4: verify/recover loop around the lower layers
    async def _layer4_verify_and_execute(
        self,
        driver: str,
        plan: TaskPlan,
        node: NodeSpec,
        task_root: Path,
    ) -> AgentResult:
        if plan.key == "calculator_hotkeys":
            return await self._run_calculator(driver, plan, node)
        if plan.key == "vscode_csv_code":
            return await self._run_vscode(driver, plan)
        if plan.key == "maps_distance":
            return await self._run_maps(driver, plan, node, task_root)
        return self._pack_error(plan, path="layer4", msg="unsupported task", elapsed=0.0)

    # Layer 2a deterministic path: calculator hotkeys
    async def _run_calculator(self, driver: str, plan: TaskPlan, node: NodeSpec) -> AgentResult:
        expr = str(node.metadata.get("expression") or "25*4")
        launch = await self._call_driver(driver, "launch_app", {"name": "Calculator"})
        pid = int(launch.get("pid") or 0)
        if pid == 0:
            return self._pack_error(plan, path="deterministic", msg="could not launch Calculator", elapsed=0.0)
        window_id = await self._resolve_window_id(driver, pid)
        await asyncio.sleep(0.5)

        await self._recorded_call(driver, "type_text", {
            "pid": pid,
            "window_id": window_id,
            "text": expr + "=",
        })
        await asyncio.sleep(0.4)
        state = await self._recorded_call(driver, "get_window_state", {
            "pid": pid,
            "window_id": window_id,
            "capture_mode": "ax",
            "query": plan.expected,
        })
        ok = plan.expected in str(state.get("tree_markdown", ""))
        if not ok:
            return self._pack_error(
                plan,
                path="deterministic",
                msg=f"expected result {plan.expected} not found in AX tree",
                elapsed=0.0,
            )
        return AgentResult(
            success=True,
            agent_name=self.NAME,
            output={
                "task": plan.key,
                "title": plan.title,
                "path": "deterministic",
                "goal": plan.goal,
                "result": plan.expected,
                "notes": "Layer 2a deterministic hotkeys completed",
            },
        )

    # Layer 2a deterministic path: VS Code keyboard-driven snippet insertion
    async def _run_vscode(self, driver: str, plan: TaskPlan) -> AgentResult:
        launch = await self._call_driver(driver, "launch_app", {"name": "Visual Studio Code"})
        pid = int(launch.get("pid") or 0)
        if pid == 0:
            launch = await self._call_driver(driver, "launch_app", {"name": "VS Code"})
            pid = int(launch.get("pid") or 0)
        if pid == 0:
            launch = await self._call_driver(driver, "launch_app", {"name": "Code"})
            pid = int(launch.get("pid") or 0)
        if pid == 0:
            return self._pack_error(plan, path="deterministic", msg="could not launch VS Code", elapsed=0.0)
        window_id = await self._resolve_window_id(driver, pid)

        code = (
            "import csv\n\n"
            "def read_csv(path: str):\n"
            "    with open(path, newline='', encoding='utf-8') as f:\n"
            "        return list(csv.DictReader(f))\n\n"
            "if __name__ == '__main__':\n"
            "    rows = read_csv('data.csv')\n"
            "    print(f'rows: {len(rows)}')\n"
        )

        # Match the expected deterministic menu path: File -> New File.
        await self._recorded_call(driver, "hotkey", {"keys": ["alt", "f"]})
        await asyncio.sleep(0.2)
        await self._recorded_call(driver, "press_key", {
            "pid": pid,
            "window_id": window_id,
            "key": "n",
        })
        await asyncio.sleep(0.3)
        await self._recorded_call(driver, "type_text", {
            "pid": pid,
            "window_id": window_id,
            "text": code,
        })
        verify = await self._recorded_call(driver, "get_window_state", {
            "pid": pid,
            "window_id": window_id,
            "capture_mode": "ax",
            "query": "import csv",
        })
        ok = "import csv" in str(verify.get("tree_markdown", ""))
        if not ok:
            return self._pack_error(
                plan,
                path="deterministic",
                msg="typed code not visible in AX tree",
                elapsed=0.0,
            )
        return AgentResult(
            success=True,
            agent_name=self.NAME,
            output={
                "task": plan.key,
                "title": plan.title,
                "path": "deterministic",
                "goal": plan.goal,
                "snippet": code,
                "notes": "Layer 2a deterministic key flow completed",
            },
        )

    # Layer 2b + Layer 3 for maps: semantic read then vision fallback
    async def _run_maps(self, driver: str, plan: TaskPlan, node: NodeSpec, task_root: Path) -> AgentResult:
        home, office = self._resolve_route(node.metadata or {})
        maps_url = (
            "https://www.google.com/maps/dir/"
            f"{quote(home, safe='')}/{quote(office, safe='')}"
        )

        launch = await self._call_driver(driver, "launch_app", {"name": "Google Chrome"})
        pid = int(launch.get("pid") or 0)
        if pid == 0:
            launch = await self._call_driver(driver, "launch_app", {"name": "Microsoft Edge"})
            pid = int(launch.get("pid") or 0)
        if pid == 0:
            return self._pack_error(plan, path="deterministic", msg="could not launch Chrome or Edge", elapsed=0.0)
        window_id = await self._resolve_window_id(driver, pid)

        # Maximize so the directions panel is fully visible to both the AX read
        # and the (window-cropped) vision screenshot. Best-effort: backends
        # without this tool simply skip it.
        try:
            await self._call_driver(driver, "maximize_window", {
                "pid": pid,
                "window_id": window_id,
            })
        except Exception:
            pass

        await self._recorded_call(driver, "hotkey", {"keys": ["ctrl", "l"]})
        await self._recorded_call(driver, "type_text", {
            "pid": pid,
            "window_id": window_id,
            "text": maps_url,
        })
        await self._recorded_call(driver, "press_key", {
            "pid": pid,
            "window_id": window_id,
            "key": "Enter",
        })

        # Layer 2b: poll the AX tree until the directions panel actually
        # renders (Google Maps loads routes asynchronously), instead of a
        # blind fixed sleep. The last captured tree feeds the extractor.
        md = await self._poll_maps_ax(driver, pid, window_id)
        semantic_distance = await self._extract_distance_from_ax(md)
        if semantic_distance:
            return AgentResult(
                success=True,
                agent_name=self.NAME,
                output={
                    "task": plan.key,
                    "title": plan.title,
                    "path": "a11y",
                    "goal": plan.goal,
                    "distance": semantic_distance,
                    "url": maps_url,
                    "notes": "Layer 2b semantic AX interpretation succeeded",
                },
            )

        # Layer 3: AX was empty / insufficient (no distance verifiably present
        # in the tree). Escalate to vision per the cascade. The window-cropped
        # screenshot keeps only the Chrome window so the directions panel
        # dominates the frame the vision model reads.
        screenshot = task_root / "maps_vision.png"
        await self._recorded_call(driver, "get_window_state", {
            "pid": pid,
            "window_id": window_id,
            "capture_mode": "vision",
            "screenshot_out_file": str(screenshot),
        })
        if not screenshot.exists():
            return self._pack_error(
                plan,
                path="vision",
                msg="vision screenshot was not written by cua-driver",
                elapsed=0.0,
            )
        distance = await self._extract_distance_from_image(screenshot)
        if not distance:
            return self._pack_error(
                plan,
                path="vision",
                msg="could not extract distance from maps screenshot",
                elapsed=0.0,
            )
        return AgentResult(
            success=True,
            agent_name=self.NAME,
            output={
                "task": plan.key,
                "title": plan.title,
                "path": "vision",
                "goal": plan.goal,
                "distance": distance,
                "url": maps_url,
                "screenshot": str(screenshot),
                "notes": "Layer 3 vision fallback succeeded",
            },
        )

    # Markers that indicate the Google Maps directions panel has rendered:
    # a verifiable distance, or route-summary tokens Maps always emits.
    _MAPS_READY_MARKERS = (
        "best route", "fastest route", " via ", " hr ", " min", " h ",
    )

    async def _poll_maps_ax(
        self,
        driver: str,
        pid: int,
        window_id: int,
        *,
        attempts: int = 3,
        interval_s: float = 2.0,
    ) -> str:
        """Poll the AX tree until the directions panel renders, returning the
        last captured tree markdown.

        Google Maps fetches routes asynchronously, so the panel is empty for
        the first second(s) after navigation. Polling for the route markers
        (or a verifiable distance) replaces a blind fixed sleep: it returns as
        soon as the content is present. Attempts are capped low because each
        AX dump of the heavy Maps page is expensive; whatever the cascade
        cannot read here falls through to the Layer 3 vision fallback.
        """
        last_md = ""
        for _ in range(attempts):
            await asyncio.sleep(interval_s)
            ax = await self._recorded_call(driver, "get_window_state", {
                "pid": pid,
                "window_id": window_id,
                "capture_mode": "ax",
                "query": "mi km",
            })
            md = str(ax.get("tree_markdown", ""))
            if md.strip():
                last_md = md
            low = md.lower()
            if self._looks_like_distance(md) or any(
                marker in low for marker in self._MAPS_READY_MARKERS
            ):
                return md
        return last_md

    # A route distance is a number (optionally decimal / thousands-separated)
    # immediately followed by a distance unit. Used to validate extracted text
    # before it is allowed to short-circuit the cascade.
    _DISTANCE_RE = re.compile(
        r"\d[\d,]*(?:\.\d+)?\s?(?:km|mi|miles|kilomet(?:re|er)s?)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _looks_like_distance(cls, text: str) -> bool:
        return bool(text) and bool(cls._DISTANCE_RE.search(text))

    async def _extract_distance_from_ax(self, tree_markdown: str) -> str:
        tree = tree_markdown.strip()
        if not tree:
            return ""
        schema = {
            "type": "object",
            "properties": {
                "distance": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["distance", "confidence"],
            "additionalProperties": False,
        }
        prompt = (
            "You are reading a Windows UI accessibility (AX) tree rendered as "
            "Markdown. Extract the driving route distance shown in the Google "
            "Maps directions panel, copied verbatim from the tree (a number "
            "followed by a distance unit such as km or mi). Do NOT guess or "
            "invent a value: if no distance text is actually present in the "
            "tree, return an empty distance and confidence 0.\n\n"
            f"TREE:\n{tree[:12000]}"
        )
        try:
            res = await self._client.chat(
                prompt,
                schema=schema,
                schema_name="distance_from_ax",
                max_tokens=120,
            )
        except Exception:
            return ""
        parsed = res.parsed or {}
        distance = str(parsed.get("distance") or "").strip()
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        # Validation gate (mirrors the browser skill's _is_useful_extract):
        # the value must look like a distance, the model must be confident,
        # and — crucially — the text must actually appear in the AX tree.
        # The containment check defeats the failure mode where the LLM parrots
        # an example value the tree never contained, which would wrongly
        # short-circuit the cascade before the Layer 3 vision fallback.
        if not self._looks_like_distance(distance):
            return ""
        if confidence < 0.5:
            return ""
        if distance.replace(" ", "") not in tree.replace(" ", ""):
            return ""
        return distance

    async def _extract_distance_from_image(self, screenshot: Path) -> str:
        schema = {
            "type": "object",
            "properties": {
                "distance": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["distance", "evidence"],
            "additionalProperties": False,
        }
        data_url = to_data_url(screenshot.read_bytes())
        prompt = (
            "Read the Google Maps directions panel in this screenshot and "
            "extract the driving route distance exactly as shown (a number "
            "followed by a distance unit such as km or mi). If no distance is "
            "visible, return an empty distance."
        )
        try:
            res = await self._client.vision(
                data_url,
                prompt,
                schema=schema,
                schema_name="distance_from_maps",
                max_tokens=160,
            )
        except Exception:
            return ""
        parsed = res.parsed or {}
        return str(parsed.get("distance") or "").strip()

    async def _recorded_call(self, driver: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        out = await self._call_driver(driver, tool, args)
        self._turns += 1
        self._actions.append({"turn": self._turns, "tool": tool, "args": args})
        return out

    def _init_backend(self):
        """Select the desktop-automation backend.

        Default is the native Windows driver (real app launch + UIA + input).
        Set COMPUTER_BACKEND=cua to use the cua-driver CLI instead.
        """
        backend = os.environ.get("COMPUTER_BACKEND", "native").strip().lower()
        if backend == "cua":
            return _CuaCliBackend(self._find_driver_binary())
        from computer.native_driver import NativeWindowsDriver
        return NativeWindowsDriver()

    async def _resolve_window_id(self, driver, pid: int) -> int:
        windows = await self._call_driver(driver, "list_windows", {})
        for w in windows.get("windows", []):
            if int(w.get("pid") or 0) == pid:
                return int(w.get("window_id") or 0)
        ws = windows.get("windows", [])
        if ws:
            return int(ws[0].get("window_id") or 0)
        raise CuaDriverError(f"no windows visible for pid={pid}")

    async def _call_driver(self, driver, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await driver.call(tool, args)

    def _task_root(self, task_key: str, started: float) -> Path:
        if self.artifacts_root:
            root = self.artifacts_root
        else:
            root = Path(__file__).resolve().parent.parent / "state" / "sessions" / (self.session or "adhoc") / "computer"
        out = root / f"{task_key}_{int(started)}"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _pack_error(self, plan: TaskPlan, *, path: str, msg: str, elapsed: float) -> AgentResult:
        return AgentResult(
            success=False,
            agent_name=self.NAME,
            error=msg,
            elapsed_s=elapsed,
            error_code="interaction_failed",
            output={
                "task": plan.key,
                "title": plan.title,
                "goal": plan.goal,
                "path": path,
            },
        )

    @staticmethod
    def _safe_eval(expr: str) -> int | float:
        tree = ast.parse(expr, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                   ast.FloorDiv, ast.USub, ast.UAdd, ast.Load, ast.Expr,
                   ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor)
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                raise ValueError(f"unsupported expression: {expr}")
        return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})

    @staticmethod
    def _find_driver_binary() -> str:
        # Try cua-driver first
        found = shutil.which("cua-driver")
        if found:
            return found
        
        home = Path.home()
        temp_root = Path(os.environ.get("TEMP", "/tmp"))
        
        candidates = [
            # cua-driver (primary) — official installer layout first
            home / "AppData" / "Local" / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
            home / ".local" / "bin" / "cua-driver",
            home / ".local" / "bin" / "cua-driver.exe",
            home / "AppData" / "Local" / "Programs" / "cua-driver" / "cua-driver.exe",
            # cua-computer-server (fallback)
            home / ".local" / "bin" / "cua-computer-server.exe",
            home / "AppData" / "Local" / "Programs" / "cua" / "cua-computer-server.exe",
            temp_root / "cua-computer-server-windows-x86_64" / "cua-computer-server" / "cua-computer-server.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        
        # Also try PATH for cua-computer-server
        found_server = shutil.which("cua-computer-server.exe")
        if found_server:
            return found_server
        
        raise FileNotFoundError(
            "neither cua-driver nor cua-computer-server.exe binary found on PATH or default install locations"
        )


class _CuaCliBackend:
    """Optional backend driving the cua-driver CLI (status/serve/call).

    Selected via COMPUTER_BACKEND=cua. Forwards the skill's tool vocabulary
    (launch_app, list_windows, type_text, hotkey, press_key, get_window_state,
    start_recording, stop_recording) to the ``cua-driver call`` contract,
    proxying through the long-running ``cua-driver serve`` daemon so the
    element-index cache survives across calls (see CUA_DRIVER_GUIDE.md §3.2).

    Every subprocess invocation is bounded by a timeout. A hung or unresponsive
    binary therefore surfaces as a fast, explicit ``CuaDriverError`` instead of
    blocking the agent indefinitely. Tune the bound with COMPUTER_CUA_TIMEOUT
    (seconds, default 30).
    """

    # The daemon should answer ``status`` quickly; the readiness probe is kept
    # short so a broken binary fails fast rather than stalling the whole agent.
    _STATUS_TIMEOUT_S = 8.0
    _SERVE_READY_TIMEOUT_S = 12.0

    def __init__(self, binary: str) -> None:
        self._binary = binary
        try:
            self._call_timeout_s = float(os.environ.get("COMPUTER_CUA_TIMEOUT", "30"))
        except ValueError:
            self._call_timeout_s = 30.0

    async def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        """Run a cua-driver subcommand with a hard timeout.

        Raises CuaDriverError (never hangs) if the binary does not return in
        time; the stray process is terminated so it cannot leak.
        """
        def _invoke() -> subprocess.CompletedProcess:
            try:
                return subprocess.run(
                    [self._binary, *args],
                    capture_output=True,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise CuaDriverError(
                    f"cua-driver '{args[0] if args else ''}' did not respond within "
                    f"{timeout:.0f}s (binary unresponsive: {self._binary}). "
                    "Reinstall the cua-driver binary or unset COMPUTER_BACKEND=cua "
                    "to use the native backend."
                ) from exc

        return await asyncio.to_thread(_invoke)

    async def ensure_ready(self) -> None:
        status = await self._run(["status"], self._STATUS_TIMEOUT_S)
        if "is running" in (status.stdout or ""):
            return
        try:
            subprocess.Popen(
                [self._binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise CuaDriverError(f"could not start cua-driver daemon: {exc}") from exc

        deadline = time.time() + self._SERVE_READY_TIMEOUT_S
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            st = await self._run(["status"], self._STATUS_TIMEOUT_S)
            if "is running" in (st.stdout or ""):
                return
        raise CuaDriverError(
            "cua-driver daemon failed to report ready within "
            f"{self._SERVE_READY_TIMEOUT_S:.0f}s"
        )

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        proc = await self._run(
            ["call", tool, json.dumps(args)],
            self._call_timeout_s,
        )
        if proc.returncode != 0:
            raise CuaDriverError(
                (proc.stderr or proc.stdout or f"cua-driver call {tool} failed").strip()
            )
        text = (proc.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
