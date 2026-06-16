"""Native Windows driver backend for the Computer skill.

This is a drop-in executor that replaces the cua-driver CLI contract with real
host automation built on widely available libraries:

  - pyautogui       keyboard / mouse synthesis + screenshots
  - pygetwindow     top-level window enumeration (ships with pyautogui)
  - uiautomation    Windows UI Automation tree reads (the "AX" layer)
  - ctypes/user32   foreground-window focus

It exposes a single async ``call(tool, args)`` method whose tool names and
return shapes match what ``ComputerSkill`` already expects:

  launch_app      {name}                     -> {pid, window_id, title}
  list_windows    {}                          -> {windows: [{pid, window_id, title}]}
  type_text       {window_id, text}           -> {ok}
  hotkey          {keys: [...]}               -> {ok}
  press_key       {key}                        -> {ok}
  get_window_state{window_id, capture_mode,    -> {tree_markdown} | {screenshot}
                   query, screenshot_out_file}
  start_recording {output_dir}                 -> {ok}
  stop_recording  {}                           -> {frames}

The five-layer discipline lives in ``ComputerSkill``; this module only provides
the deterministic primitives those layers drive.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pyautogui
import pyperclip

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02


# Friendly app name -> launch command(s). Each entry is tried in order until a
# matching window appears.
_APP_LAUNCHERS: dict[str, list[list[str]]] = {
    "calculator": [["calc.exe"]],
    "visual studio code": [["code"], ["code.cmd"]],
    "vs code": [["code"], ["code.cmd"]],
    "code": [["code"], ["code.cmd"]],
    "google chrome": [["chrome"], ["chrome.exe"]],
    "microsoft edge": [["msedge"], ["msedge.exe"]],
    "notepad": [["notepad.exe"]],
}


def _browser_paths(rel: str) -> list[list[str]]:
    """Common absolute install locations for a browser, used as a launch
    fallback when the bare command is not on PATH."""
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    out: list[list[str]] = []
    for root in roots:
        if not root:
            continue
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate):
            out.append([candidate])
    return out


# Append resolved absolute paths so browsers launch from a cold start even when
# they are not on PATH (the usual case on Windows).
_APP_LAUNCHERS["google chrome"] += _browser_paths(
    r"Google\Chrome\Application\chrome.exe")
_APP_LAUNCHERS["microsoft edge"] += _browser_paths(
    r"Microsoft\Edge\Application\msedge.exe")

# Friendly app name -> case-insensitive window-title substrings used to locate
# the launched window.
_APP_TITLE_HINTS: dict[str, list[str]] = {
    "calculator": ["calculator"],
    "visual studio code": ["visual studio code"],
    "vs code": ["visual studio code"],
    "code": ["visual studio code"],
    "google chrome": ["chrome"],
    "microsoft edge": ["edge"],
    "notepad": ["notepad"],
}


class NativeDriverError(RuntimeError):
    pass


def _pid_from_hwnd(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _focus_hwnd(hwnd: int) -> None:
    """Best-effort bring-to-foreground using the AttachThreadInput dance."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9
    # Only un-minimize; SW_RESTORE on a *maximized* window would shrink it,
    # which would defeat a prior maximize (and crop the vision screenshot).
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    cur_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(fg, None)
    win_thread = user32.GetWindowThreadProcessId(hwnd, None)
    try:
        user32.AttachThreadInput(cur_thread, target_thread, True)
        user32.AttachThreadInput(cur_thread, win_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(cur_thread, target_thread, False)
        user32.AttachThreadInput(cur_thread, win_thread, False)


def _is_foreground(hwnd: int) -> bool:
    return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]
    r = RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def _ensure_focus(hwnd: int, *, click: bool = True) -> bool:
    """Force ``hwnd`` to foreground; click its title bar as a fallback."""
    for _ in range(5):
        _focus_hwnd(hwnd)
        time.sleep(0.12)
        if _is_foreground(hwnd):
            return True
    if click:
        rect = _window_rect(hwnd)
        if rect:
            left, top, right, _bottom = rect
            # Click the title-bar strip (safe, hits no app buttons).
            cx = (left + right) // 2
            cy = top + 12
            try:
                pyautogui.click(cx, cy)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
            _focus_hwnd(hwnd)
            time.sleep(0.12)
    return _is_foreground(hwnd)



