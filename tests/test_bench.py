"""Tests for the parts that decide what a result means.

Every case here is a bug this harness actually shipped and then had to be
caught by hand. They are kept as tests because each one produced a clean,
plausible, wrong answer rather than an error — which is the failure this whole
tool exists to measure, and there is no reason to be exempt from it.

    python3 -m unittest discover -s tests
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import judge          # noqa: E402
import mine           # noqa: E402
import setup_task     # noqa: E402


class ChangedLines(unittest.TestCase):
    """The gate reverts the agent's fix; it has to know which lines are the fix.

    The first version mutated the first candidate in the file instead: on a real
    task it broke line 37 to judge a fix made on line 211, so `proven` meant
    "the suite covers some unrelated method".
    """

    def test_reports_only_the_lines_that_differ(self):
        old = "a\nb\nc\nd\n"
        new = "a\nb\nCHANGED\nd\n"
        self.assertEqual(judge.changed_lines(old, new), {3})

    def test_insertions_are_reported_at_their_new_positions(self):
        old = "a\nb\n"
        new = "a\nx\ny\nb\n"
        self.assertEqual(judge.changed_lines(old, new), {2, 3})

    def test_identical_files_report_nothing(self):
        text = "one\ntwo\n"
        self.assertEqual(judge.changed_lines(text, text), set())

    def test_a_far_away_edit_does_not_drag_in_the_top_of_the_file(self):
        old = "\n".join(f"line{i}" for i in range(1, 60)) + "\n"
        new = old.replace("line50", "line50_fixed")
        self.assertEqual(judge.changed_lines(old, new), {50})


class QualifiedTests(unittest.TestCase):
    """Only the tests the agent wrote may be run against the reverted fix.

    Running the whole module the agent edited made `proven` close to automatic:
    a project's own suite notices almost any change anywhere.
    """

    def test_finds_module_level_tests(self):
        src = "def test_one():\n    pass\n\ndef helper():\n    pass\n"
        self.assertEqual(judge.qualified_tests(src), {"test_one"})

    def test_addresses_tests_inside_classes_the_way_pytest_does(self):
        src = "class TestThing:\n    def test_two(self):\n        pass\n"
        self.assertEqual(judge.qualified_tests(src), {"TestThing::test_two"})

    def test_ignores_functions_that_are_not_tests(self):
        src = "def setup_module():\n    pass\n\nasync def test_async():\n    pass\n"
        self.assertEqual(judge.qualified_tests(src), {"test_async"})

    def test_new_tests_are_the_difference_from_the_previous_revision(self):
        before = "def test_old():\n    pass\n"
        after = "def test_old():\n    pass\n\ndef test_new():\n    pass\n"
        fresh = judge.qualified_tests(after) - judge.qualified_tests(before)
        self.assertEqual(fresh, {"test_new"})


class LogParsing(unittest.TestCase):
    """A partial read must not pass for a small repository."""

    def test_reads_every_commit_in_the_block(self):
        raw = (f"{mine.SEP}aaa|2024-01-01|first\n\n1\t2\tsrc/x.py\n"
               f"{mine.SEP}bbb|2024-01-02|second\n\n3\t4\ttests/test_x.py\n")
        commits = list(mine.parse(raw))
        self.assertEqual([c[0] for c in commits], ["aaa", "bbb"])
        self.assertEqual(commits[0][3], [(1, 2, "src/x.py")])

    def test_a_subject_containing_a_pipe_survives(self):
        raw = f"{mine.SEP}ccc|2024-01-03|fix: a|b confusion\n\n1\t0\tsrc/y.py\n"
        sha, date, subject, files = next(mine.parse(raw))
        self.assertEqual(subject, "fix: a|b confusion")

    def test_a_failed_git_log_raises_instead_of_returning_what_it_managed(self):
        # Exit 128 partway through a blob-filtered clone once read as 42 commits
        # where git itself counted 963.
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(SystemExit):
                mine.read_log(empty, None)


class SourceClassification(unittest.TestCase):
    def test_library_code_is_found_whatever_the_layout(self):
        self.assertTrue(mine.is_src("src/pkg/mod.py"))
        self.assertTrue(mine.is_src("pkg/mod.py"))       # flat layout

    def test_tests_and_tooling_are_not_library_code(self):
        for path in ("tests/test_mod.py", "docs/conf.py", "setup.py", "noxfile.py"):
            self.assertFalse(mine.is_src(path), path)

    def test_test_files_are_recognised_in_both_common_shapes(self):
        self.assertTrue(mine.is_test("tests/test_mod.py"))
        self.assertTrue(mine.is_test("pkg/tests/test_mod.py"))
        self.assertFalse(mine.is_test("src/pkg/mod.py"))


class DeclaredGroups(unittest.TestCase):
    """Environments come from the project's declaration, not from guesses.

    Reading package names out of import errors and installing the newest of each
    produced an incompatible pair, and the harness then called the project's
    suite red.
    """

    def write(self, text):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "pyproject.toml").write_text(text)
        return d

    def test_reads_a_test_group(self):
        d = self.write('[dependency-groups]\ntest = ["pytest", "trio>=0.27"]\n')
        self.assertEqual(list(setup_task.declared_groups(d)),
                         [(["pytest", "trio>=0.27"], "test")])

    def test_follows_an_include_group(self):
        d = self.write('[dependency-groups]\nbase = ["anyio"]\n'
                       'test = [{include-group = "base"}, "pytest"]\n')
        specs, name = next(setup_task.declared_groups(d))
        self.assertEqual(name, "test")
        self.assertEqual(sorted(specs), ["anyio", "pytest"])

    def test_a_cycle_does_not_hang(self):
        d = self.write('[dependency-groups]\n'
                       'a = [{include-group = "b"}, "one"]\n'
                       'b = [{include-group = "a"}, "two"]\n'
                       'test = [{include-group = "a"}]\n')
        specs, _ = next(setup_task.declared_groups(d))
        self.assertEqual(sorted(specs), ["one", "two"])

    def test_no_groups_yields_nothing(self):
        d = self.write('[project]\nname = "x"\n')
        self.assertEqual(list(setup_task.declared_groups(d)), [])

    def test_a_missing_pyproject_is_not_an_error(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.assertEqual(list(setup_task.declared_groups(d)), [])

    def test_unparseable_toml_is_not_an_error(self):
        d = self.write("this is not toml [[[")
        self.assertEqual(list(setup_task.declared_groups(d)), [])


class Timeouts(unittest.TestCase):
    """A hang is a result, not a crashed harness."""

    def test_a_command_that_overruns_is_reported_not_raised(self):
        done = setup_task.run([sys.executable, "-c", "import time; time.sleep(5)"],
                              timeout=1)
        self.assertEqual(done.returncode, setup_task.TIMED_OUT)
        self.assertIn("timed out", done.stderr)

    def test_a_normal_command_still_returns_its_own_code(self):
        done = setup_task.run([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(done.returncode, 3)


if __name__ == "__main__":
    unittest.main()
