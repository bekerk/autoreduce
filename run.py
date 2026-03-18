"""
Autoreduce orchestrator: the main reduction loop.

This script is primarily used by the agent to run the evaluation pipeline
after making a code change. It can also be run standalone to establish
a baseline or verify the setup.

Usage:
    python autoreduce/run.py baseline  # measure baseline
    python autoreduce/run.py check     # constraints + measure
    python autoreduce/run.py setup     # generate snapshots
    python autoreduce/run.py status    # show current score
"""

import argparse
import os
import subprocess
import sys

import yaml

# Add parent dir to path so we can import our modules
AUTOREDUCE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AUTOREDUCE_DIR)

from constraints import (
    load_constraints,
    run_all_constraints,
    setup_all_constraints,
)
from measure import (
    format_report,
    measure_project,
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    """Load the full config.yaml."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def resolve_target(config: dict, config_path: str) -> str:
    """Resolve the target directory relative to the config file location."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    target = config.get("target_dir", ".")
    if os.path.isabs(target):
        return target
    return os.path.normpath(os.path.join(config_dir, target))


def _measure_from_config(config: dict, target: str):
    """Run measure_project using settings from config dict."""
    weights = config.get("metric", {}).get("weights", None)
    include = config.get("include") or None
    exclude = config.get("exclude") or None
    return measure_project(target, weights=weights, include_patterns=include, exclude_patterns=exclude)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git_short_hash() -> str:
    """Get the current short commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def git_current_branch() -> str:
    """Get the current branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Results TSV
# ---------------------------------------------------------------------------

RESULTS_HEADER = "commit\tscore\tdelta\tstatus\tdescription\n"


def init_results(results_path: str):
    """Create results.tsv with header row."""
    with open(results_path, "w") as f:
        f.write(RESULTS_HEADER)
    print(f"Initialized {results_path}")


def append_result(
    results_path: str,
    commit: str,
    score: float,
    delta: float,
    status: str,
    description: str,
):
    row = f"{commit}\t{score:.2f}\t{delta:.2f}\t{status}\t{description}\n"
    with open(results_path, "a") as f:
        f.write(row)


def read_results_summary(results_path: str) -> str:
    """Read and summarize results.tsv."""
    if not os.path.exists(results_path):
        return "No results.tsv found."

    total = 0
    kept = 0
    discarded = 0
    failed = 0
    best_score = float("inf")

    with open(results_path) as f:
        next(f)  # skip header
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 4:
                continue
            total += 1
            status = parts[3]
            if status == "keep":
                kept += 1
            elif status == "discard":
                discarded += 1
            elif status == "fail":
                failed += 1

            try:
                score = float(parts[1])
                if score > 0 and status in ("keep", "baseline"):
                    best_score = min(best_score, score)
            except ValueError:
                pass

    best_str = f"{best_score:.2f}" if best_score < float("inf") else "N/A"
    return (
        f"Total attempts: {total}\n"
        f"  Kept: {kept}\n"
        f"  Discarded: {discarded}\n"
        f"  Failed: {failed}\n"
        f"  Best score: {best_str}"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_baseline(config: dict, config_path: str, results_path: str):
    """Establish baseline: measure current codebase, init results.tsv."""
    target = resolve_target(config, config_path)
    print(f"Target directory: {target}")
    print(f"Branch: {git_current_branch()}")
    print()

    print("Running constraint checks on unmodified code...")
    constraints = load_constraints(config, workdir=target)
    if constraints:
        report = run_all_constraints(constraints)
        print(report.summary())
        print()
        if not report.all_passed:
            print("ERROR: Constraints do not pass on unmodified code.")
            print("Fix the issues above before starting a reduction run.")
            sys.exit(1)
    else:
        print("WARNING: No constraints configured. The agent will have no safety net.")
        print()

    # Measure baseline
    print("Measuring baseline complexity...")
    pm = _measure_from_config(config, target)
    print(format_report(pm))
    print()

    # Init results.tsv with baseline
    init_results(results_path)
    commit = git_short_hash()
    append_result(results_path, commit, pm.composite_score, 0.0, "keep", "baseline")
    print(f"Baseline recorded: composite_score = {pm.composite_score}")
    print(f"Results file: {results_path}")


def cmd_check(config: dict, config_path: str):
    """Run constraints + measure. Used by the agent after each change."""
    target = resolve_target(config, config_path)

    # Run constraints
    constraints = load_constraints(config, workdir=target)
    if constraints:
        report = run_all_constraints(constraints)
        print(report.summary())
        if not report.all_passed:
            print()
            print("CONSTRAINTS FAILED")
            # Print failure details
            for r in report.results:
                if not r.passed and r.details:
                    print(f"\n--- {r.name} details ---")
                    print(r.details)
            sys.exit(1)
    print()

    pm = _measure_from_config(config, target)
    print(format_report(pm))


def cmd_setup(config: dict, config_path: str):
    """Generate golden snapshots."""
    target = resolve_target(config, config_path)
    constraints = load_constraints(config, workdir=target)
    snapshot_constraints = [c for c in constraints if c.name == "snapshot"]
    if not snapshot_constraints:
        print("No snapshot constraints configured in config.yaml.")
        return
    setup_all_constraints(snapshot_constraints)
    print("Snapshot setup complete.")


def cmd_status(config: dict, config_path: str, results_path: str):
    """Show current status: score, branch, recent results."""
    target = resolve_target(config, config_path)
    print(f"Branch: {git_current_branch()}")
    print(f"Commit: {git_short_hash()}")
    print(f"Target: {target}")
    print(f"Aggressiveness: {config.get('aggressiveness', 'moderate')}")
    print()

    pm = _measure_from_config(config, target)
    print(f"Current composite_score: {pm.composite_score}")
    print()

    print(read_results_summary(results_path))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Autoreduce orchestrator")
    parser.add_argument(
        "command",
        choices=["baseline", "check", "setup", "status"],
        help="Command to run",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(AUTOREDUCE_DIR, "config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument("--results", default="results.tsv", help="Path to results.tsv")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.command == "baseline":
        cmd_baseline(config, args.config, args.results)
    elif args.command == "check":
        cmd_check(config, args.config)
    elif args.command == "setup":
        cmd_setup(config, args.config)
    elif args.command == "status":
        cmd_status(config, args.config, args.results)


if __name__ == "__main__":
    main()
