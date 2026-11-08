"""Tests for korapy.hygiene: config and validation for [hygiene.<name>] rules.

Design: docs/15-project-hygiene.md, section 9 ticket 1. Config shape,
strict keys, exit 4 refusals, `rules`, `declared_roots`, and the guard
test pinning one fact one place (no second `base` plus `path` join).

Fakes and temp dirs only: every XDG base is redirected at a temp dir,
so no test touches the real state, sessions or cache dirs.
"""

import ast
import os
import pathlib
import tempfile
import unittest

from korapy import hygiene, settings


def tmp_env(tmp):
    """Every directory base redirected under `tmp`, as an env mapping."""
    return {
        "KORAPY_CONFIG": os.path.join(tmp, "config"),
        "KORAPY_STATE": os.path.join(tmp, "state"),
        "KORAPY_CACHE": os.path.join(tmp, "cache"),
        "KORAPY_SESSIONS": os.path.join(tmp, "sessions"),
        "XDG_CONFIG_HOME": os.path.join(tmp, "xdg-config"),
        "XDG_STATE_HOME": os.path.join(tmp, "xdg-state"),
        "XDG_CACHE_HOME": os.path.join(tmp, "xdg-cache"),
    }


def dir_rule(**over):
    rule = {
        "repo": "acme",
        "kind": "dir",
        "base": "cache",
        "path": "acme/scratch",
        "selector": "age",
        "days": 7,
    }
    rule.update(over)
    return rule


def image_rule(**over):
    rule = {
        "repo": "acme",
        "kind": "image",
        "prefix": "acme-dev",
        "selector": "unreferenced",
    }
    rule.update(over)
    return rule


def session_rule(**over):
    rule = {
        "repo": "acme",
        "kind": "session",
        "base": "sessions",
        "path": "acme-archive",
        "retention_days": 30,
    }
    rule.update(over)
    return rule


