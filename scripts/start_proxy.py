#!/usr/bin/env python3
"""Cross-platform Python launcher for Paritok proxy (Windows & POSIX)."""

import os
import subprocess
import tempfile
import sys
from pathlib import Path
from dotenv import load_dotenv
import yaml

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

key = os.environ.get("PARITOK_API_KEY", "").strip()
if not key:
    print("Error: PARITOK_API_KEY is missing from .env", file=sys.stderr)
    sys.exit(1)

url = os.environ.get("RAGMOD_OPENAI_URL", "").strip()
cfg_path = ROOT / "paritok.yaml"

with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}

cfg["use_gpu_server"] = True
cfg.setdefault("gpu_server", {})["api_key"] = key
cfg.setdefault("tool_discovery", {})["strategy"] = "embedding"

tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml", encoding="utf-8")
yaml.safe_dump(cfg, tmp, default_flow_style=False)
tmp_path = tmp.name
tmp.close()

cmd = [sys.executable, "-m", "paritok.cli", "proxy", "--port", "8080", "--config-file", tmp_path]
if url:
    cmd.extend(["--openai-url", url])

print(f"Starting Paritok proxy: {' '.join(cmd)}")
sys.stdout.flush()

try:
    subprocess.run(cmd, check=True)
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