class NativeWindowsDriver:
    """Synchronous Windows automation, exposed through an async ``call``."""

    def __init__(self) -> None:
        # window_id (HWND) -> {pid, title}
        self._windows: dict[int, dict[str, Any]] = {}
        self._rec_stop: threading.Event | None = None
        self._rec_thread: threading.Thread | None = None
        self._rec_dir: Path | None = None
        self._rec_frames = 0

    async def ensure_ready(self) -> None:
        # Native backend has no daemon; a quick screen probe validates that a
        # desktop session is actually attached.
        def _probe() -> None:
            pyautogui.size()
        await asyncio.to_thread(_probe)

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._dispatch, tool, args)

    # ----- dispatch -------------------------------------------------------
    def _dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_t_{tool}", None)
        if handler is None:
            raise NativeDriverError(f"unsupported tool: {tool}")
        return handler(args)

    # ----- tools ----------------------------------------------------------
    def _t_launch_app(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        key = name.lower()
        launchers = _APP_LAUNCHERS.get(key)
        hints = _APP_TITLE_HINTS.get(key, [key])
        if not launchers:
            # Unknown app: try the raw name as a command and match on its text.
            launchers = [[name]]

        # If a matching window is already open, reuse it.
        existing = self._find_window(hints)
        if existing is not None:
            hwnd, title = existing
            pid = _pid_from_hwnd(hwnd)
            self._windows[hwnd] = {"pid": pid, "title": title}
            _focus_hwnd(hwnd)
            return {"pid": pid, "window_id": hwnd, "title": title}

        last_err = ""
        for cmd in launchers:
            try:
                subprocess.Popen(cmd, shell=False)
            except FileNotFoundError:
                try:
                    subprocess.Popen(cmd, shell=True)
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                continue

            found = self._wait_for_window(hints, timeout=12.0)
            if found is not None:
                hwnd, title = found
                pid = _pid_from_hwnd(hwnd)
                self._windows[hwnd] = {"pid": pid, "title": title}
                _focus_hwnd(hwnd)
                return {"pid": pid, "window_id": hwnd, "title": title}

        raise NativeDriverError(
            f"could not launch or locate window for '{name}'"
            + (f" ({last_err})" if last_err else "")
        )

    def _t_list_windows(self, args: dict[str, Any]) -> dict[str, Any]:
        out = []
        for hwnd, meta in self._windows.items():
            out.append({
                "pid": meta.get("pid", 0),
                "window_id": hwnd,
                "title": meta.get("title", ""),
            })
        return {"windows": out}

    def _t_type_text(self, args: dict[str, Any]) -> dict[str, Any]:
        hwnd = int(args.get("window_id") or 0)
        text = str(args.get("text") or "")
        if hwnd:
            _ensure_focus(hwnd)
            time.sleep(0.15)
        if "\n" in text:
            # For code blocks, clipboard paste is significantly more reliable
            # than per-character key events (fewer dropped symbols/indents).
            if self._paste_text(text):
                return {"ok": True, "typed": len(text), "mode": "paste"}
        # Type per-character with key presses so symbols like '*' register in
        # apps (e.g. Calculator) that ignore SendInput unicode bursts.
        for ch in text:
            self._type_char(ch)
        return {"ok": True, "typed": len(text), "mode": "type"}

    def _paste_text(self, text: str) -> bool:
        try:
            previous = pyperclip.paste()
        except Exception:  # noqa: BLE001
            previous = None
        try:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.08)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            if previous is not None:
                try:
                    pyperclip.copy(previous)
                except Exception:  # noqa: BLE001
                    pass

    def _type_char(self, ch: str) -> None:
        special = {
            "*": "multiply",
            "/": "divide",
            "+": "add",
            "-": "subtract",
            "=": "enter",
            "\n": "enter",
            "\t": "tab",
            " ": "space",
        }
        mapped = special.get(ch)
        if mapped:
            pyautogui.press(mapped)
        else:
            pyautogui.write(ch, interval=0.0)
        time.sleep(0.03)

    def _t_hotkey(self, args: dict[str, Any]) -> dict[str, Any]:
        keys = [str(k).lower() for k in (args.get("keys") or [])]
        if not keys:
            return {"ok": False}
        pyautogui.hotkey(*keys)
        return {"ok": True, "keys": keys}

    def _t_press_key(self, args: dict[str, Any]) -> dict[str, Any]:
        hwnd = int(args.get("window_id") or 0)
        key = str(args.get("key") or "").lower()
        if hwnd:
            _ensure_focus(hwnd)
        if key in ("enter", "return"):
            key = "enter"
        pyautogui.press(key)
        return {"ok": True, "key": key}

    def _t_maximize_window(self, args: dict[str, Any]) -> dict[str, Any]:
        hwnd = int(args.get("window_id") or 0)
        if not hwnd:
            return {"ok": False}
        SW_MAXIMIZE = 3
        ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        time.sleep(0.3)
        return {"ok": True}

    def _t_get_window_state(self, args: dict[str, Any]) -> dict[str, Any]:
        hwnd = int(args.get("window_id") or 0)
        mode = str(args.get("capture_mode") or "ax")
        if mode == "vision":
            out_file = args.get("screenshot_out_file")
            region = None
            if hwnd:
                _ensure_focus(hwnd)
                time.sleep(0.2)
                rect = _window_rect(hwnd)
                if rect:
                    left, top, right, bottom = rect
                    width, height = right - left, bottom - top
                    # Only crop when the rect is sane and on-screen; a
                    # minimized/odd window reports bogus geometry, so fall
                    # back to the full screen instead of an empty crop.
                    if width > 100 and height > 100:
                        region = (left, top, width, height)
            img = (pyautogui.screenshot(region=region) if region
                   else pyautogui.screenshot())
            if out_file:
                Path(out_file).parent.mkdir(parents=True, exist_ok=True)
                img.save(out_file)
            return {
                "screenshot": out_file or "",
                "mode": "vision",
                "cropped": region is not None,
            }
        # default: AX / UIA tree text
        tree = self._uia_dump(hwnd)
        return {"tree_markdown": tree, "mode": "ax"}

    def _t_start_recording(self, args: dict[str, Any]) -> dict[str, Any]:
        out_dir = Path(args.get("output_dir") or ".")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._rec_dir = out_dir
        self._rec_frames = 0
        self._rec_stop = threading.Event()
        self._rec_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._rec_thread.start()
        return {"ok": True, "output_dir": str(out_dir)}

    def _t_stop_recording(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._rec_stop is not None:
            self._rec_stop.set()
        if self._rec_thread is not None:
            self._rec_thread.join(timeout=3.0)
        frames = self._rec_frames
        self._rec_thread = None
        self._rec_stop = None
        return {"ok": True, "frames": frames}

    # ----- helpers --------------------------------------------------------
    def _record_loop(self) -> None:
        assert self._rec_dir is not None and self._rec_stop is not None
        idx = 0
        while not self._rec_stop.is_set():
            try:
                img = pyautogui.screenshot()
                img.save(self._rec_dir / f"frame_{idx:04d}.png")
                idx += 1
                self._rec_frames = idx
            except Exception:  # noqa: BLE001
                pass
            self._rec_stop.wait(0.7)

    def _find_window(self, hints: list[str]) -> tuple[int, str] | None:
        try:
            wins = pyautogui.getAllWindows()
        except Exception:  # noqa: BLE001
            return None
        for w in wins:
            title = (w.title or "").strip()
            if not title:
                continue
            low = title.lower()
            if any(h in low for h in hints):
                hwnd = getattr(w, "_hWnd", None)
                if hwnd is None:
                    continue
                try:
                    hwnd_int = int(hwnd)
                except (TypeError, ValueError):
                    continue
                if hwnd_int <= 0:
                    continue
                return hwnd_int, title
        return None

    def _wait_for_window(self, hints: list[str], timeout: float) -> tuple[int, str] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self._find_window(hints)
            if found is not None:
                return found
            time.sleep(0.4)
        return None

    def _uia_dump(self, hwnd: int, max_depth: int = 6, max_nodes: int = 400) -> str:
        try:
            import uiautomation as auto
        except Exception as exc:  # noqa: BLE001
            return f"(uiautomation unavailable: {exc})"
        try:
            if hwnd:
                root = auto.ControlFromHandle(hwnd)
            else:
                root = auto.GetFocusedControl()
        except Exception as exc:  # noqa: BLE001
            return f"(uia read failed: {exc})"
        if root is None:
            return "(no control)"

        lines: list[str] = []

        def walk(ctrl, depth: int) -> None:
            if depth > max_depth or len(lines) >= max_nodes:
                return
            try:
                name = ctrl.Name or ""
            except Exception:  # noqa: BLE001
                name = ""
            value = ""
            try:
                vp = ctrl.GetValuePattern()
                value = vp.Value or ""
            except Exception:  # noqa: BLE001
                value = ""
            try:
                ctype = ctrl.ControlTypeName
            except Exception:  # noqa: BLE001
                ctype = "Control"
            label = f"{'  ' * depth}{ctype}: {name} {value}".rstrip()
            if label.strip():
                lines.append(label)
            try:
                children = ctrl.GetChildren()
            except Exception:  # noqa: BLE001
                children = []
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return "\n".join(lines) if lines else "(empty tree)"