class CheckTableCase(unittest.TestCase):
    def test_no_table_reads_as_no_rules(self):
        self.assertEqual(hygiene.check_table(None, "korapy.toml"), {})
        self.assertEqual(hygiene.check_table({}, "korapy.toml"), {})

    def test_non_table_is_refused(self):
        for bad in (["acme-scratch"], "scratch", 7):
            with self.subTest(value=bad):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table(bad, "korapy.toml")

    def test_non_table_rule_is_refused_naming_it(self):
        with self.assertRaisesRegex(settings.SettingsError, "acme-scratch"):
            hygiene.check_table({"acme-scratch": ["dir"]}, "korapy.toml")

    def test_empty_name_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"": dir_rule()}, "korapy.toml")

    def test_dir_age_parses(self):
        rules = hygiene.check_table({"acme-scratch": dir_rule()}, "korapy.toml")
        rule = rules["acme-scratch"]
        self.assertEqual(rule.name, "acme-scratch")
        self.assertEqual(rule.repo, "acme")
        self.assertEqual(rule.kind, "dir")
        self.assertEqual(rule.base, "cache")
        self.assertEqual(rule.path, "acme/scratch")
        self.assertEqual(rule.selector, "age")
        self.assertEqual(rule.days, 7)
        self.assertIsNone(rule.keep)

    def test_dir_count_parses(self):
        rules = hygiene.check_table(
            {"acme-build": dir_rule(path="acme/build", selector="count", keep=5, days=None)},
            "korapy.toml",
        )
        rule = rules["acme-build"]
        self.assertEqual((rule.selector, rule.keep), ("count", 5))
        self.assertIsNone(rule.days)

    def test_dir_size_parses(self):
        rules = hygiene.check_table(
            {
                "acme-cache": dir_rule(
                    path="acme/build-cache",
                    selector="size",
                    max_bytes=10737418240,
                    days=None,
                )
            },
            "korapy.toml",
        )
        self.assertEqual(rules["acme-cache"].max_bytes, 10737418240)

    def test_image_parses_with_and_without_floor(self):
        rules = hygiene.check_table(
            {
                "plain": image_rule(),
                "floored": image_rule(prefix="acme-other", older_than_days=3),
            },
            "korapy.toml",
        )
        self.assertIsNone(rules["plain"].older_than_days)
        self.assertEqual(rules["floored"].older_than_days, 3)
        self.assertIsNone(rules["plain"].base)

    def test_session_parses(self):
        rules = hygiene.check_table({"acme-old": session_rule()}, "korapy.toml")
        rule = rules["acme-old"]
        self.assertEqual(
            (rule.base, rule.path, rule.retention_days), ("sessions", "acme-archive", 30)
        )
        self.assertIsNone(rule.selector)

    def test_unknown_key_is_refused_naming_file_and_key(self):
        with self.assertRaisesRegex(settings.SettingsError, "bogus"):
            hygiene.check_table({"acme-scratch": dir_rule(bogus=1)}, "korapy.toml")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(kind="bucket")}, "korapy.toml")

    def test_missing_repo_and_kind_are_refused(self):
        for key in ("repo", "kind"):
            raw = dir_rule()
            del raw[key]
            with self.subTest(missing=key):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table({"x": raw}, "korapy.toml")

    def test_bad_repo_names_are_refused(self):
        for bad in ("", "a/b", "..", ".git", "acme git", 7, None):
            with self.subTest(repo=bad):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table({"x": dir_rule(repo=bad)}, "korapy.toml")

    def test_unknown_base_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(base="scratch")}, "korapy.toml")

    def test_empty_path_is_refused(self):
        for bad in ("", 7, None):
            with self.subTest(path=bad):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table({"x": dir_rule(path=bad)}, "korapy.toml")

    def test_bad_dir_selectors_are_refused(self):
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(selector="newest")}, "korapy.toml")
        raw = dir_rule()
        del raw["selector"]
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": raw}, "korapy.toml")

    def test_dir_selector_needs_its_own_key_and_no_other(self):
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(selector="count")}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(selector="count", keep=5)}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": dir_rule(selector="size", max_bytes=10)}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": session_rule(selector="age")}, "korapy.toml")
        with self.assertRaises(settings.SettingsError):
            hygiene.check_table({"x": image_rule(base="cache", path="acme/x")}, "korapy.toml")

    def test_bad_numbers_are_refused(self):
        cases = [
            (dir_rule(days=0), "days zero"),
            (dir_rule(days=-1), "days negative"),
            (dir_rule(days=True), "days bool"),
            (dir_rule(days="7"), "days string"),
            (dir_rule(selector="count", keep=0, days=None), "keep zero"),
            (dir_rule(selector="size", max_bytes=0, days=None), "budget zero"),
            (session_rule(retention_days=0), "retention zero"),
            (image_rule(older_than_days=-1), "floor negative"),
            (image_rule(older_than_days=True), "floor bool"),
        ]
        for raw, name in cases:
            with self.subTest(name=name):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table({"x": raw}, "korapy.toml")

    def test_integral_float_numbers_read_as_ints(self):
        rules = hygiene.check_table({"x": dir_rule(days=7.0)}, "korapy.toml")
        self.assertEqual(rules["x"].days, 7)
        rules = hygiene.check_table({"x": image_rule(older_than_days=3.0)}, "korapy.toml")
        self.assertEqual(rules["x"].older_than_days, 3)

    def test_zero_floor_is_allowed(self):
        rules = hygiene.check_table({"x": image_rule(older_than_days=0)}, "korapy.toml")
        self.assertEqual(rules["x"].older_than_days, 0)

    def test_bad_image_shapes_are_refused(self):
        for raw in (
            image_rule(prefix=""),
            image_rule(prefix=7),
            image_rule(prefix="acme-"),
            image_rule(selector="age"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(settings.SettingsError):
                    hygiene.check_table({"x": raw}, "korapy.toml")

    def test_prefix_must_start_with_repo_plus_separator(self):
        for prefix in ("acme-dev", "acme/dev", "acme-a-b"):
            with self.subTest(prefix=prefix):
                rules = hygiene.check_table({"x": image_rule(prefix=prefix)}, "korapy.toml")
                self.assertEqual(rules["x"].prefix, prefix)
        for prefix in ("other-dev", "acme", "acme2-dev", "xacme-dev", "dev"):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(settings.SettingsError, "acme"):
                    hygiene.check_table({"x": image_rule(prefix=prefix)}, "korapy.toml")

    def test_overlapping_prefixes_are_refused(self):
        with self.assertRaisesRegex(settings.SettingsError, "overlap"):
            hygiene.check_table(
                {
                    "one": image_rule(prefix="acme-dev"),
                    "two": image_rule(prefix="acme-dev2"),
                },
                "korapy.toml",
            )
        with self.assertRaisesRegex(settings.SettingsError, "overlap"):
            hygiene.check_table(
                {
                    "one": image_rule(prefix="acme-dev"),
                    "two": image_rule(prefix="acme-dev"),
                },
                "korapy.toml",
            )

    def test_disjoint_prefixes_are_allowed(self):
        rules = hygiene.check_table(
            {
                "one": image_rule(prefix="acme-a"),
                "two": image_rule(prefix="acme-b"),
            },
            "korapy.toml",
        )
        self.assertEqual(set(rules), {"one", "two"})


class SettingsCarryCase(unittest.TestCase):
    def test_project_table_is_carried_raw(self):
        raw = {"acme-scratch": dir_rule()}
        resolved = settings.resolve(project={"hygiene": raw}, env={})
        self.assertEqual(resolved.hygiene, raw)

    def test_absent_table_carries_empty(self):
        resolved = settings.resolve(project={}, env={})
        self.assertEqual(resolved.hygiene, {})

    def test_non_table_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            settings.resolve(project={"hygiene": ["acme-scratch"]}, env={})

    def test_hygiene_tables_do_not_read_as_unknown_settings(self):
        resolved = settings.resolve(project={"hygiene": {"acme-scratch": dir_rule()}}, env={})
        self.assertEqual(set(resolved.hygiene), {"acme-scratch"})


class RulesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-hygiene-")
        self.addCleanup(self.tmp.cleanup)
        self.ops = os.path.join(self.tmp.name, "ops")
        os.makedirs(self.ops)

    def write_project(self, text):
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_root_reads_as_no_rules(self):
        self.assertEqual(hygiene.rules(None), {})

    def test_missing_file_reads_as_no_rules(self):
        self.assertEqual(hygiene.rules(self.ops), {})

    def test_valid_file_parses(self):
        self.write_project(
            "[hygiene.acme-scratch]\n"
            'repo = "acme"\nkind = "dir"\nbase = "cache"\n'
            'path = "acme/scratch"\nselector = "age"\ndays = 7\n'
        )
        rules = hygiene.rules(self.ops)
        self.assertEqual(rules["acme-scratch"].days, 7)

    def test_one_bad_rule_refuses_the_whole_load(self):
        self.write_project(
            "[hygiene.acme-good]\n"
            'repo = "acme"\nkind = "dir"\nbase = "cache"\n'
            'path = "acme/good"\nselector = "age"\ndays = 7\n'
            "[hygiene.acme-bad]\n"
            'repo = "acme"\nkind = "image"\nprefix = "other-dev"\n'
            'selector = "unreferenced"\n'
        )
        with self.assertRaisesRegex(settings.SettingsError, "acme-bad"):
            hygiene.rules(self.ops)

    def test_broken_file_is_refused(self):
        self.write_project("[hygiene.acme-scratch\n")
        with self.assertRaises(settings.SettingsError):
            hygiene.rules(self.ops)


class DeclaredRootsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="korapy-hygiene-")
        self.addCleanup(self.tmp.cleanup)
        self.env = tmp_env(self.tmp.name)
        self.ops = os.path.join(self.tmp.name, "ops")
        os.makedirs(self.ops)

    def roots(self, raw, **kw):
        rules = hygiene.check_table(raw, "korapy.toml")
        return hygiene.declared_roots(rules, root=self.ops, env=self.env, **kw)

    def test_cache_rule_resolves_under_its_base(self):
        roots = self.roots({"acme-scratch": dir_rule()})
        self.assertEqual(
            roots,
            {
                "acme-scratch": os.path.realpath(
                    os.path.join(self.env["KORAPY_CACHE"], "acme/scratch")
                )
            },
        )

    def test_state_and_sessions_bases_resolve(self):
        roots = self.roots(
            {
                "a": dir_rule(
                    base="state", path="acme/big", selector="size", max_bytes=10, days=None
                ),
                "b": session_rule(),
            }
        )
        self.assertEqual(
            roots["a"],
            os.path.realpath(os.path.join(self.env["KORAPY_STATE"], "acme/big")),
        )
        self.assertEqual(
            roots["b"],
            os.path.realpath(os.path.join(self.env["KORAPY_SESSIONS"], "acme-archive")),
        )

    def test_image_rules_have_no_directory_root(self):
        self.assertEqual(self.roots({"acme-images": image_rule()}), {})

    def test_dot_path_is_refused_as_equal_to_its_base(self):
        with self.assertRaisesRegex(settings.SettingsError, "acme-scratch"):
            self.roots({"acme-scratch": dir_rule(path=".")})

    def test_absolute_and_parent_paths_are_refused(self):
        for bad in ("/tmp/evil", "sub/../../evil", ".."):
            with self.subTest(path=bad):
                with self.assertRaises(settings.SettingsError):
                    self.roots({"acme-scratch": dir_rule(path=bad)})

    def test_symlink_escape_is_refused(self):
        cache = self.env["KORAPY_CACHE"]
        os.makedirs(cache, exist_ok=True)
        os.symlink(self.tmp.name, os.path.join(cache, "evil"))
        with self.assertRaisesRegex(settings.SettingsError, "acme-scratch"):
            self.roots({"acme-scratch": dir_rule(path="evil/sub")})

    def test_root_equal_to_home_is_refused(self):
        home = os.path.join(self.env["KORAPY_STATE"], "home")
        with self.assertRaisesRegex(settings.SettingsError, "home"):
            self.roots({"x": dir_rule(base="state", path="home")}, home=home)

    def test_root_containing_home_is_refused(self):
        home = os.path.join(self.env["KORAPY_STATE"], "home", "nest")
        with self.assertRaisesRegex(settings.SettingsError, "home"):
            self.roots({"x": dir_rule(base="state", path="home")}, home=home)

    def test_root_equal_to_the_heavy_ledger_dir_is_refused(self):
        with self.assertRaisesRegex(settings.SettingsError, "heavy"):
            self.roots({"x": dir_rule(base="state", path="heavy")})

    def test_root_containing_the_host_ledger_file_is_refused(self):
        env = dict(tmp_env(self.tmp.name))
        env["KORAPY_STATE"] = env["XDG_STATE_HOME"]
        rules = hygiene.check_table({"x": dir_rule(base="state", path="korapy")}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "host-resources"):
            hygiene.declared_roots(rules, root=self.ops, env=env)

    def test_reviews_components_are_refused(self):
        for bad in ("reviews", "x/reviews", "x/reviews/y"):
            with self.subTest(path=bad):
                with self.assertRaisesRegex(settings.SettingsError, "reviews"):
                    self.roots({"x": dir_rule(path=bad)})

    def test_worktree_base_resolves_through_the_clone(self):
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "acme"\nclone.path = "main"\n')
        roots = self.roots(
            {"x": dir_rule(base="worktree", path="acme/scratch", selector="age", days=7)},
            worktrees_setting=".worktrees/",
        )
        self.assertEqual(
            roots,
            {"x": os.path.realpath(os.path.join(self.ops, "main", ".worktrees", "acme/scratch"))},
        )

    def test_worktree_base_needs_the_ops_root(self):
        rules = hygiene.check_table(
            {"x": dir_rule(base="worktree", path="acme/scratch")}, "korapy.toml"
        )
        with self.assertRaises(settings.SettingsError):
            hygiene.declared_roots(rules, root=None, env=self.env)

    def test_root_equal_to_a_clone_is_refused(self):
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "acme"\nclone.path = "main"\n')
        env = dict(self.env)
        env["KORAPY_CACHE"] = self.ops
        rules = hygiene.check_table({"x": dir_rule(path="main")}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "clone"):
            hygiene.declared_roots(rules, root=self.ops, env=env)


ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "korapy"

#: The one join of a rule's base and path, as (module stem, function).
#: `declared_roots` owns it; a second join anywhere else is the
#: AGENTS.md one-fact-one-place bug, even when the two agree today.
HYGIENE_JOIN_ALLOW = {("hygiene", "declared_roots")}


def _enclosing_names(tree):
    names = {}

    def visit(node, func="<module>"):
        names[node] = func
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                visit(child, func)

    visit(tree)
    return names


def _is_rule_path_join(node):
    """Whether `node` joins a base dir with a hygiene rule's path.

    Only direct joins count: `os.path.join(base, rule.path)` (or the
    `rule["path"]` spelling) and `base / rule.path`. A join of plain
    names (`os.path.join(base, path)` in policy resolution) is a
    different fact and must not trip this guard; statically invisible
    rebinding (a rule path copied into a plain variable first) stays
    a review-time check, as the docstring there records.
    """
    if isinstance(node, ast.Call):
        if ast.unparse(node.func).replace(" ", "") != "os.path.join":
            return False
        texts = [ast.unparse(arg).replace(" ", "") for arg in node.args]
        return any("base" in text for text in texts) and any(
            "rule" in text and "path" in text for text in texts
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = ast.unparse(node.left).replace(" ", "")
        right = ast.unparse(node.right).replace(" ", "")
        return ("base" in left and "rule" in right and "path" in right) or (
            "base" in right and "rule" in left and "path" in left
        )
    return False


def _rule_path_joins_in(tree):
    enclosing = _enclosing_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.BinOp)) and _is_rule_path_join(node):
            found.append((enclosing[node], node.lineno))
    return found


