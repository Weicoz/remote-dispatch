"""herdr-core（hc）App 会话后端：全部用 tests/fixtures/fake_hc.py，经 PATH 注入，不接触真实 hc。"""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_hc
from remote_dispatch.rd_projects import ProjectIndex
from tests import test_rd_serve as base
from tests import test_rd_serve_sessions as sessions_base

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_hc.py"


class HcTests(unittest.TestCase):
    setUp_sessions = sessions_base.SessionTests.setUp
    request = sessions_base.SessionTests.request
    eventually = sessions_base.SessionTests.eventually
    agent = sessions_base.SessionTests.agent
    save_fixture = sessions_base.SessionTests.save_fixture
    sessions = sessions_base.SessionTests.sessions
    assert_structure = base.ServeTests.assert_structure

    def setUp(self):
        self.setUp_sessions()
        self.hc_data = dict(seq=14, terminals=[self.terminal("t8", "claude", "working")], hold=True)
        self.hc_fixture = self.root / "hc.json"
        self.hc_log = self.root / "hc.log"
        self.hc_log.write_text("")
        self.save_hc()
        wrapper = self.bin / "hc"
        wrapper.write_text("#!/bin/sh\nexec " + sys.executable + " " + str(FAKE) + ' "$@"\n')
        wrapper.chmod(0o700)
        env = patch.dict(os.environ, {"RD_HC_BIN": "hc", "FAKE_HC": str(self.hc_fixture),
                                      "FAKE_HC_LOG": str(self.hc_log)})
        env.start()
        self.addCleanup(env.stop)

    def terminal(self, address, name, status, cwd=None, **fields):
        record = dict(address=address, argv=[name], cols=120, rows=40, created_ms=1793875864933,
                      cwd=str(cwd or self.repo), state="live", status_ms=1793875900000,
                      foreground=dict(argv=[name], cwd=str(cwd or self.repo), name=name, pid=1),
                      labels={}, status=None if status is None else dict(state=status, app=name))
        record.update(fields)
        return record

    def save_hc(self):
        staging = self.hc_fixture.with_suffix(".tmp")
        staging.write_text(json.dumps(self.hc_data))
        staging.replace(self.hc_fixture)

    def start_hc(self, expected, **options):
        service = base.ServeTests.start(self, poll_interval=options.pop("poll_interval", 3600), **options)
        self.eventually(self.sessions, lambda rows: {row["id"] for row in rows} >= set(expected))
        return service

    def by_id(self):
        return {row["id"]: row for row in self.sessions()}

    def calls(self):
        return [json.loads(line) for line in self.hc_log.read_text().splitlines()]

    def event(self, seq, **params):
        return json.dumps({"jsonrpc": "2.0", "method": "event", "params": dict(seq=seq, **params)})

    def test_discovery_lists_app_sessions_next_to_tui_with_surface_fields(self):
        self.hc_data["terminals"] += [self.terminal("t9", "zsh", None),
                                      self.terminal("vps/t7", "codex", "idle")]
        self.hc_data["ls_extra"] = [self.terminal("t10", "claude", "blocked")]
        self.save_hc()
        self.start_hc(["w1:p1", "hc:t8", "hc:vps/t7", "hc:t10"])
        rows = self.by_id()
        # 普通 shell 不是 harness，不列出，与 TUI 后端同口径。
        self.assertEqual(set(rows), {"w1:p1", "hc:t8", "hc:vps/t7", "hc:t10"})
        app = rows["hc:t8"]
        self.assertEqual((app["surface"], app["address"], app["pane_id"], app["capabilities"]),
                         ("app", "t8", None, ["screen"]))
        self.assertEqual((app["harness"], app["status"], app["project"]), ("claude", "working", "demo"))
        self.assertEqual(rows["hc:vps/t7"]["address"], "vps/t7")
        self.assertEqual(rows["hc:t10"]["status"], "blocked")
        tui = rows["w1:p1"]
        self.assertEqual((tui["surface"], tui["address"], tui["capabilities"], tui["needs_attention_count"]),
                         ("tui", "w1:p1", ["screen", "input", "terminal_ws"], 0))
        project = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual({row["id"] for row in project["sessions"]}, set(rows))
        example = json.loads((base.CONTRACTS / "sessions-app.json").read_text())
        self.assert_structure({"sessions": [app]}, {"sessions": example["response"]["body"]["sessions"]})
        # 只做只读调用。
        self.assertTrue({call[0] for call in self.calls()} <= {"events", "ls", "get", "read"})

    def test_status_mapping_table(self):
        self.assertEqual({name: rd_hc.map_status(dict(state=name)) for name in
                          ("working", "blocked", "done", "idle", "error", "never-seen")},
                         dict(working="working", blocked="blocked", done="done", idle="idle",
                              error="unknown", **{"never-seen": "unknown"}))
        self.assertEqual((rd_hc.map_status(None), rd_hc.map_status({})), ("unknown", "unknown"))
        self.hc_data["terminals"] = [self.terminal("t%d" % (20 + index), "codex", state)
                                     for index, state in enumerate(("done", "error", None))]
        self.save_hc()
        self.start_hc(["hc:t20", "hc:t21", "hc:t22"])
        rows = self.by_id()
        self.assertEqual([rows["hc:t%d" % n]["status"] for n in (20, 21, 22)], ["done", "unknown", "unknown"])
        self.assertTrue(rows["hc:t20"]["has_new_reply"])

    def test_incremental_event_refreshes_terminal_and_wakes_collection(self):
        gate = self.root / "gate"
        self.hc_data.update(gate=str(gate), lines=[
            json.dumps({"jsonrpc": "2.0", "method": "heartbeat", "params": {"seq": 14}}),
            self.event(15, kind="status", terminal="t8")])
        self.save_hc()
        self.start_hc(["hc:t8"])
        self.assertEqual(self.by_id()["hc:t8"]["status"], "working")
        # hc get 读到的新状态；事件本身只带地址。
        self.hc_data["terminals"] = [self.terminal("t8", "claude", "blocked")]
        self.save_hc()
        gate.write_text("")
        row = self.eventually(self.by_id, lambda rows: rows["hc:t8"]["status"] == "blocked")["hc:t8"]
        self.assertEqual(row["status_text"], "等待输入")
        self.assertIn(["get", "t8", "--json"], self.calls())

    def test_embedded_terminal_event_applies_and_stale_seq_is_ignored(self):
        gate = self.root / "gate"
        stale = self.terminal("t8", "claude", "blocked")
        fresh = self.terminal("t8", "claude", "idle")
        self.hc_data.update(gate=str(gate), lines=[
            self.event(14, kind="status", terminal=stale),
            self.event(16, kind="status", terminal=fresh)])
        self.save_hc()
        self.start_hc(["hc:t8"])
        gate.write_text("")
        self.eventually(self.by_id, lambda rows: rows["hc:t8"]["status"] == "idle")
        self.assertNotIn(["get", "t8", "--json"], self.calls())

    def test_removed_event_drops_session(self):
        gate = self.root / "gate"
        self.hc_data.update(gate=str(gate), lines=[self.event(15, kind="removed", terminal="t8")])
        self.save_hc()
        self.start_hc(["hc:t8"])
        gate.write_text("")
        self.eventually(self.by_id, lambda rows: "hc:t8" not in rows)

    def test_stream_loss_reconnects_and_aligns_to_fresh_snapshot(self):
        self.hc_data["hold"] = False
        self.save_hc()
        self.start_hc(["hc:t8"])
        self.hc_data["terminals"] = [self.terminal("t8", "claude", "idle"), self.terminal("t11", "claude", "working")]
        self.hc_data["seq"] = 40
        self.save_hc()
        rows = self.eventually(self.by_id, lambda rows: "hc:t11" in rows and rows["hc:t8"]["status"] == "idle")
        self.assertEqual(rows["hc:t11"]["status"], "working")
        self.assertGreaterEqual(sum(call[:1] == ["events"] for call in self.calls()), 2)

    def test_core_unavailable_hides_app_sessions_but_keeps_tui(self):
        self.hc_data["unavailable"] = True
        self.save_hc()
        self.start(poll_interval=3600)
        self.assertEqual(list(self.by_id()), ["w1:p1"])
        time.sleep(0.3)
        self.assertEqual(list(self.by_id()), ["w1:p1"])
        # core 恢复后自动并入。
        self.hc_data["unavailable"] = False
        self.save_hc()
        service = self.eventually(lambda: self.sessions_after_wake(), lambda rows: "hc:t8" in rows)
        self.assertIn("hc:t8", service)

    def sessions_after_wake(self):
        self.service.wake.sessions.set()
        return self.by_id()

    def start(self, **options):
        self.service = base.ServeTests.start(self, **options)
        self.eventually(self.sessions, bool)
        return self.service

    def test_missing_hc_binary_leaves_tui_backend_working(self):
        with patch.dict(os.environ, {"RD_HC_BIN": str(self.root / "no-such-hc")}):
            self.start(poll_interval=3600)
            self.assertEqual(list(self.by_id()), ["w1:p1"])
            self.assertEqual(self.service.sessions.hc.status()["available"], False)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "no-such-hc").exists())

    def test_unsupported_surface_returns_409_for_input_terminal_and_ws(self):
        self.hc_data["terminals"].append(self.terminal("vps/t7", "codex", "idle"))
        self.save_hc()
        self.start_hc(["hc:t8", "hc:vps/t7"])
        example = json.loads((base.CONTRACTS / "sessions-unsupported-surface.json").read_text())
        expected = (example["response"]["status"], example["response"]["body"])
        self.assertEqual(expected, (409, {"error": "unsupported_surface", "surface": "app"}))
        headers = {"Content-Type": "application/json", "Idempotency-Key": "k1"}
        for identity in ("hc:t8", "hc:vps%2Ft7"):
            self.assertEqual(self.request("/v1/sessions/%s/input" % identity, "POST",
                                          json.dumps({"text": "x"}), headers), expected)
            self.assertEqual(self.request("/v1/sessions/%s/terminal" % identity), expected)
            self.assertEqual(self.request("/v1/sessions/%s/terminal/ws" % identity), expected)
        self.assertEqual(self.request("/v1/sessions/hc:t99/input", "POST", json.dumps({"text": "x"}), headers)[0], 404)
        # 从未向 hc 写入：只有读命令。
        self.assertTrue({call[0] for call in self.calls()} <= {"events", "ls", "get", "read"})

    def test_screen_reads_through_hc_read_with_event_triggered_refresh_and_debounce(self):
        gate = self.root / "gate"
        self.hc_data.update(gate=str(gate), lines=[self.event(15, kind="title", terminal="t8")],
                            screens={"t8": "first screen"})
        self.save_hc()
        self.start_hc(["hc:t8"])
        status, screen = self.request("/v1/sessions/hc:t8/screen")
        self.assertEqual((status, screen["text"], screen["width"], screen["height"], screen["cursor"]),
                         (200, "first screen", 120, 40, None))
        self.assertEqual((screen["id"], screen["pane_id"], screen["address"], screen["surface"]),
                         ("hc:t8", None, "t8", "app"))
        # 去抖且无事件：屏幕内容变了也先服务缓存，不重复起 hc read。
        self.hc_data["screens"] = {"t8": "second screen"}
        self.save_hc()
        self.assertEqual(self.request("/v1/sessions/hc:t8/screen")[1]["text"], "first screen")
        self.assertEqual(sum(call[:1] == ["read"] for call in self.calls()), 1)
        # 该终端出现变化事件，且过了去抖窗口，才重读。
        time.sleep(0.4)
        gate.write_text("")
        self.eventually(lambda: self.request("/v1/sessions/hc:t8/screen")[1]["text"],
                        lambda text: text == "second screen")
        self.assertEqual(self.request("/v1/sessions/hc:t99/screen")[0], 404)

    def test_screen_redacts_secrets(self):
        self.hc_data["screens"] = {"t8": "access_token=app-private\nok"}
        self.save_hc()
        self.start_hc(["hc:t8"])
        text = self.request("/v1/sessions/hc:t8/screen")[1]["text"]
        self.assertNotIn("app-private", text)

    def attention_job(self, job, state, **initiator):
        rec = dict(job=job, user_id="fixture", project="demo", origin="https://example.test/user/demo.git",
                   host="target", rounds=1, agent="codex", created="2026-10-25 00:00:00", **initiator)
        ProjectIndex(self.state, "fixture").register(rec)
        self.job_states[job] = dict(state=state, round="1")

    def test_needs_attention_count_follows_ownership_rules(self):
        self.config.write_text("[host:target]\nssh=local\nworkdir=%s\nsync=direct\n" % (self.root / "worker"))
        self.job_states = {}
        boundary = patch.object(cli, "job_states", side_effect=lambda cfg, recs: {
            rec["job"]: self.job_states.get(rec["job"], {"state": "running", "round": str(rec["rounds"])})
            for rec in recs})
        boundary.start()
        self.addCleanup(boundary.stop)
        self.hc_data["terminals"].append(self.terminal("t10", "claude", "idle"))
        self.save_hc()
        # 两个 ID 都有：TUI 窗格在线，归 TUI；窗格已关闭，回落到 hc 终端。
        self.attention_job("both-open", "unreachable", initiator_herdr={"pane_id": "w1:p1"}, initiator_hc={"terminal": "t8"})
        self.attention_job("both-closed", "dead", initiator_herdr={"pane_id": "w9:p9"}, initiator_hc={"terminal": "t8"})
        self.attention_job("hc-only", "missing", initiator_hc={"terminal": "t8", "machine": None})
        self.attention_job("running", "running", initiator_hc={"terminal": "t8"})
        self.attention_job("other-terminal", "unreachable", initiator_hc={"terminal": "t10x"})
        self.attention_job("tui-only", "unreachable", initiator_herdr={"pane_id": "w1:p1"})
        self.start_hc(["hc:t8", "hc:t10"])
        expected = {"w1:p1": 2, "hc:t8": 2, "hc:t10": 0}
        rows = self.eventually(self.by_id, lambda rows: {k: rows[k]["needs_attention_count"] for k in expected} == expected)
        project = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual({row["id"]: row["needs_attention_count"] for row in project["sessions"]}, expected)
        self.assertEqual(project["needs_attention_count"], 5)
        self.assertEqual(rows["hc:t10"]["needs_attention_count"], 0)

    def test_initiator_primary_decides_ownership_and_old_records_keep_herdr_first(self):
        self.config.write_text("[host:target]\nssh=local\nworkdir=%s\nsync=direct\n" % (self.root / "worker"))
        self.job_states = {}
        boundary = patch.object(cli, "job_states", side_effect=lambda cfg, recs: {
            rec["job"]: self.job_states.get(rec["job"], {"state": "running", "round": "1"}) for rec in recs})
        boundary.start()
        self.addCleanup(boundary.stop)
        both = dict(initiator_herdr={"pane_id": "w1:p1"}, initiator_hc={"terminal": "t8"})
        self.attention_job("app-inner", "unreachable", initiator_primary="hc", **both)
        self.attention_job("tui-inner", "unreachable", initiator_primary="herdr", **both)
        self.attention_job("old-record", "unreachable", **both)
        self.attention_job("hc-primary-pane-gone", "unreachable", initiator_primary="hc",
                           initiator_herdr={"pane_id": "w9:p9"}, initiator_hc={"terminal": "t8"})
        self.attention_job("herdr-primary-pane-gone", "unreachable", initiator_primary="herdr",
                           initiator_herdr={"pane_id": "w9:p9"}, initiator_hc={"terminal": "t8"})
        self.attention_job("resolve-hc", "running", initiator_session="gone", initiator_primary="hc", **both)
        self.attention_job("resolve-old", "running", initiator_session="gone", **both)
        self.start_hc(["hc:t8"])
        expected = {"w1:p1": 2, "hc:t8": 3}
        self.eventually(self.by_id, lambda rows: {k: rows[k]["needs_attention_count"] for k in expected} == expected)
        by_hc = self.request("/v1/jobs/resolve-hc/session")[1]
        by_old = self.request("/v1/jobs/resolve-old/session")[1]
        self.assertEqual((by_hc["session"]["id"], by_hc["matched_by"]), ("hc:t8", "terminal"))
        self.assertEqual((by_old["session"]["id"], by_old["matched_by"]), ("w1:p1", "pane_id"))

    def test_summary_counts_working_sessions_from_tui_and_app(self):
        self.data["agents"] = [self.agent("w1:p1", "working"), self.agent("w1:p2", "idle")]
        self.save_fixture()
        self.hc_data["terminals"] = [self.terminal("t8", "claude", "working"), self.terminal("t10", "codex", "working"),
                                     self.terminal("t12", "codex", "blocked")]
        self.save_hc()
        self.start_hc(["w1:p2", "hc:t8", "hc:t10", "hc:t12"])
        body = self.request("/v1/projects")[1]
        self.assertEqual(body["summary"]["sessions_working"], 3)
        self.assertEqual(body["summary"], dict(needs_attention=0, in_progress=0, review_pending=0, sessions_working=3))

    def test_dispatch_defaults_and_messages_are_unsupported_for_app_sessions(self):
        self.hc_data["terminals"].append(self.terminal("vps/t7", "codex", "idle"))
        self.save_hc()
        self.start_hc(["hc:t8", "hc:vps/t7"])
        expected = (409, {"error": "unsupported_surface", "surface": "app"})
        headers = {"Content-Type": "application/json", "Idempotency-Key": "d1"}
        for identity in ("hc:t8", "hc:vps%2Ft7"):
            for leaf in ("dispatch-defaults", "dispatch-mode"):
                path = "/v1/sessions/%s/%s" % (identity, leaf)
                self.assertEqual(self.request(path), expected, path)
                self.assertEqual(self.request(path, "PUT", json.dumps({"host": "auto"}), headers), expected, path)
            self.assertEqual(self.request("/v1/sessions/%s/messages" % identity), expected)
        self.assertEqual(self.request("/v1/sessions/hc:t99/messages")[0], 404)
        for name in ("messages", "dispatch-defaults"):
            example = json.loads((base.CONTRACTS / ("sessions-unsupported-surface-%s.json" % name)).read_text())
            self.assertEqual((example["response"]["status"], example["response"]["body"]), expected)
            status, body = self.request(example["request"]["path"], example["request"]["method"])
            self.assertEqual((status, body), expected)

    def test_read_paths_never_rebuild_project_view_and_cold_cache_counts_zero(self):
        service = self.start_hc(["hc:t8"])
        routes = service.project_routes
        with patch.object(routes, "list_view", side_effect=AssertionError("读路径不得重建项目视图")):
            routes.list_payloads.clear()
            body = self.request("/v1/sessions")[1]
            self.assertEqual({row["needs_attention_count"] for row in body["sessions"]}, {0})
            self.assertEqual(self.request("/v1/jobs/none/session")[0], 404)
        self.assertIsNone(routes.cached_view())

    def test_idle_hc_stream_is_killed_and_reconnected(self):
        self.hc_data["stall"] = True
        self.save_hc()
        with patch.object(rd_hc.Hc, "IDLE_TIMEOUT", 0.4), patch.object(rd_hc.Hc, "GRACE", 0.3):
            self.start_hc(["hc:t8"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and sum(c[:1] == ["events"] for c in self.calls()) < 2:
                time.sleep(0.1)
            self.assertGreaterEqual(sum(c[:1] == ["events"] for c in self.calls()), 2)

    def test_garbage_json_from_hc_get_does_not_cause_fast_reconnect_loop(self):
        self.hc_data.update(garbage_get=True, ls_extra=[self.terminal("t10", "claude", "idle")])
        self.save_hc()
        self.start_hc(["hc:t8"])
        time.sleep(1.0)
        self.assertNotIn("hc:t10", self.by_id())
        self.assertEqual(sum(c[:1] == ["events"] for c in self.calls()), 1)

    def test_probable_session_never_counts_needs_attention(self):
        rows = [dict(surface="app", address="t8", pane_id=None, probable=True, needs_attention_count=0),
                dict(surface="tui", address="w1:p1", pane_id="w1:p1", probable=True, needs_attention_count=0)]
        recs = [dict(job="a", initiator_hc={"terminal": "t8"}), dict(job="b", initiator_herdr={"pane_id": "w1:p1"})]
        cards = {job: dict(execution_state="unreachable", outcome=None, review_state="pending") for job in ("a", "b")}
        from remote_dispatch.rd_serve_sessions import Sessions
        Sessions.count_attention(rows, recs, cards)
        self.assertEqual([row["needs_attention_count"] for row in rows], [0, 0])

    def test_job_session_resolution_falls_back_to_hc_terminal(self):
        self.config.write_text("[host:target]\nssh=local\nworkdir=%s\nsync=direct\n" % (self.root / "worker"))
        self.attention_job_fallback = None
        self.job_states = {}
        boundary = patch.object(cli, "job_states", side_effect=lambda cfg, recs: {
            rec["job"]: {"state": "running", "round": "1"} for rec in recs})
        boundary.start()
        self.addCleanup(boundary.stop)
        self.attention_job("via-hc", "running", initiator_session="gone", initiator_herdr={"pane_id": "w9:p9"},
                           initiator_hc={"terminal": "t8"})
        self.start_hc(["hc:t8"])
        body = self.request("/v1/jobs/via-hc/session")[1]
        self.assertEqual((body["session"]["id"], body["matched_by"]), ("hc:t8", "terminal"))


if __name__ == "__main__":
    unittest.main()
