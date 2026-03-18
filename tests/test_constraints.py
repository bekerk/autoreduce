"""
Tests for the constraints module.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constraints import (
    ConstraintReport,
    ConstraintResult,
    SnapshotConstraint,
    SpecConstraint,
    TestSuiteConstraint,
    load_constraints,
    run_all_constraints,
)

# ---------------------------------------------------------------------------
# ConstraintResult
# ---------------------------------------------------------------------------


def test_constraint_result_defaults():
    r = ConstraintResult(name="test", passed=True)
    assert r.name == "test"
    assert r.passed is True
    assert r.message == ""


# ---------------------------------------------------------------------------
# ConstraintReport
# ---------------------------------------------------------------------------


def test_constraint_report_summary():
    report = ConstraintReport()
    report.results = [
        ConstraintResult(name="a", passed=True, duration_seconds=1.0, message="ok"),
        ConstraintResult(name="b", passed=False, duration_seconds=2.0, message="fail"),
    ]
    report.all_passed = False
    report.total_duration = 3.0

    summary = report.summary()
    assert "[PASS] a" in summary
    assert "[FAIL] b" in summary
    assert "[FAIL] total" in summary


def test_constraint_report_all_pass():
    report = ConstraintReport()
    report.results = [
        ConstraintResult(name="a", passed=True, duration_seconds=0.5),
    ]
    report.all_passed = True
    report.total_duration = 0.5

    summary = report.summary()
    assert "[PASS] total" in summary


# ---------------------------------------------------------------------------
# TestSuiteConstraint
# ---------------------------------------------------------------------------


def test_test_suite_pass():
    c = TestSuiteConstraint(command="echo hello", timeout=10)
    result = c.check()
    assert result.passed is True
    assert result.name == "test_suite"


def test_test_suite_fail():
    c = TestSuiteConstraint(command="false", timeout=10)
    result = c.check()
    assert result.passed is False


def test_test_suite_timeout():
    c = TestSuiteConstraint(command="sleep 10", timeout=1)
    result = c.check()
    assert result.passed is False
    assert "TIMEOUT" in result.message


# ---------------------------------------------------------------------------
# SpecConstraint
# ---------------------------------------------------------------------------


def test_spec_all_pass():
    specs = [
        {"name": "check1", "command": "true"},
        {"name": "check2", "command": "echo ok"},
    ]
    c = SpecConstraint(specs=specs, timeout=10)
    result = c.check()
    assert result.passed is True
    assert "2/2" in result.message


def test_spec_partial_fail():
    specs = [
        {"name": "good", "command": "true"},
        {"name": "bad", "command": "false"},
    ]
    c = SpecConstraint(specs=specs, timeout=10)
    result = c.check()
    assert result.passed is False
    assert "1/2" in result.message


# ---------------------------------------------------------------------------
# SnapshotConstraint
# ---------------------------------------------------------------------------


def test_snapshot_setup_and_check():
    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot_dir = os.path.join(tmpdir, "snapshots")
        commands = [
            {"name": "test_echo", "command": "echo deterministic_output"},
        ]
        c = SnapshotConstraint(
            commands=commands,
            snapshot_dir=snapshot_dir,
            timeout=10,
            workdir=tmpdir,
        )

        # Setup: generate golden
        c.setup()
        golden_path = os.path.join(snapshot_dir, "test_echo.golden")
        assert os.path.exists(golden_path)

        # Check: should match
        result = c.check()
        assert result.passed is True


def test_snapshot_detects_change():
    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot_dir = os.path.join(tmpdir, "snapshots")
        commands = [
            {"name": "test_echo", "command": "echo original"},
        ]
        c = SnapshotConstraint(
            commands=commands,
            snapshot_dir=snapshot_dir,
            timeout=10,
            workdir=tmpdir,
        )
        c.setup()

        # Now change the command to produce different output
        c2 = SnapshotConstraint(
            commands=[{"name": "test_echo", "command": "echo changed"}],
            snapshot_dir=snapshot_dir,
            timeout=10,
            workdir=tmpdir,
        )
        result = c2.check()
        assert result.passed is False


# ---------------------------------------------------------------------------
# load_constraints
# ---------------------------------------------------------------------------


def test_load_constraints_empty():
    config = {}
    constraints = load_constraints(config)
    assert len(constraints) == 0


def test_load_constraints_test_suite():
    config = {
        "constraints": {
            "test_suite": {"command": "echo test", "timeout": 30},
        }
    }
    constraints = load_constraints(config)
    assert len(constraints) == 1
    assert isinstance(constraints[0], TestSuiteConstraint)


def test_load_constraints_all_types():
    config = {
        "constraints": {
            "test_suite": {"command": "echo test"},
            "specs": [{"name": "lint", "command": "true"}],
            "snapshots": [{"name": "help", "command": "echo help"}],
        }
    }
    constraints = load_constraints(config, workdir="/tmp")
    assert len(constraints) == 3


# ---------------------------------------------------------------------------
# run_all_constraints
# ---------------------------------------------------------------------------


def test_run_all_pass():
    constraints = [
        TestSuiteConstraint(command="true", timeout=10),
    ]
    report = run_all_constraints(constraints)
    assert report.all_passed is True


def test_run_all_with_failure():
    constraints = [
        TestSuiteConstraint(command="true", timeout=10),
        TestSuiteConstraint(command="false", timeout=10),
    ]
    # Override name to distinguish
    constraints[1].name = "test_suite_2"
    report = run_all_constraints(constraints)
    assert report.all_passed is False
    assert len(report.results) == 2
