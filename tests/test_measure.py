import ast
import builtins
import json
import runpy
import sys
import textwrap
from pathlib import Path

import pytest

import measure as measure_mod


def write(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_metric_dataclasses_and_line_scanner():
    file_metrics = measure_mod.FileMetrics(path="test.py", lines_of_code=10, blank_lines=3, comment_lines=2)
    assert file_metrics.total_lines == 15

    project_metrics = measure_mod.ProjectMetrics()
    assert project_metrics.num_files == 0
    assert project_metrics.composite_score == 0.0

    scanned = measure_mod._scan_lines(
        ["import os", "", "# comment", "value = 1"],
        comment_prefixes=("#",),
        import_prefixes=("import ", "from "),
    )
    assert scanned == (2, 1, 1, 1, 9.0)


def test_python_complexity_visitor_covers_special_nodes():
    source = textwrap.dedent(
        """
        class Box:
            async def run(self, xs):
                assert xs
                with open("demo.txt"):
                    return [x for x in xs if x and True] if xs and len(xs) > 1 else []

        try:
            pass
        except Exception:
            pass
        """
    )
    visitor = measure_mod.PythonComplexityVisitor()
    visitor.visit(ast.parse(source))

    assert visitor.num_functions == 1
    assert visitor.num_classes == 1
    assert visitor.max_depth >= 3
    assert visitor.complexity >= 8


def test_analyze_python_file_success_missing_and_syntax_fallback(tmp_path):
    assert measure_mod.analyze_python_file(str(tmp_path / "missing.py")).lines_of_code == 0
    assert measure_mod._read_text_file(str(tmp_path / "missing.py")) is None

    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    assert measure_mod.analyze_python_file(str(empty)).lines_of_code == 0

    valid = tmp_path / "main.py"
    write(
        valid,
        """
        import os
        from sys import argv

        # comment
        def hello():
            if True:
                return argv[0]

        class Box:
            pass
        """,
    )
    metrics = measure_mod.analyze_python_file(str(valid))
    assert metrics.lines_of_code > 0
    assert metrics.comment_lines == 1
    assert metrics.num_imports == 2
    assert metrics.num_functions == 1
    assert metrics.num_classes == 1
    assert metrics.cyclomatic_complexity >= 2

    broken = tmp_path / "broken.py"
    write(
        broken,
        """
        def nope(
            if True:
                pass
        """,
    )
    fallback = measure_mod.analyze_python_file(str(broken))
    assert fallback.cyclomatic_complexity >= 2
    assert fallback.max_nesting_depth >= 1


def test_generic_analysis_variants_and_dispatch(tmp_path):
    js_file = tmp_path / "app.js"
    write(
        js_file,
        """
        // comment
        import thing from "thing"
        function demo() {
            if (left && right) {
                return 1
            }
        }
        class Box {}
        """,
    )
    js_metrics = measure_mod.analyze_generic_file(str(js_file))
    assert js_metrics.comment_lines == 1
    assert js_metrics.num_imports == 1
    assert js_metrics.num_functions == 1
    assert js_metrics.num_classes == 1
    assert js_metrics.cyclomatic_complexity >= 2
    assert measure_mod.analyze_file(str(js_file)).num_functions == 1

    ex_file = tmp_path / "demo.ex"
    write(
        ex_file,
        """
        # comment
        defmodule Demo do
          alias Foo.Bar
          def hello, do: :ok
        end
        """,
    )
    ex_metrics = measure_mod.analyze_generic_file(str(ex_file))
    assert ex_metrics.comment_lines == 1
    assert ex_metrics.num_imports == 1
    assert ex_metrics.num_functions == 1
    assert ex_metrics.num_classes == 1

    empty = tmp_path / "empty.js"
    empty.write_text("", encoding="utf-8")
    assert measure_mod.analyze_generic_file(str(empty)).lines_of_code == 0
    assert measure_mod.analyze_generic_file(str(tmp_path / "missing.js")).lines_of_code == 0
    assert measure_mod.analyze_generic_file(str(tmp_path / "notes.md")).lines_of_code == 0


@pytest.mark.parametrize(
    ("filepath", "expected"),
    [
        pytest.param("foo.py", "python", id="python"),
        pytest.param("bar.js", "javascript", id="javascript"),
        pytest.param("demo.ex", "elixir", id="elixir"),
        pytest.param("readme.md", None, id="unknown"),
    ],
)
def test_detect_language(filepath, expected):
    assert measure_mod._detect_language(filepath) == expected


def test_regex_helpers():
    assert measure_mod._regex_cyclomatic("if x:\n    while y:\n        pass") >= 3
    assert measure_mod._regex_nesting(["\tif ok:", "\t\tpass"]) == 2


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(".git/config", True, id="git-dir"),
        pytest.param("pkg/demo.egg-info/PKG-INFO", True, id="egg-info"),
        pytest.param("src/main.py", False, id="source-file"),
    ],
)
def test_should_skip(path, expected):
    assert measure_mod.should_skip(path) is expected


