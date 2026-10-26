"""汇报接缝：真实事件、持久状态、serve 查询与 CLI；Herdr 使用隔离可执行替身。"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch.rd_serve import Service
from tests import test_rd_serve_sessions as sessions


class ReportTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    agent = sessions.SessionTests.agent
    save_fixture = sessions.SessionTests.save_fixture
    saved_session = sessions.SessionTests.saved_session
    transcript = sessions.SessionTests.transcript
    assert_structure = sessions.SessionTests.assert_structure

    def service(self):
        self.transcript()
        self.saved_session()
        service = Service(cli.Config(), self.path)
        self.addCleanup(service.close)
        service.sessions.collect()
        return service

    def job(self, name="fixture-job", **fields):
        rec = dict(job=name, title="实现汇报", user_id="fixture", host="target", rounds=1,
                   initiator_session="session-one", handoff_version=1, agent=None)
        rec.update(fields)
        cli.save_job(rec)
        return rec

    def receipt(self, rec, outcome="completed", summary="检查已通过"):
        receipt = dict(schema=1, job=rec["job"], host=rec["host"], round=rec["rounds"],
                       execution_state="done", executor_exit=0, initiator_session=rec["initiator_session"],
                       receipt_id="receipt-" + rec["job"], report=dict(outcome=outcome, summary=summary))
        cli.persist_receipt(rec, receipt)
        return receipt

    def reports(self, service):
        status, body = service.read("/v1/reports?initiator_session=session-one")
        self.assertEqual(status, 200)
        return body["reports"]

    def test_receipt_event_generates_summary_and_inbox_keeps_pending_receipt(self):
        rec = self.job()
        receipt = self.receipt(rec)
        service = self.service()
        service.reports.tick(now=0)
        self.assertEqual(self.reports(service), [])
        service.reports.tick(now=5)
        reports = self.reports(service)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["entries"], [dict(job="fixture-job", title="实现汇报", host="target",
            round=1, state="completed", state_text="已完成", summary="检查已通过")])
        with patch.object(cli, "job_states", return_value={"fixture-job": {"state": "unreachable"}}), \
             patch.dict(os.environ, {"RD_INITIATOR_SESSION": "session-one"}), \
             patch.object(sys, "argv", ["rdispatch", "inbox"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        inbox = json.loads(output.getvalue())
        self.assertEqual(inbox["reports"], reports)
        self.assertEqual(inbox["results"][0]["receipt_id"], receipt["receipt_id"])
        created = [e for e in events.read_after(self.state, 0) if e["type"] == "report.created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["data"]["report_id"], reports[0]["id"])

    def test_merge_window_is_configurable_and_pending_and_dedup_survive_restart(self):
        self.config.write_text('[monitor]\nreport_merge_window_seconds=12\n')
        first = self.job()
        self.receipt(first)
        service = self.service()
        service.reports.tick(now=100)
        second = self.job('second-job', title='检查部署')
        self.receipt(second, outcome='blocked', summary='请确认目标配置')
        service.reports.tick(now=110)
        self.assertEqual(self.reports(service), [])
        service.close()
        service = self.service()
        service.reports.tick(now=112)
        reports = self.reports(service)
        self.assertEqual(len(reports), 1)
        self.assertEqual([(e['job'], e['state'], e['summary']) for e in reports[0]['entries']],
                         [('fixture-job', 'completed', '检查已通过'), ('second-job', 'blocked', '请确认目标配置')])
        events.emit(self.state, 'receipt.ready', events.job_data(first, outcome='completed'))
        service.reports.tick(now=200)
        service.reports.tick(now=300)
        self.assertEqual(self.reports(service), reports)

    def test_state_events_include_latest_progress_and_repeat_only_after_recovery(self):
        rec = self.job()
        service = self.service()
        log = events.EventLog(self.state)
        log.observe_job(rec, dict(state='running', round='1'), dict(summary='正在校验连接', phase='check'))
        log.observe_job(rec, dict(state='unreachable', round='1'))
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        first = self.reports(service)[0]['entries'][0]
        self.assertEqual((first['state'], first['summary']), ('offline', '正在校验连接'))
        log.observe_job(rec, dict(state='unreachable', round='1'))
        service.reports.tick(now=6)
        service.reports.tick(now=11)
        self.assertEqual(len(self.reports(service)), 1)
        log.observe_job(rec, dict(state='running', round='1'))
        log.observe_job(rec, dict(state='unreachable', round='1'))
        service.reports.tick(now=12)
        service.reports.tick(now=17)
        self.assertEqual(len(self.reports(service)), 2)
        log.observe_job(rec, dict(state='dead', round='1', exit='17'))
        service.reports.tick(now=18)
        service.reports.tick(now=23)
        failure = self.reports(service)[-1]['entries'][0]
        self.assertEqual(failure['state'], 'failed')
        self.assertIn('17', failure['summary'])

    def test_minute_scan_recovers_a_receipt_with_no_event(self):
        rec = self.job()
        service = self.service()
        service.reports.tick(now=0)
        receipt = dict(job=rec['job'], round=1, receipt_id='missed', execution_state='done',
                       executor_exit=0, report=dict(outcome='failed', summary='依赖检查失败'))
        cli.messages.atomic_json(cli.result_path(rec, 1), receipt)
        service.reports.tick(now=59)
        self.assertEqual(self.reports(service), [])
        service.reports.tick(now=60)
        service.reports.tick(now=65)
        entry = self.reports(service)[0]['entries'][0]
        self.assertEqual((entry['state'], entry['summary']), ('failed', '依赖检查失败'))
        service.reports.tick(now=120)
        service.reports.tick(now=125)
        self.assertEqual(len(self.reports(service)), 1)

    def test_app_absent_and_other_user_sessions_do_not_generate(self):
        for job, fields in (
                ('app', dict(initiator_session='app:device')),
                ('absent', dict(initiator_session=None)),
                ('other', dict(user_id='other'))):
            self.receipt(self.job(job, **fields))
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        self.assertEqual(service.read('/v1/reports')[1], {'reports': []})
        self.assertFalse(any(e['type'] == 'report.created' for e in events.read_after(self.state, 0)))

    def test_contract_and_sensitive_fields_cover_all_new_display_fields(self):
        from tests.test_rd_serve import ServeTests, CONTRACTS
        rec = self.job(title='token=title-marker /private/config')
        receipt = self.receipt(rec, summary='password=summary-marker /private/config')
        receipt['report']['env_file'] = 'must-not-appear'
        receipt['report']['private_key'] = 'must-not-appear'
        cli.messages.atomic_json(cli.result_path(rec, 1), receipt)
        service = self.service()
        service.sensitive.add('/private/config')
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        body = service.read('/v1/reports')[1]
        ServeTests.assert_structure(self, body, json.loads((CONTRACTS / 'reports.json').read_text()))
        created = next(e for e in events.read_after(self.state, 0) if e['type'] == 'report.created')
        ServeTests.assert_structure(self, created, json.loads((CONTRACTS / 'report-events.json').read_text()))
        text = json.dumps([body, created], ensure_ascii=False)
        for sensitive in ('title-marker', 'summary-marker', '/private/config', 'env_file', 'private_key', 'must-not-appear'):
            self.assertNotIn(sensitive, text)
        self.assertIn('[redacted]', text)
        self.assertEqual(service.read('/v1/reports?initiator_session=other')[1], {'reports': []})

    def test_failed_event_write_rolls_back_inbox_and_retry_generates_once(self):
        self.config.write_text('[monitor]\nreport_merge_window_seconds=0\n')
        rec = self.job()
        self.receipt(rec)
        service = self.service()
        replace = os.replace
        def fail_event(source, destination):
            if str(destination).endswith('/events.jsonl'):
                raise OSError('fixture disk full')
            return replace(source, destination)
        with patch.object(os, 'replace', side_effect=fail_event), self.assertRaises(ValueError):
            service.reports.tick(now=0)
        self.assertEqual(self.reports(service), [])
        service.reports.tick(now=6)
        service.reports.tick(now=11)
        self.assertEqual(len(self.reports(service)), 1)
        self.assertEqual(sum(e['type'] == 'report.created' for e in events.read_after(self.state, 0)), 1)

    def test_large_inbox_remains_readable_and_deduplicated_after_restart(self):
        for number in range(20):
            self.receipt(self.job('job-' + str(number)), summary='x' * 8000)
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        service.close()
        service = self.service()
        service.reports.tick(now=60)
        self.assertEqual(len(self.reports(service)), 1)
        self.assertEqual(len(self.reports(service)[0]['entries']), 20)
        self.receipt(self.job('next-job'))
        service.reports.tick(now=61)
        service.reports.tick(now=66)
        self.assertEqual(len(self.reports(service)), 2)

    def test_session_closed_during_merge_window_is_kept_in_inbox(self):
        self.receipt(self.job())
        service = self.service()
        service.reports.tick(now=0)
        self.data['agents'] = []
        self.save_fixture()
        service.sessions.collect()
        service.reports.tick(now=5)
        report = self.reports(service)[0]
        self.assertEqual(report['delivery']['status'], 'inbox_only')
        self.assertEqual(report['delivery']['reason'], 'session_closed')

    def test_failed_receipt_without_summary_preserves_exit_reason(self):
        rec = self.job()
        receipt = self.receipt(rec, outcome='failed', summary='')
        receipt['executor_exit'] = 17
        cli.messages.atomic_json(cli.result_path(rec, 1), receipt)
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=5)
        entry = self.reports(service)[0]['entries'][0]
        self.assertEqual(entry['state'], 'failed')
        self.assertIn('17', entry['summary'])

    def test_scan_recovers_offline_snapshot_and_latest_progress_after_restart(self):
        rec = self.job()
        service = self.service()
        events.EventLog(self.state).observe_job(rec, dict(state='running', round='1'))
        service.reports.tick(now=0)
        service.close()
        cli.messages.atomic_json(self.state / 'serve-snapshots' / 'fixture-job.json', dict(
            user_id='fixture', generation=[1, 'target', None],
            snapshot=dict(state='unreachable', round='1', snapshot_at=cli.messages.utc_now()),
            progress=dict(summary='最后完成了检查', phase='check')))
        service = self.service()
        service.reports.tick(now=60)
        service.reports.tick(now=65)
        entry = self.reports(service)[0]['entries'][0]
        self.assertEqual((entry['state'], entry['summary']), ('offline', '最后完成了检查'))

    def test_service_lifecycle_generates_reports_without_manual_ticks(self):
        from tests.test_rd_serve import ServeTests
        self.config.write_text('[monitor]\nreport_merge_window_seconds=0\n')
        rec = self.job()
        self.receipt(rec)
        service = self.service()
        service.start()
        body = ServeTests.eventually(self, lambda: ServeTests.request(self, '/v1/reports')[1],
                                    lambda body: bool(body.get('reports')))
        self.assertEqual(body['reports'][0]['entries'][0]['job'], rec['job'])
        from tests.test_rd_serve import CONTRACTS
        self.assert_structure(body, json.loads((CONTRACTS / 'reports.json').read_text()))


class WatchSummaryTests(unittest.TestCase):
    def test_watch_completion_contains_progress_and_receipt_summary(self):
        from tests.test_rd_ack import AcknowledgementTests
        AcknowledgementTests.setUp(self)
        original = self.target
        def target(host, operation, data=None, **kwargs):
            if operation == 'report-read' and data['kind'] == 'progress':
                return dict(schema=1, kind='progress', job=self.rec['job'], round=1,
                            updated_at='2026-10-25T00:00:00+00:00', phase='check', summary='已运行检查',
                            env_file='private-marker', token='private-marker')
            return original(host, operation, data, **kwargs)
        with patch.object(cli.native, 'request', side_effect=target), \
             patch.object(sys, 'argv', ['rdispatch', 'watch', 'fixture-job']), \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_code:
            cli.main()
        self.assertEqual(exit_code.exception.code, 0)
        job = json.loads(output.getvalue())['jobs'][0]
        self.assertEqual(job['progress']['summary'], '已运行检查')
        self.assertEqual(job['result_summary']['summary'], '已完成本轮实现')
        self.assertNotIn('private-marker', output.getvalue())
        self.assertNotIn('env_file', output.getvalue())
        self.assertNotIn('token', output.getvalue())

    def target(self, *args, **kwargs):
        from tests.test_rd_ack import AcknowledgementTests
        return AcknowledgementTests.target(self, *args, **kwargs)
