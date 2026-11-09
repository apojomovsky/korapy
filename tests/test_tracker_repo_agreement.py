"""One tracker-repo resolver: the three readers agree.

`docs/16-tracker-repo-resolver.md`, option 1: `syncli.tracker_repo`
is the one helper answering which tracker repo a `[[repo]]` entry
feeds. `boarddoctor.sites_from_settings` and `doctor._repo_site`
delegate to it instead of guessing, so all three agree whether
`tracker.repo` is set or not. Fakes only: no network, no real clones.
"""

import ast
import os
import pathlib
import tempfile
import unittest

from korapy import boarddoctor, doctor, settings, syncli
from tests.test_init import FakeGit
from tests.test_tracker import FakeTracker

URL = "https://github.com/acme/demo.git"
REPO = "acme/demo"

BOARD_TOML = """\
[[repo]]
name = "demo"
clone.path = "clone"
[repo.tracker]
variant = "board"
[repo.board]
project_owner = "example"
project_number = 3
"""


def write_ops(root, body=BOARD_TOML):
    os.makedirs(os.path.join(root, "clone"), exist_ok=True)
    with open(os.path.join(root, "korapy.toml"), "w", encoding="utf-8") as handle:
        handle.write(body)
    return os.path.join(root, "clone")


def config_env():
    return {"KORAPY_CONFIG": tempfile.mkdtemp(prefix="korapy-tracker-repo-")}


def fake_tracker():
    return FakeTracker(labels=[(REPO, ["Priority"])])


