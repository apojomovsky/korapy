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


def _is_tracker_repo_const(node):
    """Whether `node` is the `"tracker.repo"` string constant."""
    return isinstance(node, ast.Constant) and node.value == "tracker.repo"


def _is_tracker_repo_read(value):
    """Whether an `or` value re-reads `tracker.repo` outside the helper.

    Subscripts (`resolved["tracker.repo"]`) and calls carrying the key
    (`resolved.get("tracker.repo")`, a `Call` rendering as
    `('tracker.repo')` rather than `["tracker.repo"]`) alike: either
    spelling in an `or` fallback re-introduces a second resolver.
    """
    if isinstance(value, ast.Subscript):
        return ast.unparse(value.slice).strip("\"'") == "tracker.repo" or (
            "tracker.repo" in ast.unparse(value)
        )
    if isinstance(value, ast.Call):
        return any(_is_tracker_repo_const(arg) for arg in value.args) or any(
            _is_tracker_repo_const(keyword.value) for keyword in value.keywords
        )
    return False


def _is_tracker_repo_get_with_default(node):
    """Whether `node` is `.get("tracker.repo", fallback)`.

    The default-argument fallback needs no `or`: the fallback default
    already re-introduces a second resolver, so it is flagged wherever
    it appears. The key may arrive positionally or as a keyword (the
    way `_is_tracker_repo_read` already treats `or` values): only a
    call carrying the key beside a fallback counts, so a bare keyword
    read with no default still passes.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get"):
        return False
    if node.args:
        if not _is_tracker_repo_const(node.args[0]):
            return False
        return len(node.args) > 1 or bool(node.keywords)
    keyed = [keyword for keyword in node.keywords if _is_tracker_repo_const(keyword.value)]
    return bool(keyed) and len(node.keywords) > len(keyed)


def _or_offenders(tree, name):
    """`["{name}:{lineno}"]` for every `or` fallback re-reading `tracker.repo`."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        if any(_is_tracker_repo_read(value) for value in node.values):
            offenders.append(f"{name}:{node.lineno}")
            continue
        whole = ast.unparse(node)
        if '["tracker.repo"]' in whole.replace(" ", ""):
            offenders.append(f"{name}:{node.lineno}")
    return offenders


def _default_offenders(tree, name):
    """`["{name}:{lineno}"]` for every `.get("tracker.repo", fallback)`."""
    return [
        f"{name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_tracker_repo_get_with_default(node)
    ]


class ResolverGuard(unittest.TestCase):
    def test_no_tracker_repo_or_fallback_outside_the_helper(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "korapy"
        offenders = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(_or_offenders(tree, path.name))
            offenders.extend(_default_offenders(tree, path.name))
        self.assertEqual(offenders, [])

    def test_or_with_subscript_fallback_is_flagged(self):
        tree = ast.parse('repo = resolved["tracker.repo"] or name\n')
        self.assertEqual(_or_offenders(tree, "snippet"), ["snippet:1"])

    def test_or_with_get_call_fallback_is_flagged(self):
        tree = ast.parse('repo = resolved.get("tracker.repo") or name\n')
        self.assertEqual(_or_offenders(tree, "snippet"), ["snippet:1"])

    def test_get_with_default_argument_is_flagged(self):
        tree = ast.parse('repo = resolved.get("tracker.repo", name)\n')
        self.assertEqual(_default_offenders(tree, "snippet"), ["snippet:1"])
        self.assertEqual(_or_offenders(tree, "snippet"), [])

    def test_get_with_keyword_key_and_default_is_flagged(self):
        tree = ast.parse('repo = resolved.get(key="tracker.repo", default=name)\n')
        self.assertEqual(_default_offenders(tree, "snippet"), ["snippet:1"])
        self.assertEqual(_or_offenders(tree, "snippet"), [])

    def test_get_with_keyword_key_and_no_default_passes(self):
        tree = ast.parse('repo = resolved.get(key="tracker.repo")\n')
        self.assertEqual(_default_offenders(tree, "snippet"), [])
        self.assertEqual(_or_offenders(tree, "snippet"), [])

    def test_plain_reads_without_a_fallback_pass(self):
        tree = ast.parse(
            'explicit = resolved["tracker.repo"]\nmaybe = resolved.get("tracker.repo")\n'
        )
        self.assertEqual(_or_offenders(tree, "snippet"), [])
        self.assertEqual(_default_offenders(tree, "snippet"), [])


if __name__ == "__main__":
    unittest.main()
