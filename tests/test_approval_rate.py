"""The revert detector must be shown to catch a revert before its zeros mean anything.

On three real repositories the detector reported zero reverts among 55 approved
PRs. That is consistent with good reviewing and with a detector that cannot see.
The one real revert in range was a direct commit with no PR, so it proved only
the negative path. This builds a repository where the positive case exists and
requires it to be found.

    python3 -m unittest tests.test_approval_rate
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import approval_rate  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class RevertDetection(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (pathlib.Path(self.repo) / "mod.py").write_text("x = 1\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-q", "-m", "base")
        # the "merged PR"
        (pathlib.Path(self.repo) / "mod.py").write_text("x = 2\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-q", "-m", "Use two instead of one")
        self.merge_sha = git(self.repo, "rev-parse", "HEAD")
        self.merged_at = git(self.repo, "log", "-1", "--format=%cI")

    def pr(self, **over):
        base = {"number": 7, "title": "Use two instead of one", "mergedAt": self.merged_at,
                "reviewDecision": "APPROVED", "author": {"login": "a"},
                "mergeCommit": {"oid": self.merge_sha},
                "reviews": {"nodes": [{"state": "APPROVED", "author": {"login": "r"}}]},
                "files": {"nodes": [{"path": "mod.py"}]},
                "closingIssuesReferences": {"nodes": []}}
        base.update(over)
        return base

    def findings_for(self, pr):
        commits = approval_rate.later_commits(self.repo, "2000-01-01")
        return approval_rate.match(pr, commits, window_days=30)

    def test_a_git_revert_of_the_merge_commit_is_found(self):
        git(self.repo, "revert", "--no-edit", self.merge_sha)
        kinds = [f["kind"] for f in self.findings_for(self.pr())]
        self.assertEqual(kinds, ["reverted"])

    def test_a_revert_by_title_is_found_when_the_sha_is_absent(self):
        (pathlib.Path(self.repo) / "mod.py").write_text("x = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", 'Revert "Use two instead of one"')
        kinds = [f["kind"] for f in self.findings_for(self.pr())]
        self.assertEqual(kinds, ["reverted"])

    def test_a_fix_touching_the_same_file_and_naming_the_pr_is_a_hotfix(self):
        (pathlib.Path(self.repo) / "mod.py").write_text("x = 3\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Fix regression from #7")
        kinds = [f["kind"] for f in self.findings_for(self.pr())]
        self.assertEqual(kinds, ["hotfixed"])

    def test_a_prs_own_squash_merge_commit_is_not_a_hotfix_of_itself(self):
        # Squash-merge: one commit, dated mergedAt, touching the PR's files, with
        # the PR's own fix-ish title and "(#7)". On tenacity this read as 11
        # hotfixes among 37 approved PRs — every one the PR judging itself.
        git(self.repo, "commit", "--amend", "-q", "-m", "Fix the off-by-one (#7)")
        sha = git(self.repo, "rev-parse", "HEAD")
        merged_at = git(self.repo, "log", "-1", "--format=%cI")
        pr = self.pr(title="Fix the off-by-one", mergedAt=merged_at,
                     mergeCommit={"oid": sha})
        commits = approval_rate.later_commits(self.repo, "2000-01-01")
        self.assertEqual(approval_rate.match(pr, commits, window_days=30), [])

    def test_a_follow_up_without_fix_language_is_not_a_hotfix(self):
        # Same file, names the PR, but says nothing about fixing anything: a docs
        # touch-up that mentions #7 must not count. The mutation check found no
        # test reached this branch — dropping the fix-language requirement left
        # the suite green.
        (pathlib.Path(self.repo) / "mod.py").write_text("x = 2  # see #7\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Add a comment pointing at #7")
        self.assertEqual(self.findings_for(self.pr()), [])

    def test_an_unrelated_later_commit_is_not_a_finding(self):
        (pathlib.Path(self.repo) / "other.py").write_text("y = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Fix typo in docs")
        self.assertEqual(self.findings_for(self.pr()), [])

    def test_the_window_is_a_real_boundary(self):
        # A same-second revert is legitimately "after" its target, so a zero-day
        # window cannot be the test. Date the merge 40 days back instead: a
        # 30-day window must exclude the revert, a 90-day window must include it.
        # `match` reads mergedAt from the PR record, not from git, so the
        # boundary is tested by handing it a 40-day-old merge date directly.
        # (Backdating via `git commit --amend --date` sets the author date; the
        # detector reads committer dates, so that route tested nothing.)
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        git(self.repo, "revert", "--no-edit", self.merge_sha)
        pr = self.pr(mergedAt=(now - dt.timedelta(days=40)).isoformat())
        commits = approval_rate.later_commits(self.repo, "2000-01-01")
        self.assertEqual(approval_rate.match(pr, commits, window_days=30), [])
        self.assertEqual([f["kind"] for f in approval_rate.match(pr, commits, window_days=90)],
                         ["reverted"])


class MergeCommitsInHistory(unittest.TestCase):
    """PR reconstruction must see real merge commits; candidate scanning must not.

    With --no-merges applied to both, one repository reported 101 merged PRs
    where its log held 255 — the other 154 were "Merge pull request #N" commits.
    """

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (pathlib.Path(self.repo) / "a.py").write_text("a = 1\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "feature")
        (pathlib.Path(self.repo) / "b.py").write_text("b = 1\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-q", "-m", "add b")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "--no-ff", "-q", "-m", "Merge pull request #9 from t/feature", "feature")

    def test_all_includes_the_merge_commit_and_exclude_drops_it(self):
        everything = approval_rate.later_commits(self.repo, "2000-01-01", merges="all")
        no_merges = approval_rate.later_commits(self.repo, "2000-01-01")
        self.assertTrue(any(c["subject"].startswith("Merge pull request #9") for c in everything))
        self.assertFalse(any(c["subject"].startswith("Merge pull request") for c in no_merges))

    def test_the_merged_pr_is_reconstructed_from_the_merge_commit(self):
        prs = approval_rate.prs_from_clone(
            approval_rate.later_commits(self.repo, "2000-01-01", merges="all"))
        self.assertEqual([p["number"] for p in prs], [9])


class CloneOnlyPRs(unittest.TestCase):
    """When GitHub is unreachable, merged PRs are read off the commit subjects."""

    def commit(self, subject, files=("mod.py",)):
        return {"sha": "abc", "date": "2026-01-01T00:00:00+00:00", "subject": subject,
                "body": "", "files": set(files)}

    def test_a_squash_merge_subject_yields_the_pr_number_and_title(self):
        prs = approval_rate.prs_from_clone([self.commit("Fix the thing (#42)")])
        self.assertEqual([(p["number"], p["title"]) for p in prs], [(42, "Fix the thing")])

    def test_a_merge_commit_subject_yields_the_pr_number(self):
        prs = approval_rate.prs_from_clone([self.commit("Merge pull request #7 from x/y")])
        self.assertEqual([p["number"] for p in prs], [7])

    def test_a_plain_commit_is_not_a_pr(self):
        self.assertEqual(approval_rate.prs_from_clone([self.commit("Tidy imports")]), [])

    def test_review_status_is_reported_as_unknown_not_approved(self):
        pr = approval_rate.prs_from_clone([self.commit("Fix (#1)")])[0]
        self.assertEqual(pr["reviews"]["nodes"], [])
        self.assertIsNone(pr["reviewDecision"])


if __name__ == "__main__":
    unittest.main()
