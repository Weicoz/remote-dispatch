"""会话 HTTP 接缝；Herdr 仅在可执行文件边界替换，不接触真实会话。"""
import json
import base64
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import test_rd_serve as base


class SessionTests(unittest.TestCase):
    request = base.ServeTests.request
    eventually = base.ServeTests.eventually
    open_events = base.ServeTests.open_events
    next_event = base.ServeTests.next_event
    snapshot = base.ServeTests.snapshot
    assert_structure = base.ServeTests.assert_structure

    def start(self, **options):
        service = base.ServeTests.start(self, **options)
        if service.sessions.enabled and not self.data.get("unavailable"):
            self.eventually(lambda: self.request("/v1/sessions"),
                            lambda reply: reply[0] == 403 or bool(reply[1].get("sessions")))
        return service

    def setUp(self):
        from remote_dispatch import cli
        temporary = tempfile.TemporaryDirectory(prefix="rd25-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.config = self.root / "config.ini"
        self.config.write_text("", encoding="utf-8")
        self.path = self.root / "serve.sock"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(cli, "STATE", str(self.state)))
        stack.enter_context(patch.object(cli, "CONFIG", str(self.config)))
        stack.enter_context(patch.dict(os.environ, {"RD_USER_ID": "fixture"}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://example.test/user/demo.git"], check=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fixture = self.root / "herdr.json"
        self.writes = self.root / "writes.jsonl"
        self.herdr_config = self.root / "herdr" / "config.toml"
        self.herdr_config.parent.mkdir()
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        script = self.bin / "herdr"
        script.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
v = json.loads(Path(os.environ["FAKE_HERDR"]).read_text())
a = sys.argv[1:]
if a == ["agent", "list"]:
    if v.get("unavailable"):
        sys.exit(1)
    print(json.dumps({"id": "cli:agent:list", "result": {"agents": v["agents"]}}))
elif a[:2] == ["pane", "layout"]:
    if "layout_result" in v:
        print(json.dumps(v["layout_result"]))
        sys.exit(0)
    print(json.dumps({"result": {"layout": {"area": {"width": 240, "height": 90},
          "panes": [{"pane_id": "other", "rect": {"width": 80, "height": 24}},
                    {"pane_id": a[a.index("--pane") + 1], "rect": v.get("viewport", {"width": 149, "height": 45})}]}}}))
elif a[:2] == ["pane", "read"]:
    sys.stdout.write(v["screen"])
elif a[:3] == ["terminal", "session", "observe"]:
    if v.get("observer_pid"):
        Path(v["observer_pid"]).write_text(str(os.getpid()))
    if v.get("sized_frames") or not v["frames"]:
        import base64
        cols = int(a[a.index("--cols") + 1]) if "--cols" in a else 120
        rows = int(a[a.index("--rows") + 1]) if "--rows" in a else 40
        text = "\\x1b[2J\\x1b[H" + v["screen"] + v.get("cursor_ansi", "\\x1b[3;5H\\x1b[?25h")
        print(json.dumps({"type": "terminal.frame", "seq": 1, "width": cols, "height": rows,
                         "full": True, "bytes": base64.b64encode(text.encode()).decode()}), flush=True)
    if v.get("frame_gate"):
        import time
        while not Path(v["frame_gate"]).exists():
            time.sleep(0.01)
    for line in v["frames"]:
        print(line, flush=True)
    if v.get("hold"):
        import time
        while True:
            time.sleep(0.1)
elif a[:2] in (["pane", "send-text"], ["pane", "send-keys"]):
    with open(os.environ["FAKE_WRITES"], "a") as f:
        f.write(json.dumps(a) + "\\n")
    if v.get("fail_input") or (v.get("fail_enter") and a[:2] == ["pane", "send-keys"]):
        sys.exit(1)
    print("{}")
else:
    sys.exit(2)
''', encoding="utf-8")
        script.chmod(0o700)
        env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                         "FAKE_HERDR": str(self.fixture), "FAKE_WRITES": str(self.writes),
                         "HERDR_CONFIG_PATH": str(self.herdr_config), "HERDR_SESSION": "",
                         "CODEX_HOME": str(self.codex), "CLAUDE_CONFIG_DIR": str(self.claude)})
        env.start()
        self.addCleanup(env.stop)
        self.data = {"agents": [self.agent()], "screen": "❯ hello\n", "frames": []}
        self.save_fixture()

    def agent(self, pane="w1:p1", status="idle", **fields):
        return dict(pane_id=pane, workspace_id="w1", agent="codex", agent_status=status,
                    cwd=str(self.repo), **fields)

    def save_fixture(self):
        staging = self.fixture.with_suffix(".tmp")
        staging.write_text(json.dumps(self.data), encoding="utf-8")
        staging.replace(self.fixture)

    def sessions(self):
        status, body = self.request("/v1/sessions")
        self.assertEqual(status, 200, body)
        return body["sessions"]

    def transcript(self, identity="session-one", text="你好", cwd=None):
        directory = self.codex / "sessions" / "2026" / "10" / "25"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("rollout-" + identity + ".jsonl")
        rows = [{"type": "session_meta", "payload": {"id": identity, "cwd": str(cwd or self.repo)}},
                {"type": "response_item", "timestamp": "2026-10-25T00:00:00Z", "payload": {
                    "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def saved_session(self, value="session-one", kind="id", number=1):
        snapshot = {"version": 3, "workspaces": [{"id": "w1", "public_pane_numbers": {"42": number},
                    "tabs": [{"panes": {"42": {"cwd": str(self.repo), "agent_session": {
                        "agent": "codex", "kind": kind, "value": value, "source": "hook"}}}}]}]}
        (self.herdr_config.parent / "session.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def test_session_rows_expose_pane_cwd_in_list_and_project_summary(self):
        foreground = self.repo / "nested"
        foreground.mkdir()
        self.data["agents"][0]["foreground_cwd"] = str(foreground)
        self.save_fixture()
        self.start(poll_interval=3600)
        row = self.sessions()[0]
        self.assertEqual(row["cwd"], str(foreground))
        self.assertIn("updated_at", row)
        project = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual(project["sessions"], [row])
        self.assertEqual((project["blocked_count"], project["offline_count"], project["failed_count"],
                          project["needs_attention_count"]), (0, 0, 0, 0))

    def test_unreadable_live_cwd_is_null_and_does_not_break_other_sessions(self):
        for live_cwd in (None, ""):
            with self.subTest(cwd=live_cwd):
                self.saved_session()
                self.data["agents"] = [dict(self.agent(), cwd=live_cwd), self.agent("w1:p2")]
                self.save_fixture()
                service = self.start(poll_interval=3600)
                rows = {row["id"]: row for row in self.sessions()}
                self.assertEqual(set(rows), {"w1:p1", "w1:p2"})
                self.assertIsNone(rows["w1:p1"]["cwd"])
                self.assertIn("updated_at", rows["w1:p1"])
                example = json.loads((base.CONTRACTS / "sessions-cwd-unknown.json").read_text())
                self.assert_structure({"sessions": [rows["w1:p1"]]}, example["response"]["body"])
                self.assertEqual(rows["w1:p2"]["cwd"], str(self.repo))
                project = self.request("/v1/projects")[1]["projects"][0]
                self.assertEqual(project["sessions"], list(rows.values()))
                service.close()

    def test_cwd_and_summary_fields_pass_sensitive_field_scan(self):
        private = self.repo / "fixture-secret-cwd"
        private.mkdir()
        self.config.write_text("[host:target]\nssh=local\nenv_file=fixture-secret-cwd\n")
        self.data["agents"][0].update(foreground_cwd=str(private),
                                     password="private-marker", agent_auth_file="private-marker")
        self.save_fixture()
        self.start(poll_interval=3600)
        rows = self.sessions()
        self.assertIn("[redacted]", rows[0]["cwd"])
        projects = self.request("/v1/projects")[1]
        self.assertEqual(projects["projects"][0]["sessions"], rows)
        self.assertEqual(projects["projects"][0]["hosts"]["target"]["state"], "none")
        encoded = json.dumps([rows, projects])
        for value in ("fixture-secret-cwd", "private-marker", "env_file", "agent_auth_file", "password"):
            self.assertNotIn(value, encoded)

    def test_snapshot_keeps_latest_session_event_time_across_rotation_and_restart_without_query_writes(self):
        from remote_dispatch import rd_events, rd_messages
        from remote_dispatch.rd_projects import project_id
        self.transcript()
        self.saved_session()
        service = self.start(poll_interval=3600)
        stream = self.open_events()
        self.next_event(stream)  # initial session.changed
        self.next_event(stream)  # initial session.message
        stream.close()
        service.close()
        with patch.object(rd_messages, "utc_now", return_value="2026-10-25T01:02:03+00:00"):
            rd_events.emit(self.state, "session.message", dict(user_id="fixture", pane_id="w1:p1",
                session_id="session-one", harness="codex",
                project_id=project_id("demo", "https://example.test/user/demo.git"),
                message_id="new-message", kind="assistant", message_time="2026-10-25T00:00:00Z"))
        rd_events.EventLog(self.state, max_bytes=1).publish("job.created", dict(user_id="fixture", job="other"))
        self.start(poll_interval=3600)
        stream = self.open_events()
        for _ in range(4):
            self.next_event(stream)
        stream.close()
        before = self.snapshot()
        self.assertEqual(self.sessions()[0]["updated_at"], "2026-10-25T01:02:03+00:00")
        self.assertEqual(self.sessions()[0]["updated_at"], "2026-10-25T01:02:03+00:00")
        self.assertEqual(self.snapshot(), before)

    def test_session_without_events_has_unknown_update_time_until_collected(self):
        service = self.start(poll_interval=3600)
        stream = self.open_events()
        initial = self.next_event(stream)[2]
        stream.close()
        self.data["agents"].append(self.agent("w1:p2"))
        self.save_fixture()
        self.assertEqual(len(self.sessions()), 1)
        service.wake.set()
        stream = self.open_events("/v1/events?after=" + str(initial["seq"]))
        changed = self.next_event(stream)[2]
        stream.close()
        self.assertEqual(changed["data"]["pane_id"], "w1:p2")
        self.assertEqual(self.sessions()[1]["updated_at"], changed["time"])

    def test_replaced_session_does_not_inherit_old_or_other_users_event_time(self):
        from remote_dispatch import rd_events
        from remote_dispatch.rd_projects import project_id
        self.transcript()
        self.saved_session()
        service = self.start(poll_interval=3600)
        stream = self.open_events()
        self.next_event(stream)
        self.next_event(stream)
        stream.close()
        self.transcript("session-two")
        self.saved_session("session-two")
        self.assertEqual(self.sessions()[0]["session_id"], "session-one")
        service.wake.set()
        self.eventually(self.sessions, lambda rows: rows[0]["session_id"] == "session-two")
        updated = self.sessions()[0]["updated_at"]
        data = dict(user_id="other", pane_id="w1:p1", session_id="session-two", harness="codex",
                    project_id=project_id("demo", "https://example.test/user/demo.git"))
        rd_events.emit(self.state, "session.changed", data)
        self.assertEqual(self.sessions()[0]["updated_at"], updated)
        rd_events.emit(self.state, "session.changed", dict(data, user_id="fixture", removed=True))
        self.assertIsNone(self.sessions()[0]["updated_at"])

    def test_input_updates_snapshot_event_time_but_idempotent_replay_does_not(self):
        from remote_dispatch import rd_messages
        service = self.start(poll_interval=3600)
        stream = self.open_events()
        self.next_event(stream)
        with patch.object(rd_messages, "utc_now", return_value="2026-10-25T02:00:00+00:00"):
            self.assertEqual(self.input({"text": "新输入"})[0], 200)
        changed = self.next_event(stream)[2]
        stream.close()
        self.assertEqual(changed["time"], "2026-10-25T02:00:00+00:00")
        self.assertEqual(self.sessions()[0]["updated_at"], "2026-10-25T02:00:00+00:00")
        with patch.object(rd_messages, "utc_now", return_value="2026-10-25T03:00:00+00:00"):
            self.assertEqual(self.input({"text": "幂等重放"})[0], 200)
        self.assertEqual(self.sessions()[0]["updated_at"], "2026-10-25T02:00:00+00:00")

    def test_saved_identity_wins_over_newer_transcript_and_messages_have_stable_after_cursor(self):
        self.transcript()
        self.transcript("newer", "其他会话")
        self.saved_session()
        self.start()
        row = self.sessions()[0]
        self.assertEqual((row["session_id"], row["probable"]), ("session-one", False))
        status, body = self.request("/v1/sessions/w1:p1/messages")
        self.assertEqual(status, 200)
        self.assertTrue(body["supported"])
        self.assertEqual([m["text"] for m in body["messages"]], ["你好"])
        cursor = body["messages"][0]["id"]
        self.assertEqual(self.request("/v1/sessions/w1:p1/messages?after=" + cursor)[1]["messages"], [])

    def test_missing_snapshot_matches_latest_cwd_transcript_with_probable_label(self):
        old = self.transcript("old")
        os.utime(old, (1, 1))
        self.transcript("newer", "新回复")
        self.transcript("unrelated", "别的项目", self.root)
        self.start()
        row = self.sessions()[0]
        self.assertEqual((row["session_id"], row["probable"], row["identity_text"]),
                         ("newer", True, "可能是这个会话"))
        self.assertEqual(self.request("/v1/sessions/w1:p1/messages")[1]["messages"][0]["text"], "新回复")

    def test_missing_session_id_falls_back_to_latest_cwd_with_probable_label(self):
        for harness in ("claude", "codex"):
            for source in ("saved", "live"):
                with self.subTest(harness=harness, source=source):
                    if harness == "codex":
                        old = self.transcript("old")
                        current = self.transcript("current", "合成最新回复")
                        unrelated = self.transcript("unrelated", "其他项目", self.root)
                    else:
                        directory = self.claude / "projects" / "synthetic-project"
                        directory.mkdir(parents=True, exist_ok=True)
                        paths = []
                        for identity, cwd in (("old", self.repo), ("current", self.repo),
                                              ("unrelated", self.root)):
                            path = directory / (identity + ".jsonl")
                            record = {"type": "assistant", "uuid": identity, "sessionId": identity,
                                      "timestamp": "example-time", "cwd": str(cwd),
                                      "message": {"role": "assistant", "content": "合成最新回复"}}
                            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                            paths.append(path)
                        old, current, unrelated = paths
                    for path, mtime in ((old, 1), (current, 2), (unrelated, 3)):
                        os.utime(path, (mtime, mtime))
                    reference = {"agent": harness, "kind": "id", "value": "missing-session"}
                    self.data["agents"][0].update(agent=harness)
                    self.data["agents"][0].pop("agent_session", None)
                    if source == "live":
                        self.data["agents"][0]["agent_session"] = reference
                    else:
                        snapshot = {"workspaces": [{"tabs": [{"panes": {"42": {
                            "cwd": str(self.repo), "agent_session": reference}}}],
                            "id": "w1", "public_pane_numbers": {"42": 1}}], "version": 3}
                        (self.herdr_config.parent / "session.json").write_text(json.dumps(snapshot))
                    self.save_fixture()
                    service = self.start(poll_interval=3600)
                    try:
                        row = self.sessions()[0]
                        self.assertEqual((row["session_id"], row["probable"], row["identity_text"]),
                                         ("current", True, "可能是这个会话"))
                        status, result = self.request("/v1/sessions/w1:p1/messages")
                        self.assertEqual(status, 200)
                        self.assertTrue(result["supported"])
                        self.assertEqual([m["text"] for m in result["messages"]], ["合成最新回复"])
                    finally:
                        service.close()

    def test_invalid_herdr_layout_returns_service_unavailable_for_screen(self):
        self.start(poll_interval=3600)
        for layout in (None, [], {"result": None}, {"result": {"layout": None}},
                       {"result": {"layout": {"panes": [None]}}}):
            with self.subTest(layout=layout):
                self.data["layout_result"] = layout
                self.save_fixture()
                self.assertEqual(self.request("/v1/sessions/w1:p1/screen"),
                                 (503, {"error": {"reason": "read_unavailable"}}))

    def test_quiet_pane_resize_restarts_observer_with_full_frame_and_monotonic_sequence(self):
        self.data.update(sized_frames=True, hold=True, observer_pid=str(self.root / "resize.pid"))
        self.save_fixture()
        self.start(poll_interval=0.05)
        response = self.open_events("/v1/sessions/w1:p1/terminal")
        def next_frame():
            self.assertEqual(response.readline(), b"event: terminal.frame\n")
            frame = json.loads(response.readline()[6:])
            self.assertEqual(response.readline(), b"\n")
            return frame
        first = next_frame()
        first_pid = int((self.root / "resize.pid").read_text())
        self.data.update(viewport={"width": 91, "height": 31}, screen="resized",
                         cursor_ansi="\x1b[30;90H\x1b[?25h")
        self.save_fixture()
        resized = next_frame()
        self.assertEqual((first["width"], first["height"]), (149, 45))
        self.assertEqual((resized["width"], resized["height"], resized["full"]), (91, 31, True))
        self.assertEqual(resized["cursor"], {"x": 89, "y": 29, "visible": True})
        self.assertGreater(resized["seq"], first["seq"])
        self.assertIn(b"resized", base64.b64decode(resized["data"]))
        self.assertNotEqual(first_pid, int((self.root / "resize.pid").read_text()))
        with self.assertRaises(ProcessLookupError):
            os.kill(first_pid, 0)
        response.close()

    def test_screen_and_frames_use_target_pane_viewport_and_cursor(self):
        self.data.update(sized_frames=True)
        self.save_fixture()
        self.start(poll_interval=3600)
        for width, height, cursor, ansi in [
                (149, 45, {"x": 4, "y": 2, "visible": True}, "\x1b[3;5H\x1b[?25h"),
                (83, 27, {"x": 82, "y": 26, "visible": False}, "\x1b[27;83H\x1b[?25l")]:
            with self.subTest(size=(width, height)):
                self.data.update(viewport={"width": width, "height": height}, cursor_ansi=ansi)
                self.save_fixture()
                status, screen = self.request("/v1/sessions/w1:p1/screen")
                self.assertEqual(status, 200)
                self.assertEqual((screen["width"], screen["height"], screen["cursor"]),
                                 (width, height, cursor))
                self.assertEqual(screen["text"], "\x1b[2J\x1b[H❯ hello\n" + ansi)
                response = self.open_events("/v1/sessions/w1:p1/terminal")
                self.assertEqual(response.readline(), b"event: terminal.frame\n")
                frame = json.loads(response.readline()[6:])
                self.assertEqual((frame["width"], frame["height"], frame["cursor"], frame["full"]),
                                 (width, height, cursor, True))
                self.assertEqual(base64.b64decode(frame["data"]).decode(), screen["text"])
                response.close()

    def test_bytes_frames_and_initial_screen_redact_secrets_without_losing_cursor(self):
        text = "\x1b[2J\x1b[Haccess_token=screen-private\nvery-\x1b[1msecret\x1b[0m\n"
        text += "\x1b]0;token=osc-private\x07\x1b[4;7H\x1b[?25h"
        self.data["frames"] = [json.dumps(dict(type="terminal.frame", seq=1, full=True,
            width=149, height=45, bytes=base64.b64encode(text.encode()).decode()))]
        self.save_fixture()
        service = self.start(poll_interval=3600)
        service.sensitive.add("very-secret")
        status, screen = self.request("/v1/sessions/w1:p1/screen")
        self.assertEqual(status, 200)
        response = self.open_events("/v1/sessions/w1:p1/terminal")
        self.assertEqual(response.readline(), b"event: terminal.frame\n")
        frame = json.loads(response.readline()[6:])
        self.assertNotIn("bytes", frame)
        cleaned = base64.b64decode(frame["data"]).decode()
        self.assertEqual(cleaned, screen["text"])
        self.assertEqual(cleaned.count("[redacted]"), 3)
        for secret in ("screen-private", "very-", "secret", "osc-private"):
            self.assertNotIn(secret, cleaned)
        self.assertEqual(frame["cursor"], {"x": 6, "y": 3, "visible": True})
        self.assertEqual(screen["cursor"], frame["cursor"])
        response.close()

    def test_screen_and_terminal_preserve_ansi_and_frame_metadata(self):
        text = "\x1b[31mhi\x1b[0m\x1b[2;9H\x1b[?25h"
        frame = dict(type="terminal.frame", data=base64.b64encode(text.encode()).decode(),
                     seq=7, full=True, width=149, height=45, encoding="ansi")
        self.data["frames"] = [json.dumps(frame), '{"type":"terminal.closed","reason":"closed"}']
        self.save_fixture()
        self.start()
        cursor = {"x": 8, "y": 1, "visible": True}
        self.assertEqual(self.request("/v1/sessions/w1:p1/screen"),
                         (200, {"pane_id": "w1:p1", "text": text, "width": 149, "height": 45,
                                "cursor": cursor}))
        response = self.open_events("/v1/sessions/w1:p1/terminal")
        self.assertEqual(response.readline(), b"event: terminal.frame\n")
        self.assertEqual(json.loads(response.readline()[6:]), dict(frame, cursor=cursor))
        self.assertEqual(response.readline(), b"\n")
        self.assertEqual(response.readline(), b"event: terminal.closed\n")
        self.assertEqual(json.loads(response.readline()[6:]), {"type": "terminal.closed", "reason": "closed"})

    def test_disabled_access_rejects_every_session_route_before_lookup_or_input_validation(self):
        self.config.write_text("[serve]\nsession_access=false\n", encoding="utf-8")
        self.start()
        for path in ("/v1/sessions", "/v1/sessions/missing/screen", "/v1/sessions/missing/terminal",
                     "/v1/sessions/missing/messages", "/v1/jobs/missing/session"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path), (403, {"error": {"reason": "session_access_disabled"}}))
        self.assertEqual(self.request("/v1/sessions/missing/input", "POST"),
                         (403, {"error": {"reason": "session_access_disabled"}}))
        self.assertFalse(self.writes.exists())

    def test_paired_device_defaults_to_denied_until_trusted_device_permission_is_enabled(self):
        from remote_dispatch import rd_serve
        class PairedHandler(rd_serve.Handler):
            session_device = {"id": "paired-phone"}
        with patch.object(rd_serve, "Handler", PairedHandler):
            self.start()
        self.assertEqual(self.request("/v1/sessions")[0], 403)
        PairedHandler.session_device = {"id": "paired-phone", "session_access": True}
        self.assertEqual(self.request("/v1/sessions")[0], 200)

    def test_dispatch_resolution_prefers_session_then_pane_then_closed_and_is_user_scoped(self):
        from remote_dispatch import cli
        self.transcript()
        self.saved_session()
        self.data["agents"].append(self.agent("w1:p2"))
        self.save_fixture()
        for job, identity, pane in (("by-id", "session-one", "w1:p2"),
                                    ("by-pane", "gone", "w1:p2"), ("closed", "gone", "gone")):
            cli.save_job(dict(job=job, user_id="fixture", initiator_session=identity,
                              initiator_herdr={"pane_id": pane}, rounds=1, host="none"))
        cli.save_job(dict(job="other", user_id="someone-else", initiator_session="session-one", rounds=1))
        # Avoid host probes for these minimal source records.
        with patch.object(cli, "all_jobs", return_value=[]):
            self.start(poll_interval=3600)
            by_id = self.request("/v1/jobs/by-id/session")
            self.assertEqual(by_id[0], 200)
            self.assertEqual((by_id[1]["session"]["id"], by_id[1]["matched_by"]), ("w1:p1", "session_id"))
            by_pane = self.request("/v1/jobs/by-pane/session")[1]
            self.assertEqual((by_pane["session"]["id"], by_pane["matched_by"]), ("w1:p2", "pane_id"))
            self.assertEqual(self.request("/v1/jobs/closed/session"),
                             (200, {"session": None, "matched_by": None, "reason": "session_closed", "text": "会话已关闭"}))
            self.assertEqual(self.request("/v1/jobs/other/session")[0], 404)

    def test_session_only_projects_vanish_on_close_without_writing_project_index(self):
        from remote_dispatch.rd_projects import ProjectIndex
        index = ProjectIndex(self.state, "fixture")
        service = self.start(poll_interval=3600)
        stream = self.open_events()
        self.assertEqual(self.next_event(stream)[1], "session.changed")
        stream.close()
        status, body = self.request("/v1/projects")
        self.assertEqual(status, 200)
        self.assertEqual([(p["name"], p["session_only"], p["dispatch_count"]) for p in body["projects"]],
                         [("demo", True, 0)])
        project = body["projects"][0]
        self.assertEqual(project["sessions"], self.sessions())
        self.assertEqual((project["needs_attention_count"], project["in_progress_count"],
                          project["week_dispatch_count"], project["hosts"]), (0, 0, 0, {}))
        for field in ("first_dispatch_at", "last_dispatch_at", "last_accepted_at", "last_harness"):
            self.assertIsNone(project[field])
        self.assertEqual(project["recent_event"]["type"], "session.changed")
        self.assertEqual(index.read(), {})
        self.assertFalse(index.path.exists())
        self.data["agents"] = []
        self.save_fixture()
        service.wake.set()
        self.eventually(lambda: self.request("/v1/projects"), lambda row: row == (200, {"projects": []}))
        self.assertFalse(index.path.exists())

    def test_live_sessions_extend_indexed_summary_and_respect_hidden_projects_and_device_access(self):
        from remote_dispatch import rd_serve
        from remote_dispatch.rd_projects import ProjectIndex, project_id
        origin = "https://example.test/user/demo.git"
        identity = project_id("demo", origin)
        index = ProjectIndex(self.state, "fixture")
        row = dict(id=identity, name="demo", origin=origin, first_dispatch_at="2026-10-24T00:00:00Z",
                   last_dispatch_at="2026-10-24T00:00:00Z", dispatch_count=3, hidden=True)
        index.write({identity: row})
        class PairedHandler(rd_serve.Handler):
            session_device = {"id": "phone", "session_access": True}
        with patch.object(rd_serve, "Handler", PairedHandler):
            service = self.start(poll_interval=3600)
        # Compare snapshots after the initial background event has been committed.
        stream = self.open_events()
        while self.next_event(stream)[1] != "session.changed":
            pass
        stream.close()
        project = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual({key: project[key] for key in row}, row)
        self.assertFalse(project["session_only"])
        self.assertEqual(project["sessions"], self.sessions())
        self.assertEqual(project["week_dispatch_count"], 0)
        service.sessions.collect()
        self.assertEqual(self.request("/v1/projects")[1]["projects"][0]["recent_event"]["type"], "session.changed")
        PairedHandler.session_device = {"id": "phone"}
        denied = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual(denied["sessions"], [])
        self.assertEqual(denied["recent_event"]["type"], "project.changed")
        self.assertEqual(index.read(), {identity: row})
        self.config.write_text("[serve]\nsession_access=false\n")
        service.close()
        service = self.start(poll_interval=3600)
        disabled = self.request("/v1/projects")[1]["projects"][0]
        self.assertEqual(disabled["sessions"], [])
        self.assertEqual(disabled["recent_event"]["type"], "project.changed")

    def input(self, body, key="input-one", pane="w1:p1"):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.request("/v1/sessions/" + pane + "/input", "POST", json.dumps(body), headers)

    def test_supported_terminal_keys_and_strict_submit_validation(self):
        self.start(poll_interval=3600)
        keys = ["backspace", "left", "right", "ctrl+d", "shift+tab", "ctrl+a", "ctrl+u",
                "space", "enter", "esc", "up", "down", "tab", "ctrl+c"]
        self.assertEqual(self.input({"keys": keys})[0], 200)
        self.assertEqual(json.loads(self.writes.read_text()), ["pane", "send-keys", "w1:p1", *keys])
        for index, body in enumerate(
                [{"keys": [key]} for key in ["delete", "home", "end", "pageup", "pagedown", "--help", "CTRL+D"]]
                + [{"text": "x", "submit": value} for value in [0, 1, "false", None]]
                + [{"keys": ["enter"], "submit": False}]):
            with self.subTest(body=body):
                self.assertEqual(self.input(body, key="invalid-" + str(index))[0], 400)
        self.assertEqual(len(self.writes.read_text().splitlines()), 1)
        self.assertEqual(self.input({"text": "x", "submit": True}, key="explicit")[0], 200)
        self.assertEqual(json.loads(self.writes.read_text().splitlines()[-1]),
                         ["pane", "send-keys", "w1:p1", "enter"])

    def test_raw_text_does_not_submit_and_replays_without_resending(self):
        service = self.start(poll_interval=3600)
        body = {"text": "--测试\n粘贴", "submit": False}
        self.assertEqual(self.input(body, key=None)[0], 400)
        first = self.input(body)
        self.assertEqual(first, (200, {"pane_id": "w1:p1", "accepted": True}))
        self.assertEqual(self.input({"text": "changed", "submit": True}), first)
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.input(body), first)
        saved = "".join(path.read_text() for path in (self.state / "sessions" / "inputs").glob("*.json"))
        self.assertNotIn("--测试", saved)
        self.assertNotIn("粘贴", saved)
        self.assertEqual([json.loads(line) for line in self.writes.read_text().splitlines()],
                         [["pane", "send-text", "w1:p1", "--", "--测试\n粘贴"]])

    def test_input_requires_key_and_replays_first_result_across_restart_without_resending(self):
        service = self.start()
        self.assertEqual(self.input({"text": "你好"}, key=None)[0], 400)
        self.assertFalse(self.writes.exists())
        first = self.input({"text": "你好\n第二行"})
        self.assertEqual(first, (200, {"pane_id": "w1:p1", "accepted": True}))
        self.assertEqual(self.input({"text": "不重复发送"}), first)
        service.close()
        self.start()
        self.assertEqual(self.input({"text": "重启仍不发送"}), first)
        self.assertEqual(self.input({"keys": ["esc", "up", "down", "tab", "ctrl+c", "enter"]}, key="keys")[0], 200)
        self.assertEqual([json.loads(line) for line in self.writes.read_text().splitlines()],
                         [["pane", "send-text", "w1:p1", "--", "你好\n第二行"],
                          ["pane", "send-keys", "w1:p1", "enter"],
                          ["pane", "send-keys", "w1:p1", "esc", "up", "down", "tab", "ctrl+c", "enter"]])

    def test_background_publishes_session_and_message_events_and_closed_state_without_content(self):
        self.transcript(text="私密消息正文")
        self.saved_session()
        self.start(poll_interval=0.05)
        response = self.open_events()
        _, _, changed = self.next_event(response)
        self.assertEqual(changed["type"], "session.changed")
        self.assertEqual(changed["data"]["pane_id"], "w1:p1")
        _, _, message = self.next_event(response)
        self.assertEqual(message["type"], "session.message")
        self.assertTrue(message["data"]["message_id"].startswith("codex:"))
        self.assertNotIn("私密消息正文", json.dumps(message, ensure_ascii=False))
        self.data["agents"] = []
        self.save_fixture()
        _, _, closed = self.next_event(response)
        self.assertEqual((closed["type"], closed["data"]["removed"]), ("session.changed", True))

    def test_new_session_in_same_pane_emits_messages_even_when_converter_line_ids_repeat(self):
        self.transcript()
        self.saved_session()
        self.start(poll_interval=0.05)
        response = self.open_events()
        self.next_event(response)
        first = self.next_event(response)[2]
        self.transcript("session-two", "另一个会话")
        self.saved_session("session-two")
        changed = self.next_event(response)[2]
        self.assertEqual(changed["data"]["session_id"], "session-two")
        second = self.next_event(response)[2]
        self.assertEqual((second["type"], second["data"]["session_id"]), ("session.message", "session-two"))
        self.assertEqual(second["data"]["message_id"], first["data"]["message_id"])

    def test_malformed_saved_snapshot_falls_back_and_saved_path_identity_handles_public_pane_numbers(self):
        path = self.transcript()
        (self.herdr_config.parent / "session.json").write_text("[]")
        service = self.start()
        self.assertTrue(self.sessions()[0]["probable"])
        self.data["agents"] = [self.agent("w1:pA")]
        self.save_fixture()
        self.saved_session(str(path), "path", 10)
        service.wake.set()
        row = self.eventually(self.sessions, lambda rows: rows[0]["id"] == "w1:pA")[0]
        self.assertEqual((row["session_id"], row["probable"]), ("session-one", False))

    def test_disabled_stream_filters_existing_session_events_but_keeps_other_events(self):
        from remote_dispatch import rd_events
        self.config.write_text("[serve]\nsession_access=false\n")
        rd_events.emit(self.state, "session.changed", dict(pane_id="w1:p1", user_id="fixture"))
        rd_events.emit(self.state, "session.message", dict(pane_id="w1:p1", user_id="fixture", message_id="hidden"))
        rd_events.emit(self.state, "job.created", dict(job="visible", user_id="fixture"))
        self.start()
        response = self.open_events()
        self.assertEqual(self.next_event(response)[1], "job.created")

    def test_input_preserves_option_like_text_and_screen_carriage_returns(self):
        self.data["screen"] = "\x1b[31mhello\r\n"
        self.save_fixture()
        self.start()
        self.assertEqual(self.input({"text": "--help"})[0], 200)
        self.assertEqual(json.loads(self.writes.read_text().splitlines()[0]),
                         ["pane", "send-text", "w1:p1", "--", "--help"])
        self.assertEqual(self.request("/v1/sessions/w1:p1/screen")[1]["text"],
                         "\x1b[2J\x1b[H\x1b[31mhello\r\n\x1b[3;5H\x1b[?25h")

    def test_local_execution_panes_are_excluded_from_initiator_sessions(self):
        from remote_dispatch import cli
        worker = self.root / "worker"
        execution_repo = worker / "users" / "fixture" / "wt" / "job"
        execution_repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(execution_repo)], check=True)
        self.config.write_text(f"[host:local]\nssh=local\nworkdir={worker}\n")
        self.data["agents"].append(dict(self.agent("w1:p2"), cwd=str(execution_repo)))
        self.save_fixture()
        with patch.object(cli, "PROBE", 'echo "os=Darwin ncpu=8 load1=1 memfree=80 disk=100 running=0 heavy=0"'):
            self.start()
            self.assertEqual([r["id"] for r in self.sessions()], ["w1:p1"])

    def test_persistent_projects_survive_herdr_outage_and_never_expose_origin_credentials(self):
        from remote_dispatch.rd_projects import ProjectIndex, project_id
        origin = "https://user:credential@example.test/demo.git"
        identity = project_id("history", origin)
        row = dict(id=identity, name="history", origin=origin, first_dispatch_at=None,
                   last_dispatch_at=None, dispatch_count=1, hidden=True)
        ProjectIndex(self.state, "fixture").write({identity: row})
        self.data["unavailable"] = True
        self.save_fixture()
        self.start()
        status, body = self.request("/v1/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"], [])
        status, body = self.request("/v1/projects")
        self.assertEqual(status, 200)
        self.assertEqual([(p["name"], p["session_only"]) for p in body["projects"]], [("history", False)])
        self.assertNotIn("credential", json.dumps(body))

    def test_failed_external_input_is_remembered_and_never_automatically_retried(self):
        self.data["fail_input"] = True
        self.save_fixture()
        self.start()
        first = self.input({"text": "可能已经送达"})
        self.assertEqual(first, (503, {"error": {"reason": "session_input_unavailable"}}))
        self.data["fail_input"] = False
        self.save_fixture()
        self.assertEqual(self.input({"text": "可能已经送达"}), first)
        self.assertEqual(len(self.writes.read_text().splitlines()), 1)

    def test_successful_input_emits_session_changed_without_input_content(self):
        self.start(poll_interval=3600)
        response = self.open_events()
        self.assertEqual(self.next_event(response)[1], "session.changed")
        self.assertEqual(self.input({"text": "输入正文"})[0], 200)
        event = self.next_event(response)[2]
        self.assertEqual((event["type"], event["data"]["pane_id"]), ("session.changed", "w1:p1"))
        self.assertNotIn("输入正文", json.dumps(event, ensure_ascii=False))

    def test_live_identity_supersedes_stale_saved_identity_in_same_pane(self):
        self.transcript()
        self.saved_session()
        self.transcript("session-two")
        self.data["agents"][0]["agent_session"] = {"agent": "codex", "kind": "id", "value": "session-two"}
        self.save_fixture()
        self.start()
        self.assertEqual(self.sessions()[0]["session_id"], "session-two")

    def test_unknown_transcript_record_preserves_chat_and_invalid_input_never_writes(self):
        path = self.transcript()
        with path.open("a") as stream:
            stream.write('{"type":"new-format"}\n')
        self.saved_session()
        self.start()
        status, result = self.request("/v1/sessions/w1:p1/messages")
        self.assertEqual(status, 200)
        self.assertTrue(result["supported"])
        self.assertEqual([m["text"] for m in result["messages"]], ["你好"])
        cursor = result["messages"][0]["id"]
        with path.open("a") as stream:
            stream.write('{"type":"new-format"}\n')
            stream.write(json.dumps({"type": "response_item", "timestamp": "example-time", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "合成追加回复"}]}}) + "\n")
        status, result = self.request("/v1/sessions/w1:p1/messages?after=" + cursor)
        self.assertEqual(status, 200)
        self.assertTrue(result["supported"])
        self.assertEqual([m["text"] for m in result["messages"]], ["合成追加回复"])
        self.assertEqual(self.request("/v1/sessions/w1:p1/screen")[0], 200)
        for index, body in enumerate(({"text": "x", "keys": ["enter"]}, {"keys": "enter"},
                                      {"keys": ["--help"]}, {"text": ""}, {"text": "\0"}, [])):
            with self.subTest(body=body):
                self.assertEqual(self.input(body, key="bad-" + str(index))[0], 400)
        self.assertFalse(self.writes.exists())

    def test_session_v1_contract_examples_match_http_and_sse_responses(self):
        from remote_dispatch import cli
        transcript = self.transcript()
        self.saved_session()
        self.data["frames"] = json.loads((base.CONTRACTS / "sessions-terminal.json").read_text())["frames"]
        self.save_fixture()
        service = self.start(poll_interval=3600)
        response = self.open_events()
        event_contract = json.loads((base.CONTRACTS / "sessions-events.json").read_text())
        for expected in event_contract["events"]:
            self.assert_structure(self.next_event(response)[2], expected)
        for name in ("projects", "list", "screen", "messages", "after", "input-text", "input-raw-text", "input-keys", "resolve", "closed"):
            with self.subTest(contract=name):
                if name == "resolve":
                    cli.save_job(dict(job="fixture-job", user_id="fixture", rounds=1, host="none", initiator_session="session-one"))
                    cli.save_job(dict(job="closed", user_id="fixture", rounds=1, host="none", initiator_session="gone"))
                example = json.loads((base.CONTRACTS / ("sessions-" + name + ".json")).read_text())
                request = example["request"]
                body = json.dumps(request["body"]) if "body" in request else None
                status, result = self.request(request["path"], request["method"], body, request.get("headers"))
                self.assertEqual(status, example["response"]["status"])
                self.assert_structure(result, example["response"]["body"])
                for expected in example["events"]:
                    event = self.next_event(response)[2]
                    while event["type"] != expected["type"]:
                        event = self.next_event(response)[2]
                    self.assert_structure(event, expected)
        response.close()
        example = json.loads((base.CONTRACTS / "sessions-terminal.json").read_text())
        response = self.open_events(example["request"]["path"])
        for frame in example["frames"]:
            self.assertEqual(response.readline().decode(), "event: " + json.loads(frame)["type"] + "\n")
            self.assertEqual(json.loads(response.readline()[6:]), json.loads(frame))
            self.assertEqual(response.readline(), b"\n")
        response.close()
        with transcript.open("a") as stream:
            stream.write('{broken\n')
        example = json.loads((base.CONTRACTS / "sessions-unsupported.json").read_text())
        self.assertEqual(self.request(example["request"]["path"]),
                         (example["response"]["status"], example["response"]["body"]))
        service.close()
        self.config.write_text("[serve]\nsession_access=false\n")
        self.start()
        example = json.loads((base.CONTRACTS / "sessions-disabled.json").read_text())
        self.assertEqual(self.request(example["request"]["path"]),
                         (example["response"]["status"], example["response"]["body"]))

    def test_terminal_observer_is_reaped_after_disconnect_and_service_shutdown(self):
        self.data.update(hold=True, observer_pid=str(self.root / "observer.pid"),
                         frames=['{"type":"terminal.frame","data":"aGk="}'])
        self.save_fixture()
        service = self.start()
        for stop_service in (False, True):
            connection = base.UnixHTTPConnection(self.path)
            self.addCleanup(connection.close)
            connection.request("GET", "/v1/sessions/w1:p1/terminal")
            response = connection.getresponse()
            self.addCleanup(response.close)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline(), b"event: terminal.frame\n")
            response.readline()
            response.readline()
            pid = int((self.root / "observer.pid").read_text())
            if stop_service:
                service.close()
            response.close()
            connection.close()
            def reaped():
                try:
                    os.kill(pid, 0)
                    return False
                except ProcessLookupError:
                    return True
            self.eventually(reaped, bool)
        self.assertFalse(self.path.exists())

    def test_service_shutdown_reaps_terminal_observer_when_client_stops_reading(self):
        self.data.update(hold=True, observer_pid=str(self.root / "slow.pid"),
                         frames=[json.dumps({"type": "terminal.frame", "data": "A" * (2 * 1024 * 1024)})])
        self.save_fixture()
        service = self.start()
        connection = base.UnixHTTPConnection(self.path)
        self.addCleanup(connection.close)
        connection.request("GET", "/v1/sessions/w1:p1/terminal")
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        pid = self.eventually(lambda: (self.root / "slow.pid").read_text() if (self.root / "slow.pid").exists() else None, bool)
        service.close()
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            pass
        else:
            self.fail("服务关闭后慢客户端的观察子进程仍然存活")

    def test_lists_only_project_agent_panes_and_maps_all_five_herdr_states(self):
        self.data["agents"] = [self.agent("w1:p" + str(i), status)
                               for i, status in enumerate(("working", "idle", "done", "blocked", "unknown"), 1)]
        self.data["agents"] += [dict(self.agent("w1:p6"), agent=None),
                                dict(self.agent("w1:p7"), cwd=str(self.root)),
                                dict(self.agent("w1:p8"), agent="dsh")]
        self.save_fixture()
        self.start()
        rows = self.sessions()
        self.assertEqual([r["status"] for r in rows], ["working", "idle", "idle", "blocked", "unknown"])
        self.assertEqual([r["status_text"] for r in rows], ["工作中", "空闲", "空闲", "等待输入", "状态未知"])
        self.assertEqual([r["has_new_reply"] for r in rows], [False, False, True, False, False])
        self.assertTrue(all(r["project"] == "demo" and r["harness"] == "codex" for r in rows))
        self.assertEqual(rows[0]["pane_id"], "w1:p1")
