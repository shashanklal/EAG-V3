"""Prompt cache — no-op for Databricks Model Serving.

Databricks endpoints do not expose a Gemini-style explicit prompt caching API.
This module is retained as a placeholder so that any remaining import references
do not break. The class is a no-op: get_or_create always returns (None, 0).
"""
from __future__ import annotations
import asyncio
from typing import Optional


class PromptCache:
    """No-op cache placeholder for Databricks-served models."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds

    async def get_or_create(self, api_key: str, model: str, text: str, base_url: str) -> tuple[Optional[str], int]:
        """Always returns (None, 0) — no caching available."""
        return None, 0


# Keep the old name as an alias for backward compatibility.
GeminiCache = PromptCache
