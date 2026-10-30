"""汇报提醒接缝：真实 serve、inbox 与事件；隔离的 Herdr 替身断言会话输入零写入。"""
import json
import threading
import time
import unittest
from unittest.mock import patch

from remote_dispatch import rd_events as events
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_notify
from remote_dispatch import rd_reports
from tests import test_rd_reports as reports_test
from tests import test_rd_serve as serve_test


class ReportDeliveryTests(unittest.TestCase):
    request = serve_test.ServeTests.request
    eventually = serve_test.ServeTests.eventually
    setUp = reports_test.ReportTests.setUp
    assert_structure = reports_test.ReportTests.assert_structure
    agent = reports_test.ReportTests.agent
    save_fixture = reports_test.ReportTests.save_fixture
    saved_session = reports_test.ReportTests.saved_session
    transcript = reports_test.ReportTests.transcript
    service = reports_test.ReportTests.service
    job = reports_test.ReportTests.job
    receipt = reports_test.ReportTests.receipt
    reports = reports_test.ReportTests.reports

    def writes_sent(self):
        return [json.loads(line) for line in self.writes.read_text().splitlines()] if self.writes.exists() else []

    def stored(self):
        return json.loads((self.state / 'reports' / 'fixture.json').read_text())

    def tick_report(self, service, start=0):
        service.reports.tick(now=start)
        service.reports.tick(now=start + 5)

    def test_report_generation_never_writes_session_input_in_any_session_state(self):
        cases = {
            'idle': lambda: None,
            'working': lambda: self.data['agents'][0].update(agent_status='working'),
            'blocked': lambda: self.data['agents'][0].update(agent_status='blocked'),
            'unverified-harness': lambda: self.data['agents'][0].update(
                agent='kimi', agent_session=dict(agent='kimi', kind='id', value='session-one')),
            'closed': lambda: self.data.update(agents=[]),
            'herdr-unavailable': lambda: self.data.update(unavailable=True),
        }
        self.config.write_text('[monitor]\nnotify=none\n')
        for name, change in cases.items():
            with self.subTest(case=name):
                change()
                self.save_fixture()
                self.receipt(self.job(name))
                service = self.service()
                self.tick_report(service)
                rows = [r for r in service.read('/v1/reports')[1]['reports']
                        if r['entries'][0]['job'] == name]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['delivery'], dict(status='inbox_only', channels=[]))
                self.assertEqual(self.writes_sent(), [])
                service.close()

    def test_session_access_switch_does_not_change_report_handling(self):
        self.config.write_text('[serve]\nsession_access=false\n[monitor]\nnotify=none\n')
        self.data['unavailable'] = True
        self.save_fixture()
        self.receipt(self.job())
        service = self.service()
        self.tick_report(service)
        report = self.reports(service)[0]
        self.assertEqual(report['delivery'], dict(status='inbox_only', channels=[]))
        self.assertEqual(self.writes_sent(), [])
        created = [e for e in events.read_after(self.state, 0) if e['type'] == 'report.created']
        self.assertEqual(len(created), 1)

    def test_new_report_publishes_created_but_no_delivered_event_and_survives_restart(self):
        self.config.write_text('[monitor]\nnotify=none\n')
        self.receipt(self.job())
        service = self.service()
        self.tick_report(service)
        report = self.reports(service)[0]
        service.close()
        service = self.service()
        service.reports.tick(now=60)
        self.assertEqual(self.reports(service)[0], report)
        types = [e['type'] for e in events.read_after(self.state, 0)]
        self.assertEqual(types.count('report.created'), 1)
        self.assertNotIn('report.delivered', types)
        self.assertEqual(self.writes_sent(), [])

    def test_digest_report_uses_the_same_delivery_result_without_input(self):
        self.config.write_text('[monitor]\ndigest_interval_seconds=10\nnotify=none\n')
        self.receipt(self.job(), summary='摘要')
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        rows = service.read('/v1/reports')[1]['reports']
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row['delivery'], dict(status='inbox_only', channels=[]))
        self.assertEqual(self.writes_sent(), [])

    def test_slow_channel_does_not_delay_report_creation_in_background_mode(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        release = threading.Event()
        slow = lambda svc, row: (release.wait(10), dict(channel='herdr', ok=True, reason=None))[1]
        with patch.dict(rd_notify.SENDERS, {'herdr': slow}):
            service.reports.tick(now=0, background=True)
            started = time.monotonic()
            service.reports.tick(now=5, background=True)
            self.assertLess(time.monotonic() - started, 2)
            created = [e for e in events.read_after(str(self.state), 0) if e['type'] == 'report.created']
            self.assertEqual(len(created), 1)
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'queued')
            release.set()
            self.eventually(lambda: self.stored()['reports'][0]['delivery']['status'],
                            lambda status: status == 'notified')
        delivered = [e for e in events.read_after(str(self.state), 0) if e['type'] == 'report.delivered']
        self.assertEqual(len(delivered), 1)
        service.close()

    def test_event_write_retry_does_not_notify_same_report_twice(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        calls = []
        sender = lambda svc, row: (calls.append(row['id']), dict(channel='herdr', ok=True, reason=None))[1]
        real_emit = events.emit
        failures = [1]

        def flaky(state, kind, data):
            if kind == 'report.created' and failures:
                failures.pop()
                raise OSError('模拟事件写入失败')
            return real_emit(state, kind, data)
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}), patch.object(events, 'emit', flaky):
            service.reports.tick(now=0)
            with self.assertRaises(OSError):
                service.reports.tick(now=5)
            service.reports.tick(now=6)
        self.assertEqual(len(calls), 1)
        service.close()

    def start_slow_delivery(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        release = threading.Event()
        slow = lambda svc, row: (release.wait(10), dict(channel='herdr', ok=True, reason=None))[1]
        patcher = patch.dict(rd_notify.SENDERS, {'herdr': slow})
        patcher.start()
        self.addCleanup(patcher.stop)
        service.reports.tick(now=0, background=True)
        service.reports.tick(now=5, background=True)
        return service, release

    def test_delivery_finishing_within_close_grace_is_recorded(self):
        service, release = self.start_slow_delivery()
        closer = threading.Thread(target=service.close)
        closer.start()
        time.sleep(0.2)
        release.set()
        closer.join(10)
        self.assertFalse(closer.is_alive())
        self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'notified')

    def test_late_notify_thread_after_close_grace_does_not_write_report_store(self):
        service, release = self.start_slow_delivery()
        real_wait = rd_reports.wait_futures
        with patch.object(rd_reports, 'wait_futures', lambda futures, timeout: real_wait(futures, timeout=0.2)):
            service.close()
        before = (self.state / 'reports' / 'fixture.json').read_bytes()
        release.set()
        time.sleep(0.3)
        self.assertEqual((self.state / 'reports' / 'fixture.json').read_bytes(), before)
        self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'queued')
        self.assertEqual(service.reports.inflight, set())

    def test_non_io_record_failure_backs_off_instead_of_redelivering_every_tick(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        calls = []
        sender = lambda svc, row: (calls.append(row['id']), dict(channel='herdr', ok=True, reason=None))[1]
        real_atomic = messages.atomic_json
        broken = [True]

        def flaky(path, data, **kwargs):
            if broken and any(r.get('delivery', {}).get('status') == 'notified' for r in data.get('reports', [])):
                raise RuntimeError('模拟非 IO 异常')
            return real_atomic(path, data, **kwargs)
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}), patch.object(messages, 'atomic_json', flaky):
            service.reports.tick(now=0)
            service.reports.tick(now=5)
            service.reports.tick(now=6)
            service.reports.tick(now=20)
            self.assertEqual(len(calls), 1)
            broken.clear()
            service.reports.tick(now=40)
        self.assertEqual(len(calls), 2)
        service.close()

    def test_legacy_records_keep_old_status_and_reason_keys_untouched(self):
        self.receipt(self.job())
        service = self.service()
        self.tick_report(service)
        saved = self.stored()
        entries = saved['reports'][0]['entries']
        legacy = [
            ('queued', 'session_not_idle', None), ('sending', 'delivery_result_unknown', None),
            ('delivered', None, '2026-10-26T00:00:00+00:00'),
            ('text_delivered', 'session_submit_failed', '2026-10-26T00:00:00+00:00'),
            ('inbox_only', 'harness_not_verified', None)]
        for i, (status, reason, at) in enumerate(legacy):
            saved['reports'].append(dict(id='legacy-%d' % i, initiator_session='session-one',
                                         created_at='2026-10-26T00:00:00+00:00', entries=entries,
                                         delivery=dict(status=status, reason=reason, pane_id='w1:p1',
                                                       delivered_at=at)))
        service.close()
        (self.state / 'reports' / 'fixture.json').write_text(json.dumps(saved))
        service = self.service()
        service.reports.tick(now=60)
        service.reports.tick(now=120)
        rows = {r['id']: r['delivery'] for r in service.read('/v1/reports')[1]['reports']}
        for i, (status, reason, at) in enumerate(legacy):
            self.assertEqual(rows['legacy-%d' % i],
                             dict(status=status, reason=reason, pane_id='w1:p1', delivered_at=at))
        self.assertEqual(self.stored()['reports'][1:], saved['reports'][1:])
        self.assertEqual(self.writes_sent(), [])

    def test_contract_examples_match_new_and_historical_delivery_shapes(self):
        self.config.write_text('[monitor]\nnotify=none\n')
        self.receipt(self.job(title='token=title-marker'))
        service = self.service()
        self.tick_report(service)
        body = service.read('/v1/reports')[1]
        self.assert_structure(body, json.loads((serve_test.CONTRACTS / 'reports.json').read_text()))
        self.assertEqual(body['reports'][0]['delivery']['status'], 'inbox_only')
        example = json.loads((serve_test.CONTRACTS / 'report-delivered.json').read_text())
        self.assertEqual(example['data']['channels'], ['herdr'])
        historical = json.loads((serve_test.CONTRACTS / 'reports-unsubmitted.json').read_text())
        self.assertEqual(historical['reports'][0]['delivery']['status'], 'text_delivered')
        for forbidden in ('title-marker', 'env_file', 'private_key'):
            self.assertNotIn(forbidden, json.dumps(body))

    def test_queued_report_is_delivered_once_after_restart(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        calls = []
        sender = lambda svc, row: (calls.append(row['id']), dict(channel='herdr', ok=True, reason=None))[1]
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}):
            with patch.object(type(service.reports), 'deliver', lambda self, row: None):
                self.tick_report(service)
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'queued')
            service.close()
            service = self.service()
            service.reports.tick(now=60)
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'notified')
            service.reports.tick(now=120)
            service.close()
            service = self.service()
            service.reports.tick(now=180)
        self.assertEqual(len(calls), 1)
        delivered = [e for e in events.read_after(str(self.state), 0) if e['type'] == 'report.delivered']
        self.assertEqual(len(delivered), 1)
        # 终态汇报的 rounds 必须经事件投影保留，推送端才能按轮次去重。
        self.assertEqual(delivered[0]['data']['rounds'], ['fixture-job:1'])
        service.close()

    def test_task_cancelled_at_shutdown_is_redelivered_after_restart(self):
        from remote_dispatch import rd_reports
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        release = threading.Event()
        calls = []

        def sender(svc, row):
            calls.append(threading.current_thread().name)
            if threading.current_thread().name.startswith('rd-notify'):
                release.wait(10)
            return dict(channel='herdr', ok=True, reason=None)
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}), \
                patch.object(rd_reports, 'wait_futures', lambda *a, **k: None):
            service.reports.tick(now=0, background=True)
            service.reports.tick(now=5, background=True)
            self.eventually(lambda: len(calls), lambda n: n >= 1)
            service.close()
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'queued')
            service = self.service()
            service.reports.tick(now=60)
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'notified')
            release.set()
        service.close()

    def test_failed_delivery_writeback_is_retried_not_dropped(self):
        self.config.write_text('[monitor]\nnotify=herdr\n')
        self.receipt(self.job())
        service = self.service()
        sender = lambda svc, row: dict(channel='herdr', ok=True, reason=None)
        real_emit = events.emit
        failures = [1]

        def flaky(state, kind, data):
            if kind == 'report.delivered' and failures:
                failures.pop()
                raise OSError('模拟回写失败')
            return real_emit(state, kind, data)
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}), patch.object(events, 'emit', flaky):
            self.tick_report(service)
            self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'queued')
            service.reports.tick(now=100)
        self.assertEqual(self.stored()['reports'][0]['delivery']['status'], 'notified')
        delivered = [e for e in events.read_after(str(self.state), 0) if e['type'] == 'report.delivered']
        self.assertEqual(len(delivered), 1)
        service.close()

    def test_summary_report_delivered_event_carries_no_rounds(self):
        from remote_dispatch import cli
        self.config.write_text('[monitor]\ndigest_interval_seconds=10\nnotify=herdr\nnotify_digest=true\n')
        self.job('digestjob')
        cli.messages.atomic_json(self.state / 'serve-snapshots' / 'digestjob.json', dict(
            user_id='fixture', generation=[1, 'target', None],
            snapshot=dict(state='running', round='1', snapshot_at=cli.messages.utc_now()),
            progress=dict(summary='进度', phase='build')))
        service = self.service()
        sender = lambda svc, row: dict(channel='herdr', ok=True, reason=None)
        with patch.dict(rd_notify.SENDERS, {'herdr': sender}):
            service.reports.tick(now=0)
            service.reports.tick(now=15)
        summaries = {r['id'] for r in self.stored()['reports'] if r.get('kind') == 'digest'}
        delivered = [e['data'] for e in events.read_after(str(self.state), 0) if e['type'] == 'report.delivered']
        mine = [d for d in delivered if d['report_id'] in summaries]
        self.assertTrue(mine)
        for data in mine:
            self.assertEqual(data['rounds'], [])
        service.close()

    def test_user_terminal_input_still_reaches_the_pane(self):
        service = self.service()
        status = service.sessions.write_input('w1:p1', 'user-input', {'text': '用户消息'}, None)[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.writes_sent(), [['pane', 'send-text', 'w1:p1', '--', '用户消息'],
                                              ['pane', 'send-keys', 'w1:p1', 'enter']])
