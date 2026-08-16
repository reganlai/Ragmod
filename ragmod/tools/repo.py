"""Filesystem-backed tools for inspecting one checked-out code repository."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ragmod.contracts import ToolResult

MAX_LIST_ENTRIES = 200
TEST_TIMEOUT_SECONDS = 120
_SOURCE_SAMPLE_LIMIT = 4000

# Keep retrieval on-repo. Searching .venv/site-packages blows free-tier TPM and
# is off-distribution for the Paritok story anyway.
_RG_EXCLUDE_GLOBS = (
    "!.git",
    "!.venv",
    "!venv",
    "!__pycache__",
    "!node_modules",
    "!*.egg-info",
    "!dist",
    "!build",
)
_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "coverage",
    "target",
    "vendor",
}

# Extension → comma-separated ripgrep globs (source code only for bootstrap).
_SOURCE_GLOB_GROUPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("py",), "*.py"),
    (("ts", "tsx"), "*.ts,*.tsx"),
    (("js", "jsx", "mjs", "cjs"), "*.js,*.jsx,*.mjs,*.cjs"),
    (("go",), "*.go"),
    (("rs",), "*.rs"),
    (("java", "kt"), "*.java,*.kt"),
    (("rb",), "*.rb"),
    (("php",), "*.php"),
    (("cs",), "*.cs"),
    (("cpp", "cc", "cxx", "h", "hpp"), "*.cpp,*.cc,*.cxx,*.h,*.hpp"),
    (("c",), "*.c,*.h"),
    (("swift",), "*.swift"),
    (("scala",), "*.scala"),
)


def detect_source_glob(root: Path | str) -> str | None:
    """Pick bootstrap search globs from the repo's dominant source languages.

    Returns a comma-separated glob list for ``search_repo``, or ``None`` to
    search without a language filter when the tree is empty/unknown.
    """
    root_path = Path(root).resolve()
    counts: Counter[str] = Counter()
    sampled = 0
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root_path)
        except ValueError:
            continue
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in rel.parts[:-1]):
            continue
        suffix = path.suffix.lower().lstrip(".")
        if not suffix:
            continue
        counts[suffix] += 1
        sampled += 1
        if sampled >= _SOURCE_SAMPLE_LIMIT:
            break

    if not counts:
        return None

    scored: list[tuple[int, str]] = []
    for exts, glob in _SOURCE_GLOB_GROUPS:
        score = sum(counts.get(ext, 0) for ext in exts)
        if score:
            scored.append((score, glob))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    total = sum(score for score, _ in scored)
    # One clear winner → single language. Mixed repos → top two groups.
    if scored[0][0] >= max(8, int(total * 0.45)) or len(scored) == 1:
        return scored[0][1]
    return ",".join(glob for _, glob in scored[:2])


def _split_globs(glob: str | None) -> list[str]:
    if not glob:
        return []
    return [part.strip() for part in glob.split(",") if part.strip()]


def _path_matches_globs(rel: Path, globs: list[str]) -> bool:
    if not globs:
        return True
    posix = rel.as_posix()
    name = rel.name
    for pattern in globs:
        if (
            fnmatch(posix, pattern)
            or fnmatch(posix, f"**/{pattern}")
            or fnmatch(name, pattern)
        ):
            return True
    return False


class RetrievalPolicy:
    """How generously tools expand context before it hits the LLM."""

    def __init__(self, *, max_search_lines: int, read_context_lines: int, label: str) -> None:
        self.max_search_lines = max_search_lines
        self.read_context_lines = read_context_lines
        self.label = label


# Fair baseline without a compressor: tight snippets a sensible engineer would use.
TIGHT = RetrievalPolicy(max_search_lines=15, read_context_lines=5, label="tight")
# Ragmod policy: over-retrieve; Paritok absorbs the cost.
GENEROUS = RetrievalPolicy(max_search_lines=200, read_context_lines=40, label="generous")


class RepositoryTools:
    """Execute Ragmod's toolset, confined to one repository root."""

    def __init__(
        self,
        root: Path | str,
        policy: RetrievalPolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"Repository root is not a directory: {self.root}")
        self.policy = policy or GENEROUS

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        handlers = {
            "search_repo": self.search_repo,
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "run_tests": self.run_tests,
        }
        handler = handlers.get(name)
        if handler is None:
            return self._error(name, f"Unknown tool: {name}")
        try:
            return handler(**arguments)
        except (OSError, TypeError, ValueError) as exc:
            return self._error(name, str(exc))

    def search_repo(self, pattern: str, glob: str | None = None) -> ToolResult:
        if not pattern:
            raise ValueError("pattern must not be empty")
        command = [
            "rg",
            "-i",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
        ]
        for exclude in _RG_EXCLUDE_GLOBS:
            command.extend(["--glob", exclude])
        for pattern_glob in _split_globs(glob):
            command.extend(["--glob", pattern_glob])
        command.extend([pattern, "."])
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode not in (0, 1):
                raise ValueError(completed.stderr.strip() or "ripgrep failed")
            all_lines = completed.stdout.splitlines()
        except FileNotFoundError:
            # ripgrep not installed — try git grep, then pure-Python fallback
            all_lines = self._git_grep_fallback(pattern, glob)
            if all_lines is None:
                all_lines = self._python_search(pattern, glob)

        limit = self.policy.max_search_lines
        hits = all_lines[:limit]
        citations = []
        for hit in hits:
            parts = hit.split(":", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            citations.append(
                {"path": Path(parts[0]).as_posix(), "start": int(parts[1]), "end": int(parts[1])}
            )
        suffix = f"\n[truncated after {limit} matches]" if len(all_lines) > len(hits) else ""
        content = "\n".join(hits) + suffix
        if not content:
            content = "No matches found."
        return ToolResult(
            name="search_repo",
            content=content,
            meta={
                "pattern": pattern,
                "glob": glob,
                "citations": citations,
                "policy": self.policy.label,
            },
        )

    def _git_grep_fallback(self, pattern: str, glob: str | None) -> list[str] | None:
        """Try git grep as a fallback when ripgrep is missing."""
        command = ["git", "grep", "-n", "--no-color", "-I", pattern, "--", "."]
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode not in (0, 1):
                return None
            return completed.stdout.splitlines()
        except FileNotFoundError:
            return None

    def _python_search(self, pattern: str, glob_filter: str | None = None) -> list[str]:
        """Pure-Python line search — last resort when neither rg nor git is available."""
        hits: list[str] = []
        globs = _split_globs(glob_filter)
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in rel.parts):
                continue
            if not _path_matches_globs(rel, globs):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            needle = pattern.lower()
            for line_no, line in enumerate(lines, start=1):
                if needle in line.lower():
                    hits.append(f"{rel.as_posix()}:{line_no}:{line}")
                    if len(hits) >= self.policy.max_search_lines:
                        return hits
        return hits

    def read_file(
        self,
        path: str,
        start: int | None = None,
        end: int | None = None,
    ) -> ToolResult:
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError(f"Not a UTF-8 text file: {path}") from exc

        requested_start = max(1, start or 1)
        requested_end = max(requested_start, end or len(lines))
        if not lines:
            raise ValueError(f"File is empty: {path}")
        if requested_start > len(lines):
            raise ValueError(f"start line {requested_start} is beyond end of file ({len(lines)} lines)")
        pad = self.policy.read_context_lines
        actual_start = max(1, requested_start - pad)
        actual_end = min(len(lines), requested_end + pad)
        selected = lines[actual_start - 1 : actual_end]
        numbered = "\n".join(
            f"{line_no}: {line}" for line_no, line in enumerate(selected, start=actual_start)
        )
        relative = file_path.relative_to(self.root).as_posix()
        return ToolResult(
            name="read_file",
            content=f"# read_file {relative}:{actual_start}-{actual_end}\n{numbered}",
            meta={
                "path": relative,
                "start": actual_start,
                "end": actual_end,
                "requested_start": requested_start,
                "requested_end": requested_end,
                "citations": [{"path": relative, "start": actual_start, "end": actual_end}],
            },
        )

    def list_dir(self, path: str = ".") -> ToolResult:
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ValueError(f"Not a directory: {path}")
        entries = sorted(directory.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        rendered = []
        for entry in entries[:MAX_LIST_ENTRIES]:
            rel = entry.relative_to(self.root).as_posix()
            rendered.append(f"{rel}/" if entry.is_dir() else rel)
        if len(entries) > len(rendered):
            rendered.append("[truncated after 200 entries]")
        return ToolResult(
            name="list_dir",
            content="\n".join(rendered) or "Directory is empty.",
            meta={"path": directory.relative_to(self.root).as_posix()},
        )

    def run_tests(self, selector: str | None = None) -> ToolResult:
        command = [sys.executable, "-m", "pytest", "-q"]
        if selector:
            candidate = Path(selector)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("selector must stay inside the repository")
            command.append(selector)
        completed = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=TEST_TIMEOUT_SECONDS,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        return ToolResult(
            name="run_tests",
            content=output or "pytest produced no output.",
            meta={"selector": selector, "returncode": completed.returncode},
        )

    def _resolve(self, requested: str) -> Path:
        candidate = Path(requested)
        if candidate.is_absolute():
            raise ValueError("Absolute paths are not allowed")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path escapes the repository root")
        return resolved

    @staticmethod
    def _error(name: str, message: str) -> ToolResult:
        return ToolResult(name=name, content=f"Tool error: {message}", meta={"error": True})