def _rule_path_joins(path):
    return _rule_path_joins_in(ast.parse(path.read_text(encoding="utf-8")))


class ResolverGuardTest(unittest.TestCase):
    def test_no_second_base_plus_path_join_exists(self):
        problems = []
        for path in sorted(ROOT.glob("*.py")):
            for func, lineno in _rule_path_joins(path):
                if (path.stem, func) not in HYGIENE_JOIN_ALLOW:
                    problems.append(f"{path.name}:{lineno} {func}")
        self.assertEqual(problems, [], f"second base plus path join: {problems}")

    def test_the_allow_list_names_real_uses(self):
        seen = set()
        for path in sorted(ROOT.glob("*.py")):
            for func, _ in _rule_path_joins(path):
                seen.add((path.stem, func))
        for entry in HYGIENE_JOIN_ALLOW:
            self.assertIn(entry, seen, f"stale allow-list entry: {entry}")

    def test_a_second_join_outside_the_resolver_fails(self):
        tree = ast.parse(
            "def somewhere_else(base, rule):\n    return os.path.join(base, rule.path)\n"
        )
        self.assertEqual(_rule_path_joins_in(tree), [("somewhere_else", 2)])

    def test_a_subscript_rule_path_join_fails(self):
        tree = ast.parse(
            'def somewhere_else(base, rule):\n    return os.path.join(base, rule["path"])\n'
        )
        self.assertEqual(_rule_path_joins_in(tree), [("somewhere_else", 2)])

    def test_a_plain_base_plus_path_join_passes(self):
        tree = ast.parse("def resolve_policy(base, path):\n    return os.path.join(base, path)\n")
        self.assertEqual(_rule_path_joins_in(tree), [])

    def test_the_resolver_join_itself_is_found(self):
        tree = ast.parse(
            "def declared_roots(base, rule):\n    return os.path.join(base, rule.path)\n"
        )
        self.assertEqual(_rule_path_joins_in(tree), [("declared_roots", 2)])


if __name__ == "__main__":
    unittest.main()
