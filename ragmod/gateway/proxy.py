"""Paritok proxy helpers — config, health, /stats → SavingsStats."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from ragmod.contracts import SavingsStats

DEFAULT_PORT = 8080
DEFAULT_BASE = f"http://127.0.0.1:{DEFAULT_PORT}"

# Minimal hosted-GPU config matching hackathon resources.
# https://build-with-paritok.devpost.com/resources
PARITOK_YAML_TEMPLATE = """\
# Ragmod — Paritok hosted GPU (hackathon requirement)
# Key: set gpu_server.api_key below OR export PARITOK_API_KEY
use_gpu_server: true
gpu_server:
  api_key: ""
tool_discovery:
  strategy: embedding
"""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def paritok_yaml_path() -> Path:
    return repo_root() / "paritok.yaml"


def ensure_paritok_yaml() -> Path:
    """Write paritok.yaml if missing. Never overwrite an existing file."""
    path = paritok_yaml_path()
    if not path.exists():
        path.write_text(PARITOK_YAML_TEMPLATE, encoding="utf-8")
    return path


def load_paritok_config() -> dict[str, Any]:
    path = ensure_paritok_yaml()
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def resolve_paritok_api_key() -> str | None:
    env = os.environ.get("PARITOK_API_KEY", "").strip()
    if env:
        return env
    cfg = load_paritok_config()
    key = (cfg.get("gpu_server") or {}).get("api_key") or ""
    key = str(key).strip()
    return key or None


def proxy_base_url(port: int | None = None) -> str:
    port = port or int(os.environ.get("RAGMOD_PROXY_PORT", DEFAULT_PORT))
    return f"http://127.0.0.1:{port}"


def proxy_env(port: int | None = None) -> dict[str, str]:
    """Env vars so OpenAI/Anthropic SDKs hit the local Paritok proxy."""
    base = proxy_base_url(port)
    return {
        "OPENAI_BASE_URL": base,
        "ANTHROPIC_BASE_URL": base,
    }


def proxy_health(base: str | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    base = base or proxy_base_url()
    try:
        r = httpx.get(f"{base}/health", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def wait_for_proxy(
    base: str | None = None,
    timeout_s: float = 60.0,
    interval_s: float = 0.5,
) -> dict[str, Any]:
    base = base or proxy_base_url()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        health = proxy_health(base)
        if health is not None:
            return health
        time.sleep(interval_s)
    raise TimeoutError(f"Paritok proxy not healthy at {base}/health within {timeout_s}s")


def fetch_stats(base: str | None = None, timeout: float = 15.0) -> dict[str, Any]:
    """GET /stats. Timeout is generous: the proxy is single-worker and can stall
    while a long chat/completion is in flight."""
    base = base or proxy_base_url()
    try:
        r = httpx.get(f"{base}/stats", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.TimeoutException as exc:
        raise TimeoutError(
            f"Timed out talking to {base}/stats after {timeout}s. "
            "The Paritok proxy is usually busy on another request "
            "(ask/bench/smoke). Wait for that to finish, or restart "
            "./scripts/start_proxy.sh and try again."
        ) from exc


def stats_to_savings(raw: dict[str, Any]) -> SavingsStats:
    """Map Paritok /stats JSON onto the frozen SavingsStats contract."""
    original = int(raw.get("input_tokens_original") or 0)
    compressed = int(raw.get("input_tokens_compressed") or 0)
    saved = int(raw.get("tokens_saved") or max(0, original - compressed))
    ratio = float(raw.get("compression_ratio") or (compressed / original if original else 1.0))
    cost = raw.get("estimated_cost_saved_usd")
    if cost is None:
        cost = "$0.00"
    return SavingsStats(
        original=original,
        compressed=compressed,
        ratio=ratio,
        saved=saved,
        cost_saved_usd=str(cost),
    )