def test_duplicate_ratio_ignores_missing_files_and_short_lines(tmp_path):
    short = tmp_path / "short.py"
    write(short, "x = 1\n")
    ratio = measure_mod.compute_duplicate_ratio(
        [
            measure_mod.FileMetrics(path=str(short)),
            measure_mod.FileMetrics(path=str(tmp_path / "missing.py")),
        ]
    )
    assert ratio == 0.0


def test_duplicate_ratio_detects_shared_code(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    shared = "def shared_function_name():\n    return 'same long line'\n"
    write(first, shared)
    write(second, shared)

    duplicate_ratio = measure_mod.compute_duplicate_ratio(
        [
            measure_mod.analyze_python_file(str(first)),
            measure_mod.analyze_python_file(str(second)),
        ]
    )
    assert duplicate_ratio > 0


def test_discover_files_include_exclude_and_skips(tmp_path, monkeypatch):
    write(tmp_path / "src" / "main.py", "print('hello')\n")
    write(tmp_path / "src" / "helper.js", "function demo() {}\n")
    write(tmp_path / "tests" / "test_main.py", "def test_ok(): pass\n")
    write(tmp_path / "node_modules" / "ignored.js", "function skip() {}\n")
    write(tmp_path / "pkg" / "demo.egg-info" / "bad.py", "print('skip')\n")
    write(tmp_path / "notes.txt", "ignore me\n")

    discovered = measure_mod.discover_files(str(tmp_path))
    assert [Path(path).name for path in discovered] == ["helper.js", "main.py", "test_main.py"]

    included = measure_mod.discover_files(str(tmp_path), include_patterns=["src/*.py"])
    assert included == [str((tmp_path / "src" / "main.py").resolve())]

    excluded = measure_mod.discover_files(str(tmp_path), exclude_patterns=["tests/*"])
    assert all(not path.endswith("test_main.py") for path in excluded)

    original_should_skip = measure_mod.should_skip

    def fake_should_skip(path):
        if path == "src/main.py":
            return True
        return original_should_skip(path)

    monkeypatch.setattr(measure_mod, "should_skip", fake_should_skip)
    skipped = measure_mod.discover_files(str(tmp_path), include_patterns=["src/*.py"])
    assert skipped == []


def test_measure_project_and_formatters(tmp_path):
    write(
        tmp_path / "main.py",
        """
        import os

        def hello():
            if True:
                return os.getcwd()
        """,
    )
    write(
        tmp_path / "util.js",
        """
        import thing from "thing"
        function helper() {
            return thing
        }
        """,
    )

    project = measure_mod.measure_project(str(tmp_path))
    assert project.num_files == 2
    assert project.total_functions == 2
    assert project.total_imports == 2
    assert project.composite_score > 0

    zero_score = measure_mod.measure_project(
        str(tmp_path),
        weights={key: 0.0 for key in measure_mod.DEFAULT_WEIGHTS},
    )
    assert zero_score.composite_score == 0.0

    repeated_project = measure_mod.measure_project(str(tmp_path))
    assert project.composite_score == repeated_project.composite_score

    report = measure_mod.format_report(project)
    data = json.loads(measure_mod.format_json(project))
    assert "composite_score:" in report
    assert data["num_files"] == 2


def test_load_config_branches(tmp_path, monkeypatch):
    assert measure_mod.load_config(None) == {"weights": measure_mod.DEFAULT_WEIGHTS.copy()}

    config_path = tmp_path / "config.yaml"
    write(
        config_path,
        """
        metric:
          weights:
            lines_of_code: 9
            num_files: 2
        """,
    )
    loaded = measure_mod.load_config(str(config_path))
    assert loaded["weights"]["lines_of_code"] == 9
    assert loaded["weights"]["num_files"] == 2
    assert loaded["weights"]["cyclomatic_complexity"] == measure_mod.DEFAULT_WEIGHTS["cyclomatic_complexity"]

    empty_path = tmp_path / "empty.yaml"
    write(empty_path, "{}")
    assert measure_mod.load_config(str(empty_path)) == {"weights": measure_mod.DEFAULT_WEIGHTS.copy()}

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert measure_mod.load_config(str(config_path)) == {"weights": measure_mod.DEFAULT_WEIGHTS.copy()}


def test_measure_main_and_entrypoint(tmp_path, monkeypatch, capsys):
    write(tmp_path / "main.py", "def foo():\n    return 42\n")
    config_path = tmp_path / "config.yaml"
    write(
        config_path,
        """
        metric:
          weights:
            lines_of_code: 2
        """,
    )

    args = ["--target", str(tmp_path), "--config", str(config_path), "--json", "--include", "*.py"]
    assert measure_mod.main(args) == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["num_files"] == 1

    assert measure_mod.main(["--target", str(tmp_path), "--exclude", "*.py"]) == 0
    assert "composite_score:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["measure.py", "--target", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("measure", run_name="__main__")

    assert exc.value.code == 0
    assert "composite_score:" in capsys.readouterr().out
