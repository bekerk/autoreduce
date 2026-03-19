import json
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import constraints as constraints_mod


def completed(*, returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def sequence_runner(*items):
    queue = list(items)

    def fake_run(*args, **kwargs):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fake_run


@pytest.fixture
def patch_run(monkeypatch):
    def apply(*items):
        monkeypatch.setattr(constraints_mod.subprocess, "run", sequence_runner(*items))

    return apply


def write_yaml(path: Path, body: str):
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def write_golden(path: Path, payload):
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
        return
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_coverage_constraint(tmp_path: Path, *, baseline_path: Path | None = None, **overrides):
    return constraints_mod.CoverageConstraint(
        command=overrides.pop("command", "ignored"),
        pattern=overrides.pop("pattern", r"(\d+(?:\.\d+)?)%"),
        baseline_path=str(baseline_path or (tmp_path / "coverage.json")),
        workdir=str(tmp_path),
        **overrides,
    )


def make_snapshot_constraint(
    tmp_path: Path,
    *,
    commands=None,
    snapshot_dir: Path | None = None,
    **overrides,
):
    return constraints_mod.SnapshotConstraint(
        commands=commands if commands is not None else [{"name": "demo", "command": "ignored"}],
        snapshot_dir=str(snapshot_dir or (tmp_path / "snapshots")),
        workdir=str(tmp_path),
        **overrides,
    )


def test_constraint_result_and_report_summary():
    result = constraints_mod.ConstraintResult(name="ok", passed=True)
    assert result.message == ""
    assert result.details == ""

    report = constraints_mod.ConstraintReport(
        results=[
            constraints_mod.ConstraintResult(name="a", passed=True, duration_seconds=1.0, message="ok"),
            constraints_mod.ConstraintResult(name="b", passed=False, duration_seconds=2.0, message="nope"),
        ],
        all_passed=False,
        total_duration=3.0,
    )
    summary = report.summary()
    assert "[PASS] a" in summary
    assert "[FAIL] b" in summary
    assert "[FAIL] total" in summary


def test_base_constraint_and_json_helpers(tmp_path):
    with pytest.raises(NotImplementedError):
        constraints_mod.BaseConstraint().check()
    assert constraints_mod.BaseConstraint().setup() is None

    nested_path = tmp_path / "a" / "b" / "data.json"
    constraints_mod._ensure_parent_dir(str(nested_path))
    assert nested_path.parent.is_dir()

    constraints_mod._ensure_parent_dir("coverage.json")

    good = tmp_path / "good.json"
    good.write_text('{"value": 1}', encoding="utf-8")
    assert constraints_mod._load_json_file(str(good)) == {"value": 1}

    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert constraints_mod._load_json_file(str(bad)) is None

    as_list = tmp_path / "list.json"
    as_list.write_text("[1, 2, 3]", encoding="utf-8")
    assert constraints_mod._load_json_file(str(as_list)) is None

    assert constraints_mod._load_json_file(str(tmp_path / "missing.json")) is None


def test_test_suite_constraint_outcomes(patch_run):
    patch_run(completed(stdout="one\ntwo\n"))
    success = constraints_mod.TestSuiteConstraint(command="ignored").check()
    assert success.passed is True
    assert success.message == "two"

    patch_run(completed(returncode=1, stderr="boom"))
    failure = constraints_mod.TestSuiteConstraint(command="ignored").check()
    assert failure.passed is False
    assert failure.message == "exit code 1"
    assert "boom" in failure.details

    patch_run(subprocess.TimeoutExpired(cmd="ignored", timeout=0.01))
    timeout = constraints_mod.TestSuiteConstraint(command="ignored", timeout=0.01).check()
    assert "TIMEOUT" in timeout.message

    patch_run(RuntimeError("boom"))
    error = constraints_mod.TestSuiteConstraint(command="ignored").check()
    assert error.passed is False
    assert error.message == "ERROR: boom"


def test_spec_constraint_success_timeout_and_error(patch_run):
    patch_run(
        completed(returncode=0),
        completed(returncode=1),
        subprocess.TimeoutExpired(cmd="slow", timeout=1),
        RuntimeError("boom"),
    )

    result = constraints_mod.SpecConstraint(
        specs=[
            {"name": "good", "command": "true"},
            {"command": "bad --flag"},
            {"command": "slow"},
            {"name": "named", "command": "explode"},
        ],
        timeout=1,
    ).check()

    assert result.passed is False
    assert "1/4 specs passed" in result.message
    assert "bad --flag: exit 1" in result.details
    assert "slow: TIMEOUT" in result.details
    assert "named: ERROR boom" in result.details


def test_coverage_constraint_helpers_and_setup(tmp_path, patch_run, capsys):
    baseline_path = tmp_path / "nested" / "coverage.json"
    baseline_path.parent.mkdir(parents=True)
    coverage = make_coverage_constraint(tmp_path, baseline_path=baseline_path, pattern=r"TOTAL (\w+)")
    assert coverage._extract_coverage("TOTAL no-number") is None

    baseline_path.write_text("{", encoding="utf-8")
    assert coverage._load_baseline() is None
    baseline_path.write_text("[]", encoding="utf-8")
    assert coverage._load_baseline() is None
    baseline_path.write_text('{"coverage_percent": "ninety"}', encoding="utf-8")
    assert coverage._load_baseline() is None

    coverage = make_coverage_constraint(
        tmp_path,
        baseline_path=baseline_path,
        pattern=r"TOTAL (\d+(?:\.\d+)?)%",
    )
    assert coverage._extract_coverage("TOTAL 91.5%") == 91.5

    patch_run(completed(stdout="TOTAL 91.5%"))
    coverage.setup()
    assert "Coverage baseline: 91.5%" in capsys.readouterr().out
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["coverage_percent"] == 91.5


def test_coverage_constraint_setup_failure_modes(tmp_path, patch_run, capsys):
    coverage = make_coverage_constraint(tmp_path)

    patch_run(completed(stdout="no percent"))
    coverage.setup()
    assert "could not extract percentage" in capsys.readouterr().out

    patch_run(RuntimeError("boom"))
    coverage.setup()
    assert "failed to capture baseline: boom" in capsys.readouterr().out


def test_coverage_constraint_requires_baseline(tmp_path):
    coverage = make_coverage_constraint(tmp_path, min_delta=2)
    missing = coverage.check()
    assert missing.passed is False
    assert "no baseline found" in missing.message


def test_coverage_constraint_accepts_small_delta(tmp_path, patch_run):
    coverage = make_coverage_constraint(tmp_path, min_delta=2)
    Path(coverage.baseline_path).write_text('{"coverage_percent": 90}', encoding="utf-8")

    patch_run(completed(stdout="92%"))
    result = coverage.check()
    assert result.passed is True
    assert "baseline 90.0%" in result.message


@pytest.mark.parametrize(
    ("run_item", "expected_message", "expected_detail"),
    [
        pytest.param(completed(stdout="87%"), "exceeds allowed delta", None, id="dropped-too-far"),
        pytest.param(
            completed(returncode=3, stdout="oops", stderr="stderr tail"),
            "exit code 3",
            "stderr tail",
            id="command-failed",
        ),
        pytest.param(completed(stdout="no match"), "could not extract", None, id="missing-percentage"),
        pytest.param(
            subprocess.TimeoutExpired(cmd="ignored", timeout=1),
            "TIMEOUT",
            None,
            id="timeout",
        ),
        pytest.param(RuntimeError("boom"), "ERROR: boom", None, id="unexpected-error"),
    ],
)
def test_coverage_constraint_check_failure_modes(
    tmp_path,
    patch_run,
    run_item,
    expected_message,
    expected_detail,
):
    coverage = make_coverage_constraint(tmp_path, min_delta=2)
    Path(coverage.baseline_path).write_text('{"coverage_percent": 90}', encoding="utf-8")

    patch_run(run_item)
    result = coverage.check()
    assert result.passed is False
    assert expected_message in result.message
    if expected_detail is not None:
        assert expected_detail in result.details


def test_snapshot_constraint_setup_and_success(tmp_path, patch_run, capsys):
    snapshot = make_snapshot_constraint(tmp_path)

    patch_run(completed(stdout="hello"), completed(stdout="hello"))
    snapshot.setup()
    assert "Generating 1 golden snapshots" in capsys.readouterr().out

    golden_path = Path(snapshot.snapshot_dir) / "demo.golden"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert golden["stdout"] == "hello"
    assert golden["stdout_hash"] == snapshot._hash_output("hello")
    assert snapshot._snapshot_path("demo").endswith("demo.golden")
    assert snapshot.check().passed is True


def test_snapshot_constraint_failure_paths(tmp_path, patch_run, capsys):
    snapshot_dir = tmp_path / "snapshots"
    snapshot = make_snapshot_constraint(
        tmp_path,
        commands=[
            {"name": "missing", "command": "ignored"},
            {"name": "invalid", "command": "ignored"},
            {"name": "changed", "command": "ignored"},
            {"name": "timeout", "command": "ignored"},
            {"name": "error", "command": "ignored"},
        ],
        snapshot_dir=snapshot_dir,
    )
    snapshot_dir.mkdir()

    write_golden(snapshot_dir / "invalid.golden", "{")
    write_golden(
        snapshot_dir / "changed.golden",
        {
            "stdout_hash": snapshot._hash_output("old"),
            "exit_code": 0,
        },
    )
    write_golden(
        snapshot_dir / "timeout.golden",
        {"stdout_hash": snapshot._hash_output("ok"), "exit_code": 0},
    )
    write_golden(
        snapshot_dir / "error.golden",
        {"stdout_hash": snapshot._hash_output("ok"), "exit_code": 0},
    )

    patch_run(
        completed(stdout="new", returncode=2),
        subprocess.TimeoutExpired(cmd="ignored", timeout=1),
        RuntimeError("boom"),
    )
    result = snapshot.check()
    assert result.passed is False
    assert "0/5 snapshots match" in result.message
    assert "missing: no golden snapshot found" in result.details
    assert "invalid: invalid golden snapshot" in result.details
    assert "changed: output changed" in result.details
    assert "changed: exit code 2 (expected 0)" in result.details
    assert "timeout: TIMEOUT" in result.details
    assert "error: ERROR boom" in result.details

    patch_run(RuntimeError("boom"))
    broken_setup = make_snapshot_constraint(tmp_path, snapshot_dir=snapshot_dir)
    broken_setup.setup()
    assert "FAILED to generate: boom" in capsys.readouterr().out


def test_load_constraints_run_all_and_setup_all(tmp_path):
    config = {
        "target_dir": str(tmp_path),
        "constraints": {
            "test_suite": {"command": "true", "timeout": 30},
            "specs": [{"name": "lint", "command": "true"}],
            "snapshots": [{"name": "help", "command": "printf help"}],
            "reduce_tests": {
                "enabled": True,
                "coverage_command": "printf '95%'",
                "coverage_pattern": r"(\d+)%",
                "min_delta": 1,
            },
            "spec_timeout": 12,
            "snapshot_timeout": 13,
        },
    }
    loaded = constraints_mod.load_constraints(config)
    assert [constraint.name for constraint in loaded] == ["test_suite", "spec", "snapshot", "coverage"]
    assert loaded[-1].baseline_path.endswith(".autoreduce/coverage_baseline.json")
    assert loaded[-1].timeout == 12

    report = constraints_mod.run_all_constraints([constraints_mod.TestSuiteConstraint(command="true", timeout=5)])
    assert report.all_passed is True

    called = []

    class DummyConstraint:
        def setup(self):
            called.append(True)

    constraints_mod.setup_all_constraints([DummyConstraint(), DummyConstraint()])
    assert called == [True, True]


@pytest.mark.parametrize(
    ("filename", "body", "expected_code", "expected_output"),
    [
        pytest.param("empty.yaml", "{}", 1, "No constraints configured", id="no-constraints"),
        pytest.param(
            "passing.yaml",
            """
            constraints:
              test_suite:
                command: "true"
            """,
            0,
            "[PASS] total",
            id="passing-suite",
        ),
        pytest.param(
            "failing.yaml",
            """
            constraints:
              test_suite:
                command: "false"
            """,
            1,
            "[FAIL] total",
            id="failing-suite",
        ),
    ],
)
def test_constraints_main_paths(tmp_path, capsys, filename, body, expected_code, expected_output):
    config_path = tmp_path / filename
    write_yaml(config_path, body)

    assert constraints_mod.main(["--config", str(config_path)]) == expected_code
    assert expected_output in capsys.readouterr().out


def test_constraints_main_setup_filters_requested_constraint(tmp_path, monkeypatch, capsys):
    coverage_config = tmp_path / "coverage.yaml"
    write_yaml(
        coverage_config,
        """
        constraints:
          reduce_tests:
            enabled: true
            coverage_command: "printf '90%'"
            coverage_pattern: '(\\d+)%'
        """,
    )

    captured = {}

    def fake_setup(items):
        captured["names"] = [item.name for item in items]

    monkeypatch.setattr(constraints_mod, "setup_all_constraints", fake_setup)
    assert constraints_mod.main(["--config", str(coverage_config), "--check", "coverage", "--setup"]) == 0
    assert captured["names"] == ["coverage"]
    assert "Setup complete." in capsys.readouterr().out


def test_constraints_module_entrypoint(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    write_yaml(
        config_path,
        """
        constraints:
          test_suite:
            command: "true"
        """,
    )

    monkeypatch.setattr(sys, "argv", ["constraints.py", "--config", str(config_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("constraints", run_name="__main__")

    assert exc.value.code == 0
    assert "[PASS] total" in capsys.readouterr().out
