#!/usr/bin/env python3
"""How often does a human approval let a bad change through?

"Manual approval for every task" is treated as the safe baseline. Nobody
measures the baseline. This does: for every pull request that was reviewed,
approved and merged, look at what happened to it afterwards.

  reverted   a later commit says `Revert "<title>"` or `This reverts commit
             <merge-sha>` within the window — the strongest signal a merge was
             wrong
  hotfixed   a later commit within the window touches the same source files and
             names the PR or its issue with fix/regression language — weaker,
             reported separately and never added to the revert count

Both are proxies. A revert can be a scope decision, not a bug; a hotfix can be
a follow-up feature. Every hit is printed with its commit so it can be read
rather than trusted.

Pull request metadata comes from GitHub's GraphQL API (repository.pullRequests,
not search). What happened next is read from a local clone's history.

usage: approval_rate.py <owner/repo> <clone-path> [--since YYYY-MM-DD] [--window-days 30]
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys

QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:MERGED, first:PAGE_SIZE, after:$cursor,
                 orderBy:{field:UPDATED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title mergedAt reviewDecision
        author { login }
        mergeCommit { oid }
        reviews(first:20) { nodes { state author { login } } }
        files(first:50) { nodes { path } }
        closingIssuesReferences(first:5) { nodes { number } }
      }
    }
  }
}
"""


def gh_graphql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            cmd += ["-f", f"{k}={v}"]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"gh api graphql failed: {done.stderr.strip()[:300]}")
    return json.loads(done.stdout)


PR_BUDGET = 3000


