# Ragmod savings table

Regenerate with: `ragmod bench --repo . --out examples/savings_table.md`

Arms:
- **baseline** — direct upstream (`RAGMOD_OPENAI_URL`), tight retrieval
- **ragmod** — Paritok hosted-GPU proxy, generous retrieval

Token counts are provider `usage.prompt_tokens` (what the upstream billed).
For ragmod that is post-compression. `proxy_saved` is the Paritok `/stats` delta.

| task | baseline prompt toks | ragmod prompt toks | Δ tokens | baseline quality | ragmod quality | baseline latency (s) | ragmod latency (s) | proxy_saved |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| stats_to_savings | 4626 | 6605 | -1979 | 2/2 | 0/2 | 17.101 | 17.778 | -520 |
| search_excludes | 4909 | 12922 | -8013 | 2/2 | 2/2 | 12.257 | 29.428 | 1491 |
| bootstrap_search | 4968 | 5183 | -215 | 2/2 | 2/2 | 9.7 | 17.145 | 2132 |

**Provider totals (usage.prompt_tokens):** baseline/tight `14503` · ragmod/generous+proxy `24710` · Δ vs tight `-10207` · ratio `1.704` (ragmod/baseline; lower means Ragmod billed less).

**Paritok `/stats` on Ragmod arms only:** `tokens_saved` delta sum = `3103`. This is the compression win on the generous tool_result path (original − compressed), independent of the tight baseline.

Note: Δ vs tight can be negative. Over-retrieval + compression can still bill more than a deliberately tiny baseline, while improving recall/quality and showing large `/stats` savings against the uncompressed generous prompt.

## Per-arm notes

- `stats_to_savings`/baseline: turns=4, cites=ragmod/gateway/proxy.py:1-135
  - answer: The proxy turns raw stats into savings in `ragmod/gateway/proxy.py` via `stats_to_savings(raw)` by mapping the raw JSON response from the proxy's `/stats` endpo
- `stats_to_savings`/ragmod: turns=6, cites=—
  - answer: Stopped after the turn budget without a usable answer. Try a narrower question or raise --max-turns.
- `search_excludes`/baseline: turns=4, cites=ragmod/tools/repo.py:1-75
  - answer: `search_repo` (`ragmod/tools/repo.py`) keeps ripgrep out of `.venv` and `__pycache__` using two mechanisms:  1. **Ripgrep Exclude Globs:** When executing the `r
- `search_excludes`/ragmod: turns=6, cites=ragmod/tools/repo.py:1-160, ragmod/tools/repo.py:81-280
  - answer: `search_repo` (defined in `ragmod/tools/repo.py`) keeps ripgrep out of `.venv`, `__pycache__`, and other unwanted directories using two main mechanisms:  1. **R
- `bootstrap_search`/baseline: turns=4, cites=ragmod/agent/loop.py:165-255
  - answer: The agent bootstraps its first `search_repo` tool result in **`ragmod/agent/loop.py`** (inside the `ask` function, specifically around lines 196–243) before the
- `bootstrap_search`/ragmod: turns=4, cites=ragmod/agent/loop.py:140-270
  - answer: The agent bootstraps its first `search_repo` tool result inside the `ask()` function in **`ragmod/agent/loop.py`** (around lines 201–205).   It detects a source
