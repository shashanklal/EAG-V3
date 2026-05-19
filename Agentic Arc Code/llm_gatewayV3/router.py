"""Capability-aware router. Same RPM/RPD bookkeeping as V1, but now it can
skip providers that lack a requested capability (tools/reasoning/structured/caching).

All providers are Databricks Model Serving endpoints sharing the same workspace."""
from __future__ import annotations
import time, asyncio
from collections import deque, defaultdict


# Rate limits for Databricks Model Serving endpoints.
# These are shared-endpoint limits — adjust per your provisioned throughput.
LIMITS = {
    "llama31_8b": {"rpm": 60,  "rpd": 10000, "tpm": 100000, "cooldown": 1, "max_ctx": 8000},
    "gemma3_12b": {"rpm": 60,  "rpd": 10000, "tpm": 100000, "cooldown": 1, "max_ctx": 8000},
    "llama4_mav": {"rpm": 40,  "rpd": 10000, "tpm": 200000, "cooldown": 1, "max_ctx": 128000},
    "qwen3_80b":  {"rpm": 30,  "rpd": 5000,  "tpm": 150000, "cooldown": 2, "max_ctx": 32000},
    "claude_s4":  {"rpm": 30,  "rpd": 5000,  "tpm": 200000, "cooldown": 2, "max_ctx": 200000},
    "gpt_oss":    {"rpm": 30,  "rpd": 5000,  "tpm": 150000, "cooldown": 2, "max_ctx": 128000},
    "claude_s46": {"rpm": 30,  "rpd": 5000,  "tpm": 200000, "cooldown": 2, "max_ctx": 200000},
}

SHORTCUTS = {
    "l": "llama31_8b", "llama": "llama31_8b", "llama31": "llama31_8b", "llama31_8b": "llama31_8b",
    "g": "gemma3_12b", "gemma": "gemma3_12b", "gemma3": "gemma3_12b", "gemma3_12b": "gemma3_12b",
    "m": "llama4_mav", "mav": "llama4_mav", "maverick": "llama4_mav", "llama4_mav": "llama4_mav",
    "q": "qwen3_80b", "qwen": "qwen3_80b", "qwen3": "qwen3_80b", "qwen3_80b": "qwen3_80b",
    "c": "claude_s4", "claude": "claude_s4", "sonnet4": "claude_s4", "claude_s4": "claude_s4",
    "gpt": "gpt_oss", "oss": "gpt_oss", "gpt_oss": "gpt_oss",
    "c6": "claude_s46", "claude46": "claude_s46", "sonnet46": "claude_s46", "claude_s46": "claude_s46",
}


def resolve(name):
    if not name:
        return None
    return SHORTCUTS.get(name.lower())


class RateState:
    def __init__(self):
        self.calls_minute = deque()
        self.tokens_minute = deque()
        self.calls_today = 0
        self.tokens_today = 0
        self.day_start = self._day_start()
        self.last_call = 0.0
        self.unavailable_until = 0.0
        self.unavailable_reason = ""

    @staticmethod
    def _day_start():
        now = time.time()
        return now - (now % 86400)

    def gc(self):
        now = time.time()
        if now - self.day_start >= 86400:
            self.calls_today = 0
            self.tokens_today = 0
            self.day_start = self._day_start()
        cutoff = now - 60
        while self.calls_minute and self.calls_minute[0] < cutoff:
            self.calls_minute.popleft()
        while self.tokens_minute and self.tokens_minute[0][0] < cutoff:
            self.tokens_minute.popleft()

    def can_use(self, limits, est_tokens=0):
        self.gc()
        now = time.time()
        if now < self.unavailable_until:
            return False, f"backoff: {self.unavailable_reason} ({self.unavailable_until - now:.0f}s left)"
        wait = limits["cooldown"] - (now - self.last_call)
        if wait > 0:
            return False, f"cooldown ({wait:.1f}s)"
        if len(self.calls_minute) >= limits["rpm"]:
            return False, "RPM limit"
        if self.calls_today >= limits["rpd"]:
            return False, "RPD limit"
        tpm = sum(t for _, t in self.tokens_minute)
        if tpm + est_tokens > limits["tpm"]:
            return False, "TPM limit"
        if "tokens_per_day" in limits and self.tokens_today + est_tokens > limits["tokens_per_day"]:
            return False, "daily token cap"
        return True, None

    def record(self, tokens):
        now = time.time()
        self.calls_minute.append(now)
        self.tokens_minute.append((now, tokens))
        self.calls_today += 1
        self.tokens_today += tokens
        self.last_call = now

    def mark_unavailable(self, seconds: float, reason: str):
        self.unavailable_until = time.time() + seconds
        self.unavailable_reason = reason

    def snapshot(self, limits):
        self.gc()
        now = time.time()
        tpm = sum(t for _, t in self.tokens_minute)
        return {
            "rpm_used": len(self.calls_minute),
            "rpm_limit": limits["rpm"],
            "rpd_used": self.calls_today,
            "rpd_limit": limits["rpd"],
            "tpm_used": tpm,
            "tpm_limit": limits["tpm"],
            "tokens_today": self.tokens_today,
            "tokens_per_day": limits.get("tokens_per_day"),
            "cooldown_remaining": max(0, limits["cooldown"] - (now - self.last_call)) if self.last_call else 0,
            "last_call": self.last_call,
            "backoff_remaining": max(0, self.unavailable_until - now),
            "backoff_reason": self.unavailable_reason if now < self.unavailable_until else "",
        }


