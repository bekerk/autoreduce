"""
Pluggable constraint backends for autoreduce.

Immutable checks that determine whether a code change is valid.

Three constraint backends:
  1. TestSuiteConstraint  - run existing tests, they must all pass
  2. SpecConstraint       - check behavioral specs (command returns 0)
  3. SnapshotConstraint   - compare program output against golden snapshots

Usage:
    python constraints.py --check test_suite --config config.yaml
    python constraints.py --check snapshot --config config.yaml
    python constraints.py --check all --config config.yaml
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

import yaml

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ConstraintResult:
    """Result of running a single constraint check."""

    name: str
    passed: bool
    duration_seconds: float = 0.0
    message: str = ""
    details: str = ""


@dataclass
class ConstraintReport:
    """Aggregate result of all constraint checks."""

    results: list = field(default_factory=list)
    all_passed: bool = False
    total_duration: float = 0.0

    def summary(self) -> str:
        lines = ["--- constraint check ---"]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{status}] {r.name} ({r.duration_seconds:.1f}s) {r.message}")
        overall = "PASS" if self.all_passed else "FAIL"
        lines.append(f"  [{overall}] total ({self.total_duration:.1f}s)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Base constraint
# ---------------------------------------------------------------------------


class BaseConstraint:
    """Base class for all constraint backends."""

    name: str = "base"

    def check(self) -> ConstraintResult:
        raise NotImplementedError

    def setup(self):
        """Optional one-time setup (e.g., generating snapshots)."""
        pass


# ---------------------------------------------------------------------------
# TestSuiteConstraint
# ---------------------------------------------------------------------------


class TestSuiteConstraint(BaseConstraint):
    """
    Run the project's existing test suite.
    The tests must all pass for the constraint to be satisfied.
    """

    name = "test_suite"

    def __init__(self, command: str, timeout: int = 300, workdir: str | None = None):
        """
        Args:
            command: Shell command to run tests
                     (e.g. "pytest", "npm test")
            timeout: Maximum seconds before killing the test run
            workdir: Working directory for running tests
        """
        self.command = command
        self.timeout = timeout
        self.workdir = workdir or os.getcwd()

    def check(self) -> ConstraintResult:
        t0 = time.time()
        try:
            result = subprocess.run(
                self.command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            duration = time.time() - t0
            passed = result.returncode == 0

            # Extract summary from output
            output = result.stdout + result.stderr
            # Truncate long output for the message
            summary_lines = output.strip().split("\n")
            summary = summary_lines[-1] if summary_lines else ""

            return ConstraintResult(
                name=self.name,
                passed=passed,
                duration_seconds=round(duration, 1),
                message=summary[:200] if passed else f"exit code {result.returncode}",
                details=output[-2000:] if not passed else "",
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - t0
            return ConstraintResult(
                name=self.name,
                passed=False,
                duration_seconds=round(duration, 1),
                message=f"TIMEOUT after {self.timeout}s",
            )
        except Exception as e:
            duration = time.time() - t0
            return ConstraintResult(
                name=self.name,
                passed=False,
                duration_seconds=round(duration, 1),
                message=f"ERROR: {e}",
            )


# ---------------------------------------------------------------------------
# SpecConstraint
# ---------------------------------------------------------------------------


class SpecConstraint(BaseConstraint):
    """
    Check behavioral specifications.
    Each spec is a command that must return exit code 0.
    Specs can be anything: type checks, lint rules, custom validation scripts.
    """

    name = "spec"

    def __init__(self, specs: list, timeout: int = 120, workdir: str | None = None):
        """
        Args:
            specs: List of dicts with 'name' and 'command' keys.
                   Example: [{"name": "typecheck", "command": "mypy src/"}]
            timeout: Max seconds per spec command
            workdir: Working directory
        """
        self.specs = specs
        self.timeout = timeout
        self.workdir = workdir or os.getcwd()

    def check(self) -> ConstraintResult:
        t0 = time.time()
        failures = []
        all_passed = True

        for spec in self.specs:
            spec_name = spec.get("name", spec["command"][:40])
            try:
                result = subprocess.run(
                    spec["command"],
                    shell=True,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                if result.returncode != 0:
                    all_passed = False
                    failures.append(f"{spec_name}: exit {result.returncode}")
            except subprocess.TimeoutExpired:
                all_passed = False
                failures.append(f"{spec_name}: TIMEOUT")
            except Exception as e:
                all_passed = False
                failures.append(f"{spec_name}: ERROR {e}")

        duration = time.time() - t0
        n_specs = len(self.specs)
        n_passed = n_specs - len(failures)

        return ConstraintResult(
            name=self.name,
            passed=all_passed,
            duration_seconds=round(duration, 1),
            message=f"{n_passed}/{n_specs} specs passed"
            if all_passed
            else f"{n_passed}/{n_specs} specs passed, failures: {'; '.join(failures)}",
            details="\n".join(failures) if failures else "",
        )


# ---------------------------------------------------------------------------
# SnapshotConstraint
# ---------------------------------------------------------------------------


class CoverageConstraint(BaseConstraint):
    """
    Run tests with coverage and verify the coverage percentage does not drop
    below a recorded baseline. Used when reduce_tests is enabled so the agent
    can simplify test files without silently removing meaningful coverage.
    """

    name = "coverage"

    def __init__(
        self,
        command: str,
        pattern: str,
        min_delta: float = 0,
        baseline_path: str = ".autoreduce/coverage_baseline.json",
        timeout: int = 300,
        workdir: str | None = None,
    ):
        self.command = command
        self.pattern = re.compile(pattern)
        self.min_delta = min_delta
        self.baseline_path = baseline_path
        self.timeout = timeout
        self.workdir = workdir or os.getcwd()

    def _extract_coverage(self, output: str) -> float | None:
        """Extract coverage % from command output using the pattern."""
        match = self.pattern.search(output)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                return None
        return None

    def _load_baseline(self) -> float | None:
        if os.path.exists(self.baseline_path):
            with open(self.baseline_path) as f:
                data = json.load(f)
            return data.get("coverage_percent")
        return None

    def setup(self):
        """Capture the baseline coverage percentage."""
        os.makedirs(os.path.dirname(self.baseline_path), exist_ok=True)
        try:
            result = subprocess.run(
                self.command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = result.stdout + result.stderr
            pct = self._extract_coverage(output)
            if pct is None:
                print("  Coverage: could not extract percentage from output")
                return
            with open(self.baseline_path, "w") as f:
                json.dump({"coverage_percent": pct, "command": self.command}, f, indent=2)
            print(f"  Coverage baseline: {pct}%")
        except Exception as e:
            print(f"  Coverage: failed to capture baseline: {e}")

    def check(self) -> ConstraintResult:
        t0 = time.time()
        baseline = self._load_baseline()
        if baseline is None:
            return ConstraintResult(
                name=self.name,
                passed=False,
                duration_seconds=0,
                message="no baseline found, run --setup first",
            )

        try:
            result = subprocess.run(
                self.command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            duration = time.time() - t0
            output = result.stdout + result.stderr

            if result.returncode != 0:
                return ConstraintResult(
                    name=self.name,
                    passed=False,
                    duration_seconds=round(duration, 1),
                    message=(f"coverage command failed with exit code {result.returncode}"),
                    details=output[-2000:],
                )

            current = self._extract_coverage(output)
            if current is None:
                return ConstraintResult(
                    name=self.name,
                    passed=False,
                    duration_seconds=round(duration, 1),
                    message="could not extract coverage percentage from output",
                )

            drop = baseline - current
            passed = drop <= self.min_delta
            return ConstraintResult(
                name=self.name,
                passed=passed,
                duration_seconds=round(duration, 1),
                message=(
                    f"coverage {current}% (baseline {baseline}%, delta {-drop:+.1f}%)"
                    if passed
                    else (
                        f"coverage dropped to {current}% "
                        f"(baseline {baseline}%, "
                        f"exceeds allowed delta of "
                        f"{self.min_delta}%)"
                    )
                ),
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - t0
            return ConstraintResult(
                name=self.name,
                passed=False,
                duration_seconds=round(duration, 1),
                message=f"TIMEOUT after {self.timeout}s",
            )
        except Exception as e:
            duration = time.time() - t0
            return ConstraintResult(
                name=self.name,
                passed=False,
                duration_seconds=round(duration, 1),
                message=f"ERROR: {e}",
            )


class SnapshotConstraint(BaseConstraint):
    """
    Compare program outputs against golden snapshots.
    Snapshots are generated once before the reduction loop starts,
    then checked after every code change.
    """

    name = "snapshot"

    def __init__(
        self,
        commands: list,
        snapshot_dir: str = ".autoreduce/snapshots",
        timeout: int = 60,
        workdir: str | None = None,
    ):
        """
        Args:
            commands: List of dicts with 'name' and 'command' keys.
                      Each command's stdout is captured and compared.
                      Example: [{"name": "help", "command": "python main.py --help"}]
            snapshot_dir: Directory to store golden snapshots
            timeout: Max seconds per command
            workdir: Working directory
        """
        self.commands = commands
        self.snapshot_dir = snapshot_dir
        self.timeout = timeout
        self.workdir = workdir or os.getcwd()

    def _snapshot_path(self, name: str) -> str:
        return os.path.join(self.snapshot_dir, f"{name}.golden")

    def _hash_output(self, output: str) -> str:
        return hashlib.sha256(output.encode("utf-8")).hexdigest()

    def setup(self):
        """Generate golden snapshots from current code state."""
        os.makedirs(self.snapshot_dir, exist_ok=True)
        print(f"Generating {len(self.commands)} golden snapshots...")

        for cmd in self.commands:
            name = cmd["name"]
            try:
                result = subprocess.run(
                    cmd["command"],
                    shell=True,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                output = result.stdout
                snapshot = {
                    "command": cmd["command"],
                    "stdout": output,
                    "stdout_hash": self._hash_output(output),
                    "exit_code": result.returncode,
                }
                with open(self._snapshot_path(name), "w") as f:
                    json.dump(snapshot, f, indent=2)
                print(f"  Snapshot '{name}': {len(output)} bytes captured")

            except Exception as e:
                print(f"  Snapshot '{name}': FAILED to generate: {e}")

    def check(self) -> ConstraintResult:
        t0 = time.time()
        failures = []
        all_passed = True

        for cmd in self.commands:
            name = cmd["name"]
            golden_path = self._snapshot_path(name)

            if not os.path.exists(golden_path):
                failures.append(f"{name}: no golden snapshot found")
                all_passed = False
                continue

            with open(golden_path) as f:
                golden = json.load(f)

            try:
                result = subprocess.run(
                    cmd["command"],
                    shell=True,
                    cwd=self.workdir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )

                current_hash = self._hash_output(result.stdout)
                if current_hash != golden["stdout_hash"]:
                    all_passed = False
                    failures.append(f"{name}: output changed")

                if result.returncode != golden["exit_code"]:
                    all_passed = False
                    failures.append(f"{name}: exit code {result.returncode} (expected {golden['exit_code']})")

            except subprocess.TimeoutExpired:
                all_passed = False
                failures.append(f"{name}: TIMEOUT")
            except Exception as e:
                all_passed = False
                failures.append(f"{name}: ERROR {e}")

        duration = time.time() - t0
        n_cmds = len(self.commands)
        n_passed = n_cmds - len(failures)

        return ConstraintResult(
            name=self.name,
            passed=all_passed,
            duration_seconds=round(duration, 1),
            message=(
                f"{n_passed}/{n_cmds} snapshots match"
                if all_passed
                else (f"{n_passed}/{n_cmds} snapshots match, failures: {'; '.join(failures)}")
            ),
            details="\n".join(failures) if failures else "",
        )


# ---------------------------------------------------------------------------
# Constraint runner
# ---------------------------------------------------------------------------


def load_constraints(config: dict, workdir: str | None = None) -> list:
    """
    Build constraint instances from a config dict.
    Expected structure (from config.yaml):

        constraints:
          test_suite:
            command: "pytest"
            timeout: 300
          specs:
            - name: typecheck
              command: "mypy src/"
            - name: lint
              command: "ruff check src/"
          snapshots:
            - name: help_output
              command: "python main.py --help"
    """
    constraints = []
    c = config.get("constraints", {})
    wd = workdir or config.get("target_dir", os.getcwd())

    if "test_suite" in c:
        ts = c["test_suite"]
        constraints.append(
            TestSuiteConstraint(
                command=ts["command"],
                timeout=ts.get("timeout", 300),
                workdir=wd,
            )
        )

    if "specs" in c:
        constraints.append(
            SpecConstraint(
                specs=c["specs"],
                timeout=c.get("spec_timeout", 120),
                workdir=wd,
            )
        )

    if "snapshots" in c:
        snapshot_dir = os.path.join(wd, ".autoreduce", "snapshots")
        constraints.append(
            SnapshotConstraint(
                commands=c["snapshots"],
                snapshot_dir=snapshot_dir,
                timeout=c.get("snapshot_timeout", 60),
                workdir=wd,
            )
        )

    rt = c.get("reduce_tests", {})
    if rt.get("enabled"):
        baseline_path = os.path.join(wd, ".autoreduce", "coverage_baseline.json")
        constraints.append(
            CoverageConstraint(
                command=rt["coverage_command"],
                pattern=rt.get("coverage_pattern", r"(\d+)%"),
                min_delta=rt.get("min_delta", 0),
                baseline_path=baseline_path,
                timeout=c.get("spec_timeout", 300),
                workdir=wd,
            )
        )

    return constraints


def run_all_constraints(constraints: list) -> ConstraintReport:
    """Run all constraints and return an aggregate report."""
    report = ConstraintReport()
    t0 = time.time()

    for constraint in constraints:
        result = constraint.check()
        report.results.append(result)

    report.total_duration = round(time.time() - t0, 1)
    report.all_passed = all(r.passed for r in report.results)
    return report


def setup_all_constraints(constraints: list):
    """Run one-time setup for all constraints that need it."""
    for constraint in constraints:
        constraint.setup()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run autoreduce constraint checks")
    parser.add_argument(
        "--check",
        default="all",
        choices=["all", "test_suite", "spec", "snapshot"],
        help="Which constraint to check",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run one-time setup (generate snapshots, etc.)",
    )
    parser.add_argument("--workdir", default=None, help="Working directory for running constraints")
    args = parser.parse_args()

    # Load config
    with open(args.config) as f:
        config = yaml.safe_load(f)
    constraints = load_constraints(config, workdir=args.workdir)

    if args.check != "all":
        constraints = [c for c in constraints if c.name == args.check]

    if not constraints:
        print("No constraints configured. Check your config.yaml.")
        sys.exit(1)

    if args.setup:
        setup_all_constraints(constraints)
        print("Setup complete.")
        sys.exit(0)

    report = run_all_constraints(constraints)
    print(report.summary())
    sys.exit(0 if report.all_passed else 1)
