"""CLI entrypoint. Wave 0: gateway smoke helpers. Wave 1+: ask / bench."""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    from ragmod.gateway.proxy import repo_root

    load_dotenv(repo_root() / ".env")
    parser = argparse.ArgumentParser(prog="ragmod", description="Ragmod CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="Print Paritok /stats as SavingsStats")
    p_stats.add_argument("--port", type=int, default=None)

    p_health = sub.add_parser("health", help="Check Paritok proxy /health")
    p_health.add_argument("--port", type=int, default=None)

    p_ask = sub.add_parser("ask", help="Ask a question about a checked-out repository")
    p_ask.add_argument("question")
    p_ask.add_argument("--repo", default=".", help="Repository to inspect (default: current directory)")
    p_ask.add_argument("--port", type=int, default=None, help="Paritok proxy port")
    p_ask.add_argument("--model", default=None, help="Upstream model name")
    p_ask.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Max model turns (last turn forces a final answer)",
    )

    p_bench = sub.add_parser(
        "bench",
        help="A/B: tight baseline (direct) vs generous Ragmod (Paritok proxy)",
    )
    p_bench.add_argument("--repo", default=".", help="Repository to inspect")
    p_bench.add_argument("--port", type=int, default=None, help="Paritok proxy port")
    p_bench.add_argument("--model", default=None, help="Upstream model name")
    p_bench.add_argument("--max-turns", type=int, default=6)
    p_bench.add_argument(
        "--cooldown",
        type=float,
        default=20.0,
        help="Seconds to sleep between arms (Groq free-tier TPM)",
    )
    p_bench.add_argument(
        "--out",
        default="examples/savings_table.md",
        help="Markdown table output path",
    )
    p_bench.add_argument(
        "--tasks",
        default=None,
        help="Optional tasks JSON (default: ragmod/bench/tasks.json)",
    )

    args = parser.parse_args(argv)

    from ragmod.gateway import (
        fetch_stats,
        proxy_base_url,
        proxy_health,
        stats_to_savings,
    )

    base = proxy_base_url(args.port)

    if args.cmd == "ask":
        from ragmod.agent import ProxyChatClient, ask

        answer = ask(
            args.question,
            args.repo,
            client=ProxyChatClient(base_url=base),
            model=args.model,
            max_turns=args.max_turns,
        )
        print(answer["text"])
        if answer["citations"]:
            print("\nSources:")
            for citation in answer["citations"]:
                print(f"- {citation['path']}:{citation['start']}-{citation['end']}")
        print(f"\nTurns: {answer['turns']}")
        return 0

    if args.cmd == "bench":
        from pathlib import Path

        from ragmod.bench import run_bench, write_savings_table

        tasks = Path(args.tasks) if args.tasks else None
        print("Running A/B bench (baseline direct+tight, ragmod proxy+generous)...")
        results = run_bench(
            args.repo,
            tasks_path=tasks,
            max_turns=args.max_turns,
            model=args.model,
            cooldown_s=args.cooldown,
            proxy_port=args.port,
        )
        out = Path(args.out)
        table = write_savings_table(results, out)
        print(table)
        print(f"Wrote {out} and {out.with_suffix('.json')}")
        if any(r.error for r in results):
            return 1
        return 0

    if args.cmd == "health":
        health = proxy_health(base)
        if health is None:
            print(f"unhealthy: {base}", file=sys.stderr)
            return 1
        print(json.dumps(health, indent=2))
        return 0

    if args.cmd == "stats":
        try:
            raw = fetch_stats(base)
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"stats failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(stats_to_savings(raw), indent=2))
        print("--- raw ---")
        print(json.dumps(raw, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