class Router:
    def __init__(self, providers: dict, order: list[str]):
        self.providers = providers
        self.order = [p for p in order if p in providers]
        self.state = defaultdict(RateState)
        self.lock = asyncio.Lock()

    def candidates(self, override=None):
        if override:
            r = resolve(override)
            return [r] if r and r in self.providers else []
        return list(self.order)

    def pick(self, est_tokens, candidates, required_caps: list[str] | None = None):
        attempts = []
        for name in candidates:
            limits = LIMITS[name]
            prov = self.providers[name]
            caps = getattr(prov, "capabilities", {})
            if required_caps:
                missing = [c for c in required_caps if not caps.get(c)]
                if missing:
                    attempts.append({"provider": name, "reason": f"skipped:no_{missing[0]}"})
                    continue
            if est_tokens > limits["max_ctx"]:
                attempts.append({"provider": name, "reason": f"prompt {est_tokens} > max_ctx {limits['max_ctx']}"})
                continue
            ok, why = self.state[name].can_use(limits, est_tokens)
            if ok:
                return name, attempts
            attempts.append({"provider": name, "reason": why})
        return None, attempts

    def all_status(self):
        out = {}
        for name in self.providers:
            out[name] = self.state[name].snapshot(LIMITS[name])
            out[name]["model"] = self.providers[name].model
            out[name]["capabilities"] = getattr(self.providers[name], "capabilities", {})
        return out


# -----------------------------------------------------------------------------
# V3 Router pool — separate failover ring for routing-decision LLM calls.
# Same rate-state machinery, separate state dict so router quotas never compete
# with worker quotas (provider keys are shared but providers meter per-model).
# -----------------------------------------------------------------------------

DEFAULT_ROUTER_ORDER = ["llama31_8b", "gemma3_12b"]


class RouterPool:
    """Failover ring for router-LLM calls. Mirrors `Router` but for the
    Perception/Memory/Decision routing classifiers. Each call is logged with
    a call_role marker (router_perception | router_memory | router_decision)
    so the dashboard can show router activity separately from worker activity.
    """
    def __init__(self, providers: dict, order: list[str]):
        self.providers = providers
        self.order = [p for p in order if p in providers]
        self.state = defaultdict(RateState)
        self.lock = asyncio.Lock()

    def candidates(self):
        return list(self.order)

    def pick(self, est_tokens=400):
        """Pick first available router provider. Caps require nothing — router
        LLMs only need to emit one word, no tools/reasoning/structured needed."""
        attempts = []
        for name in self.candidates():
            limits = LIMITS[name]
            ok, why = self.state[name].can_use(limits, est_tokens)
            if ok:
                return name, attempts
            attempts.append({"provider": name, "reason": why})
        return None, attempts

    def all_status(self):
        out = {}
        for name in self.providers:
            out[name] = self.state[name].snapshot(LIMITS[name])
            out[name]["model"] = self.providers[name].model
        return out
