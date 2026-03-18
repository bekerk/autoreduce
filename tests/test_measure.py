"""
Tests for the measure module.
These tests are the constraint boundary - they must all pass
after every code change during the reduction loop.
"""

import ast
import json
import os
import sys
import tempfile
import textwrap

# Ensure autoreduce modules are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from measure import (
    DEFAULT_WEIGHTS,
    FileMetrics,
    ProjectMetrics,
    PythonComplexityVisitor,
    _detect_language,
    _regex_cyclomatic,
    _regex_nesting,
    analyze_python_file,
    compute_duplicate_ratio,
    discover_files,
    format_json,
    format_report,
    measure_project,
    should_skip,
)

# ---------------------------------------------------------------------------
# FileMetrics dataclass
# ---------------------------------------------------------------------------


def test_metric_dataclasses():
    file_metrics = FileMetrics(path="test.py")
    assert file_metrics.path == "test.py"
    assert file_metrics.lines_of_code == 0
    assert file_metrics.blank_lines == 0
    assert file_metrics.comment_lines == 0
    assert file_metrics.total_lines == 0
    assert FileMetrics(path="x.py", lines_of_code=10, blank_lines=3, comment_lines=2).total_lines == 15

    project_metrics = ProjectMetrics()
    assert project_metrics.num_files == 0
    assert project_metrics.composite_score == 0.0
    assert project_metrics.total_lines_of_code == 0


# ---------------------------------------------------------------------------
# Python complexity visitor
# ---------------------------------------------------------------------------


def test_visitor_counts():
    cases = [
        ("def foo():\n    return 1\n", 1, 1, 0),
        ("def foo(x):\n    if x:\n        return 1\n    return 0\n", 2, 1, 0),
        ("for i in range(10):\n    print(i)\n", 2, 0, 0),
        ("class Foo:\n    def bar(self):\n        pass\n", 1, 1, 1),
    ]

    for source, complexity, num_functions, num_classes in cases:
        visitor = PythonComplexityVisitor()
        visitor.visit(ast.parse(source))
        assert visitor.complexity == complexity
        assert visitor.num_functions == num_functions
        assert visitor.num_classes == num_classes


def test_visitor_nesting():
    source = textwrap.dedent("""\
        def foo():
            if True:
                for i in range(10):
                    if i > 5:
                        pass
    """)
    tree = ast.parse(source)
    v = PythonComplexityVisitor()
    v.visit(tree)
    assert v.max_depth >= 3  # function > if > for > if


# ---------------------------------------------------------------------------
# analyze_python_file
# ---------------------------------------------------------------------------


def test_analyze_python_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(
            textwrap.dedent("""\
            import os
            import sys

            # A comment
            def hello():
                if True:
                    print("hello")

            class Foo:
                pass
        """)
        )
        f.flush()
        path = f.name

    try:
        fm = analyze_python_file(path)
        assert fm.lines_of_code > 0
        assert fm.num_imports == 2
        assert fm.num_functions == 1
        assert fm.num_classes == 1
        assert fm.comment_lines >= 1
        assert fm.cyclomatic_complexity >= 2  # base + if
    finally:
        os.unlink(path)


def test_analyze_python_file_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("")
        f.flush()
        path = f.name

    try:
        fm = analyze_python_file(path)
        assert fm.lines_of_code == 0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_measure_helpers():
    assert _detect_language("foo.py") == "python"
    assert _detect_language("bar.js") == "javascript"
    assert _detect_language("baz.ts") == "typescript"
    assert _detect_language("qux.rs") == "rust"
    assert _detect_language("thing.go") == "go"
    assert _detect_language("readme.md") is None
    assert _detect_language("data.json") is None

    source = "if x:\n    for y in z:\n        while True:\n            pass"
    assert _regex_cyclomatic(source) >= 4  # base + if + for + while
    assert (
        _regex_nesting(
            [
                "def foo():",
                "    if True:",
                "        for i in x:",
                "            pass",
                "",
            ]
        )
        >= 2
    )
    assert should_skip(".git/config") is True
    assert should_skip("node_modules/foo/bar.js") is True
    assert should_skip("__pycache__/foo.pyc") is True
    assert should_skip("src/main.py") is False
    assert should_skip("lib/utils.js") is False


# ---------------------------------------------------------------------------
# discover_files
# ---------------------------------------------------------------------------


def test_discover_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some files
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')")
        with open(os.path.join(tmpdir, "utils.py"), "w") as f:
            f.write("def foo(): pass")
        with open(os.path.join(tmpdir, "readme.md"), "w") as f:
            f.write("# readme")  # should be excluded (not a source file)

        files = discover_files(tmpdir)
        assert len(files) == 2
        assert all(f.endswith(".py") for f in files)


def test_discover_files_with_exclude():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("print('hello')")
        with open(os.path.join(tmpdir, "test_main.py"), "w") as f:
            f.write("def test(): pass")

        files = discover_files(tmpdir, exclude_patterns=["test_*.py"])
        assert len(files) == 1
        assert files[0].endswith("main.py")


# ---------------------------------------------------------------------------
# measure_project (the core function)
# ---------------------------------------------------------------------------


def test_measure_project():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write(
                textwrap.dedent("""\
                import os

                def hello():
                    if True:
                        print("hello")

                def goodbye():
                    print("bye")
            """)
            )

        pm = measure_project(tmpdir)
        assert pm.num_files == 1
        assert pm.total_lines_of_code > 0
        assert pm.total_functions == 2
        assert pm.composite_score > 0


def test_measure_project_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = measure_project(tmpdir)
        assert pm.num_files == 0
        assert pm.composite_score == 0.0


def test_measure_project_deterministic():
    """Same code must always produce the same score."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("def foo():\n    return 42\n")

        pm1 = measure_project(tmpdir)
        pm2 = measure_project(tmpdir)
        assert pm1.composite_score == pm2.composite_score


def test_measure_project_custom_weights():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write("def foo():\n    return 42\n")

        # All weights zero -> score should be 0
        zero_weights = {k: 0.0 for k in DEFAULT_WEIGHTS}
        pm = measure_project(tmpdir, weights=zero_weights)
        assert pm.composite_score == 0.0


# ---------------------------------------------------------------------------
# Format functions
# ---------------------------------------------------------------------------


def test_formatters():
    pm = ProjectMetrics()
    pm.composite_score = 123.45
    pm.total_lines_of_code = 100
    pm.num_files = 3
    report = format_report(pm)
    assert "composite_score:" in report
    assert "123.45" in report

    data = json.loads(format_json(pm))
    assert data["composite_score"] == 123.45
    assert data["lines_of_code"] == 100


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def test_duplicate_ratio():
    with tempfile.TemporaryDirectory() as tmpdir:
        a_path = os.path.join(tmpdir, "a.py")
        b_path = os.path.join(tmpdir, "b.py")

        with open(a_path, "w") as f:
            f.write("def unique_function_a():\n    return 'only in a'\n")
        with open(b_path, "w") as f:
            f.write("def unique_function_b():\n    return 'only in b'\n")
        ratio = compute_duplicate_ratio([analyze_python_file(a_path), analyze_python_file(b_path)])
        assert ratio == 0.0

        shared_code = "def shared_func():\n    return 'this is duplicated across files'\n"
        with open(a_path, "w") as f:
            f.write(shared_code)
        with open(b_path, "w") as f:
            f.write(shared_code)
        ratio = compute_duplicate_ratio([analyze_python_file(a_path), analyze_python_file(b_path)])
        assert ratio > 0.0
