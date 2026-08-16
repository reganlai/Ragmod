"""OpenAI-compatible, multi-turn tool-calling loop routed through Paritok."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from ragmod.contracts import AgentAnswer, Citation, ToolResult
from ragmod.gateway import proxy_base_url
from ragmod.tools import RepositoryTools, detect_source_glob
from ragmod.tools.base import openai_tool_schemas

SYSTEM_PROMPT = """You are Ragmod, a codebase retrieval agent.
The conversation begins with a repository search result. Pick the file that actually
defines the behavior being asked about, then call read_file on that file before
answering. Prefer implementation modules over scripts, tests, or docs when both match.
Answer only from tool results. Never invent file paths. Never say information is
unavailable when the search result names relevant files; inspect those files instead.
Be concise, explain uncertainty, and stop calling tools once you can answer.
Citations are attached from tool metadata — reading the right file matters."""

# Cap a single tool_result so one huge read cannot blow provider TPM / context.
_MAX_TOOL_CHARS = 100_000

_SEARCH_STOPWORDS = {
    "about",
    "architecture",
    "are",
    "available",
    "code",
    "codebase",
    "describe",
    "displayed",
    "does",
    "explain",
    "find",
    "from",
    "have",
    "how",
    "info",
    "information",
    "into",
    "onto",
    "please",
    "project",
    "repository",
    "requested",
    "summarize",
    "summary",
    "that",
    "the",
    "this",
    "turn",
    "using",
    "what",
    "where",
    "which",
    "with",
    "would",
    "you",
    "your",
}


class ChatClient(Protocol):
    def complete(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def openai_chat_url(base: str) -> str:
    """Resolve Chat Completions URL the same way Paritok's proxy does.

    Groq ``…/openai`` → ``…/openai/v1/chat/completions``.
    Gemini ``…/v1beta/openai`` → ``…/v1beta/openai/chat/completions``.
    """
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if re.search(r"/v\d", urlsplit(base).path):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def resolve_api_key(explicit: str | None = None) -> str:
    """Prefer OPENAI_API_KEY; accept GEMINI_API_KEY as an alias."""
    if explicit and explicit.strip():
        return explicit.strip()
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )


class ProxyChatClient:
    """OpenAI-compatible client. Default target is the local Paritok proxy."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 180.0,
        *,
        resolve_chat_url: bool = False,
    ) -> None:
        raw = (base_url or proxy_base_url()).rstrip("/")
        # Local Paritok always exposes /v1/chat/completions. Direct upstreams
        # (bench baseline, smoke) need Gemini/Groq-aware joining.
        self.base_url = raw
        self.endpoint = (
            openai_chat_url(raw) if resolve_chat_url else f"{raw}/v1/chat/completions"
        )
        self.api_key = resolve_api_key(api_key)
        self.timeout = timeout

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY (or GEMINI_API_KEY) is required for the upstream model"
            )
        # Retry briefly on rate / size pressure (Groq TPM; Gemini occasional 429).
        delays = (2.0, 5.0, 15.0, 35.0)
        last_detail = ""
        with httpx.Client(timeout=self.timeout) as client:
            for attempt, delay in enumerate((*delays, None)):
                response = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if response.status_code not in {429, 413}:
                    try:
                        response.raise_for_status()
                        return response.json()
                    except (httpx.HTTPError, ValueError) as exc:
                        detail = response.text[:500]
                        raise RuntimeError(f"Proxy request failed: {detail}") from exc
                last_detail = response.text[:500]
                if delay is None:
                    break
                time.sleep(delay)
        raise RuntimeError(f"Proxy request failed after retries: {last_detail}")


class TrackingClient:
    """Wraps a ChatClient and sums provider usage.prompt_tokens across turns."""

    def __init__(self, inner: ChatClient) -> None:
        self.inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.requests = 0

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.inner.complete(payload)
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.requests += 1
        return data


