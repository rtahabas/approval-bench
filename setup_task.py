#!/usr/bin/env python3
"""Turn a real fix commit back into a task, and prove the ground is clean.

For a commit C that fixed a bug and wrote a test for it, the state just before
C is the bug. So the task tree is C's parent: the defect is real, the brief is
the maintainer's own commit message, and the test they wrote is held back as an
answer key neither we nor the agent has seen.

Three things are checked before a task is usable, because a bench that is wrong
about its own starting state produces numbers that look fine and mean nothing:

  1. the suite at C^ is green   - otherwise a later red says nothing
  2. the held-out test FAILS at C^ - otherwise it does not test the bug
  3. the held-out test PASSES at C  - otherwise it does not test the fix

A task that misses any of those is dropped, loudly, rather than measured.

usage: setup_task.py <repo-dir> <sha> <work-root> [--venv PATH]
"""
import argparse
import json
import pathlib
import re
import shutil
import tomllib
import subprocess
import sys


TIMED_OUT = 124  # what a shell reports for a killed command; no real exit code collides


def run(cmd, cwd=None, env=None, timeout=600):
    """Run a command, and treat a hang as a result rather than an accident.

    One of the first tasks tried here was a fix for hanging on cyclic exception
    chains. Its held-out test does not fail on the buggy code — it hangs, which
    is the bug. Letting TimeoutExpired escape turned a well-behaved task into a
    crashed harness. A hang is a failing test; it just fails by not stopping.
    """
    try:
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, TIMED_OUT, (exc.stdout or b"").decode("utf8", "replace")
            if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            f"timed out after {timeout}s")


def git(repo, *args, check=True):
    done = run(["git", "-C", str(repo), *args])
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: "
                         f"{done.stderr.strip()[:300]}")
    return done.stdout


