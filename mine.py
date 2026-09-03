#!/usr/bin/env python3
"""Find real fix commits that can be turned back into tasks.

A usable task is a non-merge commit that changes library source AND test code
in the same breath: the source change is the fix, the test is the maintainer's
own statement of what the fix has to do. Reverting both gives an agent a real
problem and leaves us a held-out answer neither of us wrote.

One `git log` pass for the whole repo. The first version spawned three git
processes per commit and took longer than ten minutes on a 2,000-commit repo,
which is not a measurement cost anyone should pay to list candidates.

usage: mine.py <repo> [max-source-lines] [--since YYYY-MM-DD] [--json out.json]
"""
import argparse
import json
import subprocess

SEP = "\x1e"  # record separator, safe inside git's --format


def is_test(path):
    return path.startswith("tests/") or "/test_" in path or path.startswith("test_")


def is_src(path):
    """Library code, whatever layout the project uses.

    Requiring a `src/` prefix silently skipped every flat-layout project, which
    reads as "no candidates" rather than "the filter does not fit this repo".
    """
    if not path.endswith(".py") or is_test(path):
        return False
    head = path.split("/")[0]
    return head not in {"docs", "examples", "scripts", "bench", "benchmarks"} \
        and path not in {"setup.py", "conftest.py", "noxfile.py"}


def read_log(repo, since):
    """Read the log, and refuse to return a partial one.

    A blob-filtered clone cannot serve --numstat: git dies partway through with
    exit 128 after printing whatever it managed. Reading only stdout, this looks
    like a smaller repo rather than a broken read — 42 commits where git itself
    counts 963. So the exit code is checked here, loudly, rather than trusted to
    be zero because output arrived.
    """
    args = ["git", "-C", repo, "log", "--no-merges", "--numstat",
            f"--format={SEP}%H|%ad|%s", "--date=short"]
    if since:
        args.append(f"--since={since}")
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["(no stderr)"])[0]
        raise SystemExit(
            f"git log failed in {repo} (exit {done.returncode}) after "
            f"{done.stdout.count(SEP)} commits: {first}\n"
            "A partial (--filter=blob:none) clone cannot serve --numstat; "
            "re-clone in full or run `git fetch --refetch`.")
    return done.stdout


def parse(raw):
    """Yield (sha, date, subject, [(added, deleted, path)]) per commit."""
    for block in raw.split(SEP):
        block = block.strip("\n")
        if not block:
            continue
        head, _, rest = block.partition("\n")
        sha, date, subject = head.split("|", 2)
        files = []
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                files.append((int(parts[0]), int(parts[1]), parts[2]))
        yield sha, date, subject, files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("max_src_lines", nargs="?", type=int, default=60)
    ap.add_argument("--since")
    ap.add_argument("--json")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    commits = list(parse(read_log(args.repo, args.since)))
    rows = []
    for sha, date, subject, files in commits:
        src = [f for f in files if is_src(f[2])]
        tests = [f for f in files if is_test(f[2]) and f[2].endswith(".py")]
        other = [f for f in files if f not in src and f not in tests]
        if not src or not tests or len(other) > 1:
            continue
        churn = sum(a + d for a, d, _ in src)
        if not 0 < churn <= args.max_src_lines:
            continue
        rows.append({"sha": sha, "date": date, "subject": subject,
                     "src_churn": churn,
                     "src": [p for *_, p in src],
                     "tests": [p for *_, p in tests]})

    print(f"{args.repo.split('/')[-1]}: {len(commits)} non-merge commits -> "
          f"{len(rows)} candidates (<={args.max_src_lines} source lines)")
    for r in rows[: args.top]:
        print(f"  {r['sha'][:8]}  {r['date']}  {r['src_churn']:>3}L  "
              f"{len(r['src'])}f/{len(r['tests'])}t  {r['subject'][:62]}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=2)
        print(f"  -> {len(rows)} written to {args.json}")


if __name__ == "__main__":
    main()
