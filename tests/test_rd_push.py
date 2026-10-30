"""Observe push deliveries at the sender boundary with literal event timelines."""
import tempfile
import unittest
from pathlib import Path

from remote_dispatch.rd_push import PushDispatcher


class FakeSender:
    def __init__(self):
        self.deliveries = []

    def send(self, token, payload, *, stop):
        self.deliveries.append((token, payload))


class PushTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rp-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.sender = FakeSender()
        self.devices = [dict(device_id="phone", push_token="private-apns", revoked=False)]

    def event(self, seq, kind, **data):
        return dict(seq=seq, type=kind, time="2026-10-25T00:00:00+00:00", data=data)

    def test_receipts_and_blocked_push_once_but_stop_progress_start_review_and_sessions_do_not(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        rows = [self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed"),
                self.event(2, "receipt.ready", job="two", user_id="fixture", round=1, outcome="blocked"),
                self.event(3, "receipt.ready", job="three", user_id="fixture", round=1, outcome="failed", suppress_push=True),
                self.event(4, "progress.updated", job="one", user_id="fixture"),
                self.event(5, "job.created", job="one", user_id="fixture"),
                self.event(6, "review.updated", job="one", user_id="fixture", review_state="rejected"),
                self.event(7, "session.changed", user_id="fixture", state="blocked"),
                self.event(8, "receipt.ready", job="private", user_id="other", outcome="blocked")]
        dispatcher.dispatch(rows, self.devices, now=1792886400)
        # blocked 即时推送；完成回执先延后，等待同轮汇报。
        self.assertEqual([p["counts"] for _, p in self.sender.deliveries], [{"blocked": 1}])
        dispatcher.dispatch([], self.devices, now=1792886400 + 30)
        self.assertEqual([p["counts"] for _, p in self.sender.deliveries], [{"blocked": 1}, {"receipt_ready": 1}])
        # Replaying after restart must not redeliver.
        PushDispatcher(self.state, "fixture", self.sender).dispatch(rows, self.devices, now=1792886400 + 99)
        self.assertEqual(len(self.sender.deliveries), 2)

    def counts(self):
        return [p["counts"] for _, p in self.sender.deliveries]

    def test_same_round_receipt_and_report_push_only_report_ready(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        receipt = self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed")
        report = self.event(2, "report.delivered", user_id="fixture", report_id="r1", channels=["app"], rounds=["one:1"])
        dispatcher.dispatch([receipt], self.devices, now=1792886400)
        self.assertEqual(self.counts(), [])
        # 重启后检查点保留延后的回执；同轮汇报到达时只推 report_ready。
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt, report], self.devices, now=1792886405)
        self.assertEqual(self.counts(), [{"report_ready": 1}])
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt, report], self.devices, now=1792886999)
        self.assertEqual(self.counts(), [{"report_ready": 1}])

    def test_receipt_without_report_still_pushes_after_hold_even_across_restart(self):
        receipt = self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed")
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt], self.devices, now=1792886400)
        PushDispatcher(self.state, "fixture", self.sender).dispatch([], self.devices, now=1792886429)
        self.assertEqual(self.counts(), [])
        PushDispatcher(self.state, "fixture", self.sender).dispatch([], self.devices, now=1792886430)
        self.assertEqual(self.counts(), [{"receipt_ready": 1}])
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt], self.devices, now=1792887000)
        self.assertEqual(self.counts(), [{"receipt_ready": 1}])

    def test_report_without_app_channel_does_not_swallow_receipt(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        rows = [self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed"),
                self.event(2, "report.delivered", user_id="fixture", report_id="r1", channels=["herdr"], rounds=["one:1"])]
        dispatcher.dispatch(rows, self.devices, now=1792886400)
        dispatcher.dispatch([], self.devices, now=1792886430)
        self.assertEqual(self.counts(), [{"receipt_ready": 1}])

    def test_different_rounds_push_once_each_and_late_report_does_not_repeat(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        rows = [self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed"),
                self.event(2, "receipt.ready", job="one", user_id="fixture", round=2, outcome="completed"),
                self.event(3, "report.delivered", user_id="fixture", report_id="r1", channels=["app"], rounds=["one:1"])]
        dispatcher.dispatch(rows, self.devices, now=1792886400)
        self.assertEqual(self.counts(), [{"report_ready": 1}])
        dispatcher.dispatch([], self.devices, now=1792886430)
        self.assertEqual(self.counts(), [{"report_ready": 1}, {"receipt_ready": 1}])
        # 汇报晚于保留时间到达：回执已推过，同轮汇报不再重复。
        late = self.event(4, "report.delivered", user_id="fixture", report_id="r2", channels=["app"], rounds=["one:2"])
        dispatcher.dispatch([late], self.devices, now=1792886500)
        self.assertEqual(len(self.counts()), 2)

    def test_report_delivered_pushes_only_when_app_channel_succeeded(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        rows = [self.event(1, "report.delivered", user_id="fixture", report_id="r1", channels=["herdr"]),
                self.event(2, "report.delivered", user_id="fixture", report_id="r2", channels=["herdr", "app"]),
                self.event(3, "report.delivered", user_id="other", report_id="r3", channels=["app"])]
        dispatcher.dispatch(rows, self.devices, now=1792886400)
        self.assertEqual(self.sender.deliveries, [
            ("private-apns", {"aps": {"content-available": 1}, "counts": {"report_ready": 1}})])

    def test_dead_or_unreachable_must_persist_over_five_minutes_and_recovery_resets_timer(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        rows = [self.event(1, "job.state_changed", job="one", user_id="fixture", round=1, execution_state="dead"),
                self.event(2, "job.state_changed", job="two", user_id="fixture", round=1, execution_state="unreachable"),
                self.event(3, "job.state_changed", job="stopped", user_id="fixture", round=1, execution_state="dead", suppress_push=True)]
        dispatcher.dispatch(rows, self.devices, now=1792886400)
        dispatcher.dispatch(rows, self.devices, now=1792886700)
        self.assertEqual(self.sender.deliveries, [])
        # Persist the incident across a serve restart without restarting its timer.
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        dispatcher.dispatch(rows, self.devices, now=1792886701)
        self.assertEqual([payload["counts"] for _, payload in self.sender.deliveries], [{"dead": 1}, {"unreachable": 1}])
        dispatcher.dispatch(rows, self.devices, now=1792886800)
        self.assertEqual(len(self.sender.deliveries), 2)
        rows.append(self.event(4, "job.state_changed", job="one", user_id="fixture", round=1, execution_state="running"))
        dispatcher.dispatch(rows, self.devices, now=1792886801)
        rows.append(dict(self.event(5, "job.state_changed", job="one", user_id="fixture", round=2, execution_state="dead"),
                         time="2026-10-25T00:10:00+00:00"))
        dispatcher.dispatch(rows, self.devices, now=1792887000)
        self.assertEqual(len(self.sender.deliveries), 2)
        dispatcher.dispatch(rows, self.devices, now=1792887301)
        self.assertEqual(len(self.sender.deliveries), 3)

    def test_shutdown_cancellation_keeps_receipts_and_incidents_pending_for_restart(self):
        import threading
        from remote_dispatch.rd_push import PushCancelled
        for incident in (False, True):
            with self.subTest(incident=incident):
                state = self.state / str(incident)
                stop = threading.Event()
                stop.set()
                dispatcher = PushDispatcher(state, "fixture", self.sender, stop=stop)
                row = self.event(1, "job.state_changed" if incident else "receipt.ready", job="one",
                                 user_id="fixture", round=1, execution_state="dead", outcome="completed")
                with self.assertRaises(PushCancelled):
                    dispatcher.dispatch([row], self.devices, now=1792886701)
                count = len(self.sender.deliveries)
                PushDispatcher(state, "fixture", self.sender).dispatch([row], self.devices, now=1792886701)
                self.assertEqual(len(self.sender.deliveries), count + 1)


class PushAtLeastOnceTests(PushTests):
    def test_summary_delivered_without_rounds_does_not_swallow_later_receipt(self):
        dispatcher = PushDispatcher(self.state, "fixture", self.sender)
        receipt = self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed")
        summary = self.event(2, "report.delivered", user_id="fixture", report_id="d1", channels=["app"], rounds=[])
        dispatcher.dispatch([receipt, summary], self.devices, now=1792886400)
        self.assertEqual(self.counts(), [{"report_ready": 1}])
        dispatcher.dispatch([], self.devices, now=1792886430)
        self.assertEqual(self.counts(), [{"report_ready": 1}, {"receipt_ready": 1}])

    def test_crash_between_send_and_checkpoint_repeats_at_most_once_and_never_drops(self):
        from unittest.mock import patch
        from remote_dispatch import rd_messages
        receipt = self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed")
        report = self.event(2, "report.delivered", user_id="fixture", report_id="r1", channels=["app"], rounds=["one:1"])
        real = rd_messages.atomic_json
        calls = []

        def crash_on_report(path, data, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("崩溃")
            return real(path, data, **kwargs)
        with patch.object(rd_messages, "atomic_json", crash_on_report):
            with self.assertRaises(OSError):
                PushDispatcher(self.state, "fixture", self.sender).dispatch(
                    [receipt, report], self.devices, now=1792886400)
        self.assertEqual(self.counts(), [{"report_ready": 1}])
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt, report], self.devices, now=1792886401)
        self.assertEqual(self.counts(), [{"report_ready": 1}] * 2)
        PushDispatcher(self.state, "fixture", self.sender).dispatch([receipt, report], self.devices, now=1792887000)
        self.assertEqual(self.counts(), [{"report_ready": 1}] * 2)

    def test_hold_has_a_floor_and_each_due_item_is_checkpointed_after_its_send(self):
        for bad in (-100.0, float("nan")):
            self.assertGreaterEqual(PushDispatcher(self.state, "fixture", self.sender, hold=bad).hold, 0)
        rows = [self.event(1, "receipt.ready", job="one", user_id="fixture", round=1, outcome="completed"),
                self.event(2, "receipt.ready", job="two", user_id="fixture", round=1, outcome="completed")]
        PushDispatcher(self.state, "fixture", self.sender).dispatch(rows, self.devices, now=1792886400)

        class Slow:
            def __init__(self):
                self.n = 0

            def send(self, token, payload, *, stop):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("慢设备失败")
        slow = Slow()
        with self.assertRaises(RuntimeError):
            PushDispatcher(self.state, "fixture", slow).dispatch([], self.devices, now=1792886430)
        # 第一项已落盘为已推送；重启后只补第二项。
        PushDispatcher(self.state, "fixture", self.sender).dispatch([], self.devices, now=1792886431)
        self.assertEqual(self.counts(), [{"receipt_ready": 1}])