def merged_prs(owner, name, since, page_size=100):
    """Merged PRs newer than `since`, with their review record.

    The list is ordered by UPDATED_AT, which is not mergedAt: an old PR that
    just received a comment sits near the top with an old merge date. The first
    version returned at the first such row and reported 14 merged PRs for a
    repository that has hundreds — a silent truncation that reads as a small
    project. So: filter every row, and stop only when a whole page is older
    than the cut-off or the page budget runs out.

    A page costs page_size x (files + reviews) nodes. On a repository the size
    of langchain the server abandons the request at 100 — HTTP 502, then a
    cancelled stream. Asking for fewer rows per page is the fix; the page
    budget grows to match so the number of PRs reached stays the same.
    """
    max_pages = -(-PR_BUDGET // page_size)
    query = QUERY.replace("PAGE_SIZE", str(page_size))
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        pages += 1
        data = gh_graphql(query, owner=owner, name=name, cursor=cursor)
        block = data["data"]["repository"]["pullRequests"]
        rows = block["nodes"]
        out += [pr for pr in rows if pr["mergedAt"] >= since]
        if rows and all(pr["mergedAt"] < since for pr in rows):
            break
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    else:
        print(f"note: stopped after {max_pages} pages; older PRs may be missing",
              file=sys.stderr)
    return out


def git(clone, *args):
    done = subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {done.stderr.strip()[:200]}")
    return done.stdout


MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)|\(#(\d+)\)\s*$")


def prs_from_clone(commits):
    """Merged PRs reconstructed from history alone, for when GitHub is out of reach.

    A squash merge lands as one commit titled like the PR with "(#N)" on the
    end; a merge commit says "Merge pull request #N". Either gives the number,
    the merge sha and the date. What it cannot give is who approved — every PR
    comes back with an empty review list and is judged as "merged, review
    status unknown". The caller must say so in what it prints.
    """
    out = []
    for c in commits:
        m = MERGE_SUBJECT.search(c["subject"])
        if not m:
            continue
        number = int(m.group(1) or m.group(2))
        title = re.sub(r"\s*\(#\d+\)\s*$", "", c["subject"])
        out.append({"number": number, "title": title, "mergedAt": c["date"],
                    "reviewDecision": None, "author": {"login": "?"},
                    "mergeCommit": {"oid": c["sha"]},
                    "reviews": {"nodes": []},
                    "files": {"nodes": [{"path": p} for p in c["files"]]},
                    "closingIssuesReferences": {"nodes": []}})
    return out


def later_commits(clone, since, merges="exclude"):
    """Every commit after `since` with subject, body, files — one git call.

    `merges="exclude"` is right for the commits that might undo a PR: a merge
    commit is bookkeeping, not a change. It is wrong for reconstructing the PR
    list, where "Merge pull request #N" *is* the PR — with it excluded, one
    repository showed 101 merged PRs where its log holds 255. Callers say which
    they want.
    """
    # Explicit delimiters, because a body can span lines: splitting on the
    # first newline put the second body line into the file list and lost it
    # from the text the revert markers are searched in.
    rec, unit, end = "\x1e", "\x1f", "\x1d"
    flag = {"exclude": ["--no-merges"], "only": ["--merges"], "all": []}[merges]
    raw = git(clone, "log", f"--since={since}", *flag, "--name-only",
              f"--format={rec}%H{unit}%cI{unit}%s{unit}%b{end}")
    commits = []
    for block in raw.split(rec):
        if not block.strip():
            continue
        meta, _, files = block.partition(end)
        parts = meta.split(unit, 3)
        if len(parts) < 3:
            continue
        sha, date, subject = parts[:3]
        body = parts[3] if len(parts) == 4 else ""
        commits.append({"sha": sha, "date": date, "subject": subject.strip(),
                        "body": body.strip(), "files": set(files.split())})
    return commits


def within(a_iso, b_iso, days):
    """True if b is after a (or in the same second) and no more than `days` later.

    Strictly-after (`0 <`) rejected a revert committed in the same second as its
    target, which is how the positive-path test failed and how the detector
    reported zero on three repositories without ever having fired. A revert
    cannot precede what it reverts, so same-instant is legitimately "after".
    """
    a = dt.datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
    b = dt.datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
    return dt.timedelta(0) <= (b - a) <= dt.timedelta(days=days)


FIXISH = re.compile(r"\b(fix|regression|broke|broken|revert|hotfix|bug)\b", re.I)


def match(pr, commits, window_days):
    """Findings for one approved PR against the commits that came after it.

    Pulled out of main so it can be tested against a repository where a revert
    is known to exist — on three real repositories the detector returned zero,
    and zero from an instrument that has never been seen to fire is not a result.
    """
    merge_sha = (pr.get("mergeCommit") or {}).get("oid")
    title = pr["title"].strip()
    files = {f["path"] for f in pr["files"]["nodes"] if f["path"].endswith(".py")
             and not f["path"].startswith("tests/")}
    issues = {str(i["number"]) for i in pr["closingIssuesReferences"]["nodes"]}
    refs = {f"#{pr['number']}"} | {f"#{n}" for n in issues}
    found = []
    for c in commits:
        # A squash-merged PR lands as one commit whose date is mergedAt, whose
        # files are the PR's files, whose subject is the PR's title and carries
        # "(#N)". Once same-instant counted as "after", every such PR became a
        # hotfix of itself: tenacity showed 11 hotfixes among 37 approved PRs,
        # all of them the PR's own merge commit. The thing being judged cannot
        # be its own evidence.
        if merge_sha and c["sha"] == merge_sha:
            continue
        if not within(pr["mergedAt"], c["date"], window_days):
            continue
        text = c["subject"] + "\n" + c["body"]
        if (merge_sha and merge_sha in text) or \
           (c["subject"].startswith("Revert") and title[:40] in text):
            found.append({"pr": pr["number"], "kind": "reverted", "sha": c["sha"][:8],
                          "subject": c["subject"][:70], "merged": pr["mergedAt"][:10]})
            continue
        if files and (files & c["files"]) and FIXISH.search(c["subject"]) \
           and any(r in text for r in refs):
            found.append({"pr": pr["number"], "kind": "hotfixed", "sha": c["sha"][:8],
                          "subject": c["subject"][:70], "merged": pr["mergedAt"][:10]})
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="owner/name on GitHub")
    ap.add_argument("clone", help="local clone with full history")
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--page-size", type=int, default=100,
                    help="PRs per API page; lower it when the server abandons "
                         "the request on a large repository (default 100)")
    ap.add_argument("--json")
    ap.add_argument("--no-github", action="store_true",
                    help="reconstruct merged PRs from the clone; review status will be unknown")
    args = ap.parse_args()
    owner, name = args.repo.split("/")

    commits = later_commits(args.clone, args.since)            # candidates: no merges
    if args.no_github:
        prs = prs_from_clone(later_commits(args.clone, args.since, merges="all"))
        # Without the review record every merged PR is a candidate. This is a
        # weaker question — "did merged work get undone", not "did approved
        # work" — and the output says which one it answered.
        approved, self_merged = prs, []
    else:
        prs = merged_prs(owner, name, args.since, page_size=args.page_size)
        approved = [p for p in prs if any(r["state"] == "APPROVED" for r in p["reviews"]["nodes"])]
        self_merged = [p for p in prs if not p["reviews"]["nodes"]]

    findings = []
    for pr in approved:
        findings += match(pr, commits, args.window_days)

    reverted = {f["pr"] for f in findings if f["kind"] == "reverted"}
    hotfixed = {f["pr"] for f in findings if f["kind"] == "hotfixed"} - reverted

    print(f"{args.repo}  since {args.since}  window {args.window_days}d"
          + ("   [clone only — review status unknown]" if args.no_github else "") + "\n")
    print(f"merged PRs                 {len(prs)}")
    if args.no_github:
        print(f"  review status            unknown for all {len(prs)} (no API access; "
              f"judged as merged, not as approved)")
        label = "merged"
    else:
        print(f"  with an APPROVED review  {len(approved)}")
        print(f"  merged with no review    {len(self_merged)}   (maintainer self-merge; not judged)")
        label = "approved"
    print()
    if not approved:
        print(f"no {label} PRs in range — nothing to measure here")
        return 0
    print(f"of {len(approved)} {label} PRs:")
    print(f"  reverted within window   {len(reverted):>3}   ({100*len(reverted)/len(approved):.1f}%)")
    print(f"  hotfixed within window   {len(hotfixed):>3}   ({100*len(hotfixed)/len(approved):.1f}%)  "
          f"weaker signal, kept separate")
    if findings:
        print("\nevidence — read these, do not take them:")
        # One merge can be undone by many commits: a monorepo reverts a
        # dependency bump once per package, and langchain printed the same PR
        # eighteen times, which reads as eighteen bad merges. The counts above
        # were always of PRs, not commits; the list now says the same thing.
        by_pr = {}
        for f in sorted(findings, key=lambda f: (f["pr"], f["sha"])):
            by_pr.setdefault((f["pr"], f["kind"]), []).append(f)
        for (pr, kind), group in sorted(by_pr.items()):
            first, extra = group[0], len(group) - 1
            more = f"  (+{extra} more commit{'s' if extra > 1 else ''})" if extra else ""
            print(f"  PR #{pr:<5} merged {first['merged']}  {kind:<8} {first['sha']}  "
                  f"{first['subject']}{more}")
    print(f"\nn={len(approved)} approved PRs; a revert is a proxy for a wrong merge, "
          f"not proof of one.")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"repo": args.repo, "since": args.since, "merged": len(prs),
                       "approved": len(approved), "self_merged": len(self_merged),
                       "reverted": sorted(reverted), "hotfixed": sorted(hotfixed),
                       "findings": findings}, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
