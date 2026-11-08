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

    def test_repo_level_hygiene_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            settings.resolve(
                project={"repo": [{"name": "acme", "hygiene": {"x": dir_rule()}}]},
                env={},
            )


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

    def test_root_none_with_dir_rule_is_refused(self):
        rules = hygiene.check_table({"x": dir_rule()}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "x"):
            hygiene.declared_roots(rules, root=None, env=self.env)

    def test_root_none_with_image_only_reads_as_no_roots(self):
        rules = hygiene.check_table({"x": image_rule()}, "korapy.toml")
        self.assertEqual(hygiene.declared_roots(rules, root=None, env=self.env), {})

    def test_abs_clone_resolves_relative_against_the_root(self):
        self.assertEqual(hygiene._abs_clone("/ops", "main"), "/ops/main")
        self.assertEqual(hygiene._abs_clone("/ops", "sub/../main"), os.path.normpath("/ops/main"))

    def test_abs_clone_leaves_absolute_clones_alone(self):
        self.assertEqual(hygiene._abs_clone("/ops", "/abs/main"), "/abs/main")

    def test_root_equal_to_a_clone_is_refused(self):
        with open(os.path.join(self.ops, "korapy.toml"), "w", encoding="utf-8") as handle:
            handle.write('[[repo]]\nname = "acme"\nclone.path = "main"\n')
        env = dict(self.env)
        env["KORAPY_CACHE"] = self.ops
        rules = hygiene.check_table({"x": dir_rule(path="main")}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "clone"):
            hygiene.declared_roots(rules, root=self.ops, env=env)


DAY = 24 * 60 * 60
NOW = 1_700_000_000.0


def plan_rules(raw, roots=None, entries=(), now=NOW):
    """`hygiene.plan` over `raw` tables with a fake `scan` (no filesystem)."""
    rules = hygiene.check_table(raw, "korapy.toml")
    if roots is None:
        roots = {name: f"/cache/{name}" for name in raw}
    by_root = dict(entries)

    def scan(root):
        return list(by_root.get(root, []))

    return hygiene.plan(rules, roots, now=now, scan=scan)


def entry(path, *, mtime, size=100, symlink=False, realpath=None):
    return hygiene.DirEntry(
        path=path, mtime=mtime, size=size, is_symlink=symlink, realpath=realpath
    )


class DirPlanAgeCase(unittest.TestCase):
    def test_old_entry_is_safe_with_proof_naming_rule_and_days(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/run-4", mtime=NOW - 9 * DAY, size=100)
                ]
            },
        )
        (found,) = candidates
        self.assertEqual(
            (found.kind, found.path, found.bytes, found.safe),
            ("hygiene-dir", "/cache/acme/scratch/run-4", 100, True),
        )
        self.assertIn("acme-scratch", found.proof)
        self.assertIn("9d", found.proof)
        self.assertIn("7d", found.proof)

    def test_young_entry_is_kept_with_reason(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/run-9", mtime=NOW - 2 * DAY, size=5000)
                ]
            },
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertEqual(found.proof, "kept: touched 2d ago, rule keeps 7d")

    def test_future_mtime_is_kept_with_zero_age(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/future", mtime=NOW + 2 * DAY, size=100)
                ]
            },
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertEqual(found.proof, "kept: touched 0d ago, rule keeps 7d")

    def test_boundary_age_is_kept(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/run-7", mtime=NOW - 7 * DAY, size=100)
                ]
            },
        )
        (found,) = candidates
        self.assertFalse(found.safe)

    def test_unreadable_mtime_is_kept(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [entry("/cache/acme/scratch/run-x", mtime=None, size=100)]
            },
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertIn("modification time is unreadable", found.proof)

    def test_symlink_is_dropped_with_a_note_and_no_candidate(self):
        candidates, notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/link", mtime=NOW - 9 * DAY, symlink=True)
                ]
            },
        )
        self.assertEqual(candidates, ())
        self.assertEqual(len(notes), 1)
        self.assertIn("/cache/acme/scratch/link", notes[0])
        self.assertIn("is a symlink", notes[0])

    def test_entry_resolving_outside_the_roots_is_dropped_with_a_note(self):
        candidates, notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry(
                        "/cache/acme/scratch/evil",
                        mtime=NOW - 9 * DAY,
                        realpath="/tmp/evil",
                    )
                ]
            },
        )
        self.assertEqual(candidates, ())
        self.assertEqual(len(notes), 1)
        self.assertIn("resolves outside a declared root", notes[0])