class Agreement(unittest.TestCase):
    def test_all_three_agree_without_an_explicit_repo(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-tracker-agree-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        clone = write_ops(root)
        env = config_env()

        resolved = settings.load(root, repo="demo", env=env)
        repo, problem = syncli.tracker_repo(
            root, "demo", resolved, FakeGit(root=clone, origin_url=URL)
        )
        self.assertIsNone(problem)
        self.assertEqual(repo, REPO)

        sites, error = boarddoctor.sites_from_settings(
            root, env=env, git=FakeGit(root=clone, origin_url=URL)
        )
        self.assertIsNone(error)
        self.assertEqual([site.repo for site in sites], [REPO])

        site = doctor._repo_site(
            root,
            "demo",
            git=FakeGit(root=clone, origin_url=URL),
            tracker_factory={REPO: fake_tracker()}.__getitem__,
        )
        self.assertEqual(site.tracker_repo, REPO)

    def test_explicit_repo_agrees_and_makes_no_git_call(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-tracker-explicit-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        clone = write_ops(
            root,
            BOARD_TOML.replace(
                '[repo.tracker]\nvariant = "board"',
                '[repo.tracker]\nrepo = "acme/other"\nvariant = "board"',
            ),
        )
        env = config_env()

        def no_git(args, cwd):
            self.fail(f"no git call for an explicit tracker.repo: {args}")

        resolved = settings.load(root, repo="demo", env=env)
        repo, problem = syncli.tracker_repo(root, "demo", resolved, no_git)
        self.assertIsNone(problem)
        self.assertEqual(repo, "acme/other")

        sites, error = boarddoctor.sites_from_settings(root, env=env, git=no_git)
        self.assertIsNone(error)
        self.assertEqual([site.repo for site in sites], ["acme/other"])

        git = FakeGit(root=clone, origin_url=URL)
        site = doctor._repo_site(
            root,
            "demo",
            git=git,
            tracker_factory={"acme/other": fake_tracker()}.__getitem__,
        )
        self.assertEqual(site.tracker_repo, "acme/other")
        get_url = [call for call in git.calls if call[0] == ["remote", "get-url", "origin"]]
        self.assertEqual(len(get_url), 1)

    def test_single_read_origin_parameter_agrees_without_a_second_spawn(self):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-tracker-origin-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        write_ops(root)
        env = config_env()
        resolved = settings.load(root, repo="demo", env=env)

        def no_git(args, cwd):
            self.fail(f"origin= must not spawn git: {args}")

        repo, problem = syncli.tracker_repo(root, "demo", resolved, no_git, origin=URL)
        self.assertIsNone(problem)
        self.assertEqual(repo, REPO)


class Unresolvable(unittest.TestCase):
    def check(self, body, git):
        tmp = tempfile.TemporaryDirectory(prefix="korapy-tracker-bad-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        write_ops(root, body)
        env = config_env()
        resolved = settings.load(root, repo="demo", env=env)
        repo, problem = syncli.tracker_repo(root, "demo", resolved, git)
        self.assertIsNone(repo)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("demo", problem)
        return root, env, problem

    def test_no_clone_path(self):
        body = BOARD_TOML.replace('clone.path = "clone"\n', "")

        def no_git(args, cwd):
            self.fail(f"no git call without a clone.path: {args}")

        root, env, problem = self.check(body, no_git)
        sites, error = boarddoctor.sites_from_settings(root, env=env, git=FakeGit(root=root))
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        self.assertIn("demo", error or "")
        site = doctor._repo_site(
            root, "demo", git=FakeGit(root=root), tracker_factory={}.__getitem__
        )
        self.assertIsNone(site.tracker_repo)
        self.assertIsNotNone(site.cause)
        self.assertIn("demo", site.cause or "")
        with self.assertRaises(settings.SettingsError):
            syncli.root_short_map(root, FakeGit(root=root))

    def test_git_failure(self):
        root, env, problem = self.check(BOARD_TOML, FakeGit(root="unset"))
        self.assertIn("origin", problem)
        sites, error = boarddoctor.sites_from_settings(root, env=env, git=FakeGit(root=root))
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        self.assertIn("demo", error or "")
        site = doctor._repo_site(
            root, "demo", git=FakeGit(root=root), tracker_factory={}.__getitem__
        )
        self.assertIsNone(site.tracker_repo)
        self.assertIsNotNone(site.cause)

    def test_non_github_origin(self):
        clone = None

        def make_git(root):
            return FakeGit(root=root, origin_url="/srv/repos/demo")

        tmp = tempfile.TemporaryDirectory(prefix="korapy-tracker-nongh-")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        clone = write_ops(root)
        env = config_env()
        resolved = settings.load(root, repo="demo", env=env)
        repo, problem = syncli.tracker_repo(root, "demo", resolved, make_git(root))
        self.assertIsNone(repo)
        self.assertIn("demo", problem or "")
        sites, error = boarddoctor.sites_from_settings(root, env=env, git=make_git(root))
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        site = doctor._repo_site(root, "demo", git=make_git(root), tracker_factory={}.__getitem__)
        self.assertEqual(site.origin, "/srv/repos/demo")
        self.assertIsNone(site.tracker_repo)
        self.assertIsNotNone(site.cause)
        self.assertIn("demo", site.cause or "")

    def test_malformed_explicit_repo(self):
        body = BOARD_TOML.replace(
            '[repo.tracker]\nvariant = "board"',
            '[repo.tracker]\nrepo = "not-a-repo"\nvariant = "board"',
        )

        def no_git(args, cwd):
            self.fail(f"a malformed explicit value never reaches git: {args}")

        root, env, problem = self.check(body, no_git)
        self.assertIn("owner/name", problem)
        sites, error = boarddoctor.sites_from_settings(root, env=env, git=no_git)
        self.assertEqual(sites, [])
        self.assertIsNotNone(error)
        self.assertIn("owner/name", error or "")
        site = doctor._repo_site(
            root, "demo", git=FakeGit(root=root), tracker_factory={}.__getitem__
        )
        self.assertIsNone(site.tracker_repo)
        self.assertIsNotNone(site.cause)
        self.assertIn("owner/name", site.cause or "")


class ResolverGuard(unittest.TestCase):
    def test_no_tracker_repo_or_fallback_outside_the_helper(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "korapy"
        offenders = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
                    continue
                for value in node.values:
                    if (
                        isinstance(value, ast.Subscript)
                        and ast.unparse(value.slice).strip("\"'") == "tracker.repo"
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
                    elif isinstance(value, ast.Subscript) and "tracker.repo" in ast.unparse(value):
                        offenders.append(f"{path.name}:{node.lineno}")
                whole = ast.unparse(node)
                if '["tracker.repo"]' in whole.replace(" ", ""):
                    if f"{path.name}:{node.lineno}" not in offenders:
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
