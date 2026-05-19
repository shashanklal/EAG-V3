"""Pydantic models for the agentic architecture.

These models define the contracts at every role boundary:
Memory, Perception, Decision, Action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict
    artifact_id: Optional[str] = None
    source: str
    run_id: str
    goal_id: Optional[str] = None
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Artifact(BaseModel):
    id: str  # format: art:<sha256-prefix>
    content_type: str
    size_bytes: int
    source: str
    descriptor: str


class Goal(BaseModel):
    id: str
    text: str
    done: bool = False
    attach_artifact_id: Optional[str] = None


class Observation(BaseModel):
    goals: list[Goal]


class ToolCall(BaseModel):
    name: str
    arguments: dict


class DecisionOutput(BaseModel):
    answer: Optional[str] = None
    tool_call: Optional[ToolCall] = None
