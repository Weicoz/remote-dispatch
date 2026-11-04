"""通知通道隔离：慢通道不拖累其他通道；连续失败熔断；待发队列有上限。"""
import configparser
import contextlib
import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from remote_dispatch import rd_notify


def service(notify):
    cp = configparser.ConfigParser()
    cp.add_section("monitor")
    cp.set("monitor", "notify", notify)
    return SimpleNamespace(config=SimpleNamespace(cp=cp))


ROW = {"entries": [dict(title="t", host="h", round=1, state="completed", state_text="已完成")]}


class ChannelIsolationTests(unittest.TestCase):
    def test_slow_channel_does_not_delay_other_channel(self):
        release = threading.Event()
        slow = lambda svc, row: (release.wait(10), dict(channel="macos", ok=True, reason=None))[1]
        fast = lambda svc, row: dict(channel="herdr", ok=True, reason=None)
        svc = service("herdr,macos")
        with patch.dict(rd_notify.SENDERS, {"macos": slow, "herdr": fast}), patch.object(rd_notify, "WAIT_SECONDS", 0.5):
            started = time.monotonic()
            result = rd_notify.dispatch(svc, ROW)
            elapsed = time.monotonic() - started
        release.set()
        rd_notify.close(svc)
        self.assertLess(elapsed, 3)
        self.assertEqual(result["channels"][0], dict(channel="herdr", ok=True, reason=None))
        self.assertEqual(result["channels"][1], dict(channel="macos", ok=False, reason="timeout"))
        self.assertEqual(result["status"], "notified")

    def test_channels_run_in_parallel_not_in_series(self):
        def delayed(name):
            return lambda svc, row: (time.sleep(0.4), dict(channel=name, ok=True, reason=None))[1]
        svc = service("herdr,macos")
        with patch.dict(rd_notify.SENDERS, {"macos": delayed("macos"), "herdr": delayed("herdr")}):
            started = time.monotonic()
            rd_notify.dispatch(svc, ROW)
            self.assertLess(time.monotonic() - started, 0.75)
        rd_notify.close(svc)

    def test_consecutive_failures_open_breaker_then_half_open_recovers(self):
        now = [0.0]
        calls = []
        outcomes = [False, False, False, True]

        def sender(svc, row):
            calls.append(1)
            return dict(channel="macos", ok=outcomes.pop(0), reason=None)
        svc = service("macos")
        channel = rd_notify.Channel("macos", threshold=3, cooldown=100, clock=lambda: now[0])
        svc.notify_channels_state = {"macos": channel}
        err = io.StringIO()
        with patch.dict(rd_notify.SENDERS, {"macos": sender}), contextlib.redirect_stderr(err):
            for _ in range(3):
                rd_notify.dispatch(svc, ROW)
            self.assertEqual(len(calls), 3)
            skipped = rd_notify.dispatch(svc, ROW)
            self.assertEqual(skipped, dict(status="queued", channels=[]))
            self.assertEqual(len(calls), 3)
            now[0] = 101
            recovered = rd_notify.dispatch(svc, ROW)
        rd_notify.close(svc)
        self.assertTrue(recovered["channels"][0]["ok"])
        self.assertEqual(channel.failures, 0)
        text = err.getvalue()
        self.assertEqual(text.count("连续失败"), 1)
        self.assertIn("已恢复", text)

    def test_neutral_reasons_never_open_breaker(self):
        svc = service("app")
        channel = rd_notify.Channel("app", threshold=2)
        svc.notify_channels_state = {"app": channel}
        sender = lambda s, r: dict(channel="app", ok=False, reason="no_device")
        with patch.dict(rd_notify.SENDERS, {"app": sender}):
            for _ in range(5):
                rd_notify.dispatch(svc, ROW)
        rd_notify.close(svc)
        self.assertEqual(channel.failures, 0)

    def test_queue_is_bounded_and_drops_oldest(self):
        release = threading.Event()
        started = threading.Event()

        def blocker(svc, row):
            started.set()
            release.wait(10)
            return dict(channel="macos", ok=True, reason=None)
        channel = rd_notify.Channel("macos", queue_limit=2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            first = channel.submit(blocker, None, ROW)
            self.assertTrue(started.wait(5))
            queued = [channel.submit(blocker, None, ROW) for _ in range(4)]
        self.assertEqual(channel.dropped, 2)
        self.assertEqual(queued[0].result(1)["reason"], "queue_overflow")
        self.assertEqual(queued[1].result(1)["reason"], "queue_overflow")
        self.assertFalse(queued[3].done())
        self.assertIn("丢弃", err.getvalue())
        release.set()
        self.assertTrue(first.result(5)["ok"])
        self.assertTrue(queued[3].result(5)["ok"])
        channel.close()


class CancellationAndRefusalTests(unittest.TestCase):
    def test_wait_timeout_cancels_queued_item_so_it_is_not_sent_and_not_counted(self):
        release = threading.Event()
        sent = []

        def sender(svc, row):
            sent.append(row["id"])
            release.wait(10)
            return dict(channel="macos", ok=True, reason=None)
        svc = service("macos")
        channel = rd_notify.Channel("macos", threshold=1)
        svc.notify_channels_state = {"macos": channel}
        first = threading.Thread(target=rd_notify.dispatch, args=(svc, dict(ROW, id="one")))
        with patch.dict(rd_notify.SENDERS, {"macos": sender}), patch.object(rd_notify, "WAIT_SECONDS", 0.3):
            first.start()
            deadline = time.monotonic() + 5
            while not sent and time.monotonic() < deadline:
                time.sleep(0.01)
            second = rd_notify.dispatch(svc, dict(ROW, id="two"))
            release.set()
            first.join(5)
            time.sleep(0.3)
        rd_notify.close(svc)
        self.assertEqual(second["channels"], [dict(channel="macos", ok=False, reason="timeout")])
        self.assertEqual(sent, ["one"])
        self.assertEqual(channel.failures, 0)

    def test_open_breaker_leaves_report_queued_instead_of_inbox_only(self):
        svc = service("macos")
        channel = rd_notify.Channel("macos", threshold=1, cooldown=1000)
        svc.notify_channels_state = {"macos": channel}
        failing = lambda s, r: dict(channel="macos", ok=False, reason="notify_failed")
        with patch.dict(rd_notify.SENDERS, {"macos": failing}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rd_notify.dispatch(svc, ROW)["status"], "inbox_only")
            self.assertEqual(rd_notify.dispatch(svc, ROW), dict(status="queued", channels=[]))
        rd_notify.close(svc)

    def test_queued_result_is_not_recorded_and_is_retried_later(self):
        from remote_dispatch.rd_reports import Reports
        reports = Reports.__new__(Reports)
        reports.inflight, reports.retry_at, reports.clock = {"r1"}, {}, 100.0
        reports.notify = lambda saved, row: dict(status="queued", channels=[])
        reports.record = lambda row, result: self.fail("队列中的汇报不得写结果")
        reports.deliver(dict(id="r1"))
        self.assertEqual(reports.retry_at, {"r1": 130.0})
        self.assertEqual(reports.inflight, set())

    def test_close_marks_reports_closed_before_closing_channels(self):
        from remote_dispatch import rd_reports
        reports = Reports = rd_reports.Reports.__new__(rd_reports.Reports)
        reports.thread, reports.pool, reports.futures = None, None, []
        reports.lock, reports.closed, reports.service = threading.RLock(), False, object()
        seen = []
        with patch.object(rd_reports.notifier, "close", lambda svc: seen.append(reports.closed)):
            Reports.close()
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
