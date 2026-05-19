"""Memory service — typed store with keyword-search reads and LLM-classified writes.

Persistence: state/memory.json
Reads are pure keyword overlap (no LLM).
Writes classify via LLM gateway (auto_route="memory").
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from schemas import MemoryItem, ToolCall as SchemaToolCall

sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM

STATE_DIR = Path(__file__).parent / "state"
MEMORY_PATH = STATE_DIR / "memory.json"

STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could of in on at to for with by from and or but "
    "not no nor so yet if then else this that these those it its i me my we our "
    "you your he him his she her they them their what which who whom how when where "
    "why all each every both few more most other some such".split()
)

MEMORY_CLASSIFIER_SYSTEM = """You are MemoryClassifier, part of a goal-directed agent's memory system.

You are called each time the agent produces new information that should be stored for future iterations.
Your classification enables efficient retrieval in subsequent conversation turns.

Task: Convert the provided raw_text into a single MemoryItem JSON object.

Step 1 — Identify reasoning type:
- classification: Determine what category this information belongs to.
- extraction: Pull structured data from unstructured text.
- Tag the reasoning type to stay focused on the task.

Step 2 — Classify the kind:
- Choose `kind` from: fact | preference | scratchpad | tool_outcome.
  - fact: Durable truths (dates, attributes, relationships, definitions).
  - preference: User-expressed preferences or constraints.
  - tool_outcome: Results from a tool dispatch (fetch, search, file ops).
  - scratchpad: Ephemeral working state, intermediate calculations.

Step 3 — Extract structured value:
- Extract a structured `value` dict (not just a copied paragraph).
- Identify key entities, relationships, and data points.
- For tool outcomes: include tool name, key arguments, and result summary.
- For facts: include subject, predicate, and object/value.

Step 4 — Generate retrieval keywords:
- Provide 4-12 `keywords` likely to match future queries.
- Include entity names, action verbs, and domain terms.
- Exclude stopwords and generic terms.

Step 5 — Write descriptor:
- Provide a short `descriptor` (one concise sentence summarizing the item).

Error handling and fallbacks:
- If the text is ambiguous between two kinds, prefer the more specific kind (fact > scratchpad).
- If the text is malformed or nearly empty, use kind="scratchpad" with confidence < 0.3.
- If you cannot extract structured value, set value to {"raw": "<first 200 chars>"} and confidence < 0.5.

Self-checks before output:
- descriptor is exactly 1 line (no newlines).
- value is a structured dict, not a raw paragraph copy.
- keywords are 4-12 meaningful terms (no stopwords).
- confidence reflects actual certainty (0.0-1.0).
- kind matches the classification criteria above.
- Verify output is valid JSON matching the MemoryItem schema.

Think step by step: tag reasoning type → classify kind → extract value → generate keywords → write descriptor → self-check → output.

Output must be valid JSON matching the MemoryItem schema exactly."""


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - STOPWORDS


class Memory:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._items: list[MemoryItem] = []
        self._loaded = False
        self._llm = LLM()

    def _load(self):
        if self._loaded:
            return
        if MEMORY_PATH.exists():
            raw = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            self._items = [MemoryItem(**item) for item in raw]
        self._loaded = True

    def _save(self):
        MEMORY_PATH.write_text(
            json.dumps([item.model_dump(mode="json") for item in self._items], indent=2, default=str),
            encoding="utf-8",
        )

    def read(self, query: str, history: list[dict] = None, kinds: list[str] = None, top_k: int = 8) -> list[MemoryItem]:
        """Keyword overlap search. No LLM cost."""
        self._load()
        query_tokens = _tokenize(query)
        if history:
            for event in history[-3:]:
                query_tokens |= _tokenize(str(event.get("text", "")))

        scored: list[tuple[float, MemoryItem]] = []
        for item in self._items:
            if kinds and item.kind not in kinds:
                continue
            item_tokens = set(item.keywords) | _tokenize(item.descriptor)
            overlap = len(query_tokens & item_tokens)
            if overlap > 0:
                scored.append((overlap, item))

        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:top_k]]

    def filter(self, kinds: list[str] = None, goal_id: str = None, recent: int = None) -> list[MemoryItem]:
        """Structured filter by kind, goal, recency."""
        self._load()
        results = self._items[:]
        if kinds:
            results = [i for i in results if i.kind in kinds]
        if goal_id:
            results = [i for i in results if i.goal_id == goal_id]
        if recent:
            results = results[-recent:]
        return results

    def remember(self, raw_text: str, source: str, run_id: str, goal_id: str = None) -> MemoryItem:
        """LLM-classified write. Extracts kind, keywords, descriptor, value."""
        self._load()
        item_id = str(uuid.uuid4())[:8]

        schema = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "preference", "scratchpad", "tool_outcome"]},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "descriptor": {"type": "string"},
                "value": {"type": "object"},
                "confidence": {"type": "number"},
            },
            "required": ["kind", "keywords", "descriptor", "value", "confidence"],
            "additionalProperties": False,
        }

        response = self._llm.chat(
            prompt=f"Classify this text:\n\n{raw_text}",
            system=MEMORY_CLASSIFIER_SYSTEM,
            auto_route="memory",
            provider="g",
            temperature=0.3,
            max_tokens=1024,
            response_format={"type": "json_schema", "schema": schema, "name": "MemoryClassification", "strict": True},
        )

        parsed = response.get("parsed") or json.loads(response.get("text", "{}"))

        item = MemoryItem(
            id=item_id,
            kind=parsed.get("kind", "scratchpad"),
            keywords=parsed.get("keywords", []),
            descriptor=parsed.get("descriptor", raw_text[:80]),
            value=parsed.get("value", {"raw": raw_text}),
            artifact_id=None,
            source=source,
            run_id=run_id,
            goal_id=goal_id,
            confidence=parsed.get("confidence", 0.5),
            created_at=datetime.utcnow(),
        )

        self._items.append(item)
        self._save()
        return item

    def record_outcome(
        self,
        tool_call: SchemaToolCall,
        result_text: str,
        artifact_id: Optional[str],
        source: str,
        run_id: str,
        goal_id: str,
    ) -> MemoryItem:
        """Record tool outcome. No LLM — kind is tool_outcome by construction."""
        self._load()
        item_id = str(uuid.uuid4())[:8]

        # Keywords from tool name + argument tokens
        keywords = [tool_call.name]
        for k, v in tool_call.arguments.items():
            keywords.extend(_tokenize(str(v)) - STOPWORDS)
        keywords = list(set(keywords))[:12]

        descriptor = f"{tool_call.name}({', '.join(f'{k}={v!r}' for k, v in list(tool_call.arguments.items())[:2])}) → {result_text[:60]}"

        item = MemoryItem(
            id=item_id,
            kind="tool_outcome",
            keywords=keywords,
            descriptor=descriptor,
            value={
                "tool": tool_call.name,
                "arguments": tool_call.arguments,
                "result_preview": result_text[:200],
            },
            artifact_id=artifact_id,
            source=source,
            run_id=run_id,
            goal_id=goal_id,
            confidence=1.0,
            created_at=datetime.utcnow(),
        )

        self._items.append(item)
        self._save()
        return item
