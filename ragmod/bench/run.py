"""Run the fixed task set twice: tight baseline (direct) vs generous Ragmod (proxy)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ragmod.agent.loop import ProxyChatClient, TrackingClient, ask
from ragmod.gateway import fetch_stats, proxy_base_url, proxy_health
from ragmod.tools import RepositoryTools
from ragmod.tools.repo import GENEROUS, TIGHT

DEFAULT_TASKS = Path(__file__).with_name("tasks.json")


@dataclass
class ArmResult:
    arm: str
    task_id: str
    question: str
    prompt_tokens: int
    completion_tokens: int
    requests: int
    latency_s: float
    turns: int
    quality: int
    quality_max: int
    answer: str
    citations: list[dict[str, Any]]
    error: str | None = None
    proxy_tokens_saved_delta: int | None = None


def load_tasks(path: Path | None = None) -> list[dict[str, Any]]:
    tasks_path = path or DEFAULT_TASKS
    data = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"No tasks in {tasks_path}")
    return data


def score_quality(
    answer_text: str,
    citations: list[dict[str, Any]],
    expect_path: str,
    expect_keywords: list[str],
) -> int:
    """Simple 0–2 rubric: keyword hit + citation path hit."""
    text = answer_text.lower()
    score = 0
    if any(kw.lower() in text for kw in expect_keywords):
        score += 1
    if any(expect_path in str(c.get("path", "")) for c in citations):
        score += 1
    return score


def _direct_base_url() -> str:
    """Upstream OpenAI-compat host used for the no-proxy baseline arm."""
    return (
        os.environ.get("RAGMOD_DIRECT_BASE_URL")
        or os.environ.get("RAGMOD_OPENAI_URL")
        or "https://api.groq.com/openai"
    ).rstrip("/")


def _run_arm(
    *,
    arm: str,
    task: dict[str, Any],
    repo: Path,
    client: ProxyChatClient,
    policy_label: str,
    max_turns: int,
    model: str | None,
    cooldown_s: float,
) -> ArmResult:
    policy = TIGHT if policy_label == "tight" else GENEROUS
    tracker = TrackingClient(client)
    tools = RepositoryTools(repo, policy=policy)
    t0 = time.perf_counter()
    error = None
    answer_text = ""
    citations: list[dict[str, Any]] = []
    turns = 0
    proxy_delta = None

    stats_before = None
    if arm == "ragmod":
        try:
            stats_before = fetch_stats()
        except Exception:
            stats_before = None

    try:
        result = ask(
            task["question"],
            repo,
            client=tracker,
            tools=tools,
            model=model,
            max_turns=max_turns,
        )
        answer_text = result["text"]
        citations = list(result["citations"])
        turns = int(result["turns"])
    except Exception as exc:  # noqa: BLE001 - bench must keep going
        error = str(exc)[:500]

    latency = time.perf_counter() - t0
    if arm == "ragmod" and stats_before is not None:
        try:
            after = fetch_stats()
            proxy_delta = int(after.get("tokens_saved") or 0) - int(
                stats_before.get("tokens_saved") or 0
            )
        except Exception:
            proxy_delta = None

    quality = score_quality(
        answer_text,
        citations,
        str(task.get("expect_path") or ""),
        list(task.get("expect_keywords") or []),
    )

    if cooldown_s > 0:
        time.sleep(cooldown_s)

    return ArmResult(
        arm=arm,
        task_id=str(task["id"]),
        question=str(task["question"]),
        prompt_tokens=tracker.prompt_tokens,
        completion_tokens=tracker.completion_tokens,
        requests=tracker.requests,
        latency_s=round(latency, 3),
        turns=turns,
        quality=quality,
        quality_max=2,
        answer=answer_text,
        citations=citations,
        error=error,
        proxy_tokens_saved_delta=proxy_delta,
    )


def run_bench(
    repo: Path | str = ".",
    *,
    tasks_path: Path | None = None,
    max_turns: int = 6,
    model: str | None = None,
    cooldown_s: float = 20.0,
    proxy_port: int | None = None,
) -> list[ArmResult]:
    """Run every task on baseline then Ragmod. Returns flat list of arm results."""
    repo_path = Path(repo).resolve()
    tasks = load_tasks(tasks_path)
    model = model or os.environ.get("RAGMOD_MODEL", "llama-3.1-8b-instant")

    proxy_url = proxy_base_url(proxy_port)
    if proxy_health(proxy_url) is None:
        raise RuntimeError(
            f"Paritok proxy unhealthy at {proxy_url}. Start it with ./scripts/start_proxy.sh"
        )

    baseline_client = ProxyChatClient(
        base_url=_direct_base_url(),
        resolve_chat_url=True,
    )
    ragmod_client = ProxyChatClient(base_url=proxy_url)

    results: list[ArmResult] = []
    for task in tasks:
        results.append(
            _run_arm(
                arm="baseline",
                task=task,
                repo=repo_path,
                client=baseline_client,
                policy_label="tight",
                max_turns=max_turns,
                model=model,
                cooldown_s=cooldown_s,
            )
        )
        results.append(
            _run_arm(
                arm="ragmod",
                task=task,
                repo=repo_path,
                client=ragmod_client,
                policy_label="generous",
                max_turns=max_turns,
                model=model,
                cooldown_s=cooldown_s,
            )
        )
    return results


def write_savings_table(
    results: list[ArmResult],
    out_path: Path,
) -> str:
    """Write markdown savings table. Returns the markdown text."""
    by_task: dict[str, dict[str, ArmResult]] = {}
    for row in results:
        by_task.setdefault(row.task_id, {})[row.arm] = row

    lines = [
        "# Ragmod savings table",
        "",
        "Regenerate with: `ragmod bench --repo . --out examples/savings_table.md`",
        "",
        "Arms:",
        "- **baseline** — direct upstream (`RAGMOD_OPENAI_URL`), tight retrieval",
        "- **ragmod** — Paritok hosted-GPU proxy, generous retrieval",
        "",
        "Token counts are provider `usage.prompt_tokens` (what the upstream billed).",
        "For ragmod that is post-compression. `proxy_saved` is the Paritok `/stats` delta.",
        "",
        "| task | baseline prompt toks | ragmod prompt toks | Δ tokens | baseline quality | ragmod quality | baseline latency (s) | ragmod latency (s) | proxy_saved |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    total_base = total_rag = 0
    for task_id, arms in by_task.items():
        base = arms.get("baseline")
        rag = arms.get("ragmod")
        if not base or not rag:
            continue
        b_tok = base.prompt_tokens
        r_tok = rag.prompt_tokens
        total_base += b_tok
        total_rag += r_tok
        delta = b_tok - r_tok
        proxy_saved = rag.proxy_tokens_saved_delta
        proxy_cell = "—" if proxy_saved is None else str(proxy_saved)
        b_q = f"{base.quality}/{base.quality_max}" + (" ERR" if base.error else "")
        r_q = f"{rag.quality}/{rag.quality_max}" + (" ERR" if rag.error else "")
        lines.append(
            f"| {task_id} | {b_tok} | {r_tok} | {delta} | {b_q} | {r_q} | "
            f"{base.latency_s} | {rag.latency_s} | {proxy_cell} |"
        )

    saved_vs_tight = total_base - total_rag
    ratio = (total_rag / total_base) if total_base else 0.0
    proxy_total = sum(
        int(r.proxy_tokens_saved_delta)
        for r in results
        if r.arm == "ragmod" and r.proxy_tokens_saved_delta is not None
    )
    lines.extend(
        [
            "",
            f"**Provider totals (usage.prompt_tokens):** baseline/tight `{total_base}` · "
            f"ragmod/generous+proxy `{total_rag}` · Δ vs tight `{saved_vs_tight}` · "
            f"ratio `{ratio:.3f}` (ragmod/baseline; lower means Ragmod billed less).",
            "",
            f"**Paritok `/stats` on Ragmod arms only:** `tokens_saved` delta sum = "
            f"`{proxy_total}`. This is the compression win on the generous tool_result "
            f"path (original − compressed), independent of the tight baseline.",
            "",
            "Note: Δ vs tight can be negative. Over-retrieval + compression can still "
            "bill more than a deliberately tiny baseline, while improving recall/quality "
            "and showing large `/stats` savings against the uncompressed generous prompt.",
            "",
            "## Per-arm notes",
            "",
        ]
    )
    for row in results:
        err = f" · error: {row.error}" if row.error else ""
        cite = ", ".join(
            f"{c.get('path')}:{c.get('start')}-{c.get('end')}" for c in row.citations[:3]
        ) or "—"
        lines.append(
            f"- `{row.task_id}`/{row.arm}: turns={row.turns}, cites={cite}{err}"
        )
        if row.answer:
            snippet = row.answer.replace("\n", " ")[:160]
            lines.append(f"  - answer: {snippet}")

    # Machine-readable sidecar next to the markdown.
    json_path = out_path.with_suffix(".json")
    payload = [asdict(r) for r in results]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return text
