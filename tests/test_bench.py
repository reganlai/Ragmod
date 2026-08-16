from __future__ import annotations

from ragmod.bench.run import score_quality, write_savings_table
from ragmod.bench.run import ArmResult
from ragmod.tools import GENEROUS, TIGHT, RepositoryTools


def test_tight_policy_limits_search(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"token {i}" for i in range(50)), encoding="utf-8")
    tight = RepositoryTools(tmp_path, policy=TIGHT).search_repo("token")
    generous = RepositoryTools(tmp_path, policy=GENEROUS).search_repo("token")
    hit_lines = [ln for ln in tight["content"].splitlines() if not ln.startswith("[truncated")]
    assert len(hit_lines) <= TIGHT.max_search_lines
    assert generous["meta"]["policy"] == "generous"
    assert tight["meta"]["policy"] == "tight"


def test_score_quality_rubric():
    assert (
        score_quality(
            "calls stats_to_savings on compressed counts",
            [{"path": "ragmod/gateway/proxy.py", "start": 1, "end": 2}],
            "ragmod/gateway/proxy.py",
            ["stats_to_savings"],
        )
        == 2
    )
    assert score_quality("nope", [], "ragmod/gateway/proxy.py", ["stats_to_savings"]) == 0


def test_write_savings_table(tmp_path):
    rows = [
        ArmResult(
            arm="baseline",
            task_id="t1",
            question="q",
            prompt_tokens=1000,
            completion_tokens=10,
            requests=2,
            latency_s=1.2,
            turns=2,
            quality=2,
            quality_max=2,
            answer="stats_to_savings",
            citations=[{"path": "ragmod/gateway/proxy.py", "start": 1, "end": 2}],
        ),
        ArmResult(
            arm="ragmod",
            task_id="t1",
            question="q",
            prompt_tokens=400,
            completion_tokens=10,
            requests=2,
            latency_s=1.5,
            turns=2,
            quality=2,
            quality_max=2,
            answer="stats_to_savings",
            citations=[{"path": "ragmod/gateway/proxy.py", "start": 1, "end": 2}],
            proxy_tokens_saved_delta=600,
        ),
    ]
    out = tmp_path / "savings_table.md"
    text = write_savings_table(rows, out)
    assert out.exists()
    assert out.with_suffix(".json").exists()
    assert "600" in text
    assert "tokens_saved` delta sum = `600`" in text