class DirPlanCountCase(unittest.TestCase):
    def test_all_but_newest_keep_are_safe(self):
        candidates, _notes = plan_rules(
            {"acme-build": dir_rule(selector="count", keep=2, days=None)},
            roots={"acme-build": "/cache/acme/build"},
            entries={
                "/cache/acme/build": [
                    entry("/cache/acme/build/run-1", mtime=NOW - 3 * DAY),
                    entry("/cache/acme/build/run-2", mtime=NOW - 2 * DAY),
                    entry("/cache/acme/build/run-3", mtime=NOW - 1 * DAY),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/cache/acme/build/run-1"].safe)
        self.assertFalse(by_path["/cache/acme/build/run-2"].safe)
        self.assertFalse(by_path["/cache/acme/build/run-3"].safe)

    def test_mtime_tie_breaks_by_ascending_name(self):
        candidates, _notes = plan_rules(
            {"acme-build": dir_rule(selector="count", keep=1, days=None)},
            roots={"acme-build": "/cache/acme/build"},
            entries={
                "/cache/acme/build": [
                    entry("/cache/acme/build/run-a", mtime=NOW - 1 * DAY),
                    entry("/cache/acme/build/run-b", mtime=NOW - 1 * DAY),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/cache/acme/build/run-a"].safe)
        self.assertFalse(by_path["/cache/acme/build/run-b"].safe)

    def test_survivors_are_named_in_a_note(self):
        _candidates, notes = plan_rules(
            {"acme-build": dir_rule(selector="count", keep=1, days=None)},
            roots={"acme-build": "/cache/acme/build"},
            entries={
                "/cache/acme/build": [
                    entry("/cache/acme/build/run-1", mtime=NOW - 3 * DAY),
                    entry("/cache/acme/build/run-2", mtime=NOW - 1 * DAY),
                ]
            },
        )
        self.assertTrue(
            any("acme-build" in note and "/cache/acme/build/run-2" in note for note in notes),
            f"survivor not named in notes: {notes}",
        )

    def test_unreadable_mtime_is_kept_without_taking_a_survivor_slot(self):
        candidates, _notes = plan_rules(
            {"acme-build": dir_rule(selector="count", keep=1, days=None)},
            roots={"acme-build": "/cache/acme/build"},
            entries={
                "/cache/acme/build": [
                    entry("/cache/acme/build/run-old", mtime=NOW - 3 * DAY),
                    entry("/cache/acme/build/run-new", mtime=NOW - 1 * DAY),
                    entry("/cache/acme/build/run-x", mtime=None),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/cache/acme/build/run-old"].safe)
        self.assertFalse(by_path["/cache/acme/build/run-new"].safe)
        self.assertFalse(by_path["/cache/acme/build/run-x"].safe)
        self.assertIn("modification time is unreadable", by_path["/cache/acme/build/run-x"].proof)


class DirPlanSizeCase(unittest.TestCase):
    def test_oldest_first_until_under_budget(self):
        candidates, _notes = plan_rules(
            {
                "acme-cache": dir_rule(
                    selector="size",
                    max_bytes=100,
                    days=None,
                    path="acme/build-cache",
                )
            },
            roots={"acme-cache": "/cache/acme/build-cache"},
            entries={
                "/cache/acme/build-cache": [
                    entry("/cache/acme/build-cache/e1", mtime=NOW - 3 * DAY, size=60),
                    entry("/cache/acme/build-cache/e2", mtime=NOW - 2 * DAY, size=50),
                    entry("/cache/acme/build-cache/e3", mtime=NOW - 1 * DAY, size=40),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/cache/acme/build-cache/e1"].safe)
        self.assertFalse(by_path["/cache/acme/build-cache/e2"].safe)
        self.assertFalse(by_path["/cache/acme/build-cache/e3"].safe)
        self.assertIn("under budget now", by_path["/cache/acme/build-cache/e2"].proof)

    def test_already_under_budget_keeps_everything(self):
        candidates, _notes = plan_rules(
            {"acme-cache": dir_rule(selector="size", max_bytes=1000, days=None)},
            roots={"acme-cache": "/cache/acme/cache"},
            entries={
                "/cache/acme/cache": [
                    entry("/cache/acme/cache/e1", mtime=NOW - 3 * DAY, size=60),
                    entry("/cache/acme/cache/e2", mtime=NOW - 1 * DAY, size=40),
                ]
            },
        )
        self.assertTrue(candidates)
        self.assertTrue(all(not found.safe for found in candidates))

    def test_size_tie_breaks_by_ascending_name(self):
        candidates, _notes = plan_rules(
            {"acme-cache": dir_rule(selector="size", max_bytes=50, days=None)},
            roots={"acme-cache": "/cache/acme/cache"},
            entries={
                "/cache/acme/cache": [
                    entry("/cache/acme/cache/b", mtime=NOW - 1 * DAY, size=40),
                    entry("/cache/acme/cache/a", mtime=NOW - 1 * DAY, size=40),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/cache/acme/cache/a"].safe)
        self.assertFalse(by_path["/cache/acme/cache/b"].safe)

    def test_unreadable_mtime_counts_but_is_never_safe(self):
        candidates, _notes = plan_rules(
            {"acme-cache": dir_rule(selector="size", max_bytes=100, days=None)},
            roots={"acme-cache": "/cache/acme/cache"},
            entries={
                "/cache/acme/cache": [
                    entry("/cache/acme/cache/u", mtime=None, size=80),
                    entry("/cache/acme/cache/o", mtime=NOW - 9 * DAY, size=60),
                ]
            },
        )
        by_path = {found.path: found for found in candidates}
        self.assertFalse(by_path["/cache/acme/cache/u"].safe)
        self.assertTrue(by_path["/cache/acme/cache/o"].safe)


class DirPlanScopeCase(unittest.TestCase):
    def test_image_rules_are_skipped(self):
        candidates, notes = plan_rules({"acme-images": image_rule()})
        self.assertEqual(candidates, ())
        self.assertEqual(notes, ())

    def test_empty_scan_plans_nothing(self):
        candidates, notes = plan_rules({"acme-scratch": dir_rule()})
        self.assertEqual(candidates, ())
        self.assertEqual(notes, ())

    def test_symlink_and_escape_never_become_candidates(self):
        candidates, _notes = plan_rules(
            {"acme-scratch": dir_rule()},
            roots={"acme-scratch": "/cache/acme/scratch"},
            entries={
                "/cache/acme/scratch": [
                    entry("/cache/acme/scratch/link", mtime=NOW - 9 * DAY, symlink=True),
                    entry(
                        "/cache/acme/scratch/evil",
                        mtime=NOW - 9 * DAY,
                        realpath="/elsewhere/evil",
                    ),
                ]
            },
        )
        self.assertEqual(candidates, ())

    def test_missing_root_is_refused_naming_the_rule(self):
        rules = hygiene.check_table({"acme-scratch": dir_rule()}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "acme-scratch"):
            hygiene.plan(rules, {}, now=NOW, scan=lambda root: [])


def finished_entry(repo, number, ended_ago=100):
    return {
        "id": f"agent-{repo}-{number}",
        "started_at": NOW - 2000,
        "last_seen": NOW - 2000,
        "ended_at": NOW - ended_ago,
        "ticket": {"project": "", "repo": repo, "number": number},
    }


def live_entry(repo, number):
    return {
        "id": f"agent-{repo}-{number}",
        "started_at": NOW - 10,
        "last_seen": NOW,
        "ticket": {"project": "", "repo": repo, "number": number},
    }


def session_plan(raw, entries_by_root, *, entries=(), live=frozenset(), now=NOW, roots=None):
    """`hygiene.plan` over `raw` tables with a fake `scan` and registry data."""
    rules = hygiene.check_table(raw, "korapy.toml")
    if roots is None:
        roots = {name: f"/sessions/{name}" for name in raw}

    def scan(root):
        return list(entries_by_root.get(root, []))

    return hygiene.plan(rules, roots, now=now, scan=scan, entries=list(entries), live=live)


def leaf(root, name, *, mtime, size=500, symlink=False, realpath=None):
    return entry(
        os.path.join(root, name), mtime=mtime, size=size, symlink=symlink, realpath=realpath
    )


class SessionPlanProofCase(unittest.TestCase):
    def test_done_old_idle_leaf_is_safe_naming_rule_and_retention(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertEqual(
            (found.kind, found.path, found.safe),
            ("hygiene-session", "/sessions/acme-old/acme-7", True),
        )
        self.assertIn("acme-old", found.proof)
        self.assertIn("40d", found.proof)
        self.assertIn("30d", found.proof)

    def test_leaf_with_a_live_unit_is_kept(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live={("acme", 7)},
        )
        (found,) = candidates
        self.assertEqual(found.kind, "hygiene-session")
        self.assertFalse(found.safe)
        self.assertIn("live unit", found.proof)

    def test_leaf_with_an_open_entry_is_kept(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)]},
            entries=[live_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertIn("still open", found.proof)

    def test_young_leaf_is_kept(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 2 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertIn("retention", found.proof)

    def test_boundary_retention_is_kept(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 30 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertFalse(found.safe)

    def test_unreadable_mtime_is_kept_as_a_session_row(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=None)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertEqual(found.kind, "hygiene-session")
        self.assertFalse(found.safe)
        self.assertIn("modification time is unreadable", found.proof)

    def test_unparseable_name_is_an_unproven_keep(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {
                "/sessions/acme-old": [
                    leaf("/sessions/acme-old", "scratch-notes", mtime=NOW - 40 * DAY)
                ]
            },
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertEqual(found.kind, "unproven")
        self.assertFalse(found.safe)
        self.assertIn("has no registry entry: cannot prove nothing is lost", found.proof)

    def test_parsed_name_with_no_registry_entry_is_an_unproven_keep(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-9", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertEqual(found.kind, "unproven")
        self.assertFalse(found.safe)
        self.assertIn("has no registry entry", found.proof)

    def test_unreadable_live_listing_keeps_every_leaf(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=None,
        )
        (found,) = candidates
        self.assertFalse(found.safe)
        self.assertIn("live listing failed", found.proof)
        self.assertTrue(any("unreadable" in note for note in notes), f"no unreadable note: {notes}")

    def test_unreadable_live_listing_notes_each_session_rule(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule(), "acme-older": session_rule()},
            {
                "/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)],
                "/sessions/acme-older": [
                    leaf("/sessions/acme-older", "acme-8", mtime=NOW - 40 * DAY)
                ],
            },
            entries=[finished_entry("acme", 7), finished_entry("acme", 8)],
            live=None,
        )
        self.assertEqual(len(candidates), 2)
        for found in candidates:
            self.assertFalse(found.safe)
            self.assertIn("live listing failed", found.proof)
        for name in ("acme-old", "acme-older"):
            self.assertTrue(
                any(f"rule {name}:" in note and "unreadable" in note for note in notes),
                f"no unreadable note for {name}: {notes}",
            )

    def test_unreadable_live_listing_notes_unproven_only_rule(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule()},
            {
                "/sessions/acme-old": [
                    leaf("/sessions/acme-old", "scratch-notes", mtime=NOW - 40 * DAY)
                ]
            },
            entries=[finished_entry("acme", 7)],
            live=None,
        )
        (found,) = candidates
        self.assertEqual(found.kind, "unproven")
        self.assertFalse(found.safe)
        self.assertTrue(any("unreadable" in note for note in notes), f"no unreadable note: {notes}")

    def test_ticket_with_a_live_entry_among_finished_ones_is_kept(self):
        candidates, _notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7), live_entry("acme", 7)],
            live=set(),
        )
        (found,) = candidates
        self.assertEqual(found.kind, "hygiene-session")
        self.assertFalse(found.safe)
        self.assertIn("still open", found.proof)

    def test_done_tickets_agree_with_disk(self):
        from korapy import disk as disk_mod

        entries = [
            finished_entry("acme", 7),
            live_entry("acme", 7),
            finished_entry("acme", 8),
            finished_entry("acme", 8, ended_ago=200),
            finished_entry("acme", 9, ended_ago=2 * DAY),
        ]
        self.assertFalse(hasattr(hygiene, "_done_tickets"))
        self.assertEqual(disk_mod.done_tickets(entries, NOW), {("acme", 8), ("acme", 9)})


class SessionPlanScopeCase(unittest.TestCase):
    def test_other_repo_leaf_is_skipped_with_a_note(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "other-7", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("other", 7)],
            live=set(),
        )
        self.assertEqual(candidates, ())
        self.assertEqual(len(notes), 1)
        self.assertIn("belongs to repo", notes[0])
        self.assertIn("other", notes[0])
        self.assertIn("acme", notes[0])

    def test_reviews_entry_is_never_a_candidate(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule()},
            {"/sessions/acme-old": [leaf("/sessions/acme-old", "reviews", mtime=NOW - 40 * DAY)]},
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        self.assertEqual(candidates, ())
        self.assertEqual(len(notes), 1)
        self.assertIn("reviews", notes[0])

    def test_symlink_leaf_is_dropped_with_a_note_and_no_candidate(self):
        candidates, notes = session_plan(
            {"acme-old": session_rule()},
            {
                "/sessions/acme-old": [
                    leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY, symlink=True)
                ]
            },
            entries=[finished_entry("acme", 7)],
            live=set(),
        )
        self.assertEqual(candidates, ())
        self.assertEqual(len(notes), 1)
        self.assertIn("is a symlink", notes[0])

    def test_image_rules_are_still_skipped(self):
        candidates, notes = session_plan({"acme-images": image_rule()}, {})
        self.assertEqual(candidates, ())
        self.assertEqual(notes, ())

    def test_missing_root_is_refused_naming_the_rule(self):
        rules = hygiene.check_table({"acme-old": session_rule()}, "korapy.toml")
        with self.assertRaisesRegex(settings.SettingsError, "acme-old"):
            hygiene.plan(rules, {}, now=NOW, scan=lambda root: [])

    def test_dir_and_session_rules_plan_together(self):
        candidates, _notes = session_plan(
            {
                "acme-scratch": dir_rule(),
                "acme-old": session_rule(),
            },
            {
                "/sessions/acme-scratch": [
                    entry("/sessions/acme-scratch/run-4", mtime=NOW - 9 * DAY, size=100)
                ],
                "/sessions/acme-old": [
                    leaf("/sessions/acme-old", "acme-7", mtime=NOW - 40 * DAY, size=200)
                ],
            },
            entries=[finished_entry("acme", 7)],
            live=set(),
            roots={"acme-scratch": "/sessions/acme-scratch", "acme-old": "/sessions/acme-old"},
        )
        by_path = {found.path: found for found in candidates}
        self.assertTrue(by_path["/sessions/acme-scratch/run-4"].safe)
        self.assertEqual(by_path["/sessions/acme-scratch/run-4"].kind, "hygiene-dir")
        self.assertTrue(by_path["/sessions/acme-old/acme-7"].safe)
        self.assertEqual(by_path["/sessions/acme-old/acme-7"].kind, "hygiene-session")


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
        for path in sorted(ROOT.rglob("*.py")):
            for func, lineno in _rule_path_joins(path):
                if (path.stem, func) not in HYGIENE_JOIN_ALLOW:
                    problems.append(f"{path.relative_to(ROOT)}:{lineno} {func}")
        self.assertEqual(problems, [], f"second base plus path join: {problems}")

    def test_the_allow_list_names_real_uses(self):
        seen = set()
        for path in sorted(ROOT.rglob("*.py")):
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
