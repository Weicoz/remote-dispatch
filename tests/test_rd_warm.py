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


class ReviewFixesTest(WarmFixture):
    """独立审查员第 1 轮发现的问题：先复现，再修。"""

    def make_bare(self, branches=4):
        bare = self.root / "worker" / "users" / "employee_a" / "repos" / "x.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        for index in range(branches):
            self.git("push", "-q", str(bare), f"HEAD:refs/heads/b{index}")
        return rd.scoped_host(rd.Config().host("self"), "employee_a")

    def claim(self, host, index, slots=1):
        script = warm.claim_script(f'mkdir -p "$ROOT/jobs/j{index}" "$ROOT/wt"\n', "repos/x.git", f"j{index}",
                                   f"b{index}", "p-key", slots, [])
        return warm.parse_result(rd.ssh(host, script).stdout)

    def unlock(self, path):
        subprocess.run(["chmod", "-R", "u+rwx", str(path)], check=False, capture_output=True)

    # P0-1
    def test_unwritable_job_dir_aborts_takeover_and_keeps_tracked_changes(self):
        first = self.dispatch("echo precious >> tracked.txt")
        directory = self.worker(first) / "jobs" / first["job"]
        os.chmod(directory, 0o500)
        self.addCleanup(os.chmod, directory, 0o755)
        second = self.dispatch("true")
        self.assertEqual(warm.parse_field(second)[0], "cold")
        self.assertEqual(second["warm_cold_reason"], "snapshot_failed")
        self.assertIn("precious", (self.slot_path(first) / "tracked.txt").read_text())
        self.assertEqual(sorted(p.name for p in directory.glob("final-*")), [])
        self.assertEqual(rd.worktree_changes(self.host(first), first)[0], first["base"])

    # P0-2
    def test_preparing_claim_stays_busy_while_heartbeat_runs_even_after_long_prep(self):
        host = self.make_bare()
        with patch.object(warm, "HEARTBEAT_SECONDS", 1), patch.object(warm, "HEARTBEAT_STALE_SECONDS", 4):
            first = self.claim(host, 0)
            self.assertEqual(first["mode"], "warm")
            at = Path(host["workdir"]) / "warm" / (first["slot"] + ".claim") / "at"
            at.write_text(str(int(time.time()) - 7200) + "\n")
            time.sleep(2.5)
            self.assertGreater(int(at.read_text()), time.time() - 4)
            second = self.claim(host, 1)
            self.assertEqual((second["mode"], second.get("reason")), ("cold", "slots_full"))

    def test_preparer_gone_means_heartbeat_stops_and_stale_claim_can_be_taken(self):
        host = self.make_bare()
        with patch.object(warm, "HEARTBEAT_SECONDS", 1), patch.object(warm, "HEARTBEAT_MAX_SECONDS", 1), \
             patch.object(warm, "HEARTBEAT_STALE_SECONDS", 4):
            first = self.claim(host, 0)
            at = Path(host["workdir"]) / "warm" / (first["slot"] + ".claim") / "at"
            time.sleep(2.5)  # 心跳只跑一圈就停（模拟准备者消失）
            at.write_text(str(int(time.time()) - 60) + "\n")
            second = self.claim(host, 1)
            self.assertEqual((second["mode"], second["slot"]), ("warm", first["slot"]))

    def test_unreadable_heartbeat_counts_as_busy(self):
        host = self.make_bare()
        first = self.claim(host, 0)
        claim = Path(host["workdir"]) / "warm" / (first["slot"] + ".claim")
        (claim / "at").write_text("garbage\n")
        (claim / "job").write_text("j0\n")
        second = self.claim(host, 1)
        self.assertEqual(second["mode"], "cold")

    # P1-3
    def test_ignored_include_file_does_not_leak_to_next_job(self):
        (self.repo / ".gitignore").write_text("build/\nlocal.cfg\n")
        self.git("add", ".")
        self.git("commit", "-qm", "ignore local.cfg")
        (self.repo / "local.cfg").write_text("A-secret\n")
        first = self.dispatch("cat local.cfg > a.out", include=["local.cfg"])
        (self.repo / "local.cfg").unlink()
        second = self.dispatch("if [ -e local.cfg ]; then echo leaked > seen.txt; else echo none > seen.txt; fi")
        self.assertEqual(self.slot_path(first), self.slot_path(second))
        self.assertEqual((self.slot_path(second) / "seen.txt").read_text(), "none\n")

    # P1-4
    def test_unremovable_untracked_dir_is_repaired_and_slot_stays_consistent(self):
        first = self.dispatch("mkdir -p junk/sub; touch junk/sub/f; chmod 000 junk/sub")
        self.addCleanup(self.unlock, self.slot_path(first) / "junk")
        second = self.dispatch("git status --porcelain --untracked-files=no > st.txt; git diff --summary > modes.txt; "
                               "git ls-files -s | awk '{print $1}' | sort -u > ls.txt")
        self.assertEqual(warm.parse_field(second)[0], "warm")
        self.assertFalse((self.slot_path(second) / "junk").exists())
        # 只放开 clean 报错的未跟踪路径：跟踪文件权限不变，新任务领用后工作树与提交一致。
        self.assertEqual((self.slot_path(second) / "st.txt").read_text(), "")
        self.assertEqual((self.slot_path(second) / "modes.txt").read_text(), "")
        self.assertEqual((self.slot_path(second) / "ls.txt").read_text().split(), ["100644"])
        self.assertEqual(os.stat(self.slot_path(second) / "tracked.txt").st_mode & 0o111, 0)
        self.assertEqual(self.sh(second, f'git -C "{self.slot_path(second)}" symbolic-ref --short HEAD').strip(), second["branch"])
        self.assertEqual(self.state(first)["head"], first["base"])

    # P1-5
    def test_nested_git_repo_is_removed_between_jobs(self):
        self.dispatch("git init -q nested; echo x > nested/f")
        second = self.dispatch("git status --porcelain > st.txt")
        self.assertFalse((self.slot_path(second) / "nested").exists())
        self.assertNotIn("nested", (self.slot_path(second) / "st.txt").read_text())
        self.assertEqual(self.state(second)["dirty"], "1")  # 只有 st.txt 自己

    # P2-6
    def test_dead_or_empty_stale_mutex_is_reclaimed(self):
        host = self.make_bare()
        mutex = Path(host["workdir"]) / "warm" / ".mutex"
        mutex.mkdir(parents=True)
        (mutex / "pid").write_text("999999\n")
        self.assertEqual(self.claim(host, 0, slots=2)["mode"], "warm")
        mutex.mkdir(exist_ok=True)
        old = time.time() - 600
        os.utime(mutex, (old, old))
        self.assertEqual(self.claim(host, 1, slots=2)["mode"], "warm")
        self.assertFalse(mutex.exists())

    # P2-7
    def test_target_side_pin_markers_protect_slots_from_other_controllers(self):
        first = self.dispatch("true")
        directory = self.worker(first) / "jobs" / first["job"]
        (directory / "pinned").write_text("")
        second = self.dispatch("true")
        self.assertNotEqual(second["worktree"].split("#")[0], first["worktree"].split("#")[0])
        (directory / "pinned").unlink()
        (directory / "pinned-until").write_text(str(int(time.time()) + 3600) + "\n")
        third = self.dispatch("true")
        self.assertNotEqual(third["worktree"].split("#")[0], first["worktree"].split("#")[0])
        (directory / "pinned-until").write_text(str(int(time.time()) - 5) + "\n")
        self.wait_done(second)
        fourth = self.dispatch("true")
        self.assertIn(fourth["worktree"].split("#")[0], {first["worktree"].split("#")[0], second["worktree"].split("#")[0]})

    def test_keep_writes_and_clears_pin_on_target(self):
        first = self.dispatch("true")
        pin = self.worker(first) / "jobs" / first["job"] / "pinned-until"
        rd.cmd_keep(argparse.Namespace(job=first["job"], hours=2), self.cfg)
        self.assertGreater(int(pin.read_text()), time.time() + 3600)
        rd.cmd_keep(argparse.Namespace(job=first["job"], hours=0), self.cfg)
        self.assertFalse(pin.exists())

    # P2-8
    def test_artifact_pull_after_takeover_reports_taken_instead_of_pulling_wrong_files(self):
        first = self.dispatch("mkdir -p out; echo mine > out/a.txt", artifact=["out"])
        self.assertTrue((self.worker(first) / "jobs" / first["job"] / "pinned").exists())
        (self.worker(first) / "jobs" / first["job"] / "pinned").unlink()
        with patch.object(warm, "pinned_jobs", return_value=[]):  # 控制端侧的钉住也撤掉，模拟另一个控制端
            self.dispatch("mkdir -p out; echo theirs > out/a.txt")
        results = rd.pull_artifacts(self.cfg, rd.load_job(first["job"]))
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("接手", results[0]["error"])
        self.assertFalse((self.repo / "out").exists())

    def test_pulling_all_artifacts_clears_the_pin(self):
        first = self.dispatch("mkdir -p out; echo mine > out/a.txt", artifact=["out"])
        pin = self.worker(first) / "jobs" / first["job"] / "pinned"
        self.assertTrue(pin.exists())
        results = rd.pull_artifacts(self.cfg, rd.load_job(first["job"]))
        self.assertEqual(results[0]["status"], "fetched")
        self.assertFalse(pin.exists())
        self.assertEqual((self.repo / "out" / "a.txt").read_text(), "mine\n")

    # P2-9
    def test_preparation_failure_releases_the_claim(self):
        self.write_config("setup = true")
        with patch.object(rd, "put_file", side_effect=RuntimeError("upload broke")), self.assertRaises(RuntimeError):
            self.start("true")
        failed = rd.all_jobs()[0]
        self.assertEqual(failed["dispatch_state"], "preparation_failed")
        name = warm.parse_field(failed)[1]
        self.assertFalse((self.worker(failed) / "warm" / f"{name}.claim").exists())

    # 第 3 轮：P1 权限恢复、心跳 pid / 上限、launching 失败释放、产物占用提示、include 清单
    def test_clean_retry_does_not_touch_tracked_file_modes_in_nested_unremovable_paths(self):
        first = self.dispatch("mkdir -p j2/s; touch j2/s/f; chmod 555 j2/s; mkdir -p junk/sub; touch junk/sub/f; chmod 000 junk/sub")
        self.addCleanup(self.unlock, self.slot_path(first) / "j2")
        self.addCleanup(self.unlock, self.slot_path(first) / "junk")
        second = self.dispatch("git status --porcelain --untracked-files=no > st.txt; ls j2 junk > left.txt 2>&1; true")
        self.assertEqual((self.slot_path(second) / "st.txt").read_text(), "")
        self.assertIn("No such file", (self.slot_path(second) / "left.txt").read_text())

    def test_heartbeat_pid_alive_keeps_claim_busy_even_when_at_is_stale(self):
        host = self.make_bare()
        with patch.object(warm, "HEARTBEAT_SECONDS", 30), patch.object(warm, "HEARTBEAT_STALE_SECONDS", 4):
            first = self.claim(host, 0)
            claim = Path(host["workdir"]) / "warm" / (first["slot"] + ".claim")
            for _ in range(50):
                if (claim / "hb.pid").exists():
                    break
                time.sleep(0.1)
            pid = int((claim / "hb.pid").read_text())
            self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
            (claim / "at").write_text(str(int(time.time()) - 7200) + "\n")
            self.assertEqual(self.claim(host, 1)["mode"], "cold")
            os.kill(pid, 15)
            time.sleep(0.5)
            self.assertEqual(self.claim(host, 2)["mode"], "warm")

    def test_heartbeat_limit_follows_this_dispatchs_prep_timeout(self):
        self.assertEqual(warm.heartbeat_limit(3600, 120), 4200)
        self.assertEqual(warm.heartbeat_limit(20, 120), 720)
        seen = {}
        real = warm.claim_script

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with patch.object(warm, "claim_script", spy):
            self.dispatch("true", timeout=20)
        self.assertEqual(seen["heartbeat_max"], 720)

    def test_launch_failure_without_run_script_releases_the_claim(self):
        with patch.object(rd, "launch", side_effect=RuntimeError("launch broke")), self.assertRaises(RuntimeError):
            self.start("true")
        failed = rd.all_jobs()[0]
        self.assertEqual(failed["dispatch_state"], "launching")
        name = warm.parse_field(failed)[1]
        self.assertFalse((self.worker(failed) / "warm" / f"{name}.claim").exists())

    def test_launch_failure_after_run_script_upload_keeps_the_claim(self):
        def half_launch(host, rec, *args):
            rd.put_file(host, f"jobs/{rec['job']}/run.r1.sh", "true\n")
            raise RuntimeError("launch broke late")

        with patch.object(rd, "launch", side_effect=half_launch), self.assertRaises(RuntimeError):
            self.start("true")
        failed = rd.all_jobs()[0]
        name = warm.parse_field(failed)[1]
        self.assertTrue((self.worker(failed) / "warm" / f"{name}.claim").exists())

    def test_pending_artifacts_are_reported_as_occupying_the_slot(self):
        first = self.dispatch("mkdir -p out; echo mine > out/a.txt", artifact=["out"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rd.cmd_clean(self.clean_args(expired=True, dry_run=True, json=True), self.cfg)
        row = next(r for r in json.loads(out.getvalue()) if r["job"] == first["job"])
        self.assertEqual(row["code"], rd.BLOCK_ARTIFACTS_PENDING)
        self.assertIn("热槽位因产物未拉回被占用", row["reason"])
        self.assertIn("fetch JOB --artifacts", row["reason"])
        line = rd.describe(rd.load_job(first["job"]), self.state(first))
        self.assertIn("热槽位因产物未拉回被占用", line)
        from remote_dispatch import rd_board as board
        stub = type("Q", (), dict(delivery=lambda s, r, x: "received",
                                  result_path=lambda s, r, n: self.root / "no-results" / "r1.json"))()
        block = board.reclaim_block(stub, rd.load_job(first["job"]), dict(state="done"), {}, [])
        self.assertEqual(block["code"], "artifacts_pending")

    def test_unpacked_listing_covers_every_include_form_including_dot(self):
        listing = "./\n./a/\n./a/b.txt\n./.git/config\n.git/HEAD\nc.txt\n../evil\n/abs\nd/e/f.cfg\n"
        self.assertEqual(warm.unpacked_files(listing), ["a/b.txt", "c.txt", "d/e/f.cfg"])

    def test_ignored_files_inside_included_directory_are_cleaned_for_next_job(self):
        (self.repo / ".gitignore").write_text("build/\n*.cfg\n")
        (self.repo / "conf").mkdir()
        (self.repo / "conf" / "local.cfg").write_text("secret\n")
        (self.repo / "conf" / "n space.cfg").write_text("secret2\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore cfg")
        first = self.dispatch("ls conf > a.out", include=["conf"])
        unpacked = (self.worker(first) / "warm" / (warm.parse_field(first)[1] + ".unpacked")).read_text().split("\n")
        self.assertIn("conf/n space.cfg", unpacked)
        second = self.dispatch("ls conf > seen.txt 2>&1; true")
        self.assertIn("No such file", (self.slot_path(second) / "seen.txt").read_text())

    # P2-10
    def test_broken_slot_without_snapshot_is_kept_and_job_goes_cold(self):
        first = self.dispatch("echo work > stray.txt")
        slot = self.slot_path(first)
        (slot / ".git").unlink()
        second = self.dispatch("true")
        self.assertEqual((warm.parse_field(second)[0], second["warm_cold_reason"]), ("cold", "slot_invalid"))
        self.assertEqual((slot / "stray.txt").read_text(), "work\n")


if __name__ == "__main__":
    unittest.main()
