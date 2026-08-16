# Measured results — what Ragmod owns vs what Paritok owns

Judges (and us) should not confuse **compression** with **product design**.

| Layer | Who owns it | What it is |
|---|---|---|
| Compression math | **Paritok** | Hosted 4B reduces fat `tool_result` / history tokens |
| Retrieval policy | **Ragmod** | Default **GENEROUS**: 200 search hits, ±40 line reads — *more* context than a normal agent |
| Message shape | **Ragmod** | Every file/search lands as OpenAI `role=tool` so Paritok’s training distribution applies |
| Measurement | **Ragmod** | `/stats` deltas, Wave 0 gate, A/B bench vs a tight baseline |
| Upstream LLM | Config | Gemini / Groq / any OpenAI-compat — model-agnostic |

**Thesis:** Without Paritok, Ragmod’s policy is too expensive. Without Ragmod’s policy, Paritok is “compress whatever you already send.” Ragmod’s twist is **over-retrieve on purpose** because compression is in the path.

That is not a thin wrapper. A wrapper would call the same tiny prompts through Paritok and claim the ratio. We deliberately make prompts *fatter*, then prove the compressor still wins on `/stats`.

---

## Receipt 1 — Wave 0 smoke (compression engages)

```bash
./scripts/start_proxy.sh          # terminal 1
python scripts/wave0_smoke.py     # terminal 2
```

One forced fat `read_file` `tool_result` through the hosted GPU. **Fails if `tokens_saved` delta ≤ 0.**

Observed (Gemini `gemini-flash-lite-latest` upstream, 2026-08-15):

| Metric | Value |
|---|---:|
| `input_tokens_original` | 33388 |
| `input_tokens_compressed` | 24140 |
| `tokens_saved` (delta, this run) | **1433** |
| compression ratio | **0.723** |

Earlier run (2026-08-02): delta 1619, ratio 0.42 — the shape (`saved > 0`, `ratio < 1`) is what the gate checks; the exact numbers move with session history and model.

---

## Receipt 2 — Live ask session (`/stats`)

```bash
ragmod ask "<question>" --repo .
ragmod stats
```

Numbers below are cumulative `/stats` for that proxy session (single `ask` each, freshly started proxy), not a before/after subtraction — simpler to reproduce, same shape to check: `saved > 0`, `ratio < 1`.

**Run A — self-repo** (2026-08-15, `--repo .` inside Ragmod):

```
ragmod ask "How does the proxy turn raw stats into savings?" --repo .
```

Answered from `ragmod/gateway/proxy.py:70-135` in 5 turns.

| Metric | After session |
|---|---:|
| `total_requests` | 5 |
| `input_tokens_original` | 5908 |
| `input_tokens_compressed` | 3834 |
| `tokens_saved` | **2074** |
| compression ratio | **0.649** |

**Run B — independent codebase** (2026-08-15, `--repo .` inside a separate ~13k-line Expo/React Native app, unrelated to Ragmod or Paritok):

```
ragmod ask "What does this project do?" --repo .
```

Answered from `README.md`, `package.json`, and `app/_layout.tsx` in 9 turns.

| Metric | After session |
|---|---:|
| `total_requests` | 24 |
| `input_tokens_original` | 25801 |
| `input_tokens_compressed` | 19457 |
| `tokens_saved` | **6344** |
| compression ratio | **0.754** |

Run B matters more than Run A for the generalization claim: it's not Ragmod answering questions about its own source, it's the same retrieval policy + compression path working unmodified against a codebase it has never seen, in a different language (TypeScript) than its own bootstrap-glob default (Python) — `detect_source_glob` picked `*.ts,*.tsx` correctly on its own.

Re-run yourself; numbers move with model, repo size, and turns. The **shape** should stay: `saved > 0`, `ratio < 1`.

---

## Receipt 3 — A/B bench (`examples/savings_table.md`)

```bash
ragmod bench --repo . --out examples/savings_table.md
```

Fixed tasks in `ragmod/bench/tasks.json`:

- **baseline** — direct upstream, **TIGHT** retrieval (15 hits / ±5 lines)  
- **ragmod** — Paritok proxy, **GENEROUS** retrieval  

We report **two** deltas on purpose:

1. **`proxy_saved`** — Paritok `/stats` on the Ragmod arm only (compression win on the fat path).  
2. **Provider `prompt_tokens` vs tight** — what the upstream billed. Over-retrieval can still exceed a *tiny* baseline even after compression; we document that instead of hiding it.

**Regenerated 2026-08-15** (`gemini-flash-lite-latest`, cooldown 20s):

| Metric | Value |
|---|---:|
| Paritok `proxy_saved` sum (3 Ragmod arms) | **3103** |
| Provider prompt toks — baseline/tight | 14503 |
| Provider prompt toks — ragmod/generous+proxy | 24710 |
| Δ vs tight (provider) | **−10207** (Ragmod billed *more*, ratio 1.70) |
| Quality (baseline / ragmod, per task) | 2/2 · 2/2 · 2/2 (baseline) — 2/2 · 2/2 · **0/2** (ragmod) |

Full table: [`examples/savings_table.md`](../examples/savings_table.md).

Per-task on this run: `search_excludes` (−8013 vs tight) and `bootstrap_search` (−215) cost more than tight but matched baseline quality (2/2). `stats_to_savings` (−1979) is the real story — the ragmod arm hit its 6-turn budget without producing a usable answer (`examples/savings_table.md`'s per-arm notes: *"Stopped after the turn budget without a usable answer"*), scoring **0/2**. Those extra tokens bought nothing. `/stats` still shows real compression (3103 tokens) on the generous path Ragmod actually sent, independent of whether the final answer landed.

This is the honest counter-example to the 2026-08-04 run above. That run billed **less** than tight (+5147), with quality at 2/2, 2/2, 1/2 on *both* arms — one task partially failed for baseline and ragmod alike, not a Ragmod-specific weakness. This run billed **more** than tight (−10207), and the shortfall traces to one task where ragmod alone ran out of turns. Same code, same tasks, two different sessions, two different failure shapes — we keep both rather than only reporting the flattering one. The one number that's held across every run we've captured is `/stats` itself never going negative; that's the claim Ragmod actually owns. `Δ vs tight` and per-task quality are real, but session-dependent — worth widening `--max-turns` and re-running before treating either single result as representative.

---

## Ablation we hit during the hackathon (Ragmod design, not Paritok marketing)

When we seeded Gemini with search hits as a **`role=user`** dump (to dodge synthetic `tool_calls`), `/stats` showed **~0 savings** on the same questions.

After forcing a real signed `search_repo` tool call and attaching the fat body as **`role=tool`**, the same ask path produced **thousands** of `tokens_saved`.

So the wins above are not “Paritok was turned on.” They are **message-shape + retrieval policy** choices that let Paritok do its job.

Feedback filed for organizers:
- https://github.com/Paritok-official/paritok-4b-v1/issues/19 — `/stats` on 429s, A/B framing, install weight  
- https://github.com/Paritok-official/paritok-4b-v1/issues/22 — Gemini `thought_signature` vs synthetic tool bootstraps (zero savings trap)

---

## What to show judges in 30 seconds

1. Open this file or `ragmod stats` before/after.  
2. Point at `tokens_saved` and ratio.  
3. Say: *“We retrieve more than a normal agent; Paritok compresses the tool results; we measure both.”*

Dashboard: [paritok.com](https://paritok.com) (same account email as the Devpost form).
