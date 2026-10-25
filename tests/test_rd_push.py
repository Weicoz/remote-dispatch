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
        self.assertEqual(self.sender.deliveries, [
            ("private-apns", {"aps": {"content-available": 1}, "counts": {"receipt_ready": 1}}),
            ("private-apns", {"aps": {"content-available": 1}, "counts": {"blocked": 1}})])
        # Replaying after restart must not redeliver.
        PushDispatcher(self.state, "fixture", self.sender).dispatch(rows, self.devices, now=1792886400)
        self.assertEqual(len(self.sender.deliveries), 2)

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
