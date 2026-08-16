"""Frozen shared contracts. Workers stub against these; do not redefine."""

from __future__ import annotations

from typing import Any, TypedDict


class ToolResult(TypedDict):
    name: str
    content: str
    meta: dict[str, Any]


class Citation(TypedDict):
    path: str
    start: int
    end: int


class AgentAnswer(TypedDict):
    text: str
    citations: list[Citation]
    turns: int


class SavingsStats(TypedDict):
    original: int
    compressed: int
    ratio: float
    saved: int
    cost_saved_usd: str
