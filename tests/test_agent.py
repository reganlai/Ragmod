from __future__ import annotations

import copy
import json
from typing import Any

from ragmod.agent.loop import ask, openai_chat_url


class ScriptedClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"answer.py","start":1,"end":2}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "The answer is implemented in answer.py."}}]},
        ]

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(copy.deepcopy(payload))
        return self.responses.pop(0)


def test_agent_executes_tool_and_returns_citations(tmp_path):
    (tmp_path / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    client = ScriptedClient()

    answer = ask("Where is the answer?", tmp_path, client=client, model="test-model")

    assert answer["text"] == "The answer is implemented in answer.py."
    assert answer["citations"] == [{"path": "answer.py", "start": 1, "end": 2}]
    assert answer["turns"] == 2
    assert client.payloads[0]["tool_choice"] == "auto"
    bootstrap_message = client.payloads[0]["messages"][-1]
    assert bootstrap_message["role"] == "tool"
    assert bootstrap_message["content"].startswith("# tool_result search_repo")
    bootstrap_args = json.loads(
        client.payloads[0]["messages"][-2]["tool_calls"][0]["function"]["arguments"]
    )
    assert bootstrap_args.get("glob") == "*.py"
    assert "answer.py:" in bootstrap_message["content"]
    tool_message = client.payloads[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("# tool_result read_file")


def test_agent_bootstraps_typescript_glob(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "LogViewer.tsx").write_text("export function LogViewer() {}\n", encoding="utf-8")
    (src / "logs.ts").write_text("export function streamLogs() {}\n", encoding="utf-8")

    class CaptureClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.payloads.append(copy.deepcopy(payload))
            return {"choices": [{"message": {"content": "Logs stream over WebSocket."}}]}

    client = CaptureClient()
    ask("How are logs displayed?", tmp_path, client=client, model="test-model", max_turns=1)
    args = json.loads(client.payloads[0]["messages"][-2]["tool_calls"][0]["function"]["arguments"])
    assert args.get("glob") == "*.ts,*.tsx"
    assert client.payloads[0]["tool_choice"] == "none"


def test_gemini_bootstrap_uses_real_tool_result(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "LogViewer.tsx").write_text("export function LogViewer() {}\n", encoding="utf-8")

    class GeminiScriptedClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []
            self._n = 0

        def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.payloads.append(copy.deepcopy(payload))
            self._n += 1
            if self._n == 1:
                assert payload["tool_choice"] == {
                    "type": "function",
                    "function": {"name": "search_repo"},
                }
                # No synthetic tool messages before the forced call.
                assert all(m["role"] != "tool" for m in payload["messages"])
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "extra_content": {"google": {"thought_signature": "sig"}},
                                "tool_calls": [
                                    {
                                        "id": "call_gemini_search",
                                        "type": "function",
                                        "function": {
                                            "name": "search_repo",
                                            "arguments": '{"pattern":"logs"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            assert any(m["role"] == "tool" for m in payload["messages"])
            tool_msgs = [m for m in payload["messages"] if m["role"] == "tool"]
            assert tool_msgs[0]["content"].startswith("# tool_result search_repo")
            return {"choices": [{"message": {"content": "See LogViewer.tsx"}}]}

    client = GeminiScriptedClient()
    answer = ask(
        "How are logs displayed?",
        tmp_path,
        client=client,
        model="gemini-flash-lite-latest",
        max_turns=2,
    )
    assert answer["text"] == "See LogViewer.tsx"
    assert answer["turns"] == 2
    assert len(client.payloads) == 2


def test_openai_chat_url_gemini_and_groq():
    assert openai_chat_url("https://api.groq.com/openai").endswith("/v1/chat/completions")
    assert (
        openai_chat_url("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