def ask(
    question: str,
    repo: Path | str,
    *,
    client: ChatClient | None = None,
    tools: RepositoryTools | None = None,
    model: str | None = None,
    max_turns: int = 8,
) -> AgentAnswer:
    """Answer a repository question through a bounded tool-calling conversation."""
    if not question.strip():
        raise ValueError("question must not be empty")
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")

    tools = tools or RepositoryTools(repo)
    chat = client or ProxyChatClient()
    selected_model = model or os.environ.get("RAGMOD_MODEL", "gpt-4o-mini")
    gemini_tools = _gemini_style_model(selected_model)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    citations: list[Citation] = []

    # Seed every run with a broad, on-distribution retrieval dump. Language glob is
    # detected from the repo so TS/Go/etc. repos are not stuck searching *.py.
    bootstrap_glob = detect_source_glob(tools.root)
    pattern = _bootstrap_search_pattern(question)
    bootstrap_args: dict[str, Any] = {"pattern": pattern}
    if bootstrap_glob:
        bootstrap_args["glob"] = bootstrap_glob
    bootstrap = tools.execute("search_repo", bootstrap_args)
    used_args = dict(bootstrap_args)
    if bootstrap_glob and "No matches found" in bootstrap.get("content", ""):
        used_args = {"pattern": pattern}
        bootstrap = tools.execute("search_repo", used_args)

    start_turn = 1
    if gemini_tools:
        # Gemini thinking models reject *synthetic* assistant tool_calls (no
        # thought_signature). Force a real search_repo call, then attach our
        # precomputed hits as role=tool so Paritok can compress them.
        start_turn = _seed_gemini_bootstrap(
            chat,
            messages,
            selected_model,
            bootstrap=bootstrap,
            used_args=used_args,
        )
    else:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bootstrap_search",
                            "type": "function",
                            "function": {
                                "name": "search_repo",
                                "arguments": json.dumps(used_args),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_bootstrap_search",
                    "content": _tool_content(bootstrap),
                },
            ]
        )

    for turn in range(start_turn, max_turns + 1):
        last_turn = turn == max_turns
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "tools": openai_tool_schemas(),
            # Force a written answer on the final turn instead of dying mid-tool-loop.
            "tool_choice": "none" if last_turn else "auto",
            "temperature": 0,
        }
        data = chat.complete(payload)
        message = _response_message(data)
        tool_calls = message.get("tool_calls") or []
        messages.append(_assistant_message(message))

        if last_turn or not tool_calls:
            text = str(message.get("content") or "").strip()
            if not text:
                text = (
                    "Stopped after the turn budget without a usable answer. "
                    "Try a narrower question or raise --max-turns."
                )
            return AgentAnswer(text=text, citations=_dedupe_citations(citations), turns=turn)

        for tool_call in tool_calls:
            tool_name, arguments = _tool_call_parts(tool_call)
            result = tools.execute(tool_name, arguments)
            if tool_name == "read_file":
                citations.extend(result.get("meta", {}).get("citations", []))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tool_call.get("id", tool_name)),
                    "content": _tool_content(result),
                }
            )

    return AgentAnswer(
        text=f"Stopped after {max_turns} tool turns before reaching a final answer.",
        citations=_dedupe_citations(citations),
        turns=max_turns,
    )


def _response_message(data: dict[str, Any]) -> dict[str, Any]:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected chat completion response: {json.dumps(data)[:500]}") from exc
    if not isinstance(message, dict):
        raise RuntimeError("Unexpected chat completion message")
    return message


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    # Preserve Gemini thought_signature / extra_content so follow-up tool turns work.
    keep = {
        "content",
        "tool_calls",
        "name",
        "extra_content",
        "reasoning_content",
        "refusal",
    }
    return {key: value for key, value in message.items() if key in keep} | {
        "role": "assistant"
    }


def _gemini_style_model(model: str) -> bool:
    name = model.lower()
    return "gemini" in name or "gemma" in name


def _seed_gemini_bootstrap(
    chat: ChatClient,
    messages: list[dict[str, Any]],
    model: str,
    *,
    bootstrap: ToolResult,
    used_args: dict[str, Any],
) -> int:
    """Force a signed search_repo tool call; return next turn index (usually 2)."""
    payload = {
        "model": model,
        "messages": messages
        + [
            {
                "role": "user",
                "content": (
                    "Call search_repo now with this exact JSON arguments object "
                    f"(do not invent a different pattern):\n{json.dumps(used_args)}"
                ),
            }
        ],
        "tools": openai_tool_schemas(),
        "tool_choice": {"type": "function", "function": {"name": "search_repo"}},
        "temperature": 0,
    }
    data = chat.complete(payload)
    message = _response_message(data)
    tool_calls = message.get("tool_calls") or []
    messages.append(_assistant_message(message))

    if not tool_calls:
        # Last resort: keep answerable context, but Paritok won't compress this path.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Initial repository search results (tool call unavailable):\n\n"
                    f"{_tool_content(bootstrap)}"
                ),
            }
        )
        return 1

    # Prefer our language-aware bootstrap body so Paritok sees a fat tool_result.
    tool_call = tool_calls[0]
    messages.append(
        {
            "role": "tool",
            "tool_call_id": str(tool_call.get("id", "call_bootstrap_search")),
            "content": _tool_content(bootstrap),
        }
    )
    # Drop the nudge user message from history? It's already not in `messages`
    # (only injected into the force payload). Good.
    return 2


def _tool_call_parts(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = tool_call.get("function") or {}
    name = str(function.get("name") or "")
    raw_arguments = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def _tool_content(result: ToolResult) -> str:
    """Keep a tool_result-shaped payload so Paritok can compress the large content."""
    body = f"# tool_result {result['name']}\n{result['content']}"
    if len(body) <= _MAX_TOOL_CHARS:
        return body
    return body[: _MAX_TOOL_CHARS] + "\n[truncated: tool_result exceeded size cap]"


def _bootstrap_search_pattern(question: str) -> str:
    """Turn a question into a broad, safe regex for the first repository search."""
    terms = []
    for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question.lower()):
        if term not in _SEARCH_STOPWORDS and term not in terms:
            terms.append(term)
        if len(terms) == 5:
            break
    return "|".join(re.escape(term) for term in terms) or "TODO|FIXME"


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    seen: set[tuple[str, int, int]] = set()
    unique: list[Citation] = []
    for citation in citations:
        try:
            key = (citation["path"], int(citation["start"]), int(citation["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            unique.append(Citation(path=key[0], start=key[1], end=key[2]))
    return unique
