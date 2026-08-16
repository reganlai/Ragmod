#!/usr/bin/env python3
"""Wave 0 gate: one real LLM call through Paritok hosted GPU with fat tool_result.

Done when /stats shows non-zero tokens_saved.

Usage (two terminals):
  # terminal 1
  ./scripts/start_proxy.sh

  # terminal 2
  python scripts/wave0_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from ragmod.gateway import (  # noqa: E402
    ensure_paritok_yaml,
    fetch_stats,
    proxy_base_url,
    resolve_paritok_api_key,
    stats_to_savings,
    wait_for_proxy,
)
from ragmod.tools.base import openai_tool_schemas  # noqa: E402


def fat_tool_result() -> str:
    """On-distribution code blob — Paritok compresses tool_result / file_read shape."""
    sample = (ROOT / "ragmod" / "gateway" / "proxy.py").read_text(encoding="utf-8")
    # Keep under Groq free-tier TPM (~6k) while still giving Paritok a fat tool_result.
    body = "\n\n".join(
        f"# --- read_file ragmod/gateway/proxy.py pass {i} ---\n{sample}" for i in range(2)
    )
    return f"# tool_result read_file\n{body}"


def _gemini_style(model: str) -> bool:
    name = model.lower()
    return "gemini" in name or "gemma" in name


def build_openai_payload(model: str) -> dict:
    """Groq/OpenAI path: synthetic assistant tool_call + fat tool_result is fine."""
    tool_call_id = "call_wave0_read_file"
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a terse code assistant. Answer from the tool result only. "
                    "One short sentence."
                ),
            },
            {
                "role": "user",
                "content": "What does ensure_paritok_yaml do?",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(
                                {"path": "ragmod/gateway/proxy.py"}
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": fat_tool_result(),
            },
        ],
        "tools": openai_tool_schemas(),
        "max_tokens": 80,
        "temperature": 0,
    }


def run_gemini_fat_tool_call(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    model: str,
) -> str:
    """Gemini needs a real signed tool_call before a role=tool payload is accepted."""
    base_messages = [
        {
            "role": "system",
            "content": (
                "You are a terse code assistant. Answer from the tool result only. "
                "One short sentence."
            ),
        },
        {
            "role": "user",
            "content": "What does ensure_paritok_yaml do? Call read_file on "
            "ragmod/gateway/proxy.py first.",
        },
    ]
    force = {
        "model": model,
        "messages": base_messages,
        "tools": openai_tool_schemas(),
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
        "temperature": 0,
    }
    resp = client.post(url, headers=headers, json=force)
    if resp.status_code >= 400:
        raise RuntimeError(f"force tool call failed {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    message = data["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError(f"Gemini did not return a tool call: {json.dumps(data)[:500]}")

    assistant = {
        key: value
        for key, value in message.items()
        if key
        in {"content", "tool_calls", "name", "extra_content", "reasoning_content", "refusal"}
    }
    assistant["role"] = "assistant"
    answer_payload = {
        "model": model,
        "messages": [
            *base_messages,
            assistant,
            {
                "role": "tool",
                "tool_call_id": str(tool_calls[0].get("id", "call_wave0_read_file")),
                "content": fat_tool_result(),
            },
        ],
        "tools": openai_tool_schemas(),
        "tool_choice": "none",
        "max_tokens": 80,
        "temperature": 0,
    }
    resp2 = client.post(url, headers=headers, json=answer_payload)
    if resp2.status_code >= 400:
        raise RuntimeError(f"answer call failed {resp2.status_code}: {resp2.text[:500]}")
    data2 = resp2.json()
    return str(data2["choices"][0]["message"].get("content") or "")


def main() -> int:
    ensure_paritok_yaml()

    if not resolve_paritok_api_key():
        print(
            "Missing PARITOK_API_KEY (env or paritok.yaml gpu_server.api_key).\n"
            "Get one at https://paritok.com → dashboard → API keys.",
            file=sys.stderr,
        )
        return 2

    openai_key = (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )
    if not openai_key:
        print(
            "Missing OPENAI_API_KEY (upstream LLM; proxy forwards it).\n"
            "Free option — Gemini or Groq in .env (see .env.example).",
            file=sys.stderr,
        )
        return 2

    base = proxy_base_url()
    print(f"Waiting for proxy at {base} ...")
    try:
        health = wait_for_proxy(base, timeout_s=30)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        print(
            "Start it with:\n"
            "  ./scripts/start_proxy.sh",
            file=sys.stderr,
        )
        return 1
    print("health:", json.dumps(health))

    before_raw = fetch_stats(base)
    before = stats_to_savings(before_raw)
    print("stats before:", json.dumps(before, indent=2))

    model = os.environ.get("RAGMOD_MODEL", "gpt-4o-mini")
    url = f"{base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }
    print(f"POST {url} model={model} (fat tool_result) ...")

    with httpx.Client(timeout=180.0) as client:
        try:
            if _gemini_style(model):
                text = run_gemini_fat_tool_call(
                    client, url=url, headers=headers, model=model
                )
            else:
                resp = client.post(
                    url, headers=headers, json=build_openai_payload(model)
                )
                if resp.status_code >= 400:
                    print(
                        f"upstream/proxy error {resp.status_code}:\n{resp.text}",
                        file=sys.stderr,
                    )
                    return 1
                data = resp.json()
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    text = json.dumps(data)[:500]
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print("model reply:", text)

    after_raw = fetch_stats(base)
    after = stats_to_savings(after_raw)
    delta_saved = int(after["saved"]) - int(before["saved"])
    print("stats after:", json.dumps(after, indent=2))
    print("raw /stats:", json.dumps(after_raw, indent=2))
    print(f"tokens_saved delta this run: {delta_saved}")

    if delta_saved <= 0:
        print(
            "FAIL: tokens_saved delta is still <= 0. Compression did not engage "
            "(check tool_result shape / use_gpu_server).",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nWAVE 0 PASS — tokens_saved_delta={delta_saved} "
        f"ratio={after['ratio']} cost_saved={after['cost_saved_usd']}"
    )
    print("Confirm the same call on the Paritok dashboard at https://paritok.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