def declared_groups(work):
    """Yield (pip-args, group-name) for each test-ish PEP 735 dependency group.

    Groups may list plain requirement strings or {include-group: other}; the
    include is followed so a group that only points elsewhere still resolves.
    """
    pyproject = work / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        data = tomllib.loads(pyproject.read_text())
    except tomllib.TOMLDecodeError:
        return
    groups = data.get("dependency-groups") or {}

    def flatten(name, seen):
        if name in seen or name not in groups:
            return []
        seen.add(name)
        out = []
        for item in groups[name]:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and "include-group" in item:
                out += flatten(item["include-group"], seen)
        return out

    for name in ("test", "tests", "dev"):
        specs = flatten(name, set())
        if specs:
            yield specs, name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("sha")
    ap.add_argument("work_root")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    work = pathlib.Path(args.work_root).resolve() / f"{repo.name}-{args.sha[:8]}"
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)

    parent = git(repo, "rev-parse", f"{args.sha}^").strip()
    subject = git(repo, "log", "-1", "--format=%s", args.sha).strip()
    body = git(repo, "log", "-1", "--format=%b", args.sha).strip()
    files = git(repo, "show", "--name-only", "--format=", args.sha).split()
    tests = [f for f in files if f.endswith(".py")
             and (f.startswith("tests/") or "/test_" in f)]
    srcs = [f for f in files if f.endswith(".py") and f not in tests]

    # A worktree at the parent: the repository exactly as it was with the bug.
    # Prune first: a run that died leaves the path registered but absent, and
    # git then refuses the next add for a directory that is not there.
    git(repo, "worktree", "prune")
    git(repo, "worktree", "add", "--force", "--detach", str(work), parent)

    venv = work / ".venv"
    run([args.python, "-m", "venv", str(venv)])
    py = venv / "bin" / "python"
    pip = run([str(py), "-m", "pip", "install", "-q", "-e", ".", "pytest"], cwd=work)
    if pip.returncode != 0:
        print(f"DROP {repo.name}-{args.sha[:8]}: install failed — "
              f"{pip.stderr.strip().splitlines()[-1][:160]}")
        return 3

    def pytest(*extra, timeout=900):
        return run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra],
                   cwd=work, timeout=timeout)

    # Test dependencies, the project's own declaration first.
    #
    # These repos do not agree on where they keep them: tenacity has a [test]
    # extra, the others have none. Rather than hand-build an environment per
    # repo -- which makes the bench a thing we assembled instead of a thing the
    # project defines -- try the declared extras, then close whatever gap is
    # left by reading the import errors. Everything added is recorded in the
    # task's metadata, so "it ran" always comes with "in what".
    installed_extras, installed_missing, installed_groups = [], [], []

    # PEP 735 dependency groups, which is where urllib3 keeps its test deps.
    # Without this the fallback below guessed package names from import errors
    # and pulled the newest of each: the newest quart has no RequestContext, so
    # quart-trio failed to import and the suite "was red at parent". It was not
    # — we had built an environment the project never asked for.
    for spec, group in declared_groups(work):
        if run([str(py), "-m", "pip", "install", "-q", *spec],
               cwd=work).returncode == 0:
            installed_groups.append(group)

    for extra in ("test", "tests", "dev"):
        if run([str(py), "-m", "pip", "install", "-q", "-e", f".[{extra}]"],
               cwd=work).returncode == 0:
            installed_extras.append(extra)
    for _ in range(4):
        probe = pytest("--collect-only", "-q")
        missing = sorted({m for m in re.findall(
            r"No module named '([A-Za-z0-9_]+)'", probe.stdout + probe.stderr)})
        if not missing:
            break
        if run([str(py), "-m", "pip", "install", "-q", *missing],
               cwd=work).returncode != 0:
            break
        installed_missing += missing

    # Meet the code with a test runner of roughly its own era.
    #
    # Installing the newest pytest made click's suite fail to collect at most
    # revisions: modern pytest turns "a non-Collection iterable passed to
    # parametrize" into an error, and click's tests of that period do exactly
    # that. Read literally the harness said "this project's suite is red",
    # which is false — the project is fine, the runner is from the wrong year.
    # 139 candidates were being discarded for a fault in our own environment.
    # Set aside test modules that cannot even be imported here.
    #
    # urllib3's suite needs a quart/quart-trio pair whose own version ranges no
    # longer resolve to a working combination, so one module fails to import
    # while 1,974 tests collect fine. Dropping the whole task for that discards
    # a usable bug; dropping the module keeps the ground honest as long as the
    # answer key does not live in it — which is checked, not assumed. Whatever
    # is set aside is recorded, so a green baseline always says green over what.
    ignored = []
    probe = pytest("--collect-only", "-q")
    if probe.returncode != 0:
        broken = sorted(set(re.findall(r"ERROR collecting (\S+)",
                                       probe.stdout + probe.stderr)))
        held = {t for t in tests}
        if broken and not (held & set(broken)):
            ignored = [f"--ignore={b}" for b in broken]

    def pytest_ok(*extra, **kw):
        return pytest(*ignored, *extra, **kw)

    baseline = pytest_ok()
    pytest_pin = None
    noise = baseline.stdout + baseline.stderr
    if baseline.returncode != 0 and ("PytestRemovedIn" in noise
                                     or "PytestDeprecationWarning" in noise):
        for pin in ("pytest<9", "pytest<8", "pytest<7"):
            if run([str(py), "-m", "pip", "install", "-q", pin],
                   cwd=work).returncode != 0:
                continue
            baseline = pytest_ok()
            if baseline.returncode == 0:
                pytest_pin = pin
                break

    if baseline.returncode != 0:
        tail = baseline.stdout.strip().splitlines()[-1:] or ["(no output)"]
        print(f"DROP {repo.name}-{args.sha[:8]}: suite already red at parent — {tail[0][:160]}")
        return 4

    pytest_version = run([str(py), "-m", "pytest", "--version"],
                         cwd=work).stdout.strip().splitlines()[:1]
    pytest_version = pytest_version[0] if pytest_version else "?"

    # Bring in only the maintainer's test, and require it to fail on the bug.
    for t in tests:
        (work / t).parent.mkdir(parents=True, exist_ok=True)
        (work / t).write_text(git(repo, "show", f"{args.sha}:{t}"))
    held_fails_before = pytest_ok(*tests, timeout=90).returncode != 0

    # And to pass once the real fix is in place.
    for s in srcs:
        (work / s).write_text(git(repo, "show", f"{args.sha}:{s}"))
    held_passes_after = pytest_ok(*tests, timeout=90).returncode == 0

    # Put the tree back to the bug state and take the answer key out of it.
    #
    # The key is the maintainer's *new* test, not the file that contains it.
    # Removing the whole file broke the rest of the suite in tenacity, where
    # other modules do `from .test_tenacity import NoIOErrorAfterCount` — and
    # because the baseline was checked before the file was moved, the harness
    # reported a green ground it was not going to run on. So each test file
    # goes back to its parent revision, which has the shared helpers and not
    # the new test. A file that did not exist at the parent is simply removed.
    for s in srcs:
        (work / s).write_text(git(repo, "show", f"{parent}:{s}"))
    answers = work.parent / "answers" / f"{repo.name}-{args.sha[:8]}"
    answers.mkdir(parents=True, exist_ok=True)
    for t in tests:
        shutil.copy(work / t, answers / pathlib.Path(t).name)
        before = run(["git", "-C", str(repo), "show", f"{parent}:{t}"])
        if before.returncode == 0:
            (work / t).write_text(before.stdout)
        else:
            (work / t).unlink()

    # Re-check the ground in the state the agent will actually meet.
    final = pytest_ok()
    if final.returncode != 0:
        tail = (final.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(f"DROP {repo.name}-{args.sha[:8]}: suite red after removing the "
              f"held-out test — {tail[:150]}")
        return 6

    ok = held_fails_before and held_passes_after
    meta = {"repo": repo.name, "clone": str(repo), "sha": args.sha, "parent": parent,
            "env_extras": installed_extras, "env_added": installed_missing,
            "env_groups": installed_groups, "ignored_modules": ignored,
            "pytest_pin": pytest_pin, "pytest_version": pytest_version,
            "subject": subject, "body": body, "src": srcs, "tests": tests,
            "baseline_green": True,
            "held_out_fails_on_bug": held_fails_before,
            "held_out_passes_on_fix": held_passes_after,
            "usable": ok, "work": str(work), "answers": str(answers)}
    (work.parent / f"{repo.name}-{args.sha[:8]}.json").write_text(json.dumps(meta, indent=2))

    mark = "OK  " if ok else "DROP"
    print(f"{mark} {repo.name}-{args.sha[:8]}  baseline=green  "
          f"held_out: fails_on_bug={held_fails_before} passes_on_fix={held_passes_after}"
          f"  | {subject[:52]}")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
