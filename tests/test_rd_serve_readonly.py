"""serve 在线时 inbox/status 走进程内缓存：输出与 CLI 直跑一致，不起子进程、不碰 ssh。"""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_serve
from remote_dispatch.rd_serve import Service
from tests import test_rd_serve as support


class CachedReadTests(unittest.TestCase):
    eventually = support.ServeTests.eventually

    def setUp(self):
        support.ServeTests.setUp(self)
        self.config.write_text("[host:target]\nssh=local\nworkdir=%s/worker\nsync=direct\n" % self.root,
                               encoding="utf-8")
        for target in (patch.object(cli, "probe", lambda host: dict(host=host["name"], reachable=True)),
                       patch.object(cli, "assess", lambda host, info: [])):
            target.start()
            self.addCleanup(target.stop)
        self.states = {}
        self.state_calls = []
        stub = patch.object(cli, "job_states", self.job_states)
        stub.start()
        self.addCleanup(stub.stop)
        self.absent = str(self.root / "absent.sock")

    def job_states(self, cfg, recs):
        self.state_calls.append([rec["job"] for rec in recs])
        return {rec["job"]: dict(self.states.get(rec["job"], dict(state="running", round=str(rec["rounds"]))))
                for rec in recs}

    def job(self, name, state="done", owner="owner", **extra):
        rec = dict(job=name, user_id="fixture", host="target", rounds=1, project="demo", origin="",
                   initiator_session=owner, agent=None, sync="direct", base="base", branch="rd/" + name,
                   created="2026-10-25 00:00:00", **extra)
        cli.save_job(rec)
        self.states[name] = dict(state=state, round="1", exit="0" if state == "done" else "",
                                 head="base", dirty="0", observed_at="2026-10-25T00:00:01Z", last="工作中")
        return rec

    def serve(self):
        service = Service(cli.Config(), self.path, poll_interval=3600)
        service.start()
        self.addCleanup(service.close)
        self.eventually(lambda: service.collector.last_cycle_seconds, bool)
        self.eventually(lambda: service.reports.ready.is_set(), bool)
        return service

    def run_cli(self, *argv, serve=True):
        out, err = io.StringIO(), io.StringIO()
        socket = str(self.path) if serve else self.absent
        with patch.dict(os.environ, {"RD_SERVE_SOCKET": socket}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                cli.main(list(argv))
                code = 0
            except SystemExit as exit_:
                code = exit_.code or 0
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def no_remote(self, service):
        """serve 路径下既不能起子进程执行命令，也不能触发 ssh 或批量状态查询。"""
        before = len(self.state_calls)
        with patch.object(Service, "execute", side_effect=AssertionError("子进程执行")), \
                patch.object(cli, "ssh", side_effect=AssertionError("同步 ssh")):
            yield
        self.assertEqual(len(self.state_calls), before)

    def seed(self):
        self.job("job-done")
        self.job("job-running", "running")
        self.job("job-other", owner="other-owner")
        return self.serve()

    def test_status_text_and_json_match_direct_cli_without_remote_calls(self):
        service = self.seed()
        for argv in (("status",), ("status", "--json"), ("status", "job-done"), ("status", "job-running", "--json")):
            with self.subTest(argv=argv):
                direct = self.run_cli(*argv, serve=False)
                with self.no_remote(service):
                    cached = self.run_cli(*argv)
                self.assertEqual(cached, direct)
                self.assertEqual(cached[0], 0)
                self.assertTrue(cached[1])

    def test_inbox_matches_direct_cli_without_remote_calls_and_lists_unreachable(self):
        self.job("job-gone", "unreachable")
        service = self.seed()
        self.states["job-gone"] = dict(state="unreachable")
        direct = self.run_cli("inbox", "--initiator-session", "owner", serve=False)
        with self.no_remote(service):
            cached = self.run_cli("inbox", "--initiator-session", "owner")
        self.assertEqual(cached, direct)
        body = json.loads(cached[1])
        self.assertEqual([row["job"] for row in body["results"]], ["job-done"])
        self.assertEqual(body["initiator_session"], "owner")

    def test_acknowledged_and_delegated_receipts_match_direct_cli(self):
        service = self.seed()
        receipt = json.loads(self.run_cli("result", "job-done", "--initiator-session", "owner", serve=False)[1])
        self.assertEqual(self.run_cli("ack", "job-done", "--round", "1", "--receipt-id", receipt["receipt_id"],
                                      "--initiator-session", "owner", "--acked-by", "someone-else", serve=False)[0], 0)
        direct = self.run_cli("inbox", "--initiator-session", "owner", serve=False)
        with self.no_remote(service):
            cached = self.run_cli("inbox", "--initiator-session", "owner")
        self.assertEqual(cached, direct)
        self.assertTrue(json.loads(cached[1])["results"][0].get("delegated"))

    def test_repeated_queries_are_fast_and_never_touch_the_remote(self):
        service = self.seed()
        with self.no_remote(service):
            started = datetime.now()
            for _ in range(20):
                self.assertEqual(self.run_cli("status")[0], 0)
            self.assertLess((datetime.now() - started).total_seconds(), 5)

    def test_stale_snapshot_falls_back_to_the_direct_path(self):
        service = self.seed()
        self.job("job-late", "running")
        # 新登记、尚无缓存快照：缓存路径放弃，由直跑路径查询。
        before = len(self.state_calls)
        code, out, err = self.run_cli("status", "job-late")
        self.assertEqual(code, 0)
        self.assertIn("job-late", out)
        self.assertGreater(len(self.state_calls), before)
        # 快照超过 2 个轮询周期：同样回退（直跑再次查询目标机）。
        future = datetime.now(timezone.utc) + timedelta(seconds=3600 * 3)
        service.cached_reads.clock = lambda: future.timestamp()
        before = len(self.state_calls)
        self.assertEqual(self.run_cli("status", "job-running")[0], 0)
        self.assertGreater(len(self.state_calls), before)
        reply = service.cached_reads.handle(dict(command="status", params=dict(job="job-running", json=False),
                                                 user_id="fixture", config=str(self.config.resolve())))
        self.assertEqual(reply, dict(handled=False))

    def test_settled_terminal_job_is_served_from_cache_however_old(self):
        service = self.seed()
        record = cli.load_job("job-done")
        self.assertTrue(service.collector.is_settled(record))
        future = datetime.now(timezone.utc) + timedelta(days=3)
        service.cached_reads.clock = lambda: future.timestamp()
        reply = service.cached_reads.handle(dict(command="status", params=dict(job="job-done", json=False),
                                                 user_id="fixture", config=str(self.config.resolve())))
        self.assertTrue(reply["handled"])
        self.assertIn("job-done", reply["stdout"])

    def test_terminal_job_without_a_saved_receipt_falls_back_so_the_cli_can_collect_it(self):
        self.job("job-done")
        with patch.object(cli, "collect_result", side_effect=ValueError("暂不可用")), \
                contextlib.redirect_stderr(io.StringIO()):
            service = self.serve()
            service.collector.cycle(rd_serve.events.EventLog(self.state))
        # 回执没落盘，快照也不发布 -> 缓存路径必须放弃
        reply = service.cached_reads.handle(dict(command="inbox", params=dict(initiator_session="owner"),
                                                 user_id="fixture", config=str(self.config.resolve())))
        self.assertEqual(reply, dict(handled=False))

    def restart(self):
        self.service_stop()
        service = Service(cli.Config(), self.path, poll_interval=3600)
        service.start()
        self.addCleanup(service.close)
        self.eventually(lambda: service.collector.last_cycle_seconds, bool)
        return service

    def service_stop(self):
        self.running.close()

    def test_acknowledged_job_is_still_served_from_cache_after_restart_with_identical_output(self):
        self.running = self.seed()
        rec = cli.load_job("job-done")
        receipt = cli.messages.read_json(cli.result_path(rec, 1))
        cli.acknowledge_receipt(rec, receipt, "owner", "cli")
        self.running.collector.cycle(rd_serve.events.EventLog(self.state))
        service = self.restart()
        self.assertTrue(service.job_refresh_complete(rec))  # 补采一次后不再轮询它
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        service.cached_reads.clock = lambda: future.timestamp()
        direct = self.run_cli("status", "job-done", serve=False)
        with self.no_remote(service):
            self.assertEqual(self.run_cli("status", "job-done"), direct)
        # 重启后终态快照缺 last／network_sample，已按旧格式补采一次：--json 也由缓存回答。
        direct = self.run_cli("status", "job-done", "--json", serve=False)
        with self.no_remote(service):
            self.assertEqual(self.run_cli("status", "job-done", "--json"), direct)

    def test_snapshot_restored_in_the_old_subset_format_is_not_used_for_cached_output(self):
        self.running = self.seed()
        self.running.close()
        path = self.state / "serve-snapshots" / "job-running.json"
        saved = json.loads(path.read_text())
        saved.pop("raw_round")
        saved["snapshot"] = {key: saved["snapshot"][key] for key in ("state", "round", "exit", "snapshot_at")}
        path.write_text(json.dumps(saved))
        service = Service(cli.Config(), self.path, poll_interval=3600)  # 只恢复，不启动采集
        self.assertNotIn("job-running", service.complete_snapshots)
        self.assertNotIn("job-done", service.complete_snapshots)  # 终态缺 last／network_sample：待补采
        reply = service.cached_reads.handle(dict(command="status", params=dict(job="job-running", json=False),
                                                 user_id="fixture", config=str(self.config.resolve())))
        self.assertEqual(reply, dict(handled=False))

    def legacy_format(self):
        for path in (self.state / "serve-snapshots").glob("*.json"):
            saved = json.loads(path.read_text())
            saved.pop("raw_round", None)
            saved["snapshot"] = {key: saved["snapshot"][key] for key in
                                 ("state", "round", "exit", "head", "dirty", "started_at", "snapshot_at")
                                 if key in saved["snapshot"]}
            path.write_text(json.dumps(saved))

    def acknowledged_legacy_service(self):
        self.running = self.seed()
        rec = cli.load_job("job-done")
        cli.acknowledge_receipt(rec, cli.messages.read_json(cli.result_path(rec, 1)), "owner", "cli")
        self.running.close()
        self.legacy_format()
        return rec

    def test_legacy_acknowledged_job_is_collected_once_then_served_from_cache(self):
        rec = self.acknowledged_legacy_service()
        service = self.restart_service()
        self.assertTrue(service.job_refresh_complete(rec))  # 升级补采一次后恢复降频
        self.assertIn("job-done", service.complete_snapshots)
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        service.cached_reads.clock = lambda: future.timestamp()
        direct = self.run_cli("status", "job-done", serve=False)
        with self.no_remote(service):
            self.assertEqual(self.run_cli("status", "job-done"), direct)

    def restart_service(self):
        service = Service(cli.Config(), self.path, poll_interval=3600)
        self.assertNotIn("job-done", service.complete_snapshots)
        self.assertFalse(service.job_refresh_complete(cli.load_job("job-done")))
        service.start()
        self.addCleanup(service.close)
        self.eventually(lambda: service.collector.last_cycle_seconds, bool)
        self.eventually(lambda: "job-done" in service.complete_snapshots, bool)
        return service

    def test_legacy_job_on_unreachable_host_keeps_its_terminal_snapshot(self):
        self.acknowledged_legacy_service()
        self.states["job-done"] = dict(state="unreachable")
        service = Service(cli.Config(), self.path, poll_interval=3600)
        service.start()
        self.addCleanup(service.close)
        self.eventually(lambda: service.collector.last_cycle_seconds, bool)
        self.assertEqual(service.snapshots["job-done"]["state"], "done")

    def test_persisted_snapshot_has_no_remote_output_line_and_tokens_are_redacted(self):
        self.job("job-secret")
        self.states["job-secret"]["last"] = "key sk-abcdefghijklmnopqrstuvwx"
        self.states["job-secret"]["network_sample"] = {"x": 1}
        service = self.serve()
        saved = json.loads((self.state / "serve-snapshots" / "job-secret.json").read_text())
        self.assertNotIn("last", saved["snapshot"])
        self.assertNotIn("network_sample", saved["snapshot"])
        self.assertNotIn("sk-abcdefgh", json.dumps(saved))
        # 实采的内存快照仍带 last，running 时 CLI 文本输出依赖它。
        self.assertIn("last", service.snapshots["job-secret"])

    def test_common_token_shapes_are_redacted_by_the_shared_redactor(self):
        service = self.serve()
        for token in ("sk-ant-api03-abcdefghijklmnop", "ghp_" + "a" * 36, "github_pat_" + "B" * 30,
                      "xoxb-1234567890-abcdefghij", "AKIAABCDEFGHIJKLMNOP"):
            self.assertEqual(service.redact("see " + token + " ok"), "see [redacted] ok", token)
        self.assertEqual(service.redact("Authorization: Bearer abcDEF123456.xyz"), "Authorization: [redacted]")
        self.assertEqual(service.redact("normal text sk-short"), "normal text sk-short")

    def test_dispatch_ids_with_token_like_fragments_survive_in_identifier_fields_and_free_text(self):
        service = self.serve()
        service.sensitive = {"devserver"}
        jid = "weicoz-pk-arena-fix-login-20261104-1200"
        ids = [jid, "rd/weicoz-sk-learn-fix-20261104-1200", "weicoz-rk-ops-deploy-nightly-20261104-1200",
               "weicoz-devserver-fix-20261104-1200"]
        body = dict(jobs=ids, rows=[dict(job=i, branch="rd/" + i, source_branch=i, last="ran " + i + " on devserver",
                                         summary="see " + i) for i in ids], worker_root="/srv/devserver/rd")
        out = service.redact(body)
        self.assertEqual(out["jobs"], ids)
        for row, i in zip(out["rows"], ids):
            self.assertEqual((row["job"], row["branch"], row["source_branch"]), (i, "rd/" + i, i))
            self.assertIn(i, row["summary"])
            self.assertEqual(row["last"], "ran " + i + " on [redacted]")
        self.assertEqual(out["worker_root"], "/srv/[redacted]/rd")  # 路径按自由文本处理

    def test_real_tokens_are_redacted_in_free_text_and_nested_non_identifier_fields(self):
        service = self.serve()
        service.sensitive = {"devserver"}
        tokens = ["sk-ant-api03-abcdefghijklmnop", "sk-proj-abcdefghijklmnopqr", "ghp_" + "a" * 36,
                  "github_pat_" + "B" * 30, "Bearer abcDEF123456", "xoxb-1234567890-abcdefghij", "AKIAABCDEFGHIJKLMNOP",
                  "sk-" + "a1" * 12]
        out = service.redact(dict(job="j", items=[dict(deep=dict(text="x " + t + " y")) for t in tokens]))
        self.assertEqual(out["job"], "j")
        for item in out["items"]:
            self.assertEqual(item["deep"]["text"], "x [redacted] y")
        self.assertEqual(service.redact("pk-arena-fix-login-20261104-1200 and weicoz-pk-arena-fix-login-20261104-1200"),
                         "[redacted] and weicoz-pk-arena-fix-login-20261104-1200")

    def test_persisted_snapshot_keeps_token_like_job_branch(self):
        self.job("weicoz-pk-arena-fix-20261104-1200")
        service = self.serve()
        saved = json.loads((self.state / "serve-snapshots" / "weicoz-pk-arena-fix-20261104-1200.json").read_text())
        self.assertEqual(saved["snapshot"]["state"], service.snapshots["weicoz-pk-arena-fix-20261104-1200"]["state"])

    def test_options_the_cache_cannot_reproduce_stay_on_the_original_path(self):
        parser_args = type("Args", (), {})
        for command, flags in (("status", dict(job=None, json=False, details=True, unmerged=False, into=None,
                                              project=None, dir=None, top_dir=None)),
                               ("status", dict(job=None, json=False, details=False, unmerged=True, into=None,
                                              project=None, dir=None, top_dir=None)),
                               ("status", dict(job=None, json=False, details=False, unmerged=False, into=None,
                                              project="demo", dir=None, top_dir=None)),
                               ("inbox", dict(wait=True, orphans=False, initiator_session="owner")),
                               ("inbox", dict(wait=False, orphans=True, initiator_session="owner"))):
            args = parser_args()
            args.command = command
            for key, value in flags.items():
                setattr(args, key, value)
            self.assertIsNone(rd_serve.cached_read_params(args), (command, flags))

    def test_serve_offline_returns_none_so_direct_path_runs(self):
        self.job("job-done")
        code, out, err = self.run_cli("status", serve=False)
        self.assertEqual(code, 0)
        self.assertIn("job-done", out)

    def test_rejects_other_user_or_config(self):
        service = self.seed()
        with self.assertRaises(ValueError):
            service.cached_reads.handle(dict(command="status", params={}, user_id="someone", config=str(self.config)))
        with self.assertRaises(ValueError):
            service.cached_reads.handle(dict(command="status", params={}, user_id="fixture", config="/elsewhere"))


if __name__ == "__main__":
    unittest.main()
