#!/usr/bin/env python3
"""Turn the finished runs into the one table this bench exists to produce.

Rows are tasks, and each carries two independent readings: what the gate said,
and what the maintainer's held-out test said. The cell that matters is
gate=proven with truth=fail — work the check accepted that does not work. A
report that leads with "the gate proved N of M" and leaves that cell in a
footnote is doing the thing this whole exercise is against.

usage: report.py <work-dir>
"""
import argparse
import json
import pathlib

GATES = ("proven", "unresolved", "refused")
TRUTHS = ("pass", "fail", "error")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    args = ap.parse_args()

    rows = []
    for p in sorted(pathlib.Path(args.work).glob("*.run.json")):
        rows.append(json.loads(p.read_text()))
    if not rows:
        raise SystemExit(f"no finished runs under {args.work}")

    print(f"{len(rows)} task(s)\n")
    print(f"{'task':<22} {'gate':<11} {'truth':<6} {'turns':>5} {'cost':>7}  subject")
    total = 0.0
    for r in rows:
        v = r.get("verdict") or {}
        cost = r["agent"].get("cost_usd") or 0.0
        total += cost
        print(f"{r['task']:<22} {str(v.get('gate')):<11} {str(v.get('truth')):<6} "
              f"{str(r['agent'].get('turns')):>5} {cost:>7.2f}  {r['subject'][:38]}")

    grid = {(g, t): 0 for g in GATES for t in TRUTHS}
    for r in rows:
        v = r.get("verdict") or {}
        key = (v.get("gate"), v.get("truth"))
        if key in grid:
            grid[key] += 1

    print(f"\ntotal spent: ${total:.2f}\n")
    print(f"{'':<12}" + "".join(f"{'truth=' + t:>14}" for t in TRUTHS))
    for g in GATES:
        print(f"{'gate=' + g:<12}" + "".join(f"{grid[(g, t)]:>14}" for t in TRUTHS))

    decided = sum(grid[(g, t)] for g in ("proven", "unresolved") for t in ("pass", "fail"))
    false_pass = grid[("proven", "fail")]
    false_alarm = grid[("unresolved", "pass")]
    caught = grid[("unresolved", "fail")]
    clean = grid[("proven", "pass")]

    print()
    if decided == 0:
        print("nothing decided: every task refused or errored — no rate to report")
        return 0
    print(f"of {decided} decided task(s):")
    print(f"  accepted and correct   {clean}")
    print(f"  let a bad fix through  {false_pass}   <- the number that matters")
    print(f"  flagged a good fix     {false_alarm}   (cost of the check)")
    print(f"  caught a bad fix       {caught}")
    print(f"\nhuman would review {false_pass + false_alarm + caught} of {decided} "
          f"instead of {decided}")
    print(f"n={decided} — too small to generalise; this is a pilot, not a rate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
