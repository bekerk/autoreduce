"""
Composite complexity measurement for autoreduce.

The fixed metric that the agent tries to minimize. It computes a weighted composite score
from multiple code complexity dimensions.

Usage:
    python measure.py                          # measure cwd
    python measure.py --target ../some/project # measure specific path
    python measure.py --config config.yaml     # use custom weights

The metric is deterministic: same code always produces the same score.
"""

import argparse
import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "lines_of_code": 1.0,
    "cyclomatic_complexity": 2.0,
    "nesting_depth": 3.0,
    "num_functions": 0.5,
    "num_classes": 0.5,
    "num_files": 1.0,
    "total_imports": 0.3,
    "duplicate_ratio": 5.0,
}

# File extensions to analyze by language
LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs"},
    "typescript": {".ts", ".tsx"},
    "rust": {".rs"},
    "go": {".go"},
    "java": {".java"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".hpp", ".cc", ".cxx"},
    "ruby": {".rb"},
    "shell": {".sh", ".bash"},
    "dart": {".dart"},
    "kotlin": {".kt", ".kts"},
    "swift": {".swift"},
    "elixir": {".ex", ".exs"},
}

# Reverse lookup: extension -> language (built once)
_EXT_TO_LANG = {ext: lang for lang, exts in LANGUAGE_EXTENSIONS.items() for ext in exts}

# All recognized extensions (built once)
_ALL_EXTENSIONS = set(_EXT_TO_LANG)

# Directories to always skip (exact match)
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".eggs",
    ".next",
    ".nuxt",
    "target",
    "vendor",
    ".dart_tool",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
    ".pub",
    ".pub-cache",
    "Pods",
    ".gradle",
    "deps",
    "_build",
    ".elixir_ls",
    ".fetch",
}

# Suffix patterns for skip dirs (e.g. *.egg-info -> .egg-info)
_SKIP_SUFFIXES = (".egg-info",)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FileMetrics:
    """Metrics for a single file."""

    path: str
    lines_of_code: int = 0
    blank_lines: int = 0
    comment_lines: int = 0
    cyclomatic_complexity: int = 0
    max_nesting_depth: int = 0
    num_functions: int = 0
    num_classes: int = 0
    num_imports: int = 0
    avg_line_length: float = 0.0

    @property
    def total_lines(self):
        return self.lines_of_code + self.blank_lines + self.comment_lines


@dataclass
class ProjectMetrics:
    """Aggregate metrics for an entire project."""

    files: list = field(default_factory=list)
    total_lines_of_code: int = 0
    total_blank_lines: int = 0
    total_comment_lines: int = 0
    total_cyclomatic_complexity: int = 0
    max_nesting_depth: int = 0
    total_functions: int = 0
    total_classes: int = 0
    total_imports: int = 0
    num_files: int = 0
    duplicate_ratio: float = 0.0
    composite_score: float = 0.0


# ---------------------------------------------------------------------------
# Python-specific analysis (AST-based, most accurate)
# ---------------------------------------------------------------------------


