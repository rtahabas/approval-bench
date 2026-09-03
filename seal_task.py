#!/usr/bin/env python3
"""Close the side channels before an agent is let into a task tree.

The tree is built as a git worktree, which is convenient for us and a gift to
the agent: its .git points back at the real repository, where the fix commit,
its message and the maintainer's test are all still readable. An agent that
runs `git log` there is not solving the task, it is reading the answer — and
the run would look like a success.

Three earlier experiments in this workspace leaked through their own scaffolding
(a spec left in a guessable path, .pytest_cache holding test names). Each time
the numbers looked fine. So sealing is a separate, explicit step, and it records
a manifest of every source file's hash so the runner can tell what the agent
changed without asking git afterwards.

usage: seal_task.py <task.json>
"""
import hashlib
import json
import pathlib
import shutil
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    meta_path = pathlib.Path(sys.argv[1])
    meta = json.loads(meta_path.read_text())
    work = pathlib.Path(meta["work"])

    removed = []
    # In a worktree, .git is a file pointing at the parent repo. Either shape
    # hands over the history, so both go.
    dot_git = work / ".git"
    if dot_git.is_file():
        dot_git.unlink()
        removed.append(".git (worktree pointer)")
    elif dot_git.is_dir():
        shutil.rmtree(dot_git)
        removed.append(".git (directory)")
    for junk in (".pytest_cache", ".tox", ".nox"):
        p = work / junk
        if p.exists():
            shutil.rmtree(p)
            removed.append(junk)

    manifest = {}
    for p in sorted(work.rglob("*.py")):
        if ".venv" in p.parts:
            continue
        manifest[str(p.relative_to(work))] = sha(p)

    # The answer key must not be one `ls ..` away either. It is built next to
    # the task tree for convenience during setup; move it out of the work root
    # entirely before anyone is allowed in.
    answers = pathlib.Path(meta["answers"]).resolve()
    keys_root = work.parent.parent / "keys"
    if answers.is_relative_to(work.parent.resolve()):
        keys_root.mkdir(parents=True, exist_ok=True)
        moved = keys_root / answers.name
        if moved.exists():
            shutil.rmtree(moved)
        shutil.move(str(answers), str(moved))
        answers = moved.resolve()
        meta["answers"] = str(answers)
        removed.append(f"answers -> {keys_root.name}/")
    leak = answers.is_relative_to(work.resolve()) or \
        answers.is_relative_to(work.parent.resolve())

    meta["sealed"] = True
    meta["sealed_removed"] = removed
    meta["manifest"] = manifest
    meta["answers_inside_tree"] = leak
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"{'LEAK' if leak else 'SEALED'} {work.name}: removed={removed or 'nothing'} "
          f"files_tracked={len(manifest)} answers_outside_tree={not leak}")
    return 1 if leak else 0


if __name__ == "__main__":
    raise SystemExit(main())
