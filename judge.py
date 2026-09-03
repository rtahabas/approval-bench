#!/usr/bin/env python3
"""Read one finished task twice: once as the gate would, once as the truth.

gate   Take the agent's fix away — revert the source files it edited to the
       revision it started from — and require the tests it wrote to fail. A
       test that still passes without the fix was not testing the fix, whatever
       it asserts. Only the tests the agent added are run, and only the files
       it changed are reverted, so neither side of the question is borrowed
       from the project's existing suite.

truth  Restore the maintainer's held-out test and run it. The agent never saw
       it, and it was verified during setup to fail on the bug and pass on the
       real fix, so it is a fair question.

Both readings are recorded even when they agree. The interesting cell is
gate=proven with truth=fail: work that passed the check and does not work.

usage: judge.py <task.json>
"""
import argparse
import ast
import difflib
import json
import pathlib
import shutil
import subprocess
import sys

TIMED_OUT = 124


def run(cmd, cwd, timeout=300):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, TIMED_OUT, "", f"timed out after {timeout}s")


def pytest(work, *targets, timeout=300):
    py = str(work / ".venv" / "bin" / "python")
    return run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
               work, timeout)


def before(clone, parent, rel):
    """The file as it was before the agent touched it."""
    done = run(["git", "-C", str(clone), "show", f"{parent}:{rel}"], clone)
    return done.stdout if done.returncode == 0 else None


def changed_lines(old, new):
    """Line numbers in `new` that the agent added or altered."""
    lines = set()
    matcher = difflib.SequenceMatcher(
        None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return lines


def qualified_tests(source):
    """Test functions in a file, as pytest would address them."""
    found = set()

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, prefix + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name.startswith("test"):
                found.add("::".join(prefix + [child.name]))
    walk(ast.parse(source), [])
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--arm", default="A")
    args = ap.parse_args()

    meta = json.loads(pathlib.Path(args.task).read_text())
    suffix = ".run.json" if args.arm == "A" else f".run-{args.arm}.json"
    run_path = pathlib.Path(args.task).with_suffix(suffix)
    row = json.loads(run_path.read_text())
    work = pathlib.Path(meta["work"])

    clone = pathlib.Path(meta["clone"])
    added, edited = row["files_added"], row["files_edited"]
    test_files = [f for f in added + edited if "test" in pathlib.Path(f).name]
    edited_sources = [f for f in edited if f not in test_files]

    # Only the tests the agent actually wrote.
    #
    # The agents here edited the project's existing test module rather than
    # adding a file, so "the agent's test" resolved to the whole suite for that
    # module -- which notices almost any mutation anywhere and made `proven`
    # close to automatic. Ask pytest for the new test functions by name.
    agent_tests = []
    for rel in test_files:
        if not (work / rel).exists():        # a file the run left behind
            continue
        new = (work / rel).read_text()
        old = before(clone, meta["parent"], rel)
        fresh = qualified_tests(new) - (qualified_tests(old) if old else set())
        agent_tests += [f"{rel}::{name}" for name in sorted(fresh)]

    verdict = {"gate": None, "gate_why": None, "truth": None, "truth_why": None}

    # --- gate -------------------------------------------------------------
    if not edited_sources:
        verdict.update(gate="refused", gate_why="agent changed no source file")
    elif not agent_tests:
        verdict.update(gate="refused", gate_why="agent wrote no test")
    else:
        control = pytest(work, *agent_tests)
        if control.returncode != 0:
            verdict.update(gate="refused",
                           gate_why="agent's own test does not pass before mutation")
        else:
            # Take the agent's fix away and require its test to complain.
            #
            # Synthesising a mutation was the first approach and it is the
            # weaker one: `mutants` only produces comparison, boolean, number
            # and early-return edits, so a fix built from anything else leaves
            # nothing to break and the gate has to refuse. On the jinja task
            # the agent changed seven lines and not one of them carried a
            # mutable construct.
            #
            # Reverting is always available and it asks the question directly.
            # A test that still passes with the fix removed was not testing the
            # fix, whatever it asserts.
            held = {}
            reverted = []
            for rel in edited_sources:
                old = before(clone, meta["parent"], rel)
                if old is None or not (work / rel).exists():
                    continue
                path = work / rel
                held[rel] = path.read_text()
                touched = changed_lines(old, held[rel])
                path.write_text(old)
                reverted.append(f"{rel} ({len(touched)} line(s))")
            if not reverted:
                verdict.update(gate="refused",
                               gate_why="no pre-agent revision to revert to")
            else:
                try:
                    noticed = pytest(work, *agent_tests).returncode != 0
                finally:
                    for rel, text in held.items():
                        (work / rel).write_text(text)
                for rel, text in held.items():
                    assert (work / rel).read_text() == text, f"{rel} not restored"
                verdict.update(
                    gate="proven" if noticed else "unresolved",
                    gate_why="reverted " + ", ".join(reverted) + " -> " +
                             ("test noticed" if noticed else "test did not notice"))

    # --- truth ------------------------------------------------------------
    #
    # The held-out test usually lives in a file the agent also edited, so
    # putting the answer key in place overwrites the agent's work. The first
    # version copied the key over it and then deleted the file outright, which
    # destroyed the very thing under test: after judging, three tasks had no
    # agent test left and could not be re-judged. Whatever is there is put back
    # exactly as it was, including "there was nothing here".
    restored, saved = [], {}
    for key in sorted(pathlib.Path(meta["answers"]).glob("*.py")):
        target = None
        for t in meta["tests"]:
            if pathlib.Path(t).name == key.name:
                target = work / t
        if target is None:
            continue
        rel = str(target.relative_to(work))
        saved[rel] = target.read_text() if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(key, target)
        restored.append(rel)
    if not restored:
        verdict.update(truth="error", truth_why="no held-out test restored")
    else:
        held = pytest(work, *restored, timeout=120)
        verdict.update(
            truth="pass" if held.returncode == 0 else "fail",
            truth_why=(held.stdout.strip().splitlines() or ["(no output)"])[-1][:140])
    for rel in restored:                     # put the tree back exactly as found
        target = work / rel
        if saved[rel] is None:
            target.unlink()
        else:
            target.write_text(saved[rel])

    row["verdict"] = verdict
    row["agent_tests"] = agent_tests
    row["edited_sources"] = edited_sources
    run_path.write_text(json.dumps(row, indent=2))

    print(f"{row['task']}: gate={verdict['gate']:<10} truth={verdict['truth']}")
    print(f"    gate : {verdict['gate_why']}")
    print(f"    truth: {verdict['truth_why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
