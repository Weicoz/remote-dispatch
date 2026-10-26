"""汇报送达接缝：真实 serve、inbox 与事件，隔离的 Herdr 可执行替身。"""
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from unittest.mock import patch
import unittest

from remote_dispatch import rd_events as events
from tests import test_rd_reports as reports_test
from tests import test_rd_serve as serve_test


class ReportDeliveryTests(unittest.TestCase):
    request = serve_test.ServeTests.request
    eventually = serve_test.ServeTests.eventually
    open_events = serve_test.ServeTests.open_events
    next_event = serve_test.ServeTests.next_event
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

    def test_report_text_removes_controls_flattens_lines_and_limits_whole_message(self):
        controls = ''.join(chr(n) for n in range(32)) + ''.join(chr(n) for n in range(127, 160))
        self.receipt(self.job(title='标题\x1b[31m\0\n下一行'),
                     summary='摘要\r\n第二行\u2028第三行' + controls + '尾部' + '长' * 10000)
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        writes = self.writes_sent()
        self.assertEqual(len(writes), 2)
        text = writes[0][4]
        self.assertIn('标题[31m', text)
        self.assertIn('摘要 | 第二行 | 第三行', text)
        self.assertFalse(any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text))
        self.assertNotIn('\u2028', text)
        self.assertLessEqual(len(text), 8000)
        self.assertEqual(self.reports(service)[0]['delivery']['status'], 'delivered')

    def test_idle_report_is_written_to_herdr_and_delivered_once_across_restart(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        writes = self.writes_sent()
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][:4], ['pane', 'send-text', 'w1:p1', '--'])
        for content in ('实现汇报', 'target', '1', '已完成', '检查已通过'):
            self.assertIn(content, writes[0][4])
        self.assertEqual(writes[1], ['pane', 'send-keys', 'w1:p1', 'enter'])
        report = self.reports(service)[0]
        self.assertEqual(report['delivery']['status'], 'delivered')
        delivered = [e for e in events.read_after(self.state, 0) if e['type'] == 'report.delivered']
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['data']['report_id'], report['id'])
        service.close()
        service = self.service()
        service.reports.tick(now=60)
        self.assertEqual(self.writes_sent(), writes)
        self.assertEqual(self.reports(service)[0], report)

    def test_cleaning_cannot_reassemble_a_sensitive_value_in_sent_text(self):
        self.receipt(self.job(), summary='摘要 PRI\0VATE_MARKER')
        service = self.service()
        service.sensitive.add('PRIVATE_MARKER')
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        text = self.writes_sent()[0][4]
        self.assertNotIn('PRIVATE_MARKER', text)
        self.assertIn('[redacted]', text)

    def test_invalid_input_does_not_abort_other_sessions_in_same_delivery_batch(self):
        self.transcript(identity='session-two')
        self.data['agents'].append(self.agent('w1:p2', agent_session=dict(
            agent='codex', kind='id', value='session-two')))
        self.save_fixture()
        self.receipt(self.job('first'))
        self.receipt(self.job('second', initiator_session='session-two'))
        service = self.service()
        service.reports.tick(now=0)
        run = subprocess.run
        def reject_first(command, *args, **kwargs):
            if command[:3] == ['herdr', 'pane', 'send-text'] and command[3] == 'w1:p1':
                raise ValueError('fixture invalid argument')
            return run(command, *args, **kwargs)
        with patch.object(subprocess, 'run', side_effect=reject_first):
            service.reports.tick(now=5)
        rows = service.read('/v1/reports')[1]['reports']
        self.assertEqual([r['delivery']['status'] for r in rows], ['inbox_only', 'delivered'])
        self.assertEqual(rows[0]['delivery']['reason'], 'session_input_failed')
        self.assertEqual([a[2] for a in self.writes_sent()], ['w1:p2', 'w1:p2'])

    def test_working_reports_wait_and_leave_in_fifo_order_after_restart(self):
        self.data['agents'][0]['agent_status'] = 'working'
        self.save_fixture()
        self.receipt(self.job('first', title='先到的汇报'))
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        self.assertEqual(self.writes_sent(), [])
        self.assertEqual(self.reports(service)[0]['delivery']['status'], 'queued')
        self.receipt(self.job('second', title='后到的汇报'))
        service.reports.tick(now=6)
        service.reports.tick(now=11)
        service.close()
        self.data['agents'][0]['agent_status'] = 'idle'
        self.save_fixture()
        service = self.service()
        service.reports.tick(now=12)
        texts = [a[4] for a in self.writes_sent() if a[1] == 'send-text']
        self.assertEqual(len(texts), 2)
        self.assertIn('先到的汇报', texts[0])
        self.assertIn('后到的汇报', texts[1])
        self.assertEqual([r['delivery']['status'] for r in self.reports(service)], ['delivered', 'delivered'])

    def test_failed_input_stays_in_inbox_without_retry_and_does_not_block_next_report(self):
        self.data['fail_input'] = True
        self.save_fixture()
        self.receipt(self.job('first'))
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        report = self.reports(service)[0]
        self.assertEqual(report['delivery']['status'], 'inbox_only')
        self.assertEqual(report['delivery']['reason'], 'session_input_failed')
        self.assertEqual(len(self.writes_sent()), 1)  # Text may already have crossed the boundary.
        service.close()
        self.data['fail_input'] = False
        self.save_fixture()
        service = self.service()
        self.receipt(self.job('second', title='继续发送下一条'))
        service.reports.tick(now=6)
        service.reports.tick(now=11)
        texts = [a[4] for a in self.writes_sent() if a[1] == 'send-text']
        self.assertEqual(len(texts), 2)
        self.assertIn('继续发送下一条', texts[1])
        self.assertEqual([r['delivery']['status'] for r in self.reports(service)], ['inbox_only', 'delivered'])
        self.assertEqual(sum(e['type'] == 'report.delivered' for e in events.read_after(self.state, 0)), 1)

    def test_enter_failure_preserves_text_delivery_without_retry_or_success_event(self):
        self.data['fail_enter'] = True
        self.save_fixture()
        self.receipt(self.job(title='token=title-marker'), summary='password=summary-marker')
        self.data['agents'][0].update(pane_id='w1:p1 token=pane-marker', agent_session=dict(
            agent='codex', kind='id', value='session-one'))
        self.save_fixture()
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        report = self.reports(service)[0]
        self.assertEqual(report['delivery']['status'], 'text_delivered')
        self.assertEqual(report['delivery']['reason'], 'session_submit_failed')
        self.assertIsNotNone(report['delivery']['delivered_at'])
        example = json.loads((serve_test.CONTRACTS / 'reports-unsubmitted.json').read_text())
        self.assert_structure({'reports': [report]}, example)
        self.assertEqual(report['delivery']['status'], example['reports'][0]['delivery']['status'])
        self.assertEqual(report['delivery']['reason'], example['reports'][0]['delivery']['reason'])
        serialized = json.dumps([report, service.read('/v1/reports')[1],
                                 json.loads(service.reports.path.read_text())])
        for forbidden in ('title-marker', 'summary-marker', 'pane-marker', 'env_file', 'private_key'):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('title-marker', self.writes_sent()[0][4])
        self.assertNotIn('summary-marker', self.writes_sent()[0][4])
        self.assertEqual(len(self.writes_sent()), 2)
        service.close()
        self.data['fail_enter'] = False
        self.save_fixture()
        service = self.service()
        service.reports.tick(now=60)
        self.assertEqual(self.reports(service)[0], report)
        self.assertEqual(len(self.writes_sent()), 2)
        self.assertFalse(any(e['type'] == 'report.delivered' for e in events.read_after(self.state, 0)))

    def test_closed_or_missing_herdr_session_keeps_report_in_inbox_without_writing(self):
        self.receipt(self.job(initiator_session='closed-session'))
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        reports = service.read('/v1/reports?initiator_session=closed-session')[1]['reports']
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['delivery']['reason'], 'session_closed')
        self.assertEqual(reports[0]['delivery']['status'], 'inbox_only')
        self.assertEqual(self.writes_sent(), [])

    def test_session_closing_after_collection_does_not_use_stale_pane(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        self.data['agents'] = []
        self.save_fixture()  # Deliberately retain the cached session list.
        service.reports.tick(now=5)
        self.assertEqual(self.writes_sent(), [])
        self.assertEqual(self.reports(service)[0]['delivery']['reason'], 'session_closed')

    def test_only_verified_codex_and_claude_harnesses_receive_reports(self):
        for harness in ('kimi', 'agy', 'claude'):
            with self.subTest(harness=harness):
                self.data['agents'][0].update(agent=harness, agent_session=dict(
                    agent=harness, kind='id', value='session-one'))
                self.save_fixture()
                self.receipt(self.job(harness, initiator_harness=harness))
                service = self.service()
                service.reports.tick(now=0)
                service.reports.tick(now=5)
                delivery = self.reports(service)[-1]['delivery']
                if harness == 'claude':
                    self.assertEqual(delivery['status'], 'delivered')
                    self.assertEqual(len(self.writes_sent()), 2)
                else:
                    self.assertEqual(delivery['status'], 'inbox_only')
                    self.assertEqual(delivery['reason'], 'harness_not_verified')
                    self.assertEqual(self.writes_sent(), [])
                service.close()

    def test_session_access_switch_off_keeps_inbox_without_even_querying_herdr(self):
        self.config.write_text('[serve]\nsession_access=false\n')
        self.data['unavailable'] = True
        self.save_fixture()
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        self.assertEqual(self.writes_sent(), [])
        delivery = self.reports(service)[0]['delivery']
        self.assertEqual(delivery['status'], 'inbox_only')
        self.assertEqual(delivery['reason'], 'session_access_disabled')

    def test_crash_after_external_send_never_repeats_an_uncertain_delivery(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        replace = os.replace
        def fail_after_send(source, destination):
            if str(destination).endswith('/reports/fixture.json') and len(self.writes_sent()) == 2:
                raise OSError('fixture disk full after external send')
            return replace(source, destination)
        with patch.object(os, 'replace', side_effect=fail_after_send), self.assertRaises(OSError):
            service.reports.tick(now=5)
        service.close()
        service = self.service()
        service.reports.tick(now=6)
        delivery = self.reports(service)[0]['delivery']
        self.assertEqual(delivery['status'], 'inbox_only')
        self.assertEqual(delivery['reason'], 'delivery_result_unknown')
        self.assertEqual(len(self.writes_sent()), 2)
        self.assertFalse(any(e['type'] == 'report.delivered' for e in events.read_after(self.state, 0)))

    def test_failed_delivery_event_is_recovered_without_sending_report_again(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        replace = os.replace
        def fail_delivery_event(source, destination):
            if str(destination).endswith('/events.jsonl') and self.writes_sent():
                raise OSError('fixture disk full publishing delivery event')
            return replace(source, destination)
        with patch.object(os, 'replace', side_effect=fail_delivery_event), self.assertRaises(ValueError):
            service.reports.tick(now=5)
        self.assertEqual(self.reports(service)[0]['delivery']['status'], 'delivered')
        service.close()
        service = self.service()
        service.reports.tick(now=6)
        service.reports.tick(now=7)
        self.assertEqual(len(self.writes_sent()), 2)
        self.assertEqual(sum(e['type'] == 'report.delivered' for e in events.read_after(self.state, 0)), 1)

    def test_delivery_contract_and_sensitive_scan_cover_new_fields(self):
        from tests.test_rd_serve import CONTRACTS, ServeTests
        self.receipt(self.job(title='token=title-marker', initiator_harness='codex'))
        self.data['agents'][0].update(pane_id='w1:p1 token=delivery-marker', agent_session=dict(
            agent='codex', kind='id', value='session-one'))
        self.save_fixture()
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        body = service.read('/v1/reports')[1]
        delivered = next(e for e in events.read_after(self.state, 0) if e['type'] == 'report.delivered')
        ServeTests.assert_structure(self, body, json.loads((CONTRACTS / 'reports.json').read_text()))
        ServeTests.assert_structure(self, delivered, json.loads((CONTRACTS / 'report-delivered.json').read_text()))
        for forbidden in ('title-marker', 'delivery-marker', 'env_file', 'private_key'):
            self.assertNotIn(forbidden, json.dumps([body, delivered]))
        self.assertNotIn('delivery-marker', service.reports.path.read_text())
        self.assertNotIn('title-marker', str(self.writes_sent()))

    def test_report_input_cannot_interleave_with_user_text_and_enter(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        run = subprocess.run
        user_started, release_user = threading.Event(), threading.Event()
        def during_user_input(command, *args, **kwargs):
            if command[:3] == ['herdr', 'pane', 'send-text'] and command[-1] == '用户消息':
                user_started.set()
                if not release_user.wait(5):
                    raise RuntimeError('用户输入测试未释放')
            return run(command, *args, **kwargs)
        # Two callers are necessary here to exercise the shared input boundary.
        with patch.object(subprocess, 'run', side_effect=during_user_input), ThreadPoolExecutor(max_workers=2) as pool:
            user = pool.submit(service.sessions.write_input, 'w1:p1', 'user-input', {'text': '用户消息'}, None)
            try:
                self.assertTrue(user_started.wait(5))
                report = pool.submit(service.reports.tick, now=5)
                with self.assertRaises(FutureTimeout):
                    report.result(timeout=0.1)
                self.assertEqual(self.writes_sent(), [])
            finally:
                release_user.set()
            self.assertEqual(user.result(timeout=5)[0], 200)
            report.result(timeout=5)
        writes = self.writes_sent()
        self.assertEqual(writes[:2], [['pane', 'send-text', 'w1:p1', '--', '用户消息'],
                                     ['pane', 'send-keys', 'w1:p1', 'enter']])
        self.assertEqual(len(writes), 4)

    def test_session_becoming_idle_between_checks_cannot_let_second_report_overtake(self):
        self.data['agents'][0]['agent_status'] = 'working'
        self.save_fixture()
        self.receipt(self.job('first', title='第一条'))
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        self.receipt(self.job('second', title='第二条'))
        service.reports.tick(now=6)
        service.reports.tick(now=11)
        run = subprocess.run
        def become_idle_after_first_check(command, *args, **kwargs):
            result = run(command, *args, **kwargs)
            if command == ['herdr', 'agent', 'list']:
                self.data['agents'][0]['agent_status'] = 'idle'
                self.save_fixture()
            return result
        with patch.object(subprocess, 'run', side_effect=become_idle_after_first_check):
            service.reports.tick(now=12)
        self.assertEqual(self.writes_sent(), [])
        service.reports.tick(now=13)
        texts = [a[4] for a in self.writes_sent() if a[1] == 'send-text']
        self.assertEqual(len(texts), 2)
        self.assertIn('第一条', texts[0])
        self.assertIn('第二条', texts[1])

    def test_replacement_pane_with_only_probable_old_identity_never_receives_report(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        (self.herdr_config.parent / 'session.json').unlink()
        self.data['agents'] = [self.agent('w1:p2')]
        self.save_fixture()  # Only the old transcript remains in the same cwd.
        service.reports.tick(now=5)
        self.assertEqual(self.writes_sent(), [])
        delivery = self.reports(service)[0]['delivery']
        self.assertEqual(delivery['status'], 'inbox_only')
        self.assertEqual(delivery['reason'], 'session_identity_unconfirmed')

    def test_herdr_outage_keeps_inbox_without_using_cached_session_or_repeated_input(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        self.data['unavailable'] = True
        self.save_fixture()
        service.reports.tick(now=5)
        delivery = self.reports(service)[0]['delivery']
        self.assertEqual(delivery['status'], 'inbox_only')
        self.assertEqual(delivery['reason'], 'herdr_unavailable')
        self.data['unavailable'] = False
        self.save_fixture()
        service.reports.tick(now=6)
        self.assertEqual(self.writes_sent(), [])

    def test_http_and_sse_follow_delivery_contracts(self):
        self.config.write_text('[monitor]\nreport_merge_window_seconds=0\n')
        self.receipt(self.job())
        service = self.service()
        service.start()
        body = self.eventually(lambda: self.request('/v1/reports')[1],
                               lambda body: bool(body.get('reports')) and
                               body['reports'][0]['delivery']['status'] == 'delivered')
        self.assert_structure(body, json.loads((serve_test.CONTRACTS / 'reports.json').read_text()))
        self.eventually(lambda: events.read_after(self.state, 0),
                        lambda rows: any(e['type'] == 'report.delivered' for e in rows))
        response = self.open_events()
        while True:
            event = self.next_event(response)[2]
            if event['type'] == 'report.delivered':
                break
        self.assert_structure(event, json.loads((serve_test.CONTRACTS / 'report-delivered.json').read_text()))
        self.assertEqual(event['data']['report_id'], body['reports'][0]['id'])
        self.assertNotIn('检查已通过', json.dumps(event, ensure_ascii=False))
        response.close()

    def test_waiting_for_input_and_unknown_status_never_receive_automatic_text(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        for now, status in enumerate(('blocked', 'unknown'), 5):
            self.data['agents'][0]['agent_status'] = status
            self.save_fixture()
            service.reports.tick(now=now)
            self.assertEqual(self.writes_sent(), [])
            self.assertEqual(self.reports(service)[0]['delivery']['status'], 'queued')
        self.data['agents'][0]['agent_status'] = 'done'
        self.save_fixture()
        service.reports.tick(now=7)
        self.assertEqual(len(self.writes_sent()), 2)
