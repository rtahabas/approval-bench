#!/usr/bin/env python3
"""Measure whether an agent's "done" can be trusted, on your own repository.

    python3 approval_bench.py <repo-path-or-url> --tasks 5

The repository supplies both the problems and the answers. A commit that fixed
a bug and wrote a test for it becomes a task: the commit before is the bug, the
maintainer's message is the brief, and their test is held back. The agent fixes
it and writes its own test. Then two readings are taken independently —

    gate    take the agent's fix away and run only the tests it wrote. A test
            that still passes without the fix was not testing the fix.
    truth   restore the held-out test the agent never saw.

and the pair is printed as a table. The cell worth knowing is gate=proven with
truth=fail: work a check accepted that does not work.

Nothing here needs access to anything but the repository you point it at, and
the agent runs on your machine under your own credentials.
"""
import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def step(script, *args):
    done = subprocess.run([sys.executable, str(HERE / script), *map(str, args)],
                          capture_output=True, text=True)
    line = (done.stdout.strip().splitlines() or [""])[-1]
    return done.returncode, line, done.stdout


def resolve_repo(target, work):
    """Accept a path or a URL; clone in full if it is a URL.

    A blob-filtered clone cannot serve `git log --numstat`: git dies partway
    with exit 128 after printing what it managed, which reads as a smaller
    repository rather than a broken read. So: full clone, deliberately.
    """
    path = pathlib.Path(target).expanduser()
    if path.exists():
        return path.resolve()
    name = target.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = work / "repos" / name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"cloning {target} …")
        if subprocess.run(["git", "clone", "-q", target, str(dest)]).returncode != 0:
            raise SystemExit(f"could not clone {target}")
    return dest.resolve()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="local path or git URL")
    ap.add_argument("--tasks", type=int, default=5, help="how many usable tasks to run")
    ap.add_argument("--since", default="2021-01-01")
    ap.add_argument("--max-src-lines", type=int, default=60)
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--work", default=str(HERE / "run"))
    ap.add_argument("--dry-run", action="store_true",
                    help="build and validate tasks, run no agent and spend nothing")
    args = ap.parse_args()

    work = pathlib.Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    repo = resolve_repo(args.repo, work)

    cand_file = work / f"candidates-{repo.name}.json"
    rc, line, _ = step("mine.py", repo, args.max_src_lines,
                       "--since", args.since, "--json", cand_file, "--top", 0)
    if rc != 0:
        raise SystemExit(line)
    candidates = json.loads(cand_file.read_text())
    print(f"{repo.name}: {len(candidates)} candidate commit(s) change source and "
          f"test together\n")
    if not candidates:
        raise SystemExit("nothing to build from — try --since earlier or "
                         "--max-src-lines larger")

    # Most candidates do not survive validation, and that is the point: a task
    # whose ground is not provably clean would produce a number that means
    # nothing. Walk the list until enough of them do.
    built, tried = [], 0
    for cand in candidates:
        if len(built) >= args.tasks:
            break
        tried += 1
        sha = cand["sha"][:8]
        rc, line, _ = step("setup_task.py", repo, cand["sha"], work / "tasks")
        print(f"  [{tried}] {line}")
        if rc != 0:
            continue
        meta = work / "tasks" / f"{repo.name}-{sha}.json"
        rc, line, _ = step("seal_task.py", meta)
        if rc != 0:
            print(f"      {line}")
            continue
        built.append(meta)

    print(f"\n{len(built)} usable task(s) from {tried} candidate(s) examined")
    if args.dry_run or not built:
        print("dry run — no agent was called, nothing was spent")
        return 0

    for meta in built:
        print(f"\n── {meta.stem} ──")
        _, line, _ = step("run_task.py", meta, "--max-turns", args.max_turns,
                          "--timeout", args.timeout)
        print(line)
        _, _, out = step("judge.py", meta)
        print(out.strip())

    print("\n" + "═" * 60)
    _, _, out = step("report.py", work / "tasks")
    print(out.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
