# approval-bench

Measures whether an agent's "done" can be trusted, using your repository as
both the exam and the answer key.

```
python3 approval_bench.py <repo-path-or-url> --tasks 5
python3 approval_bench.py <repo-path-or-url> --tasks 5 --dry-run   # builds only, spends nothing
```

Python 3.11+, `git`, and whatever command runs your agent. Nothing leaves the
machine; the repository is the only input.

## What it does

A commit that fixed a bug **and** wrote a test for it in the same breath is a
ready-made exam question. The commit before it is the bug. The maintainer's
commit message is the brief. Their test is held back as an answer key the agent
never sees.

The agent is asked to fix the bug and prove the fix with a test. Then two
readings are taken, independently:

| | what it asks |
|---|---|
| **gate** | Take the agent's fix away and run only the tests the agent wrote. A test that still passes without the fix was not testing the fix. |
| **truth** | Restore the maintainer's held-out test. |

The result is one table:

```
                truth=pass    truth=fail
gate=proven              9             0
gate=unresolved          0             0
```

`gate=proven, truth=fail` is the cell worth knowing: work a check accepted that
does not work. `gate=unresolved, truth=pass` is what the check costs you in
false alarms.

## Why a task can be rejected

Most candidates are thrown out, deliberately. A task counts only if:

1. the suite is green at the parent commit — otherwise a later red says nothing
2. the held-out test **fails** on the bug — otherwise it does not test the bug
3. the held-out test **passes** on the real fix — otherwise it does not test the fix
4. the suite is still green once the answer key is removed

A bench that is wrong about its own starting state produces numbers that look
fine and mean nothing.

## What it does to stay honest

- The task tree has no `.git` and the answer key is stored outside it, so the
  agent cannot read the fix out of history. Verified, not assumed.
- The environment comes from the project's own declaration — extras, PEP 735
  dependency groups — not from guessing package names out of import errors.
- If the newest pytest rejects code of the repository's era, it steps back a
  major version rather than reporting the project as broken.
- Test modules that cannot be imported at all are set aside **and recorded**, so
  "green" always says green over what. Never if the answer key is among them.
- Judging restores everything it touched, including the agent's own test.
- A hang is recorded as a failure, not as a crashed harness.
- Every install, pin and exclusion is written into the task's metadata.

## Limits

- One task is one commit. Bugs that no single commit fixed are out of reach.
- Mature libraries fix bugs in small diffs; this measures that kind of work and
  says nothing about long or underspecified tasks.
- The agent has network access and could in principle look the project up. The
  brief carries no repository name or commit id, but this is not prevented.
- A run costs real money — roughly one agent session per task.

## Files

| | |
|---|---|
| `approval_bench.py` | entry point; everything below is called by it |
| `mine.py` | finds commits that change source and tests together |
| `setup_task.py` | builds one task and runs the four validity checks |
| `seal_task.py` | closes the side channels before the agent is let in |
| `run_task.py` | hands the task to the agent, records cost and turns |
| `judge.py` | the gate and the truth readings |
| `report.py` | the table |
| `approval_rate.py` | the other side of the question: how often a *human* approval let a bad change through — approved-and-merged PRs that were reverted or hot-fixed within a window, read from GitHub reviews plus the clone's history. Every hit is printed with its commit; a revert is a proxy, not proof |
| `results/` | the nine runs behind the table in this README — real output, kept so the numbers can be checked rather than taken |
| `tests/` | 22 tests, each one a bug this harness shipped and had to have caught by hand |
