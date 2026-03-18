# autoreduce

WIP - It might break stuff.

Autoresearch, but for reducing code. Credit goes to https://github.com/karpathy/autoresearch.

## Running the agent

Copy autoreduce to your project directory, set up uv, and prompt it with anything that points to program.md.

```bash
uv sync # set up venv
```

Open claude / codex / opencode / ... whatever you use.

```bash
claude --dangerously-skip-permissions # ]:- >
...
Have a look at the instructions in program.md and run them.
```

If the agent stops working for some reason and ignores the "never stop" instructions, you can run it in a bash loop, e.g.

```bash
#/usr/bin/env bash
while true
do
  claude --dangerously-skip-permissions -p "run program.md, setup if needed, if not continue from the '## The Loop' section"
  sleep 60
done
```
