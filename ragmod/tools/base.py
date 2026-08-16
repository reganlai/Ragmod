"""Tool names and OpenAI-style schemas for the Ragmod agent."""

from __future__ import annotations

from typing import Any

TOOL_NAMES = ("search_repo", "read_file", "list_dir", "run_tests")


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Full toolset so Paritok can stub unused schemas (cache-friendly)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_repo",
                "description": "Search the repository with ripgrep. Returns many hits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "ripgrep regular expression",
                        },
                        "glob": {
                            "type": "string",
                            "description": (
                                "Optional gitignore-style file glob, e.g. *.py or "
                                "comma-separated *.ts,*.tsx"
                            ),
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a source file with generous surrounding context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start": {
                            "type": "integer",
                            "description": "Optional 1-based starting line",
                        },
                        "end": {
                            "type": "integer",
                            "description": "Optional 1-based ending line",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List a directory in the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the project test suite and return stdout/stderr.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                    },
                },
            },
        },
    ]
