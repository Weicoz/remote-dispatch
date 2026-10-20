"""Actual Git/filesystem cache boundaries; no existing task directories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

from remote_dispatch import rd_cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "users/fixture/wt/job-a"
        self.root.mkdir(parents=True)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text("private/\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("branch", "accepted")
        self.cache = self.root / "private/DerivedData/Build/Intermediates.noindex"
        self.cache.mkdir(parents=True)
        (self.cache / "object.o").write_bytes(b"temporary")
        self.product = self.root / "private/DerivedData/Build/Products/App"
        self.product.parent.mkdir(parents=True)
        self.product.write_bytes(b"retained-product")
        self.closed = {"head": self.head, "all_owned_resources_closed": True,
                       "source_freeze_ended": True, "observed_current": []}
        self.env = patch.dict(os.environ, {"RD_USER_ID": "fixture", "RD_JOB": "job-a", "RD_WT": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True)

    def run_cache(self, apply=False):
        return rd_cache.prune(self.root, "accepted", self.closed,
                              [str(self.cache.relative_to(self.root))],
                              [str(self.product.relative_to(self.root))], apply=apply)

    def test_plan_retains_cache_and_apply_deletes_only_intermediates(self):
        plan = self.run_cache()
        self.assertTrue(self.cache.exists())
        self.assertEqual(plan["deleted"], [])
        with patch.object(rd_cache, "has_open_files", return_value=False):
            result = self.run_cache(apply=True)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.product.read_bytes(), b"retained-product")
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(result["retained_before"], result["retained_after"])

    def test_many_retained_files_use_a_small_tree_digest_not_a_large_result_dump(self):
        evidence = self.root / "private/evidence"
        evidence.mkdir()
        for number in range(500):
            (evidence / (str(number) + ".json")).write_text("{}")
        result = rd_cache.prune(self.root, "accepted", self.closed,
                               [str(self.cache.relative_to(self.root))], ["private/evidence"])
        self.assertLess(len(json.dumps(result)), 8192)

    def test_rejects_live_processes_unmerged_work_and_other_employee(self):
        self.closed["observed_current"] = [{"pid": 123}]
        with self.assertRaises(ValueError):
            self.run_cache(True)
        self.closed["observed_current"] = []
        with patch.dict(os.environ, {"RD_USER_ID": "other"}), self.assertRaises(ValueError):
            self.run_cache(True)
        (self.root / "change.txt").write_text("new source")
        self.git("add", "change.txt")
        with self.assertRaises(ValueError):
            self.run_cache(True)
        self.assertTrue(self.cache.exists())

    def test_rejects_symlink_targets_and_protected_overlap(self):
        outside = self.root.parent / "preserved"
        outside.mkdir()
        (self.cache / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_cache(True)
        (self.cache / "link").unlink()
        with self.assertRaises(ValueError):
            rd_cache.prune(self.root, "accepted", self.closed,
                           [str(self.cache.relative_to(self.root))],
                           [str(self.cache.relative_to(self.root))], apply=True)
        self.assertTrue(outside.exists())

    def test_open_files_or_unknown_lsof_result_prevent_deletion(self):
        with patch.object(rd_cache, "has_open_files", return_value=True), self.assertRaises(ValueError):
            self.run_cache(True)
        self.assertTrue(self.cache.exists())

    def test_cache_containing_a_nested_worktree_is_preserved(self):
        nested = self.cache / "nested-worktree"
        self.git("worktree", "add", "--detach", str(nested), "HEAD")
        with patch.object(rd_cache, "has_open_files", return_value=False), self.assertRaises(ValueError):
            self.run_cache(True)
        self.assertTrue((nested / ".git").exists())

    def test_cli_output_cannot_overwrite_retained_or_closed_evidence(self):
        directory = self.root / "private/evidence"
        directory.mkdir()
        closed = directory / "closure.json"
        closed.write_text(json.dumps(self.closed))
        original = closed.read_bytes()
        for output, retained in [(closed, directory), (directory / "new.json", directory),
                                 (self.product, self.product)]:
            with self.subTest(output=output):
                result = subprocess.run([sys.executable, str(Path(rd_cache.__file__)),
                    "--worktree", str(self.root), "--merged-into", "accepted",
                    "--closed-receipt", str(closed), "--cache", str(self.cache.relative_to(self.root)),
                    "--retain", str(retained.relative_to(self.root)), "--output", str(output), "--apply"],
                    capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(closed.read_bytes(), original)
                self.assertEqual(self.product.read_bytes(), b"retained-product")
                self.assertTrue(self.cache.exists())

    def test_post_acceptance_markdown_commit_can_reuse_actual_closure_but_code_cannot(self):
        (self.root / "README.md").write_text("# Accepted\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "status")
        self.git("branch", "-f", "accepted", "HEAD")
        self.assertEqual(self.run_cache()["state"], "planned")
        (self.root / "client.swift").write_text("let changed = true\n")
        self.git("add", "client.swift")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "code")
        self.git("branch", "-f", "accepted", "HEAD")
        with self.assertRaises(ValueError):
            self.run_cache()

    @unittest.skipUnless(shutil.which("lsof"), "lsof is not installed")
    def test_real_open_file_is_detected(self):
        with (self.cache / "object.o").open("rb"):
            self.assertTrue(rd_cache.has_open_files(self.cache))


if __name__ == "__main__":
    unittest.main()