class PythonComplexityVisitor(ast.NodeVisitor):
    """Walk a Python AST to compute cyclomatic complexity and nesting depth."""

    def __init__(self):
        self.complexity = 1  # base complexity
        self.max_depth = 0
        self._current_depth = 0
        self.num_functions = 0
        self.num_classes = 0

    def _visit_branch_nesting(self, node):
        """Common handler for nodes that add a branch and a nesting level."""
        self.complexity += 1
        self._current_depth += 1
        self.max_depth = max(self.max_depth, self._current_depth)
        self.generic_visit(node)
        self._current_depth -= 1

    def _visit_nesting_only(self, node):
        """Common handler for nodes that nest but don't branch."""
        self._current_depth += 1
        self.max_depth = max(self.max_depth, self._current_depth)
        self.generic_visit(node)
        self._current_depth -= 1

    # Branch + nesting nodes
    visit_If = _visit_branch_nesting
    visit_For = _visit_branch_nesting
    visit_While = _visit_branch_nesting
    visit_ExceptHandler = _visit_branch_nesting

    # Nesting-only nodes
    visit_With = _visit_nesting_only

    def visit_BoolOp(self, node):
        self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_Assert(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.complexity += 1 + len(node.ifs)
        self.generic_visit(node)

    # Structure nodes
    def visit_FunctionDef(self, node):
        self.num_functions += 1
        self._visit_nesting_only(node)

    def visit_AsyncFunctionDef(self, node):
        self.num_functions += 1
        self._visit_nesting_only(node)

    def visit_ClassDef(self, node):
        self.num_classes += 1
        self._visit_nesting_only(node)


def analyze_python_file(filepath: str) -> FileMetrics:
    """Analyze a Python file using the AST for accurate metrics."""
    metrics = FileMetrics(path=filepath)

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return metrics

    lines = source.splitlines()
    if not lines:
        return metrics

    # Count line types
    code_lines = 0
    blank_lines = 0
    comment_lines = 0
    line_lengths = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_lines += 1
        elif stripped.startswith("#"):
            comment_lines += 1
        else:
            code_lines += 1
            line_lengths.append(len(line))
            if stripped.startswith("import ") or stripped.startswith("from "):
                metrics.num_imports += 1

    metrics.lines_of_code = code_lines
    metrics.blank_lines = blank_lines
    metrics.comment_lines = comment_lines
    metrics.avg_line_length = sum(line_lengths) / len(line_lengths) if line_lengths else 0

    # AST analysis for complexity
    try:
        tree = ast.parse(source, filename=filepath)
        visitor = PythonComplexityVisitor()
        visitor.visit(tree)
        metrics.cyclomatic_complexity = visitor.complexity
        metrics.max_nesting_depth = visitor.max_depth
        metrics.num_functions = visitor.num_functions
        metrics.num_classes = visitor.num_classes
    except SyntaxError:
        # Fallback: rough regex-based complexity for files that don't parse
        metrics.cyclomatic_complexity = _regex_cyclomatic(source)
        metrics.max_nesting_depth = _regex_nesting(lines)

    return metrics


# Generic analysis (regex-based)

_BRANCH_KEYWORDS = re.compile(r"\b(if|else\s+if|elif|for|while|catch|except|case|&&|\|\|)\b")

_COMMENT_PATTERNS = {
    "hash": re.compile(r"^\s*#"),  # Python, Ruby, Shell
    "slash": re.compile(r"^\s*//"),  # JS, TS, Go, Rust, Java, C, C++
}

_IMPORT_PATTERNS = {
    "python": re.compile(r"^\s*(import |from \S+ import )"),
    "javascript": re.compile(r"^\s*(import |require\s*\()"),
    "typescript": re.compile(r"^\s*(import |require\s*\()"),
    "rust": re.compile(r"^\s*use "),
    "go": re.compile(r"^\s*import "),
    "java": re.compile(r"^\s*import "),
    "c": re.compile(r"^\s*#include "),
    "cpp": re.compile(r"^\s*#include "),
    "ruby": re.compile(r"^\s*require "),
    "shell": re.compile(r"^\s*source "),
    "dart": re.compile(r"^\s*(import |export |part )"),
    "kotlin": re.compile(r"^\s*import "),
    "swift": re.compile(r"^\s*import "),
    "elixir": re.compile(r"^\s*(import |alias |use |require )"),
}


_FUNC_PATTERNS = {
    "dart": re.compile(
        r"^\s*"
        r"(?:@\w+\s+)*"  # optional annotations
        r"(?:static\s+)?"  # optional static
        r"(?:Future|Stream|Iterable|void|int|double|String|bool|num|dynamic|var"
        r"|List|Map|Set|Widget|State|[A-Z]\w*)"  # return type
        r"(?:<[^>]+>)?"  # optional generics
        r"\??\s+\w+\s*\("  # name + opening paren
    ),
    "elixir": re.compile(r"^\s*(def |defp |defmacro |defmacrop |defguard |defguardp |defdelegate )"),
}

_CLASS_PATTERNS = {
    "dart": re.compile(
        r"^\s*(?:abstract\s+)?(?:base\s+)?(?:sealed\s+)?(?:final\s+)?"
        r"(class |mixin |enum |extension )"
    ),
    "elixir": re.compile(r"^\s*(defmodule |defprotocol |defimpl )"),
}

_GENERIC_FUNC_RE = re.compile(
    r"^\s*(def |defp |defmacro |defmacrop |defguard |defdelegate "
    r"|function |fn |func |public |private |protected )"
)
_GENERIC_CLASS_RE = re.compile(
    r"^\s*(class |struct |impl |interface |enum "
    r"|defmodule |defprotocol |defimpl |mixin )"
)


def _regex_cyclomatic(source: str) -> int:
    """Approximate cyclomatic complexity via regex."""
    return 1 + len(_BRANCH_KEYWORDS.findall(source))


def _regex_nesting(lines: list) -> int:
    """Approximate max nesting depth by indentation."""
    max_depth = 0
    for line in lines:
        if line.strip():
            indent = len(line) - len(line.lstrip())
            # Approximate: 4 spaces or 1 tab = 1 level
            depth = indent // 4 if "\t" not in line else line.count("\t")
            max_depth = max(max_depth, depth)
    return max_depth


def _detect_language(filepath: str) -> str | None:
    """Detect language from file extension."""
    return _EXT_TO_LANG.get(Path(filepath).suffix.lower())


def analyze_generic_file(filepath: str) -> FileMetrics:
    """Analyze any source file using regex-based heuristics."""
    metrics = FileMetrics(path=filepath)
    lang = _detect_language(filepath)
    if lang is None:
        return metrics

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return metrics

    lines = source.splitlines()
    if not lines:
        return metrics

    comment_style = "hash" if lang in ("python", "ruby", "shell", "elixir") else "slash"
    comment_re = _COMMENT_PATTERNS.get(comment_style)
    import_re = _IMPORT_PATTERNS.get(lang)

    code_lines = 0
    blank_lines = 0
    comment_lines = 0
    line_lengths = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_lines += 1
        elif comment_re and comment_re.match(line):
            comment_lines += 1
        else:
            code_lines += 1
            line_lengths.append(len(line))

    metrics.lines_of_code = code_lines
    metrics.blank_lines = blank_lines
    metrics.comment_lines = comment_lines
    metrics.avg_line_length = sum(line_lengths) / len(line_lengths) if line_lengths else 0

    # Imports
    if import_re:
        metrics.num_imports = sum(1 for line in lines if import_re.match(line))

    # Complexity (regex approximation)
    metrics.cyclomatic_complexity = _regex_cyclomatic(source)
    metrics.max_nesting_depth = _regex_nesting(lines)

    # Functions and classes (use per-language patterns if available)
    func_re = _FUNC_PATTERNS.get(lang, _GENERIC_FUNC_RE)
    class_re = _CLASS_PATTERNS.get(lang, _GENERIC_CLASS_RE)
    metrics.num_functions = sum(1 for line in lines if func_re.match(line))
    metrics.num_classes = sum(1 for line in lines if class_re.match(line))

    return metrics


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def compute_duplicate_ratio(file_metrics_list: list) -> float:
    """
    Compute a rough duplicate ratio across files.
    Uses normalized line hashing -- lines that appear in multiple files
    count as duplicates. Returns ratio in [0, 1].
    """
    line_file_map = {}  # normalized_line -> set of file paths
    total_code_lines = 0

    for fm in file_metrics_list:
        try:
            with open(fm.path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 10:
                continue
            total_code_lines += 1
            norm = re.sub(r"\s+", " ", stripped)
            if norm not in line_file_map:
                line_file_map[norm] = set()
            line_file_map[norm].add(fm.path)

    if total_code_lines == 0:
        return 0.0

    # Count lines that appear in more than one file
    duplicate_lines = sum(len(files) - 1 for files in line_file_map.values() if len(files) > 1)
    return min(1.0, duplicate_lines / total_code_lines)


# ---------------------------------------------------------------------------
# Project-level measurement
# ---------------------------------------------------------------------------


def should_skip(path: str) -> bool:
    """Check if a path component matches skip patterns."""
    for part in Path(path).parts:
        if part in SKIP_DIRS:
            return True
        if any(part.endswith(s) for s in _SKIP_SUFFIXES):
            return True
    return False


def discover_files(
    target_dir: str,
    include_patterns: list | None = None,
    exclude_patterns: list | None = None,
) -> list:
    """
    Discover source files in the target directory.
    If include_patterns is provided, only files matching those globs are included.
    """
    from fnmatch import fnmatch

    files = []
    target = Path(target_dir).resolve()

    for root, dirs, filenames in os.walk(target):
        dirs[:] = [d for d in dirs if not should_skip(os.path.join(root, d))]

        for fname in filenames:
            filepath = os.path.join(root, fname)
            rel_path = os.path.relpath(filepath, target)

            if should_skip(rel_path):
                continue
            if Path(fname).suffix.lower() not in _ALL_EXTENSIONS:
                continue
            if include_patterns and not any(fnmatch(rel_path, p) for p in include_patterns):
                continue
            if exclude_patterns and any(fnmatch(rel_path, p) for p in exclude_patterns):
                continue

            files.append(filepath)

    return sorted(files)


def analyze_file(filepath: str) -> FileMetrics:
    """Analyze a single file, using AST for Python and regex for others."""
    lang = _detect_language(filepath)
    if lang == "python":
        return analyze_python_file(filepath)
    return analyze_generic_file(filepath)


def measure_project(
    target_dir: str,
    weights: dict | None = None,
    include_patterns: list | None = None,
    exclude_patterns: list | None = None,
) -> ProjectMetrics:
    """
    Measure composite complexity of a project.

    This is the core function -- the autoreduce equivalent of evaluate_bpb.
    Returns a ProjectMetrics with the composite_score that the agent minimizes.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS.copy()

    files = discover_files(target_dir, include_patterns, exclude_patterns)
    file_metrics = [analyze_file(f) for f in files]

    pm = ProjectMetrics()
    pm.files = file_metrics
    pm.num_files = len(file_metrics)

    for fm in file_metrics:
        pm.total_lines_of_code += fm.lines_of_code
        pm.total_blank_lines += fm.blank_lines
        pm.total_comment_lines += fm.comment_lines
        pm.total_cyclomatic_complexity += fm.cyclomatic_complexity
        pm.max_nesting_depth = max(pm.max_nesting_depth, fm.max_nesting_depth)
        pm.total_functions += fm.num_functions
        pm.total_classes += fm.num_classes
        pm.total_imports += fm.num_imports

    pm.duplicate_ratio = compute_duplicate_ratio(file_metrics)

    # Compute composite score
    score = 0.0
    score += weights.get("lines_of_code", 0) * pm.total_lines_of_code
    score += weights.get("cyclomatic_complexity", 0) * pm.total_cyclomatic_complexity
    score += weights.get("nesting_depth", 0) * pm.max_nesting_depth
    score += weights.get("num_functions", 0) * pm.total_functions
    score += weights.get("num_classes", 0) * pm.total_classes
    score += weights.get("num_files", 0) * pm.num_files
    score += weights.get("total_imports", 0) * pm.total_imports
    score += weights.get("duplicate_ratio", 0) * (pm.duplicate_ratio * 1000)

    pm.composite_score = round(score, 2)
    return pm


def format_report(pm: ProjectMetrics) -> str:
    """Format a human-readable measurement report."""
    lines = [
        "---",
        f"composite_score:          {pm.composite_score}",
        f"lines_of_code:            {pm.total_lines_of_code}",
        f"cyclomatic_complexity:    {pm.total_cyclomatic_complexity}",
        f"max_nesting_depth:        {pm.max_nesting_depth}",
        f"num_functions:            {pm.total_functions}",
        f"num_classes:              {pm.total_classes}",
        f"num_files:                {pm.num_files}",
        f"total_imports:            {pm.total_imports}",
        f"duplicate_ratio:          {pm.duplicate_ratio:.4f}",
    ]
    return "\n".join(lines)


def format_json(pm: ProjectMetrics) -> str:
    """Format metrics as JSON (for programmatic consumption)."""
    data = {
        "composite_score": pm.composite_score,
        "lines_of_code": pm.total_lines_of_code,
        "cyclomatic_complexity": pm.total_cyclomatic_complexity,
        "max_nesting_depth": pm.max_nesting_depth,
        "num_functions": pm.total_functions,
        "num_classes": pm.total_classes,
        "num_files": pm.num_files,
        "total_imports": pm.total_imports,
        "duplicate_ratio": round(pm.duplicate_ratio, 4),
    }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_config(config_path: str | None) -> dict:
    """Load weights and settings from a YAML config file."""
    if config_path is None:
        return {"weights": DEFAULT_WEIGHTS.copy()}

    try:
        import yaml
    except ImportError:
        return {"weights": DEFAULT_WEIGHTS.copy()}

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    config = {"weights": DEFAULT_WEIGHTS.copy()}
    if raw and "metric" in raw and "weights" in raw["metric"]:
        config["weights"].update(raw["metric"]["weights"])
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure composite code complexity (the autoreduce metric)")
    parser.add_argument(
        "--target",
        default=".",
        help="Target directory to measure (default: current directory)",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml with custom weights")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable format",
    )
    parser.add_argument(
        "--include",
        nargs="*",
        default=None,
        help="File glob patterns to include (e.g. 'src/**/*.py')",
    )
    parser.add_argument("--exclude", nargs="*", default=None, help="File glob patterns to exclude")
    args = parser.parse_args()

    config = load_config(args.config)
    pm = measure_project(
        args.target,
        weights=config["weights"],
        include_patterns=args.include,
        exclude_patterns=args.exclude,
    )

    if args.json:
        print(format_json(pm))
    else:
        print(format_report(pm))
