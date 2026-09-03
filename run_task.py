#!/usr/bin/env python3
"""Hand one task to an agent, then judge the result twice.

The agent is told what is broken and asked to fix it and prove the fix with a
test. Then two independent readings are taken:

  gate  - what a `proof-of-done` style check says: is the agent's own test
          load-bearing? Break the line the agent changed and require its test
          to notice.
  truth - what the maintainer's held-out test says. The agent never saw it.

The pair is the whole point. `gate=proven, truth=fail` is the case worth
knowing about: the system accepted work that does not work. Reporting only the
gate would hide exactly that.

usage: run_task.py <task.json> [--model NAME] [--max-turns N] [--timeout S]
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

BRIEF = """There is a bug in this project.

{subject}

{body}

Fix it, and prove the fix with a test.

- Write your test in the project's existing tests/ directory.
- Run the suite with `.venv/bin/python -m pytest -q` from this directory. It is
  green right now, and it has to stay green.
- Do not weaken or delete existing tests.

When you are done, say what you changed and why your test would fail if the fix
were reverted."""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_files(work, manifest):
    """What the agent touched, read off the tree rather than asked of git.

    The tree has no .git by the time an agent sees it, so a hash manifest taken
    at seal time is the only record of what the starting state was.
    """
    added, edited = [], []
    for p in sorted(work.rglob("*.py")):
        if ".venv" in p.parts:
            continue
        rel = str(p.relative_to(work))
        if rel not in manifest:
            added.append(rel)
        elif sha(p) != manifest[rel]:
            edited.append(rel)
    return added, edited


def run_agent(work, brief, model, max_turns, timeout):
    cmd = ["claude", "-p", brief, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions"]
    if model:
        cmd += ["--model", model]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    try:
        done = subprocess.run(cmd, cwd=work, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"cost_usd": None, "turns": None, "result": None,
                "outcome": "timeout", "raw_lines": 0}

    cost = turns = result = None
    lines = 0
    for line in done.stdout.splitlines():
        lines += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            cost = event.get("total_cost_usd")
            turns = event.get("num_turns")
            result = event.get("result")
    return {"cost_usd": cost, "turns": turns, "result": result,
            "outcome": "ok" if cost is not None else "no-result",
            "raw_lines": lines, "returncode": done.returncode}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--model")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=1800)
    # The second arm of the experiment swaps only the description of the bug:
    # the maintainer's commit message (written after diagnosis) for the user's
    # original issue (written before it). Both are real artefacts from the same
    # project, so neither arm is one we authored to make a point.
    ap.add_argument("--brief-file", help="use this text as the bug report")
    ap.add_argument("--arm", default="A", help="label recorded with the result")
    args = ap.parse_args()

    meta_path = pathlib.Path(args.task)
    meta = json.loads(meta_path.read_text())
    if not meta.get("sealed"):
        raise SystemExit(f"{meta_path.name} is not sealed — run seal_task.py first")
    work = pathlib.Path(meta["work"])

    if args.brief_file:
        report = pathlib.Path(args.brief_file).read_text().strip()
        brief = BRIEF.format(subject=report, body="")
    else:
        brief = BRIEF.format(subject=meta["subject"], body=meta["body"] or "")
    agent = run_agent(work, brief, args.model, args.max_turns, args.timeout)
    added, edited = changed_files(work, meta["manifest"])

    row = {"task": meta_path.stem, "repo": meta["repo"], "sha": meta["sha"],
           "subject": meta["subject"], "arm": args.arm, "brief": brief, "agent": agent,
           "files_added": added, "files_edited": edited}
    suffix = ".run.json" if args.arm == "A" else f".run-{args.arm}.json"
    out = meta_path.with_suffix("").with_suffix(suffix) \
        if meta_path.suffix == ".json" else meta_path.with_suffix(suffix)
    out.write_text(json.dumps(row, indent=2))

    print(f"{meta_path.stem}: outcome={agent['outcome']} "
          f"turns={agent['turns']} cost=${agent['cost_usd'] or 0:.2f} "
          f"added={len(added)} edited={len(edited)}")
    for f in added + edited:
        print(f"    {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
