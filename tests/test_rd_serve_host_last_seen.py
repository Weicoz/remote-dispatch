"""最后在线时间经真实 Unix HTTP 读取；只替换探针 RPC 和时钟边界。"""
import json
import threading
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve as base


class HostLastSeenTests(unittest.TestCase):
    setUp = base.ServeTests.setUp
    start = base.ServeTests.start
    request = base.ServeTests.request
    eventually = base.ServeTests.eventually
    snapshot = base.ServeTests.snapshot

    def test_successful_probe_time_refreshes_and_survives_offline_and_restart(self):
        self.config.write_text("[host:target]\nssh=target.invalid\nplatform=windows\n"
                               "[host:never]\nssh=never.invalid\nplatform=windows\n")
        online = True

        def probe(host, action, *args, **kwargs):
            if host['name'] == 'never' or not online:
                raise OSError('目标机离线')
            return dict(os='Windows', ncpu='8', cpu_pct='20', memfree='80', disk='100')

        with patch.object(cli.native, 'request', side_effect=probe), \
             patch.object(cli.messages, 'utc_now', return_value='2026-10-26T01:00:00+00:00') as now:
            service = self.start(poll_interval=0.05)
            rows = self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'],
                                   lambda rows: all(row['probed'] for row in rows))
            self.assertEqual([row['last_seen_at'] for row in rows],
                             ['2026-10-26T01:00:00+00:00', None])
            now.return_value = '2026-10-26T01:01:00+00:00'
            self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'][0],
                            lambda row: row['last_seen_at'] == '2026-10-26T01:01:00+00:00')
            online = False
            rows = self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'],
                                   lambda rows: not rows[0]['reachable'])
            self.assertEqual([row['last_seen_at'] for row in rows],
                             ['2026-10-26T01:01:00+00:00', None])
            example = json.loads((base.CONTRACTS / 'hosts-offline.json').read_text())
            self.assertEqual(rows[0], example['response']['body']['hosts'][0])
            service.close()
            now.return_value = '2026-10-26T02:00:00+00:00'
            service = self.start(poll_interval=3600)
            rows = self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'],
                                   lambda rows: all(row['probed'] for row in rows))
            self.assertEqual([row['last_seen_at'] for row in rows],
                             ['2026-10-26T01:01:00+00:00', None])
            service.close()

    def test_restart_reads_saved_time_while_background_probe_is_blocked(self):
        self.config.write_text('[host:target]\nssh=target.invalid\nplatform=windows\n')
        with patch.object(cli.native, 'request', return_value=dict(os='Windows')), \
             patch.object(cli.messages, 'utc_now', return_value='2026-10-26T01:00:00+00:00'):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'][0], lambda row: row['probed'])
            service.close()
        entered, release = threading.Event(), threading.Event()
        callers = []

        def probe(*args, **kwargs):
            callers.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise AssertionError('后台探针未释放')
            return {}

        with patch.object(cli.native, 'request', side_effect=probe):
            service = self.start(poll_interval=3600)
            try:
                self.assertTrue(entered.wait(2))
                before = self.snapshot()
                for _ in range(3):
                    status, body = self.request('/v1/hosts')
                    self.assertEqual(status, 200)
                    self.assertEqual(body['hosts'][0]['last_seen_at'], '2026-10-26T01:00:00+00:00')
                    self.assertFalse(body['hosts'][0]['probed'])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(callers), 1)
            finally:
                release.set()
                service.close()

    def test_failed_posix_probe_retains_the_last_successful_time(self):
        self.config.write_text('[host:target]\nssh=local\n')
        with patch.object(cli, 'PROBE', 'echo os=Darwin'), \
             patch.object(cli.messages, 'utc_now', return_value='2026-10-26T01:00:00+00:00'):
            service = self.start(poll_interval=3600)
            self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'][0], lambda row: row['probed'])
            service.close()
        with patch.object(cli, 'PROBE', 'exit 1'):
            service = self.start(poll_interval=3600)
            row = self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'][0], lambda row: row['probed'])
            self.assertFalse(row['reachable'])
            self.assertEqual(row['last_seen_at'], '2026-10-26T01:00:00+00:00')
            service.close()

    def test_invalid_saved_times_do_not_prevent_startup_or_leak_sensitive_fields(self):
        self.config.write_text('[host:target]\nssh=local\n')
        self.state.mkdir()
        saved = self.state / 'serve-host-last-seen.json'
        with patch.object(cli, 'PROBE', 'exit 1'):
            for content in ('{', '[]', '{"target": "token=PRIVATE_MARKER", "auth_file": "PRIVATE_MARKER"}'):
                with self.subTest(content=content):
                    saved.write_text(content)
                    service = self.start(poll_interval=3600)
                    row = self.eventually(lambda: self.request('/v1/hosts')[1]['hosts'][0],
                                          lambda row: row['probed'])
                    self.assertIsNone(row['last_seen_at'])
                    for forbidden in ('PRIVATE_MARKER', 'token', 'auth_file'):
                        self.assertNotIn(forbidden, json.dumps(row))
                    service.close()
