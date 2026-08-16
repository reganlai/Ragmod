from __future__ import annotations

from ragmod.tools import RepositoryTools, detect_source_glob


def test_read_file_adds_context_and_citation(tmp_path):
    source = tmp_path / "src.py"
    source.write_text("\n".join(f"line {number}" for number in range(1, 101)), encoding="utf-8")

    result = RepositoryTools(tmp_path).read_file("src.py", start=50, end=50)

    assert "10: line 10" in result["content"]
    assert "90: line 90" in result["content"]
    assert result["meta"]["citations"] == [{"path": "src.py", "start": 10, "end": 90}]


def test_tools_reject_paths_outside_repository(tmp_path):
    result = RepositoryTools(tmp_path).execute("read_file", {"path": "../outside.py"})

    assert result["meta"]["error"] is True
    assert "escapes" in result["content"]


def test_search_repo_returns_line_citations(tmp_path):
    (tmp_path / "app.py").write_text("def target():\n    return 1\n", encoding="utf-8")

    result = RepositoryTools(tmp_path).search_repo("target")

    assert "app.py:1:def target" in result["content"]
    assert result["meta"]["citations"] == [{"path": "app.py", "start": 1, "end": 1}]


def test_detect_source_glob_prefers_typescript(tmp_path):
    src = tmp_path / "app" / "src"
    src.mkdir(parents=True)
    for name in ("a.ts", "b.ts", "c.tsx", "d.tsx", "e.ts"):
        (src / name).write_text("export const x = 1\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# hi\n", encoding="utf-8")

    assert detect_source_glob(tmp_path) == "*.ts,*.tsx"


def test_search_repo_accepts_comma_separated_globs(tmp_path):
    (tmp_path / "a.ts").write_text("const logViewer = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("log_viewer = 1\n", encoding="utf-8")

    result = RepositoryTools(tmp_path).search_repo("log", glob="*.ts,*.tsx")

    assert "a.ts:" in result["content"]
    assert "b.py:" not in result["content"]
