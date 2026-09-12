#!/usr/bin/env python3
"""Create a tiny, throwaway project for exercising the Synagon pipeline end to end.

Usage:
    python demo/setup_demo_project.py [target_dir]

`target_dir` defaults to `./demo/scratch` (git-ignored, safe to delete and re-run). The
project it creates has one deliberately failing test - `test_calc.py` imports a `multiply`
function that `calc.py` does not define yet - so a single goal exercises the whole loop:

    Research (what's here, what's missing) -> Planning (what to add, and where)
    -> Implementation (write it) -> Verification (does it pass) -> Acceptance
    (the orchestrator's own `pytest -q`, which overrules a verifier that says PASS
    over a red suite)

It also copies this project's own verified-working agent/model pairings into the demo's
own `orchestrator.yaml`, because a project config fully replaces the built-in default
rather than merging with it (see README.md, Known limitations) - a minimal override would
fail validation.

After setup, from the Synagon checkout:

    python -m orchestrator --project-root demo/scratch \\
        "Add a multiply(a, b) function to calc.py so the existing tests in \\
         test_calc.py pass. Do not modify test_calc.py."

To see retry, escalation and resume: kill the process mid-run and pass the same
`--project-root` with `--resume <run_id>` (printed at the start of every run); to see the
budget ceiling: add `--max-total-tokens 5000`; to see process cleanup: `--prune-runs` and
`--close-sessions` afterwards.
"""

import os
import subprocess
import sys

CALC_PY = '''"""A tiny calculator module used to demonstrate the Synagon pipeline."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
'''

TEST_CALC_PY = """from calc import add, subtract, multiply


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(4, 5) == 20
"""

# A project's orchestrator.yaml is self-contained - it does not merge with the built-in
# default - so this names every field the loader requires, using the same agent/model
# pairings this project's own orchestrator.yaml settled on after measuring the
# alternatives (see CHANGELOG.md, "the researcher moved off Antigravity").
ORCHESTRATOR_YAML = """models:
  antigravity:
    - id: gemini-3.8-flash-high
      name: Gemini 3.8 Flash (High)

  claude:
    - id: sonnet
      name: Claude Sonnet (CLI alias)

  opencode:
    - id: opencode/big-pickle
      name: Big Pickle (OpenCode)

agents:
  - agent: claude
    model: sonnet
    role: researcher

  - agent: claude
    model: sonnet
    role: planner

  - agent: opencode
    model: opencode/big-pickle
    role: implementer

  - agent: antigravity
    model: gemini-3.8-flash-high
    role: verifier

verification:
  consensus: unanimous
  acceptance:
    command: "pytest -q"
    required: true

roles:
  decomposer:
    responsibility: >-
      Break the user's goal into the smallest set of focused, independently
      deliverable tasks, with explicit dependencies, the areas each task is
      expected to touch, and how each one would be judged done. Plan only:
      never implement.

  researcher:
    responsibility: >-
      Investigate and analyze the supplied project context and user task.
      Provide project-specific observations, identify structural gaps, and
      outline areas for deeper technical inspection.

  planner:
    responsibility: >-
      Review the user task, project context, and researcher findings.
      Formulate concrete, prioritized planning recommendations, architectural
      decisions, and actionable next steps.

  implementer:
    responsibility: >-
      Implement the approved architectural and code changes directly in the
      workspace with clean, maintainable modifications.

  verifier:
    responsibility: >-
      Verify the implementation against the original user task,
      project context, research findings, planning recommendations,
      and implementation result. Execute relevant tests when appropriate,
      identify regressions or errors, and return a clear PASS or FAIL
      verdict with supporting findings.
"""


def main() -> None:
    target = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "scratch"))

    if os.path.exists(target):
        print(f"Refusing to overwrite an existing directory: {target}")
        print("Delete it first if you want a fresh demo project.")
        sys.exit(1)

    os.makedirs(target)

    with open(os.path.join(target, "calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CALC_PY)
    with open(os.path.join(target, "test_calc.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(TEST_CALC_PY)
    with open(os.path.join(target, "orchestrator.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(ORCHESTRATOR_YAML)

    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Initial calc module: add and subtract only, tests expect multiply too"],
        cwd=target,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Synagon Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
             "GIT_COMMITTER_NAME": "Synagon Demo", "GIT_COMMITTER_EMAIL": "demo@example.com"},
    )

    print(f"Demo project ready at {target}")
    print()
    print("If this is the first time your implementer CLI has touched this exact directory,")
    print("grant it workspace trust once first - a fully headless run has no terminal to")
    print("answer that first-run trust prompt, so every file edit is denied until this has")
    print("run once (see README.md, Troubleshooting, for why). For OpenCode:")
    print(f'  cd "{target}" && opencode run --auto "read calc.py" --model opencode/big-pickle')
    print("For Claude Code, open the directory once in its own interactive CLI instead.")
    print()
    print("Then run the demo with:")
    print(f'  python -m orchestrator --project-root "{target}" \\')
    print('      "Add a multiply(a, b) function to calc.py so the existing tests in ' \
          'test_calc.py pass. Do not modify test_calc.py."')


if __name__ == "__main__":
    main()
