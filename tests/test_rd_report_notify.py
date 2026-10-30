"""提醒通道接缝：各配置组合下的通道调用与跳过；文本边界；失败记录；digest 默认不提醒。"""
import json
import unittest

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from tests import test_rd_report_delivery as delivery_test


class NotifyChannelTests(unittest.TestCase):
    request = delivery_test.ReportDeliveryTests.request
    eventually = delivery_test.ReportDeliveryTests.eventually
    setUp = delivery_test.ReportDeliveryTests.setUp
    assert_structure = delivery_test.ReportDeliveryTests.assert_structure
    agent = delivery_test.ReportDeliveryTests.agent
    save_fixture = delivery_test.ReportDeliveryTests.save_fixture
    saved_session = delivery_test.ReportDeliveryTests.saved_session
    transcript = delivery_test.ReportDeliveryTests.transcript
    service = delivery_test.ReportDeliveryTests.service
    job = delivery_test.ReportDeliveryTests.job
    receipt = delivery_test.ReportDeliveryTests.receipt
    reports = delivery_test.ReportDeliveryTests.reports
    writes_sent = delivery_test.ReportDeliveryTests.writes_sent
    tick_report = delivery_test.ReportDeliveryTests.tick_report

    def notifications_sent(self):
        return [json.loads(line) for line in self.notifications.read_text().splitlines()] \
            if self.notifications.exists() else []

    def delivered_events(self):
        return [e for e in events.read_after(self.state, 0) if e['type'] == 'report.delivered']

    def report_with(self, config, **receipt):
        self.config.write_text(config)
        self.receipt(self.job(), **receipt)
        service = self.service()
        self.tick_report(service)
        return service, self.reports(service)[0]

    def test_default_config_notifies_herdr_and_skips_unimplemented_channels(self):
        service, report = self.report_with('', summary='SECRET-摘要')
        self.assertEqual(report['delivery'], dict(status='notified', channels=[
            dict(channel='herdr', ok=True, reason=None),
            dict(channel='macos', ok=False, reason='not_implemented'),
            dict(channel='app', ok=False, reason='not_implemented')]))
        sent = self.notifications_sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:2], ['notification', 'show'])
        self.assertEqual(sent[0][sent[0].index('--sound') + 1], 'done')
        text = json.dumps(sent, ensure_ascii=False)
        self.assertIn('实现汇报', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('检查', text)
        self.assertEqual(self.writes_sent(), [])
        delivered = self.delivered_events()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['data']['channels'], ['herdr'])
        self.assertEqual(delivered[0]['data']['report_id'], report['id'])
        self.assertNotIn('实现汇报', json.dumps(delivered[0]['data'], ensure_ascii=False))

    def test_channel_combinations(self):
        cases = {
            'herdr': ('notified', ['herdr'], 1),
            'macos': ('inbox_only', ['macos'], 0),
            'app,herdr': ('notified', ['herdr', 'app'], 1),
            ' Herdr , app ': ('notified', ['herdr', 'app'], 1),
            'none': ('inbox_only', [], 0),
        }
        for index, (value, (status, names, calls)) in enumerate(cases.items()):
            with self.subTest(notify=value):
                self.notifications.unlink(missing_ok=True)
                self.config.write_text('[monitor]\nnotify=%s\n' % value)
                name = 'combo%d' % index
                self.receipt(self.job(name))
                service = self.service()
                self.tick_report(service)
                rows = [r for r in self.reports(service) if r['entries'][0]['job'] == name]
                self.assertEqual(rows[0]['delivery']['status'], status)
                self.assertEqual(sorted(c['channel'] for c in rows[0]['delivery']['channels']), sorted(names))
                self.assertEqual(len(self.notifications_sent()), calls)
                service.close()

    def test_failure_uses_request_sound(self):
        self.report_with('[monitor]\nnotify=herdr\n', outcome='failed', summary='x')
        sent = self.notifications_sent()
        self.assertEqual(sent[0][sent[0].index('--sound') + 1], 'request')

    def test_herdr_failure_is_recorded_not_retried_and_report_is_stored(self):
        self.data['fail_notify'] = True
        self.save_fixture()
        service, report = self.report_with('[monitor]\nnotify=herdr\n')
        self.assertEqual(report['delivery'], dict(status='inbox_only', channels=[
            dict(channel='herdr', ok=False, reason='herdr_unavailable')]))
        service.reports.tick(now=60)
        self.assertEqual(len(self.notifications_sent()), 1)
        self.assertEqual(self.delivered_events(), [])

    def test_digest_does_not_notify_by_default_but_does_when_enabled(self):
        base = '[monitor]\ndigest_interval_seconds=10\nnotify=herdr\n'
        for extra, calls in (('', 0), ('notify_digest=true\n', 1)):
            with self.subTest(extra=extra):
                self.notifications.unlink(missing_ok=True)
                self.config.write_text(base + extra)
                name = 'digest%d' % calls
                self.job(name, title='任务' + name)
                cli.messages.atomic_json(self.state / 'serve-snapshots' / (name + '.json'), dict(
                    user_id='fixture', generation=[1, 'target', None],
                    snapshot=dict(state='running', round='1', snapshot_at=cli.messages.utc_now()),
                    progress=dict(summary='SECRET-进度', phase='build')))
                service = self.service()
                service.reports.tick(now=0)
                service.reports.tick(now=15)
                digest = [r for r in service.read('/v1/reports')[1]['reports'] if r.get('kind') == 'digest']
                self.assertTrue(digest)
                self.assertEqual(len(self.notifications_sent()), calls)
                self.assertNotIn('SECRET', json.dumps(self.notifications_sent(), ensure_ascii=False))
                if not calls:
                    self.assertEqual(digest[0]['delivery'], dict(status='inbox_only', channels=[]))
                service.close()

    def test_invalid_notify_value_is_a_config_error_with_exit_code_4(self):
        for value in ('herdr,bogus', 'none,herdr', 'sms'):
            with self.subTest(value=value):
                self.config.write_text('[monitor]\nnotify=%s\n' % value)
                with self.assertRaises(SystemExit) as caught:
                    cli.Config().notify_channels()
                self.assertEqual(caught.exception.code, 4)
        self.config.write_text('[monitor]\nnotify=macos, app\n')
        self.assertEqual(cli.Config().notify_channels(), ('macos', 'app'))
        self.config.write_text('')
        self.assertEqual(cli.Config().notify_channels(), ('herdr', 'macos', 'app'))


if __name__ == '__main__':
    unittest.main()
