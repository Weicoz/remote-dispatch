"""派单与清理回归：真实临时 Git 仓库，本机 shell/RPC 模拟，不连接设备。"""
import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_rd import rd, native


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-dispatch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo 中文"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        (self.repo / "remove.txt").write_text("remove\n")
        (self.repo / "binary.dat").write_bytes(bytes(range(256)))
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("branch", "baseline")
        self.worker = self.root / "worker" / "users" / "employee_a"
        config = self.root / "config.ini"
        config.write_text(f"[host:fixture]\nssh=fixture.invalid\nworkdir={self.root / 'worker'}\nsync=direct\npath={os.environ['PATH']}\n")
        self.real_launch = rd.launch
        for mock in (
            patch.object(rd, "CONFIG", str(config)),
            patch.object(rd, "STATE", str(self.root / "state")),
            patch.dict(os.environ, {"RD_USER_ID": "employee_a", "RD_REPORTS_DIR": str(self.root / "saved reports")}),
            patch.object(rd, "ssh", side_effect=self.local_shell),
            patch.object(rd, "scp_path", side_effect=lambda h, rel: str(Path(h["workdir"]) / rel)),
            patch.object(native, "request", side_effect=self.local_rpc),
            patch.object(rd, "launch", side_effect=self.finish),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mock.__enter__()
            self.addCleanup(mock.__exit__, None, None, None)
        self.cfg = rd.Config()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def local_shell(self, host, script, check=True, stdin_data=None, stdin=None, timeout=None, stdout=None):
        shared = dict(host, workdir=host.get("shared_workdir", host["workdir"]))
        pre = f'ROOT={rd.root_expr(host)}; SHARED_ROOT={rd.root_expr(shared)}; '
        result = subprocess.run(["sh", "-c", pre + script], input=stdin_data, stdin=stdin,
                                stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, text=stdin_data is not None, timeout=timeout or 20)
        out = "" if stdout is not None else result.stdout
        err = result.stderr
        if isinstance(out, bytes):
            out = out.decode()
        if isinstance(err, bytes):
            err = err.decode()
        if check and result.returncode:
            rd.die(err)
        return subprocess.CompletedProcess(result.args, result.returncode, out, err)

    def local_rpc(self, host, operation, data=None, payload=None, output=None, **kwargs):
        query = dict(data or {}, operation=operation, workdir=host["workdir"],
                     shared_workdir=host["shared_workdir"], path="")
        if output is not None:
            capture = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with patch.object(sys, "stdout", capture):
                native.serve(query)
            capture.flush()
            capture.buffer.seek(0)
            self.assertTrue(json.loads(capture.buffer.readline())["ok"])
            for line in capture.buffer:
                output.write(base64.b64decode(line))
            return None
        if isinstance(payload, bytes):
            payload = io.BytesIO(payload)
        return native.handle(query, payload or io.BytesIO())

    def finish(self, host, rec, *args):
        directory = Path(host["workdir"]) / "jobs" / rec["job"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "exit_code").write_text("0")
        suffix = "json" if host["platform"] == "windows" else "sh"
        (directory / f"run.r{rec['rounds']}.{suffix}").write_text("")

    def args(self, **overrides):
        values = dict(dir=str(self.repo), agent=None, prompt_file=None, cmd="true", test=False,
                      heavy=False, host="fixture", force_host=True, tag=[], ref="baseline", wip=False,
                      branch=None, name="fixture", project=None, include=[], model=None, effort=None,
                      timeout=15, initiator_session=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def dispatch(self, **kwargs):
        before = {r["job"] for r in rd.all_jobs()}
        rd.cmd_run(self.args(**kwargs), self.cfg)
        return next(r for r in rd.all_jobs() if r["job"] not in before)

    @contextlib.contextmanager
    def initiator_context(self, processes, **environment):
        """伪造进程信息的系统边界，其余 Git、落盘与查询照常执行。"""
        original = subprocess.run
        def run(command, *args, **kwargs):
            if command[0] == "ps":
                pid = int(command[command.index("-p") + 1])
                parent, executable = processes.get(pid, (0, "/sbin/launchd"))
                return subprocess.CompletedProcess(command, 0, f"{parent} {executable}\n", "")
            return original(command, *args, **kwargs)
        values = dict(RD_INITIATOR_SESSION="", CODEX_THREAD_ID="codex-session",
                      CLAUDE_CODE_SESSION_ID="claude-session", DSH_SESSION_ID="dsh-session",
                      ANTIGRAVITY_CONVERSATION_ID="agy-session", KIMI_SESSION_ID="unconfirmed-kimi",
                      HERDR_SESSION="herdr-session", HERDR_WORKSPACE_ID="workspace-1", HERDR_PANE_ID="pane-1")
        values.update(environment)
        with patch.dict(os.environ, values), patch.object(subprocess, "run", side_effect=run), \
             patch("socket.gethostname", return_value="initiator-fixture"):
            yield

    def queried_job(self, job):
        args = argparse.Namespace(job=job, details=False, json=True)
        return json.loads(self.capture(rd.cmd_status, args))[0]

    def test_run_records_codex_origin_and_dispatch_time_herdr_context(self):
        processes = {os.getppid(): (700001, "/bin/zsh -c rdispatch run"),
                     700001: (1, "/opt/bin/codex exec")}
        with self.initiator_context(processes):
            record = self.dispatch()
        # 查询时的环境不应改变派单时捕获的值。
        with patch.dict(os.environ, {"HERDR_PANE_ID": "moved-pane"}):
            row = self.queried_job(record["job"])
        self.assertEqual(row["initiator_harness"], "codex")
        self.assertEqual(row["initiator_host"], "initiator-fixture")
        self.assertEqual(row["initiator_session"], "codex-session")
        self.assertEqual(row["initiator_herdr"], dict(session="herdr-session", workspace_id="workspace-1",
                                                   pane_id="pane-1", captured_at=row["created"]))

    def test_run_uses_nearest_harness_despite_conflicting_session_variables(self):
        for harness, session in (("claude", "claude-session"), ("dsh", "dsh-session"),
                                 ("agy", "agy-session"), ("kimi", None)):
            with self.subTest(harness=harness):
                processes = {os.getppid(): (700001, "/bin/bash -c codex"),
                             700001: (700002, f"/opt/bin/{harness} --print"),
                             700002: (1, "/opt/bin/codex")}
                with self.initiator_context(processes):
                    record = self.dispatch()
                row = self.queried_job(record["job"])
                self.assertEqual(row["initiator_harness"], harness)
                self.assertEqual(row["initiator_session"], session)
                self.assertEqual(row["initiator_host"], "initiator-fixture")
                self.assertEqual(row["initiator_herdr"], dict(session="herdr-session", workspace_id="workspace-1",
                                                           pane_id="pane-1", captured_at=row["created"]))

    def test_run_recognizes_harness_launched_through_node_or_python(self):
        cases = (("node /opt/node_modules/@openai/codex/bin/codex.js exec", "codex", "codex-session"),
                 ("node /opt/node_modules/@anthropic-ai/claude-code/cli.js -p", "claude", "claude-session"),
                 ("node /opt/node_modules/@deepseek-ai/dsh/lib/index.js headless", "dsh", "dsh-session"),
                 ("python3 -m kimi_cli --auto", "kimi", None))
        for command, harness, session in cases:
            with self.subTest(command=command), self.initiator_context({os.getppid(): (1, command)}):
                record = self.dispatch()
                row = self.queried_job(record["job"])
                self.assertEqual(row["initiator_harness"], harness)
                self.assertEqual(row["initiator_session"], session)

    def test_run_harness_detection_ignores_quotes_in_prompt_arguments(self):
        for command in ("/opt/bin/codex --prompt don't guess", "node /opt/bin/codex.js --prompt don't guess"):
            with self.subTest(command=command), self.initiator_context({os.getppid(): (1, command)}):
                record = self.dispatch()
                row = self.queried_job(record["job"])
                self.assertEqual(row["initiator_harness"], "codex")
                self.assertEqual(row["initiator_session"], "codex-session")

    def test_run_preserves_explicit_and_rd_session_override_priority(self):
        for explicit, expected in (("app:device-1", "app:device-1"), (None, "rd-session")):
            with self.subTest(explicit=explicit), self.initiator_context(
                    {os.getppid(): (1, "/opt/bin/claude")}, RD_INITIATOR_SESSION="rd-session"):
                record = self.dispatch(initiator_session=explicit)
                row = self.queried_job(record["job"])
                self.assertEqual(row["initiator_session"], expected)
                self.assertEqual(row["initiator_harness"], "claude")

    def test_run_outside_herdr_records_empty_context(self):
        with self.initiator_context({os.getppid(): (1, "/opt/bin/agy")},
                                    HERDR_SESSION="", HERDR_WORKSPACE_ID="", HERDR_PANE_ID=""):
            record = self.dispatch()
        row = self.queried_job(record["job"])
        self.assertEqual(row["initiator_harness"], "agy")
        self.assertEqual(row["initiator_host"], "initiator-fixture")
        self.assertEqual(row["initiator_session"], "agy-session")
        self.assertEqual(row["initiator_herdr"], dict(session=None, workspace_id=None, pane_id=None,
                                                   captured_at=row["created"]))

    def test_run_unknown_process_chain_ignores_unrelated_session_variables(self):
        cases = (("/bin/zsh -c 'codex exec'", 0),
                 ("python3 -c 'print(\"claude\")'", os.getppid()),
                 ("node -e 'console.log(\"dsh\")'", 0))
        for command, parent in cases:
            with self.subTest(command=command), self.initiator_context({os.getppid(): (parent, command)}):
                record = self.dispatch()
                row = self.queried_job(record["job"])
                self.assertEqual(row["initiator_harness"], "unknown")
                self.assertIsNone(row["initiator_session"])

    def test_run_records_unknown_origin_when_process_information_is_unavailable(self):
        original = subprocess.run
        def run(command, *args, **kwargs):
            if command[0] == "ps":
                raise PermissionError("process information unavailable")
            return original(command, *args, **kwargs)
        with patch.dict(os.environ, {"RD_INITIATOR_SESSION": "", "CODEX_THREAD_ID": "unrelated-session"}), \
             patch.object(subprocess, "run", side_effect=run):
            record = self.dispatch()
        row = self.queried_job(record["job"])
        self.assertEqual(row["initiator_harness"], "unknown")
        self.assertIsNone(row["initiator_session"])

    def test_origin_without_session_variable_does_not_use_other_harness_session(self):
        with self.initiator_context({os.getppid(): (1, "/opt/bin/claude")}, CLAUDE_CODE_SESSION_ID=""):
            record = self.dispatch()
        row = self.queried_job(record["job"])
        self.assertEqual(row["initiator_harness"], "claude")
        self.assertIsNone(row["initiator_session"])

    def test_harness_can_query_its_result_without_an_explicit_session(self):
        with self.initiator_context({os.getppid(): (1, "/opt/bin/claude")}):
            record = self.dispatch()
            result = json.loads(self.capture(rd.cmd_result, argparse.Namespace(
                job=record["job"], round=None, initiator_session=None)))
        self.assertEqual(result["initiator_session"], "claude-session")
        self.assertEqual(result["job"], record["job"])

    def test_status_accepts_legacy_jobs_without_initiator_fields(self):
        record = self.dispatch()
        for key in ("initiator_harness", "initiator_host", "initiator_session", "initiator_herdr"):
            record.pop(key, None)
        rd.save_job(record)
        self.assertEqual(self.queried_job(record["job"])["job"], record["job"])
        details = json.loads(self.capture(rd.cmd_status, argparse.Namespace(
            job=record["job"], details=True, json=True, initiator_session=None)))
        self.assertEqual(details[0]["job"], record["job"])
        self.assertIsNone(details[0]["initiator_session"])

    def test_run_refuses_network_failure_before_registering_or_launching_job(self):
        info = dict(reachable=True, memfree="50", disk="100", network={"ok": False, "error": "TLS handshake failed"})
        for host in ("fixture", "auto"):
            with self.subTest(host=host), patch.object(rd, "probe", return_value=info), \
                 patch.object(rd, "launch") as launch, self.assertRaises(SystemExit) as result:
                self.dispatch(host=host, force_host=False)
            self.assertEqual(result.exception.code, 3)
            launch.assert_not_called()
            self.assertEqual(rd.all_jobs(), [])
            self.assertFalse(self.worker.exists())

    def test_force_named_host_keeps_existing_probe_bypass_semantics(self):
        with patch.object(rd, "probe", side_effect=AssertionError("forced host must bypass probe")) as probe:
            record = self.dispatch(force_host=True)
        self.assertEqual(record["host"], "fixture")
        probe.assert_not_called()

    def clean(self, record, **overrides):
        values = dict(job=record["job"], force=True, delete_branch=True, discard_reports=False, initiator_session=None)
        values.update(overrides)
        rd.cmd_clean(argparse.Namespace(**values), self.cfg)

    def test_missing_includes_list_all_paths_before_host_probe(self):
        errors = io.StringIO()
        with patch.object(rd, "pick_host") as probe, patch.object(rd, "ssh") as ssh, \
             patch.object(native, "request") as rpc, contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            rd.cmd_run(self.args(include=["missing one", "missing two"], force_host=False), self.cfg)
        self.assertIn("missing one", errors.getvalue())
        self.assertIn("missing two", errors.getvalue())
        self.assertIn(str(self.repo), errors.getvalue())
        probe.assert_not_called()
        ssh.assert_not_called()
        rpc.assert_not_called()
        self.assertEqual(rd.all_jobs(), [])

    def test_include_pack_failure_is_local_and_names_error(self):
        (self.repo / "extra.txt").write_text("extra")
        original = subprocess.run
        def fail_tar(args, **kwargs):
            if args[0] == "tar":
                return subprocess.CompletedProcess(args, 1, b"", b"extra.txt: permission denied")
            return original(args, **kwargs)
        errors = io.StringIO()
        with patch.object(subprocess, "run", side_effect=fail_tar), patch.object(rd, "ssh") as ssh, \
             contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            self.dispatch(include=["extra.txt"])
        ssh.assert_not_called()
        self.assertIn("extra.txt: permission denied", errors.getvalue())

    def test_include_uses_selected_repository_root_with_spaces_and_option_names(self):
        for name in ["extra 中文.txt", "-extra.txt"]:
            (self.repo / name).write_text(name)
        record = self.dispatch(include=["extra 中文.txt", "-extra.txt"])
        for name in ["extra 中文.txt", "-extra.txt"]:
            self.assertEqual((self.worker / "wt" / record["job"] / name).read_text(), name)

    def test_wip_preserves_ref_and_sends_staged_unstaged_binary_and_deletions(self):
        for platform in ("posix", "windows"):
            for staged in (False, True):
                with self.subTest(platform=platform, staged=staged):
                    self.cfg.cp.set("host:fixture", "platform", platform)
                    (self.repo / "tracked.txt").write_text(f"edited {platform} {staged}\n")
                    (self.repo / "binary.dat").write_bytes(b"\x00\xff" * 50)
                    (self.repo / "remove.txt").unlink(missing_ok=True)
                    (self.repo / "untracked.txt").write_text("not included")
                    if staged:
                        self.git("add", "-u")
                    before = self.git("status", "--porcelain")
                    record = self.dispatch(wip=True)
                    wt = self.worker / "wt" / record["job"]
                    self.assertEqual(record["base"], self.base)
                    self.assertEqual(native.git(wt, "rev-parse", "HEAD").stdout.strip(), self.base)
                    self.assertEqual((wt / "tracked.txt").read_bytes(), (self.repo / "tracked.txt").read_bytes())
                    self.assertEqual((wt / "binary.dat").read_bytes(), (self.repo / "binary.dat").read_bytes())
                    self.assertFalse((wt / "remove.txt").exists())
                    self.assertFalse((wt / "untracked.txt").exists())
                    self.assertEqual(len(native.git(wt, "diff", "--cached", "--name-only").stdout.splitlines()), 3)
                    self.assertEqual(self.git("status", "--porcelain"), before)

    def test_wip_against_different_ref_includes_head_changes_and_working_tree(self):
        (self.repo / "committed.txt").write_text("after baseline")
        self.git("add", ".")
        self.git("commit", "-qm", "after baseline")
        (self.repo / "tracked.txt").write_text("index version")
        self.git("add", "tracked.txt")
        (self.repo / "tracked.txt").write_text("final worktree version")
        record = self.dispatch(wip=True, ref="baseline")
        wt = self.worker / "wt" / record["job"]
        self.assertEqual(native.git(wt, "rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual((wt / "committed.txt").read_text(), "after baseline")
        self.assertEqual((wt / "tracked.txt").read_text(), "final worktree version")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "final worktree version")
        self.assertIn("index version", self.git("show", ":tracked.txt"))

    def test_wip_staged_addition_rename_and_custom_diff_prefix(self):
        self.git("config", "diff.noprefix", "true")
        (self.repo / "added.txt").write_text("new")
        self.git("add", "added.txt")
        self.git("mv", "tracked.txt", "renamed.txt")
        record = self.dispatch(wip=True)
        wt = self.worker / "wt" / record["job"]
        self.assertEqual((wt / "added.txt").read_text(), "new")
        self.assertEqual((wt / "renamed.txt").read_text(), "base\n")
        self.assertFalse((wt / "tracked.txt").exists())

    def test_failure_before_worktree_creation_can_remove_created_branch(self):
        def fail_worktree(host, script, **kwargs):
            if "worktree add" in script:
                rd.die("injected worktree failure")
            return self.local_shell(host, script, **kwargs)
        with patch.object(rd, "ssh", side_effect=fail_worktree), self.assertRaises(SystemExit):
            self.dispatch(branch="rd/employee_a/before-worktree")
        record = rd.all_jobs()[-1]
        self.assertFalse((self.worker / "wt" / record["job"]).exists())
        self.assertTrue(record["owns_worker_branch"])
        self.clean(record)
        self.assertNotEqual(native.git(self.worker / record["bare"], "show-ref", "--verify", "refs/heads/" + record["branch"], check=False).returncode, 0)

    def test_origin_sync_and_cleanup_use_only_local_bare_fixture(self):
        origin = self.root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(origin)], check=True)
        self.git("remote", "add", "origin", str(origin))
        self.cfg.cp.set("host:fixture", "sync", "origin")
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                self.worker = self.root / ("origin-worker-" + platform) / "users/employee_a"
                self.cfg.cp.set("host:fixture", "workdir", str(self.worker.parent.parent))
                record = self.dispatch(branch="rd/employee_a/origin-" + platform)
                self.assertTrue(record["owns_origin_branch"])
                self.assertEqual(native.git(origin, "rev-parse", record["branch"]).stdout.strip(), self.base)
                # Exercise normal worktree and fetched-commit gates without force.
                rd.save_job(dict(record, handoff_version=None))
                self.clean(record, force=False)
                self.assertNotEqual(native.git(origin, "show-ref", "--verify", "refs/heads/" + record["branch"], check=False).returncode, 0)
                self.assertEqual(native.git(origin, "rev-parse", "baseline").stdout.strip(), self.base)

    def test_empty_wip_stops_before_remote_even_with_untracked_files(self):
        (self.repo / "untracked.txt").write_text("untracked")
        errors = io.StringIO()
        with patch.object(rd, "ssh") as ssh, contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            self.dispatch(wip=True)
        ssh.assert_not_called()
        self.assertIn("--wip", errors.getvalue())
        self.assertIn("没有改动", errors.getvalue())
        self.assertEqual(rd.all_jobs(), [])

    def test_failure_after_worktree_creation_is_registered_and_cleanable(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                def lost_ssh(host, script, **kwargs):
                    result = self.local_shell(host, script, **kwargs)
                    if "worktree add" in script:
                        rd.die("injected lost response")
                    return result
                def lost_rpc(host, operation, *args, **kwargs):
                    result = self.local_rpc(host, operation, *args, **kwargs)
                    if operation == "init":
                        raise ValueError("injected lost response")
                    return result
                with patch.object(rd, "ssh", side_effect=lost_ssh), patch.object(native, "request", side_effect=lost_rpc), \
                     self.assertRaises((SystemExit, ValueError)):
                    self.dispatch(branch="rd/employee_a/retry")
                record = rd.all_jobs()[-1]
                self.assertEqual(record["dispatch_state"], "preparation_failed")
                with self.assertRaises(SystemExit) as stopped:
                    rd.cmd_wait(argparse.Namespace(job=record["job"], timeout=1, interval=0.01), self.cfg)
                self.assertEqual(stopped.exception.code, 1)
                wt = self.worker / "wt" / record["job"]
                self.assertTrue(wt.exists())
                self.clean(record)
                self.assertFalse(wt.exists())
                self.assertFalse(Path(rd.job_path(record["job"])).exists())
                self.assertNotEqual(native.git(self.worker / record["bare"], "show-ref", "--verify", "refs/heads/" + record["branch"], check=False).returncode, 0)

    def test_existing_branch_and_other_employee_survive_failed_dispatch_cleanup(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                branch = "rd/employee_a/existing-" + platform
                original = self.dispatch(branch=branch)
                other = self.root / "worker/users/employee_b/wt/other/evidence.txt"
                other.parent.mkdir(parents=True, exist_ok=True)
                other.write_text("keep")
                with self.assertRaises((SystemExit, ValueError)):
                    self.dispatch(branch=branch)
                failed = rd.all_jobs()[-1]
                self.clean(failed)
                self.assertEqual(other.read_text(), "keep")
                self.assertTrue((self.worker / "wt" / original["job"]).exists())
                self.assertEqual(native.git(self.worker / original["bare"], "rev-parse", branch).stdout.strip(), self.base)
                with patch.dict(os.environ, {"RD_USER_ID": "employee_b"}), self.assertRaises(SystemExit):
                    self.clean(original)
                self.assertTrue(Path(rd.job_path(original["job"])).exists())

    def reports(self, record):
        reports = self.worker / "jobs" / record["job"] / "reports"
        reports.mkdir(parents=True)
        (reports / "nested").mkdir()
        (reports / "nested/repro.py").write_bytes(b"print('repro')\n\x00\xff")
        (reports / "summary.md").write_text("复现报告")
        return reports

    def test_clean_backs_up_entire_reports_tree_before_removing_remote(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                record = self.dispatch()
                reports = self.reports(record)
                expected = (reports / "nested/repro.py").read_bytes()
                self.clean(record)
                backup = self.root / "saved reports" / record["job"]
                self.assertEqual((backup / "nested/repro.py").read_bytes(), expected)
                self.assertEqual((backup / "summary.md").read_text(), "复现报告")
                self.assertFalse(reports.parent.exists())

    def test_failed_backup_keeps_remote_and_record_even_with_force(self):
        record = self.dispatch()
        reports = self.reports(record)
        with patch.object(rd, "backup_reports", side_effect=OSError("disk full")), self.assertRaises(SystemExit):
            self.clean(record)
        self.assertTrue(reports.exists())
        self.assertTrue((self.worker / "wt" / record["job"]).exists())
        self.assertTrue(Path(rd.job_path(record["job"])).exists())
        with patch.object(rd, "backup_reports") as backup:
            self.clean(record, discard_reports=True)
        backup.assert_not_called()
        self.assertFalse(reports.parent.exists())

    def test_reports_symlink_cannot_escape_or_trigger_remote_deletion(self):
        record = self.dispatch()
        reports = self.reports(record)
        outside = self.root / "private.txt"
        outside.write_text("keep")
        (reports / "link").symlink_to(outside)
        with self.assertRaises(SystemExit):
            self.clean(record)
        self.assertTrue(reports.exists())
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((self.root / "saved reports" / record["job"]).exists())

    def test_repeated_backup_keeps_previous_copy(self):
        record = self.dispatch()
        reports = self.reports(record)
        first = Path(rd.backup_reports(rd.job_host(self.cfg, record), record))
        (reports / "summary.md").write_text("第二版")
        second = Path(rd.backup_reports(rd.job_host(self.cfg, record), record))
        self.assertNotEqual(first, second)
        self.assertEqual((first / "summary.md").read_text(), "复现报告")
        self.assertEqual((second / "summary.md").read_text(), "第二版")

    def test_launching_without_pid_cannot_be_cleaned_as_failed_preparation(self):
        record = self.dispatch()
        record["dispatch_state"] = "launching"
        rd.save_job(record)
        (self.worker / "jobs" / record["job"] / "exit_code").unlink()
        with self.assertRaises(SystemExit):
            self.clean(record)
        self.assertTrue((self.worker / "wt" / record["job"]).exists())

    def bulk_args(self, **overrides):
        values = dict(job=None, merged=True, force=False, delete_branch=False, discard_reports=False,
                      initiator_session=None, into=None, dry_run=False, project=None, dir=str(self.repo))
        values.update(overrides)
        return argparse.Namespace(**values)

    def capture(self, command, args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            command(args, self.cfg)
        return output.getvalue()

    def remote_head(self, record, commit=False, fetch=False):
        wt = self.worker / "wt" / record["job"]
        if commit:
            (wt / "result.txt").write_text(record["job"])
            native.git(wt, "add", "result.txt")
            native.git(wt, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                       "commit", "-qm", "fixture result")
        head = native.git(wt, "rev-parse", "HEAD").stdout.strip()
        (self.worker / "jobs" / record["job"] / "head").write_text(head)
        self.remote_head_value = head
        if fetch:
            self.git("fetch", "-q", str(self.worker / record["bare"]), record["branch"])
        return head

    def acknowledge(self, record):
        snapshot = rd.job_states(self.cfg, [record])[record["job"]]
        receipt = rd.collect_result(self.cfg, record, snapshot)
        rd.cmd_ack(argparse.Namespace(job=record["job"], round=receipt["round"],
                                     receipt_id=receipt["receipt_id"],
                                     initiator_session=record.get("initiator_session")), self.cfg)

    def test_source_branch_and_include_paths_are_recorded_independently_of_ref(self):
        source = self.git("symbolic-ref", "--short", "HEAD").strip()
        (self.repo / "extra.txt").write_text("input")
        record = self.dispatch(include=["./extra.txt"])
        self.assertEqual(record["source_branch"], source)
        self.assertEqual(record["include_paths"], ["extra.txt"])
        self.assertEqual(record["base"], self.base)
        self.git("checkout", "--detach", "-q", "HEAD")
        record = self.dispatch()
        self.assertIsNone(record["source_branch"])

    def test_status_unmerged_reports_merged_unmerged_unfetched_and_missing_ref(self):
        merged = self.dispatch(name="merged")
        merged_head = self.remote_head(merged, commit=True, fetch=True)
        self.git("merge", "--ff-only", merged_head)
        unmerged = self.dispatch(name="unmerged", initiator_session="another-session")
        unmerged_head = self.remote_head(unmerged, commit=True, fetch=True)
        unfetched = self.dispatch(name="unfetched")
        self.remote_head(unfetched, commit=True)
        legacy = self.dispatch(name="legacy")
        legacy_head = self.remote_head(legacy, commit=True, fetch=True)
        del legacy["source_branch"]
        rd.save_job(legacy)
        unchanged = self.dispatch(name="unchanged")
        self.remote_head(unchanged)
        foreign = dict(unmerged, job="foreign-job", user_id="employee_b")
        rd.save_job(foreign)
        args = self.bulk_args(unmerged=True, json=True, details=False)
        rows = json.loads(self.capture(rd.cmd_status, args))
        by_job = {r["job"]: r for r in rows}
        self.assertEqual(set(by_job), {unmerged["job"], unfetched["job"], legacy["job"]})
        self.assertIn("未合入", by_job[unmerged["job"]]["merge_status"])
        self.assertIn("未取回", by_job[unfetched["job"]]["merge_status"])
        self.assertIn("需指定 --into", by_job[legacy["job"]]["merge_status"])
        args.into = legacy_head
        rows = json.loads(self.capture(rd.cmd_status, args))
        self.assertNotIn(legacy["job"], [r["job"] for r in rows])
        args.into = unmerged_head
        rows = json.loads(self.capture(rd.cmd_status, args))
        self.assertNotIn(unmerged["job"], [r["job"] for r in rows])
        args.into, args.json = None, False
        self.assertIn("未取回", self.capture(rd.cmd_status, args))

    def test_source_branch_uses_local_branch_even_when_a_tag_has_the_same_name(self):
        source = self.git("symbolic-ref", "HEAD").strip()[len("refs/heads/"):]
        self.git("tag", source)
        record = self.dispatch()
        self.assertEqual(record["source_branch"], source)
        head = self.remote_head(record, commit=True, fetch=True)
        self.git("tag", "-f", source, head)
        self.assertIn("未合入", "；".join(rd.merge_check(record, head, None)))
        self.assertEqual(rd.merge_check(record, head, "refs/tags/" + source), [])

    def test_bulk_scope_filters_current_employee_repository_and_project(self):
        record = self.dispatch()
        self.remote_head(record, commit=True, fetch=True)
        foreign = dict(record, job="foreign-job", user_id="employee_b")
        other_repo = dict(record, job="other-repo", repo="/home/user/other-repo")
        other_project = dict(record, job="other-project", project="other-project")
        for item in (foreign, other_repo, other_project):
            rd.save_job(item)
        self.assertEqual({r["job"] for r in rd.scoped_jobs(self.bulk_args())},
                         {record["job"], other_project["job"]})
        self.assertEqual({r["job"] for r in rd.scoped_jobs(self.bulk_args(dir=None, project=record["project"]))},
                         {record["job"], other_repo["job"]})
        with patch.object(os, "getcwd", return_value=str(self.repo)):
            self.assertEqual({r["job"] for r in rd.scoped_jobs(self.bulk_args(dir=None))},
                             {record["job"], other_project["job"]})
        with patch.object(rd, "job_states", wraps=rd.job_states) as states:
            self.capture(rd.cmd_clean, self.bulk_args(project=record["project"]))
        self.assertEqual([r["job"] for r in states.call_args.args[1]], [record["job"]])

    def test_clean_merged_dry_run_then_preserves_reports_and_branches_on_both_platforms(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                inputs = self.repo / "inputs folder"
                inputs.mkdir(exist_ok=True)
                (inputs / "中文\ninput.txt").write_text("included")
                record = self.dispatch(include=["inputs folder"], initiator_session="previous-session")
                self.remote_head(record)
                self.acknowledge(record)
                reports = self.reports(record)
                wt = self.worker / "wt" / record["job"]
                before = reports.parent
                with patch.object(rd, "backup_reports", wraps=rd.backup_reports) as backup:
                    output = self.capture(rd.cmd_clean, self.bulk_args(dry_run=True))
                backup.assert_not_called()
                self.assertIn("would clean " + record["job"], output)
                self.assertTrue(wt.exists())
                self.assertTrue(before.exists())
                self.assertTrue(Path(rd.job_path(record["job"])).exists())
                self.assertIn("cleaned " + record["job"], self.capture(rd.cmd_clean, self.bulk_args()))
                self.assertFalse(wt.exists())
                self.assertFalse(before.exists())
                self.assertFalse(Path(rd.job_path(record["job"])).exists())
                self.assertEqual(native.git(self.worker / record["bare"], "rev-parse", record["branch"]).stdout.strip(), self.base)
                self.assertEqual((self.root / "saved reports" / record["job"] / "summary.md").read_text(), "复现报告")

    def test_clean_merged_accepts_ancestor_without_rd_tracking_ref_and_old_job_with_into(self):
        record = self.dispatch()
        head = self.remote_head(record, commit=True, fetch=True)
        self.git("merge", "--ff-only", head)
        self.git("commit", "--allow-empty", "-qm", "later local result")
        self.acknowledge(record)
        into = record.pop("source_branch")
        rd.save_job(record)
        output = self.capture(rd.cmd_clean, self.bulk_args())
        self.assertIn("需指定 --into", output)
        self.assertTrue(Path(rd.job_path(record["job"])).exists())
        self.assertIn("cleaned " + record["job"], self.capture(rd.cmd_clean, self.bulk_args(into=into)))

    def age_ack(self, record, hours):
        """把本任务全部回执确认时间推到 hours 小时前。"""
        stamp = time.time() - hours * 3600
        for path in rd.result_path(record, 1).parent.glob("r*.ack.json"):
            os.utime(path, (stamp, stamp))

    def expired(self, **overrides):
        with patch.object(rd, "disk_low", return_value=overrides.pop("low", False)):
            return self.capture(rd.cmd_clean, self.bulk_args(merged=False, expired=True, **overrides))

    def agent_job(self, record):
        record["agent"] = "codex"
        rd.save_job(record)
        return record

    def test_clean_expired_reclaims_command_jobs_once_acknowledged(self):
        record = self.dispatch()
        self.remote_head(record)
        self.assertIn("回执待收取", self.expired())
        self.acknowledge(record)
        self.assertIn("would clean " + record["job"], self.expired(dry_run=True))
        self.assertTrue((self.worker / "wt" / record["job"]).exists())
        output = self.expired()
        self.assertIn("命令任务不能续跑", output)
        self.assertFalse((self.worker / "wt" / record["job"]).exists())
        self.assertFalse(Path(rd.job_path(record["job"])).exists())

    def test_clean_expired_keeps_accepted_agent_work_for_a_day(self):
        record = self.agent_job(self.dispatch())
        self.remote_head(record)
        self.acknowledge(record)
        output = self.expired()
        self.assertIn("keep " + record["job"], output)
        self.assertIn("已合入或无新提交", output)
        self.assertTrue((self.worker / "wt" / record["job"]).exists())
        self.age_ack(record, 25)
        self.assertIn("clean " + record["job"], self.expired())
        self.assertFalse((self.worker / "wt" / record["job"]).exists())

    def test_stop_on_posix_and_native_targets_auto_acknowledges_failed_receipt(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                record = self.dispatch(name="stop-" + platform, initiator_session="sender")
                directory = self.worker / "jobs" / record["job"]
                holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                try:
                    (directory / "exit_code").unlink()
                    (directory / "pid").write_text(str(holder.pid))
                    self.capture(rd.cmd_stop, argparse.Namespace(job=record["job"], initiator_session="stopper"))
                    holder.wait(timeout=5)
                    result = json.loads(self.capture(rd.cmd_result, argparse.Namespace(
                        job=record["job"], initiator_session="sender", round=None)))
                    self.assertEqual(result["report"]["outcome"], "failed")
                    self.assertEqual(result["delivery_state"], "received")
                    self.assertEqual(result["acknowledgement"]["acked_by"], "stopper")
                    self.assertEqual(result["acknowledgement"]["via"], "cli")
                    self.assertEqual(result["review_state"], "pending")
                finally:
                    if holder.poll() is None:
                        holder.kill()
                    holder.wait(timeout=5)

    def test_reject_can_move_to_another_target_with_the_same_harness_and_new_model(self):
        destination = self.root / "other-worker"
        self.cfg.cp.add_section("agent:dsh")
        self.cfg.cp.set("agent:dsh", "model", "--model {model}")
        executable = self.root / "dsh"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        auth = self.root / "dsh-auth.json"
        auth.write_text("{}")
        self.cfg.cp.add_section("host:other")
        for key, value in dict(ssh="local", workdir=str(destination), sync="direct",
                               dsh_auth_file=str(auth), path=str(self.root) + ":" + os.environ["PATH"]).items():
            self.cfg.cp.set("host:other", key, value)
        record = self.agent_job(self.dispatch(name="move", initiator_session="sender"))
        record.update(agent="dsh", isolated=True)
        record["receiver_session"] = "receiver"
        rd.save_job(record)
        old_head = self.remote_head(record, commit=True)
        source_wt = self.worker / "wt" / record["job"]
        (source_wt / "staged.txt").write_text("未提交的暂存内容")
        native.git(source_wt, "add", "staged.txt")
        self.reports(record)
        args = argparse.Namespace(job=record["job"], round=1, reject="请继续实现", review_action=None,
                                  reviewed_by="device", via="serve", initiator_session="sender",
                                  host="other", agent=None, model="new-model", effort=None, session=None,
                                  timeout=None, force_host=False)
        original_shell = self.local_shell
        def shell(host, script, **kwargs):
            if script == rd.PROBE:
                return subprocess.CompletedProcess([], 0, "memfree=90 disk=100", "")
            return original_shell(host, script, **kwargs)
        def finish_with_git(host, rec, *args):
            self.finish(host, rec, *args)
            wt = Path(host["workdir"]) / "wt" / rec["job"]
            directory = Path(host["workdir"]) / "jobs" / rec["job"]
            # A worker must be able to inspect/commit the transferred index.
            native.git(wt, "diff", "--cached", "--binary")
            (directory / "head").write_text(native.git(wt, "rev-parse", "HEAD").stdout.strip())
            (directory / "dirty").write_text(str(len(native.git(wt, "status", "--porcelain").stdout.splitlines())))
        with patch.object(rd, "ssh", side_effect=shell), \
             patch.object(rd, "launch", side_effect=finish_with_git), \
             patch.dict(os.environ, {"PATH": str(self.root) + ":" + os.environ["PATH"]}):
            output = self.capture(rd.cmd_review, args)
        self.assertIn('"review_state": "rejected"', output)
        status = self.queried_job(record["job"])
        self.assertEqual((status["host"], status["rounds"], status["model"], status["agent"]),
                         ("other", 2, "new-model", "dsh"))
        self.assertEqual(status["head"], old_head)
        self.assertEqual(status["review_state"], "pending")
        self.assertEqual(status["dirty"], "1")
        old = json.loads(self.capture(rd.cmd_result, argparse.Namespace(
            job=record["job"], round=1, initiator_session="sender")))
        self.assertEqual(old["review_state"], "rejected")
        self.assertEqual(old["acknowledgement"]["acked_by"], "device")

    def test_native_launch_failure_restores_original_round_and_pending_review(self):
        self.cfg.cp.set("host:fixture", "platform", "windows")
        record = self.agent_job(self.dispatch(name="launch-failure", initiator_session="sender"))
        record.update(isolated=True, receiver_session="receiver")
        rd.save_job(record)
        self.remote_head(record)
        self.reports(record)
        with Path(rd.CONFIG).open("w") as config:
            self.cfg.cp.write(config)
        original_popen = subprocess.Popen
        def popen(command, *args, **kwargs):
            if len(command) > 1 and str(command[1]).endswith("rd_native.py"):
                raise OSError("fixture cannot launch")
            return original_popen(command, *args, **kwargs)
        original_rpc = self.local_rpc
        def rpc(host, operation, data=None, **kwargs):
            if operation == "probe":
                return dict(memfree="90", disk="100")
            if operation == "launch":
                data = dict(data, profile_code=Path(rd.HERE, "rd_profiles.py").read_text(),
                            worker_code=Path(rd.HERE, "rd_native.py").read_text())
            return original_rpc(host, operation, data, **kwargs)
        # Only process creation fails; real worker preparation and queries run.
        with patch.object(rd, "launch", side_effect=self.real_launch), \
             patch.object(native, "request", side_effect=rpc), patch.object(subprocess, "Popen", side_effect=popen), \
             patch.object(sys, "argv", ["rdispatch", "review", record["job"], "--round", "1", "--reject", "请继续",
                                        "--initiator-session", "sender"]), contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as stopped:
                rd.main()
        self.assertEqual(stopped.exception.code, 4)
        self.assertIn("resume_launch_failed", error.getvalue())
        row = self.queried_job(record["job"])
        self.assertEqual((row["rounds"], row["round"], row["review_state"]), (1, "1", "pending"))
        receipt = json.loads(self.capture(rd.cmd_result, argparse.Namespace(
            job=record["job"], round=1, initiator_session="sender")))
        self.assertEqual(receipt["delivery_state"], "pending")

    def test_codex_resume_bootstraps_profile_on_a_target_without_first_round_files(self):
        self.cfg.cp.set("host:fixture", "ssh", "local")
        self.cfg.cp.set("host:fixture", "path", str(self.root) + ":" + os.environ["PATH"])
        executable = self.root / "codex"
        executable.write_text('#!/bin/sh\nprintf \'{"type":"thread.started","thread_id":"receiver"}\\n\'\n')
        executable.chmod(0o755)
        source = self.root / "profile-source"
        source.mkdir()
        (source / "config.toml").write_text('approval_policy="never"\nsandbox_mode="workspace-write"\n')
        (source / "AGENTS.md").write_text("仅执行测试")
        (source / "git.json").write_text('{"name":"Fixture","email":"fixture@example.invalid"}')
        profile, payload = rd.profiles.bundle(source, "employee_a", self.root / "profile-state")
        version = self.worker / "profiles" / profile["fingerprint"]
        version.mkdir(parents=True)
        subprocess.run(["tar", "-xf", "-", "-C", str(version)], input=payload, check=True)
        record = self.agent_job(self.dispatch(name="codex-resume", initiator_session="sender"))
        record.update(profile=profile, receiver_session="receiver")
        rd.save_job(record)
        self.remote_head(record)
        prompt = self.root / "continue.md"
        prompt.write_text("继续")
        args = argparse.Namespace(job=record["job"], initiator_session="sender", session=None,
                                  prompt_file=str(prompt), model=None, effort=None, timeout=10, force_host=True)
        with patch.object(rd, "launch", side_effect=self.real_launch):
            self.capture(rd.cmd_resume, args)
        deadline = time.monotonic() + 5
        while True:
            row = self.queried_job(record["job"])
            if row["state"] == "done":
                break
            if time.monotonic() > deadline:
                self.fail("fixture Codex did not finish")
            time.sleep(0.02)
        self.assertEqual(row["round"], "2")
        self.assertEqual(row["exit"], "0")

    def test_delegated_and_original_acknowledgements_start_the_same_retention_period(self):
        for name, signer in (("ordinary", "sender"), ("delegated", "App MacBook")):
            with self.subTest(signer=signer):
                record = self.agent_job(self.dispatch(name=name, initiator_session="sender"))
                self.remote_head(record)
                old = time.time() - 10 * 24 * 3600
                os.utime(rd.job_path(record["job"]), (old, old))
                snapshot = rd.job_states(self.cfg, [record])[record["job"]]
                receipt = rd.collect_result(self.cfg, record, snapshot)
                rd.cmd_ack(argparse.Namespace(job=record["job"], round=1, receipt_id=receipt["receipt_id"],
                                              initiator_session="sender", acked_by=signer, via="cli"), self.cfg)
                self.assertIn("keep " + record["job"], self.expired())
                self.assertIn("保留期 24 小时", self.expired())
                self.age_ack(record, 25)
                self.assertIn("clean " + record["job"], self.expired())
                self.assertFalse(Path(rd.job_path(record["job"])).exists())

    def test_clean_expired_keeps_unmerged_fetched_work_for_a_week(self):
        record = self.agent_job(self.dispatch())
        self.remote_head(record, commit=True, fetch=True)
        self.acknowledge(record)
        self.age_ack(record, 6 * 24)
        self.assertIn("未合入", self.capture(rd.cmd_clean, self.bulk_args()))
        output = self.expired()
        self.assertIn("keep " + record["job"], output)
        self.assertIn("已取回未合入", output)
        self.age_ack(record, 8 * 24)
        self.assertIn("clean " + record["job"], self.expired())
        self.assertFalse((self.worker / "wt" / record["job"]).exists())
        self.assertEqual(native.git(self.worker / record["bare"], "rev-parse", record["branch"]).stdout.strip(),
                         self.remote_head_value)

    def test_clean_expired_never_reclaims_unfetched_commits(self):
        record = self.agent_job(self.dispatch())
        self.remote_head(record, commit=True)
        self.acknowledge(record)
        self.age_ack(record, 365 * 24)
        self.assertIn("未取回", self.expired(low=True))
        self.assertTrue((self.worker / "wt" / record["job"]).exists())

    def test_clean_expired_respects_keep_and_low_disk(self):
        record = self.agent_job(self.dispatch())
        self.remote_head(record, commit=True, fetch=True)
        self.acknowledge(record)
        self.age_ack(record, 30 * 24)
        rd.cmd_keep(argparse.Namespace(job=record["job"], hours=2), self.cfg)
        self.assertIn("已用 rdispatch keep 钉住", self.expired(low=True))
        rd.cmd_keep(argparse.Namespace(job=record["job"], hours=0), self.cfg)
        self.age_ack(record, 2)
        self.assertIn("keep " + record["job"], self.expired())
        output = self.expired(low=True, dry_run=True)
        self.assertIn("磁盘不足", output)
        self.assertIn("keep " + record["job"], output)
        self.age_ack(record, 25)
        self.assertIn("would clean " + record["job"], self.expired(low=True, dry_run=True))
        self.assertIn("keep " + record["job"], self.expired(dry_run=True))

    def test_retention_hours_follow_config_and_reject_invalid_values(self):
        self.assertEqual(self.cfg.retention_hours(), rd.RETENTION_HOURS)
        self.cfg.cp.add_section("retention")
        self.cfg.cp.set("retention", "unmerged_hours", "48")
        with patch.dict(os.environ, {"RD_RETAIN_ACCEPTED_HOURS": "0"}):
            self.assertEqual(self.cfg.retention_hours(), {"command": 0, "accepted": 0, "unmerged": 48})
        for bad in ("-1", "soon", "nan"):
            self.cfg.cp.set("retention", "unmerged_hours", bad)
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.cfg.retention_hours()

    def test_clean_merged_skips_each_unsafe_state(self):
        record = self.dispatch()
        self.remote_head(record)
        self.acknowledge(record)
        for state, dispatch_state, reason in (
            ("running", "started", "运行中"),
            ("starting", "started", "准备中"),
            ("done", "preparing", "准备中"),
            ("unreachable", "started", "不可达"),
            ("missing", "started", "未确认任务已结束"),
            ("unknown", "started", "未确认任务已结束"),
        ):
            with self.subTest(state=state, dispatch_state=dispatch_state):
                record["dispatch_state"] = dispatch_state
                rd.save_job(record)
                with patch.object(rd, "job_states", return_value={record["job"]: {"state": state}}), \
                     patch.object(rd, "worktree_changes") as changes:
                    self.assertIn(reason, self.capture(rd.cmd_clean, self.bulk_args()))
                changes.assert_not_called()
                self.assertTrue(Path(rd.job_path(record["job"])).exists())

    def test_clean_merged_skips_unfetched_unmerged_invalid_ref_and_unreadable_worktree(self):
        record = self.dispatch()
        self.remote_head(record, commit=True)
        self.acknowledge(record)
        self.assertIn("未取回", self.capture(rd.cmd_clean, self.bulk_args()))
        self.remote_head(record, fetch=True)
        self.assertIn("未合入", self.capture(rd.cmd_clean, self.bulk_args()))
        missing_ref = record["source_branch"] + "-" + record["job"]
        self.assertIn("REF 不存在", self.capture(rd.cmd_clean, self.bulk_args(into=missing_ref)))
        with patch.object(rd, "worktree_changes", side_effect=ValueError("无法核验 worktree")):
            self.assertIn("无法核验 worktree", self.capture(rd.cmd_clean, self.bulk_args()))
        self.assertTrue(Path(rd.job_path(record["job"])).exists())
        self.assertTrue((self.worker / "wt" / record["job"]).exists())

    def test_clean_merged_blocks_current_and_historical_pending_receipts(self):
        record = self.dispatch()
        self.remote_head(record)
        self.assertIn("结果回执待收取", self.capture(rd.cmd_clean, self.bulk_args()))
        # Acknowledging a later round must not discard an earlier unreceived result.
        record["rounds"] = 2
        rd.save_job(record)
        self.finish(rd.job_host(self.cfg, record), record)
        self.acknowledge(record)
        self.assertIn("结果回执待收取", self.capture(rd.cmd_clean, self.bulk_args()))
        receipt = rd.messages.read_json(rd.result_path(record, 1))
        rd.cmd_ack(argparse.Namespace(job=record["job"], round=1, receipt_id=receipt["receipt_id"], initiator_session=None), self.cfg)
        self.assertIn("cleaned ", self.capture(rd.cmd_clean, self.bulk_args()))

    def test_clean_merged_blocks_unexpected_paths_renames_and_legacy_dirty_content(self):
        for platform in ("posix", "windows"):
            with self.subTest(platform=platform):
                self.cfg.cp.set("host:fixture", "platform", platform)
                record = self.dispatch(include=["tracked.txt"])
                self.remote_head(record)
                self.acknowledge(record)
                wt = self.worker / "wt" / record["job"]
                native.git(wt, "mv", "tracked.txt", "outside.txt")
                self.assertIn("非 --include", self.capture(rd.cmd_clean, self.bulk_args()))
                native.git(wt, "reset", "--hard", "HEAD")
                (wt / "tracked.txt").write_text("allowed")
                # Existing jobs without the include record must not infer a whitelist.
                del record["include_paths"]
                rd.save_job(record)
                self.assertIn("非 --include", self.capture(rd.cmd_clean, self.bulk_args()))
                self.assertTrue(wt.exists())

    def test_clean_merged_legacy_without_pending_result_and_failed_backup(self):
        record = self.dispatch()
        record.pop("handoff_version")
        record.pop("source_branch")
        rd.save_job(record)
        self.remote_head(record)
        self.assertIn("需指定 --into", self.capture(rd.cmd_clean, self.bulk_args()))
        with patch.object(rd, "backup_reports", side_effect=OSError("fixture disk full")):
            self.assertIn("清理失败", self.capture(rd.cmd_clean, self.bulk_args(into=record["base"])))
        self.assertTrue(Path(rd.job_path(record["job"])).exists())
        self.assertTrue((self.worker / "wt" / record["job"]).exists())
        self.assertIn("cleaned ", self.capture(rd.cmd_clean, self.bulk_args(into=record["base"])))

    def test_bulk_cli_rejects_unsafe_combinations(self):
        for option in (dict(force=True), dict(delete_branch=True), dict(discard_reports=True), dict(job="fixture-job")):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                rd.cmd_clean(self.bulk_args(**option), self.cfg)


if __name__ == "__main__":
    unittest.main()
