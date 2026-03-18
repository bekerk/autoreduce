# autoreduce

Autonomous code reduction: an AI agent simplifies a codebase in a loop,
keeping only changes that preserve correctness while reducing complexity.

Inspired by [autoresearch](https://github.com/karpathy/autoresearch) --
but instead of minimizing val_bpb, you minimize composite code complexity.

## Setup

To set up a new reduction run, work with the user to:

1. **Agree on a run tag**: propose a tag based on today's date (e.g. `mar18`). The branch `autoreduce/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b autoreduce/<tag>` from the current branch.
3. **Read the in-scope files**: Read these autoreduce files for full context:
   - `autoreduce/program.md` — this file, your operating instructions.
   - `autoreduce/config-explained.yaml` — annotated config template with placeholder values.
   - `autoreduce/measure.py` — the complexity metric. Do not modify.
   - `autoreduce/constraints.py` — the constraint checks. Do not modify.
   - `autoreduce/run.py` — the orchestration script. Do not modify.
4. **Generate config.yaml**: If `autoreduce/config.yaml` does not exist, create it:
   - Read `autoreduce/config-explained.yaml` as a reference for all available fields.
   - Inspect the target project: look at the directory structure, file extensions, test runner, linters, build commands.
   - Also check existing config templates (`configs/config-self.yaml`, `configs/config-elixir.yaml`, `configs/config-flutter.yaml`) if the project matches one of those stacks.
   - Write `autoreduce/config.yaml` with real values based on what you found.
   - Show the config to the human and wait for approval before continuing.
5. **Understand the target**: Read the target codebase (configured in `config.yaml` under `target_dir`, `include`, and `exclude`). Understand what the code does, its structure, and its dependencies.
6. **Run the constraint check**: `python autoreduce/constraints.py --config autoreduce/config.yaml`. All constraints must pass on the unmodified code. If they don't, tell the human — the code must be in a passing state before reduction begins. If constraints fail because of a config issue, fix `config.yaml` and retry.
7. **Run setup** (snapshots, coverage baseline, etc): `python autoreduce/constraints.py --config autoreduce/config.yaml --setup`
8. **Establish baseline**: Run `python autoreduce/measure.py --target <target_dir> --config autoreduce/config.yaml` to get the baseline composite score.
9. **Initialize results.tsv**: Create `results.tsv` with header row.
10. **Confirm and go**: Confirm setup looks good to the human.

Once you get confirmation, kick off the reduction loop.

## The Reduction Loop

**What you CAN do:**
- Modify any source file in the target directory (respecting `include`/`exclude` patterns in config.yaml).
- Remove dead code, unused imports, unreachable branches.
- Inline trivial functions or constants.
- Simplify control flow (flatten nested ifs, reduce boolean complexity).
- Merge duplicate or near-duplicate code.
- At `moderate` or `aggressive` level: simplify abstractions, flatten inheritance, merge modules, rewrite algorithms.

**What you CANNOT do:**
- Modify autoreduce core files (`measure.py`, `constraints.py`, `run.py`, `program.md`, `config-explained.yaml`). Exception: you may create/edit `config.yaml` during setup or to fix config issues.
- Modify test files — unless `reduce_tests.enabled` is true in the config. When it is, you may simplify tests (remove redundant assertions, deduplicate test cases, clean up noise) but the coverage constraint must still pass. Never delete a test just to make the suite pass.
- Install new packages or change dependencies.
- Change the public API or observable behavior (unless snapshots/specs explicitly allow it).

**The goal: minimize the composite complexity score while all constraints pass.**

## Aggressiveness Levels

Read the `aggressiveness` setting from `config.yaml` and calibrate your approach:

### `conservative`
- Remove dead code (unreachable branches, unused variables, unused functions)
- Remove unused imports
- Inline trivial one-line functions that are called once
- Remove redundant type conversions
- Simplify boolean expressions (`if x == True` -> `if x`)
- Remove empty exception handlers, pass-only functions
- Deduplicate identical code blocks

### `moderate` (default)
Everything in `conservative`, plus:
- Simplify unnecessary abstractions (unwrap single-implementation interfaces)
- Flatten shallow inheritance hierarchies
- Merge small modules that are always used together
- Simplify over-engineered patterns (e.g., strategy pattern with one strategy)
- Replace verbose patterns with idiomatic equivalents
- Consolidate scattered configuration into simpler structures

### `aggressive`
Everything in `moderate`, plus:
- Rewrite algorithms for simplicity (even if slightly less optimal)
- Change data structures to simpler alternatives
- Restructure module boundaries
- Collapse unnecessary layers of indirection
- Merge or split files for better cohesion
- Remove entire abstraction layers if they add no value

## Output Format

After measuring, the metric prints:

```
---
composite_score:          1234.50
lines_of_code:            850
cyclomatic_complexity:    120
max_nesting_depth:        6
num_functions:            45
num_classes:              12
num_files:                8
total_imports:            35
duplicate_ratio:          0.0312
```

You extract the key metric:
```
grep "^composite_score:" measure.log
```

## Logging Results

Log every attempt to `results.tsv` (tab-separated):

```
commit	score	delta	status	description
```

1. git commit hash (short, 7 chars)
2. composite_score achieved (e.g. 1234.50) — use 0.00 for constraint failures
3. delta from previous best (e.g. -15.30 means improvement)
4. status: `keep`, `discard`, or `fail`
5. short description of what this reduction tried

Example:

```
commit	score	delta	status	description
a1b2c3d	1500.00	0.00	keep	baseline
b2c3d4e	1485.50	-14.50	keep	remove 3 unused imports in utils.py
c3d4e5f	1485.50	0.00	discard	inline helper_fn (no complexity change)
d4e5f6g	0.00	0.00	fail	remove error handler (tests fail)
e5f6g7h	1470.20	-15.30	keep	flatten nested if/else in parser.py
```

## The Loop

LOOP FOREVER:

1. **Analyze**: Look at the current codebase. Identify the highest-impact simplification opportunity given the current aggressiveness level. Prioritize changes that give the biggest score reduction for the least risk.

2. **Plan**: Describe what you'll change and why, in one sentence.

3. **Edit**: Make the change. Keep changes atomic — one conceptual simplification per iteration. This makes it easy to keep or discard.

4. **Commit**: `git add -A && git commit -m "reduce: <description>"`

5. **Check constraints**: Run the constraint checks:
   ```
   python autoreduce/constraints.py --config autoreduce/config.yaml > constraint.log 2>&1
   ```
   Read `constraint.log`. If any constraint fails, this change is invalid.

6. **Measure**: If constraints pass, measure complexity:
   ```
   python autoreduce/measure.py --target <target_dir> --config autoreduce/config.yaml > measure.log 2>&1
   ```
   Extract `composite_score` from `measure.log`.

7. **Decide**:
   - If constraints **fail**: status = `fail`, git reset to previous state.
   - If constraints pass and score **decreased** (improved): status = `keep`.
   - If constraints pass but score **increased or unchanged**: status = `discard`, git reset.

8. **Log**: Append the result to `results.tsv`.

9. **Adapt**: If you've had many consecutive failures/discards (`stagnation_threshold` in config), consider:
   - Trying a completely different area of the codebase
   - Combining multiple small changes that individually were neutral
   - If configured, escalating aggressiveness

10. **Repeat**: Go to step 1. Never stop.

## Git Workflow

```
git reset --hard HEAD~1    # Discard last change (revert to previous state)
git add -A && git commit   # Commit a change
```

The branch advances only when a change is kept. Failed/discarded changes are reset.

**Do NOT commit results.tsv** — it stays untracked.

## Rules

- **NEVER STOP**: Once the loop begins, do NOT pause to ask the human. Run indefinitely until manually interrupted. If you run out of obvious simplifications, look harder — there are always more.
- **NEVER modify autoreduce core files**: `measure.py`, `constraints.py`, `run.py`, `program.md`, `config-explained.yaml` are immutable. You may edit `config.yaml` during setup or if constraints fail due to config issues.
- **Test files are immutable by default**. If `reduce_tests.enabled` is true, you may simplify test files but coverage must not drop below baseline. Never remove a test to make the suite pass — that defeats the point.
- **ATOMIC changes**: One simplification per commit. Never bundle unrelated changes.
- **Conservative by default**: When in doubt about whether a change is safe, don't make it. The constraint checks are the final arbiter, but don't rely on them as your only safety net.
- **Track what you've tried**: Keep a mental log of attempted reductions so you don't repeat yourself.
- **Crashes in constraint checks**: If the constraint check itself crashes (not a test failure, but an infrastructure error), try to diagnose. If trivial, fix and retry. If not, log and move on.
- **Stagnation**: If nothing works for a while, re-read the codebase. Fresh eyes often reveal new opportunities.
