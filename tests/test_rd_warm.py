"""命令任务热工作树：本机目标机（ssh = local）真实执行领用、复用、交接快照与回收，不连远端，不调用模型。"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch import rd_warm as warm

DAY = 86400


class WarmFixture(unittest.TestCase):
    config_extra = ""
    features = ""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-warm-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("build/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        self.git("remote", "add", "origin", "https://example.invalid/team/repo.git")
        self.config_path = self.root / "config.ini"
        self.write_config()
        for mock in (
            patch.object(rd, "CONFIG", str(self.config_path)),
            patch.object(rd, "STATE", str(self.root / "state")),
            patch.dict(os.environ, {"RD_USER_ID": "employee_a", "RD_RETAIN_COMMAND_HOURS": "1000"}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mock.__enter__()
            self.addCleanup(mock.__exit__, None, None, None)
        self.cfg = rd.Config()

    def write_config(self, project=""):
        self.config_path.write_text(
            f"[host:self]\nssh = local\nworkdir = {self.root / 'worker'}\nsync = direct\n"
            f"path = {os.environ['PATH']}\ntimeout = 30\n[project:repo]\n{project}\n{self.config_extra}\n{self.features}",
            encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def host(self, rec):
        return rd.job_host(rd.Config(), rec)

    def worker(self, rec):
        return Path(rec["worker_root"])

    def args(self, command, **options):
        values = dict(dir=str(self.repo), agent=None, prompt_file=None, cmd=command, test=False, heavy=False,
                      host="self", force_host=True, tag=[], ref="HEAD", wip=False, branch=None, name="warm",
                      project=None, include=[], model=None, effort=None, timeout=20, initiator_session=None,
                      workdir=None)
        values.update(options)
        return argparse.Namespace(**values)

    def start(self, command, **options):
        known = {r["job"] for r in rd.all_jobs()}
        self.cfg = rd.Config()
        rd.cmd_run(self.args(command, **options), self.cfg)
        return next(r for r in rd.all_jobs() if r["job"] not in known)

    def state(self, rec):
        return rd.job_states(self.cfg, [rd.load_job(rec["job"])])[rec["job"]]

    def wait_done(self, rec):
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.state(rec).get("state") in ("done", "dead"):
                return
            time.sleep(0.1)
        self.fail("本机派单未在 30 秒内结束")

    def dispatch(self, command, **options):
        rec = self.start(command, **options)
        self.wait_done(rec)
        return rd.load_job(rec["job"])

    def slot_path(self, rec):
        return self.worker(rec) / warm.job_worktree(rec)

    def ack(self, rec):
        rec = rd.load_job(rec["job"])
        receipt = rd.collect_result(self.cfg, rec, rd.job_states(self.cfg, [rec])[rec["job"]])
        rd.cmd_ack(argparse.Namespace(job=rec["job"], round=receipt["round"], receipt_id=receipt["receipt_id"],
                                      initiator_session=None), self.cfg)
        return receipt

    def clean_args(self, **options):
        values = dict(job=None, force=False, delete_branch=False, discard_reports=False, merged=False, expired=False,
                      into=None, dry_run=False, json=False, project=None, initiator_session=None, slim=False,
                      dir=str(self.repo))
        values.update(options)
        return argparse.Namespace(**values)

    def sh(self, rec, script):
        return rd.ssh(self.host(rec), script).stdout


class SlotLifecycleTest(WarmFixture):
    def test_second_command_reuses_slot_keeps_ignored_cache_and_cleans_untracked(self):
        first = self.dispatch("mkdir -p build; echo cache > build/obj.o; echo stray > stray.txt; echo edit >> tracked.txt")
        self.assertTrue(first["worktree"].startswith("warm:"), first["worktree"])
        slot = self.slot_path(first)
        self.assertIn("/wt/_warm/repo-", str(slot))
        self.assertTrue((slot / "build" / "obj.o").exists())
        second = self.dispatch("cat build/obj.o > seen.txt; test ! -e stray.txt && echo clean > clean.txt; cat tracked.txt >> clean.txt")
        self.assertEqual(self.slot_path(second), slot)
        self.assertTrue(second["warm_reused"])
        self.assertEqual(second["warm_prev_job"], first["job"])
        self.assertEqual(second["warm_prev_commit"], first["base"])
        self.assertEqual((slot / "seen.txt").read_text(), "cache\n")
        self.assertEqual((slot / "clean.txt").read_text(), "clean\nbase\n")
        # 第二个任务在自己的分支上，第一个任务的分支不再被槽位检出。
        self.assertEqual(self.sh(second, f'git -C "{slot}" symbolic-ref --short HEAD').strip(), second["branch"])
        self.assertEqual(second["worktree"].split("#")[0], first["worktree"].split("#")[0])
        self.assertNotEqual(second["worktree"], first["worktree"])

    def test_handoff_snapshot_records_head_dirty_patch_and_untracked_list(self):
        first = self.dispatch("echo edit >> tracked.txt; echo stray > stray.txt; git commit -qam c1 --allow-empty; echo more >> tracked.txt")
        slot = self.slot_path(first)
        head_before = self.sh(first, f'git -C "{slot}" rev-parse HEAD').strip()
        self.dispatch("true")
        directory = self.worker(first) / "jobs" / first["job"]
        state = json.loads((directory / "final-state.json").read_text())
        self.assertEqual(state["head"], head_before)
        self.assertEqual(state["dirty"], 2)
        self.assertEqual(state["untracked_count"], 1)
        self.assertTrue(state["patch_saved"])
        self.assertFalse(state["patch_over_limit"])
        self.assertIn("+more", (directory / state["patch_file"]).read_text())
        self.assertEqual((directory / "final-untracked.txt").read_text().split(), ["stray.txt"])
        # 之后第一个任务的状态读快照，不读槽位现场（槽位此刻在第二个任务的分支上）。
        self.assertEqual(self.state(first)["head"], head_before)
        self.assertEqual(self.state(first)["dirty"], "2")
        head, entries = rd.worktree_changes(self.host(first), first)
        self.assertEqual(head, head_before)
        self.assertEqual(sorted(path for _, path in entries), ["stray.txt", "tracked.txt"])

    def test_patch_over_twenty_megabytes_keeps_only_statistics(self):
        first = self.dispatch("head -c 22000000 /dev/urandom > tracked.txt")
        self.dispatch("true")
        directory = self.worker(first) / "jobs" / first["job"]
        state = json.loads((directory / "final-state.json").read_text())
        self.assertFalse(state["patch_saved"])
        self.assertTrue(state["patch_over_limit"])
        self.assertIsNone(state["patch_file"])
        self.assertEqual(state["tracked_changed"], 1)
        self.assertEqual(state["dirty"], 1)
        self.assertFalse((directory / "final.patch").exists())
        self.assertFalse((directory / "final-patch.tmp").exists())

    def test_receipt_carries_warm_fields(self):
        first = self.dispatch("true")
        second = self.dispatch("true")
        one, two = self.ack(first), self.ack(second)
        self.assertEqual((one["worktree_mode"], one["warm_reused"]), ("warm", False))
        self.assertNotIn("warm_prev_commit", one)
        self.assertEqual((two["worktree_mode"], two["warm_reused"], two["warm_prev_commit"]), ("warm", True, second["base"]))
        cold = self.dispatch("true", fresh=True)
        self.assertEqual(self.ack(cold)["worktree_mode"], "cold")

    def test_task_script_exports_slot_path_as_rd_wt(self):
        rec = self.dispatch("pwd -P > wt-path.txt; echo $RD_WT > rd-wt.txt")
        slot = self.slot_path(rec)
        self.assertEqual((slot / "wt-path.txt").read_text().strip(), str(slot.resolve()))
        self.assertEqual(Path((slot / "rd-wt.txt").read_text().strip()).resolve(), slot.resolve())


class SlotLimitTest(WarmFixture):
    def test_concurrent_jobs_get_distinct_slots_then_fall_back_to_cold(self):
        recs = [self.start("sleep 4") for _ in range(3)]
        modes = [warm.parse_field(r)[0] for r in map(lambda r: rd.load_job(r["job"]), recs)]
        self.assertEqual(modes, ["warm", "warm", "cold"])
        slots = {rd.load_job(r["job"])["worktree"].split("#")[0] for r in recs[:2]}
        self.assertEqual(len(slots), 2)
        self.assertEqual(rd.load_job(recs[2]["job"])["warm_cold_reason"], "slots_full")
        for rec in recs:
            self.wait_done(rec)
        # 全部结束后新任务复用空闲槽位。
        again = self.dispatch("true")
        self.assertEqual(warm.parse_field(again)[0], "warm")
        self.assertTrue(again["warm_reused"])

    def test_warm_slots_option_limits_slots(self):
        self.write_config("warm_slots = 1")
        recs = [self.start("sleep 3") for _ in range(2)]
        self.assertEqual([warm.parse_field(rd.load_job(r["job"]))[0] for r in recs], ["warm", "cold"])
        for rec in recs:
            self.wait_done(rec)

    def test_parallel_claims_never_share_a_slot(self):
        bare = self.root / "worker" / "users" / "employee_a" / "repos" / "x.git"
        bare.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        for index in range(5):
            self.git("push", "-q", str(bare), f"HEAD:refs/heads/b{index}")
        host = rd.Config().host("self")
        host = rd.scoped_host(host, "employee_a")
        outputs = {}

        def claim(index):
            script = warm.claim_script(f'mkdir -p "$ROOT/jobs/j{index}" "$ROOT/wt"\n', "repos/x.git", f"j{index}",
                                       f"b{index}", "p-key", 2, [])
            outputs[index] = warm.parse_result(rd.ssh(host, script).stdout)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        warm_slots = [o["slot"] for o in outputs.values() if o["mode"] == "warm"]
        self.assertEqual(len(warm_slots), 2, outputs)
        self.assertEqual(len(set(warm_slots)), 2)
        self.assertEqual(sum(1 for o in outputs.values() if o["mode"] == "cold"), 3)

    def test_pinned_job_slot_is_not_taken_over(self):
        first = self.dispatch("echo keep > build-note.txt")
        rd.cmd_keep(argparse.Namespace(job=first["job"], hours=5), self.cfg)
        second = self.dispatch("true")
        self.assertNotEqual(second["worktree"].split("#")[0], first["worktree"].split("#")[0])
        self.assertTrue((self.slot_path(first) / "build-note.txt").exists())

    def test_stale_index_lock_is_removed_only_when_old_and_fresh_lock_falls_back_to_cold(self):
        first = self.dispatch("true")
        gitdir = Path(self.sh(first, f'git -C "{self.slot_path(first)}" rev-parse --absolute-git-dir').strip())
        lock = gitdir / "index.lock"
        lock.write_text("")
        fresh = self.dispatch("true")
        self.assertEqual(warm.parse_field(fresh)[0], "cold")
        self.assertEqual(fresh["warm_cold_reason"], "index_locked")
        old = time.time() - 600
        os.utime(lock, (old, old))
        reused = self.dispatch("true")
        self.assertEqual(warm.parse_field(reused)[0], "warm")
        self.assertFalse(lock.exists())


class EligibilityTest(WarmFixture):
    def test_off_switches_and_exclusions_use_cold_worktree(self):
        self.assertEqual(self.dispatch("true", fresh=True)["worktree"], f"cold:wt/{rd.all_jobs()[0]['job']}")
        self.write_config("warm = off")
        self.assertTrue(self.dispatch("true")["worktree"].startswith("cold:"))
        self.write_config()
        self.config_extra = ""
        self.features = "[features]\nwarm_worktree = false\n"
        self.write_config()
        self.assertTrue(self.dispatch("true")["worktree"].startswith("cold:"))
        self.features = ""
        self.write_config()
        gate = self.dispatch("true", gate={"ref": "HEAD", "commit": "x"})
        self.assertTrue(gate["worktree"].startswith("cold:"))
        self.assertTrue(self.dispatch("true")["worktree"].startswith("warm:"))

    def test_windows_target_and_agent_jobs_are_cold(self):
        self.assertFalse(warm.eligible(self.args("x"), {}, {"platform": "windows"}))
        self.assertFalse(warm.eligible(self.args("x", agent="codex", cmd=None), {}, {"platform": "posix"}))
        self.assertTrue(warm.eligible(self.args("x"), {}, {"platform": "posix"}))
        self.assertFalse(warm.eligible(self.args("x"), {"warm_slots": "0"}, {"platform": "posix"}))

    def test_invalid_slots_are_rejected_before_dispatch(self):
        self.write_config("warm_slots = many")
        with self.assertRaises(SystemExit) as raised:
            self.start("true")
        self.assertEqual(raised.exception.code, 4)

    def test_scheduled_reclaim_defaults_on_and_explicit_false_turns_off(self):
        self.assertTrue(rd.Config().scheduled_reclaim())
        self.features = "[features]\nscheduled_reclaim = false\n"
        self.write_config()
        self.assertFalse(rd.Config().scheduled_reclaim())


class CompatibilityTest(WarmFixture):
    def test_record_without_worktree_field_reads_cold_path(self):
        rec = self.dispatch("echo edit >> tracked.txt", fresh=True)
        legacy = dict(rec)
        legacy.pop("worktree")
        rd.save_job(legacy)
        self.assertEqual(warm.job_worktree(legacy), f"wt/{rec['job']}")
        head, entries = rd.worktree_changes(self.host(legacy), legacy)
        self.assertEqual(head, rec["base"])
        self.assertEqual([path for _, path in entries], ["tracked.txt"])
        self.assertEqual(self.state(legacy)["dirty"], "1")


class CleanTest(WarmFixture):
    def test_clean_releases_slot_and_logs_but_keeps_worktree_and_cache(self):
        rec = self.dispatch("mkdir -p build; echo cache > build/obj.o")
        slot = self.slot_path(rec)
        self.ack(rec)
        rd.cmd_clean(self.clean_args(job=rec["job"], force=True), self.cfg)
        self.assertTrue((slot / "build" / "obj.o").exists())
        self.assertFalse((self.worker(rec) / "jobs" / rec["job"]).exists())
        self.assertFalse(Path(str(self.worker(rec) / "warm" / (rec["worktree"].split(":")[1].split("#")[0] + ".claim"))).exists())
        self.assertIsNone(next((r for r in rd.all_jobs() if r["job"] == rec["job"]), None))
        # 释放后的槽位被下一个任务直接领用，缓存仍在。
        nxt = self.dispatch("test -f build/obj.o && echo hit > hit.txt")
        self.assertEqual(self.slot_path(nxt), slot)
        self.assertEqual((slot / "hit.txt").read_text(), "hit\n")

    def test_delete_branch_detaches_idle_slot_before_deleting_branch(self):
        rec = self.dispatch("true")
        self.ack(rec)
        slot = self.slot_path(rec)
        self.assertEqual(self.sh(rec, f'git -C "{slot}" symbolic-ref --short HEAD').strip(), rec["branch"])
        rd.cmd_clean(self.clean_args(job=rec["job"], force=True, delete_branch=True), self.cfg)
        bare = self.worker(rec) / rec["bare"]
        refs = subprocess.run(["git", "-C", str(bare), "branch", "--list", rec["branch"]], capture_output=True, text=True).stdout
        self.assertEqual(refs.strip(), "")
        self.assertTrue(slot.is_dir())
        detached = subprocess.run(["git", "-C", str(slot), "symbolic-ref", "-q", "HEAD"], capture_output=True)
        self.assertNotEqual(detached.returncode, 0)

    def test_reclaim_check_reads_snapshot_after_slot_was_taken_over(self):
        first = self.dispatch("echo note > stray.txt")
        self.ack(first)
        self.dispatch("true")
        rows = json.loads(self.expired_rows())
        row = next(item for item in rows if item["job"] == first["job"])
        self.assertEqual(row["code"], rd.BLOCK_DIRTY, row)
        self.assertIn("stray.txt", row["reason"])

    def expired_rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(self.clean_args(expired=True, dry_run=True, json=True), self.cfg)
        return out.getvalue()

    def test_expired_clean_releases_acknowledged_warm_job_without_removing_slot(self):
        rec = self.dispatch("true")
        self.ack(rec)
        slot = self.slot_path(rec)
        with patch.dict(os.environ, {"RD_RETAIN_COMMAND_HOURS": "0"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rd.cmd_clean(self.clean_args(expired=True), self.cfg)
        self.assertTrue(slot.is_dir())
        self.assertFalse((self.worker(rec) / "jobs" / rec["job"]).exists())


class IdleReclaimTest(WarmFixture):
    def age(self, rec, days):
        old = time.time() - days * DAY
        slot = rec["worktree"].split(":")[1].split("#")[0]
        for path in (self.worker(rec) / "warm" / f"{slot}.claim" / "released",
                     self.worker(rec) / "jobs" / rec["job"] / "exit_code"):
            if path.exists():
                os.utime(path, (old, old))

    def reap(self, rec, pinned=(), days=7):
        slot = rec["worktree"].split(":")[1].split("#")[0]
        return rd.ssh(self.host(rec), warm.reap_script(slot, list(pinned), days)).stdout.strip()

    def listing(self, rec, pinned=(), days=7):
        return rd.ssh(self.host(rec), warm.list_script(list(pinned), days)).stdout

    def test_idle_slot_is_reaped_with_snapshot_but_recent_or_claimed_slot_is_kept(self):
        rec = self.dispatch("echo work > stray.txt")
        slot = self.slot_path(rec)
        self.assertEqual(self.reap(rec), "kept:not_idle")
        self.age(rec, 8)
        self.assertEqual(self.reap(rec, pinned=[rec["job"]]), "kept:busy")
        self.assertIn("\tfree\t1", self.listing(rec))
        self.assertEqual(self.reap(rec), "reaped")
        self.assertFalse(slot.exists())
        state = json.loads((self.worker(rec) / "jobs" / rec["job"] / "final-state.json").read_text())
        self.assertEqual((state["reason"], state["dirty"]), ("idle_reclaim", 1))
        # 槽位被回收后任务仍可读快照，下一个任务重新建槽位。
        self.assertEqual(self.state(rec)["dirty"], "1")
        head, entries = rd.worktree_changes(self.host(rec), rec)
        self.assertEqual((head, [p for _, p in entries]), (rec["base"], ["stray.txt"]))
        again = self.dispatch("true")
        self.assertEqual(warm.parse_field(again)[0], "warm")
        self.assertFalse(again["warm_reused"])

    def test_running_job_slot_is_never_reaped(self):
        rec = self.start("sleep 4")
        rec = rd.load_job(rec["job"])
        self.age(rec, 30)
        self.assertEqual(self.reap(rec, days=0), "kept:busy")
        self.assertTrue(self.slot_path(rec).is_dir())
        self.wait_done(rec)

    def test_slots_count_towards_worktree_size_listing(self):
        from remote_dispatch import rd_reclaim_schedule as schedule
        rec = self.dispatch("true")
        sizes = schedule.root_sizes(self.host(rec))
        slot = rec["worktree"].split(":")[1].split("#")[0]
        self.assertIn("_warm/" + slot, sizes)
        self.assertNotIn("_warm", sizes)


class IncludeGuardTest(WarmFixture):
    def setUp(self):
        super().setUp()
        (self.repo / "data").mkdir()
        (self.repo / "data" / "input.txt").write_text("original\n")

    def rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(self.clean_args(expired=True, dry_run=True, json=True), self.cfg)
        return {row["job"]: row for row in json.loads(out.getvalue())}

    def check(self, command, **options):
        with patch.dict(os.environ, {"RD_RETAIN_COMMAND_HOURS": "0"}):
            rec = self.dispatch(command, include=["data"], **options)
            self.assertEqual(rec["include_hashes"], {"data/input.txt": rd.include_guard.hash_includes(str(self.repo), ["data"])["data/input.txt"]})
            self.ack(rec)
            return self.rows()[rec["job"]]

    def test_unchanged_include_input_is_exempt(self):
        row = self.check("cat data/input.txt > /dev/null")
        self.assertTrue(row["reclaimable"], row)

    def test_new_file_under_include_dir_blocks(self):
        row = self.check("echo result > data/output.txt")
        self.assertEqual(row["code"], rd.BLOCK_DIRTY, row)
        self.assertIn("data/output.txt", row["reason"])

    def test_modified_include_input_blocks_on_cold_worktree_too(self):
        row = self.check("echo changed > data/input.txt", fresh=True)
        self.assertEqual(row["code"], rd.BLOCK_DIRTY, row)
        self.assertIn("data/input.txt", row["reason"])

    def test_record_without_hashes_keeps_directory_exemption(self):
        with patch.dict(os.environ, {"RD_RETAIN_COMMAND_HOURS": "0"}):
            rec = self.dispatch("echo result > data/output.txt", include=["data"])
            legacy = dict(rec)
            legacy.pop("include_hashes")
            rd.save_job(legacy)
            self.ack(legacy)
            self.assertTrue(self.rows()[rec["job"]]["reclaimable"])


if __name__ == "__main__":
    unittest.main()
