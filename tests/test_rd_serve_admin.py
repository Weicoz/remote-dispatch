"""票 73：通过本地 HTTP 与配对设备鉴权验证设置行为。"""
import json
import fcntl
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve_host_config as hosts
from tests import test_rd_serve_network as network
from tests import test_rd_serve_sessions as sessions


class AdminConfigTests(unittest.TestCase):
    setUp = hosts.HostConfigTests.setUp
    configured = hosts.HostConfigTests.configured
    start = hosts.HostConfigTests.start
    request = hosts.HostConfigTests.request
    write = hosts.HostConfigTests.write
    assert_structure = hosts.HostConfigTests.assert_structure

    def test_session_access_rejects_bad_requests_and_preserves_config_on_replace_failure(self):
        self.configured()
        self.config.write_bytes(b'; preserved\r\n[serve]\r\n; setting note\r\nsession_access=true\r\n'
                                b'[project:demo]\r\n# project note\r\ntest=echo okay\r\n')
        before = self.config.read_bytes()
        for index, body in enumerate(({}, {'enabled': 'false'}, {'enabled': 0}, {'enabled': None},
                                      {'enabled': False, 'extra': True})):
            self.assertEqual(self.write('/v1/config/session-access', body, method='PUT', key=f'invalid-{index}')[0], 400)
        self.assertEqual(self.request('/v1/config/session-access', 'PUT', '{}', {'Content-Type': 'application/json'})[0], 400)
        replace = os.replace
        def fail_replace(source, target):
            if str(target) == str(self.config):
                raise OSError('fixture config write failure')
            return replace(source, target)
        with patch('os.replace', side_effect=fail_replace):
            self.assertEqual(self.write('/v1/config/session-access', {'enabled': False}, method='PUT', key='failed')[0], 503)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.request('/v1/config/session-access'), (200, {'enabled': True}))
        self.assertEqual(next(self.root.glob('config.ini.bak.*')).read_bytes(), before)
        self.assertEqual(self.write('/v1/config/session-access', {'enabled': False}, method='PUT', key='new')[0], 200)
        saved = self.config.read_bytes()
        self.assertIn(b'; setting note\r\nsession_access=false\r\n', saved)
        self.assertTrue(saved.endswith(b'[project:demo]\r\n# project note\r\ntest=echo okay\r\n'))
        self.assertNotIn(b'\n', saved.replace(b'\r\n', b''))

    def test_setting_contract_examples_match_responses(self):
        self.configured()
        for name in ('session-access-get', 'session-access-put', 'session-access-invalid', 'host-config-max-heavy-invalid'):
            example = json.loads((hosts.serve_tests.CONTRACTS / (name + '.json')).read_text())
            request = example['request']
            reply = self.request(request['path'], request['method'],
                                 json.dumps(request['body']) if 'body' in request else None,
                                 request.get('headers'))
            self.assertEqual(reply, (example['response']['status'], example['response']['body']))

    def test_max_heavy_accepts_above_eight_and_explains_invalid_values(self):
        self.configured()
        reply = self.write('/v1/config/hosts/target', {'max_heavy': '99'}, method='PUT', key='large')
        self.assertEqual(reply[0], 200)
        self.assertEqual(reply[1]['host']['values']['max_heavy'], '99')
        for index, value in enumerate(('0', '-1', '1.5', 'NaN', 'abc', 2.5, True)):
            with self.subTest(value=value):
                reply = self.write('/v1/config/hosts/target', {'max_heavy': value}, method='PUT', key=f'bad-{index}')
                self.assertEqual(reply, (400, {'error': {'reason': 'invalid_host_field', 'field': 'max_heavy',
                                                       'text': '并发上限必须是大于等于 1 的整数。'}}))
        self.assertEqual(len(list(self.root.glob('config.ini.bak.*'))), 1)

    def test_delete_reports_total_and_caps_active_ids_across_users(self):
        self.configured()
        for index in range(25):
            cli.save_job(dict(job=f'active-{index:02}', user_id='other', host='target', rounds=1))
        reply = self.write('/v1/config/hosts/target', method='DELETE', key='delete-many')
        self.assertEqual(reply[0], 409)
        self.assertEqual(reply[1]['error'], {'reason': 'host_has_active_dispatches', 'running_count': 25,
                                           'running_job_ids': [f'active-{index:02}' for index in range(20)]})
        self.assertEqual(len(list(self.root.glob('config.ini.bak.*'))), 0)

    def test_global_session_access_is_persisted_and_takes_effect_without_restart(self):
        service = self.configured()
        original = self.config.read_bytes()
        self.assertEqual(self.request('/v1/config/session-access'), (200, {'enabled': True}))
        reply = self.write('/v1/config/session-access', {'enabled': False}, method='PUT', key='disable')
        self.assertEqual(reply, (200, {'enabled': False}))
        self.assertEqual(self.request('/v1/config/session-access'), reply)
        self.assertEqual(self.request('/v1/sessions')[0], 403)
        self.assertTrue(self.config.read_bytes().startswith(original))
        self.assertEqual(next(self.root.glob('config.ini.bak.*')).read_bytes(), original)
        self.assertEqual(self.write('/v1/config/session-access', {'enabled': False}, method='PUT', key='disable'), reply)
        self.assertEqual(len(list(self.root.glob('config.ini.bak.*'))), 1)
        self.assertEqual(self.write('/v1/config/session-access', {'enabled': True}, method='PUT', key='enable'),
                         (200, {'enabled': True}))
        self.assertEqual(self.request('/v1/sessions')[0], 200)
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.request('/v1/config/session-access'), (200, {'enabled': True}))


class AdminNetworkTests(unittest.TestCase):
    setUp = network.NetworkTests.setUp
    start = network.NetworkTests.start
    request = network.NetworkTests.request
    configure_network = network.NetworkTests.configure_network
    network_info = network.NetworkTests.network_info
    tls = network.NetworkTests.tls
    pair = network.NetworkTests.pair

    def test_stopping_rejects_new_device_authentication_before_final_flush(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        _, headers = self.pair(info)
        connection = self.tls(info)
        connection.connect()
        service.stop.set()
        self.assertEqual(self.request('/v1/jobs', connection=connection, headers=headers),
                         (503, {'error': {'reason': 'serve_stopping'}}))
        self.assertIsNone(self.request('/v1/devices')[1]['devices'][0]['last_seen_at'])

    def test_encoded_global_setting_path_remains_local_only(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        _, headers = self.pair(info)
        for method, body in (('GET', None), ('PUT', {'enabled': False})):
            self.assertEqual(self.request('/v1/config/%73ession-access', method, body,
                             connection=self.tls(info), headers=headers)[0], 403)
        self.assertEqual(self.request('/v1/config/session-access'), (200, {'enabled': True}))

    def test_network_classifies_selected_addresses_including_disabled_rows(self):
        expected = {'100.64.0.1': 'tailscale', '100.127.255.254': 'tailscale',
                    'fd7a:115c:a1e0::1': 'tailscale', '10.144.144.5': 'easytier',
                    '10.126.126.254': 'easytier', '10.144.145.5': 'lan',
                    '192.168.1.2': 'lan', '172.16.0.1': 'lan', '169.254.1.2': 'lan',
                    'fc00::1': 'lan', 'fe80::1': 'lan', '127.0.0.1': 'other', '::1': 'other'}
        self.config.write_text('[serve]\nlisten_addresses=' + json.dumps([
            dict(address=address, enabled=False) for address in expected]) + '\n')
        self.start()
        rows = self.network_info()['listen_addresses']
        self.assertEqual({row['address']: row['kind'] for row in rows}, expected)

    def test_background_timestamp_flush_does_not_block_reads_and_merges_concurrent_auth(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        device, headers = self.pair(info)
        self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
        entered, release = threading.Event(), threading.Event()
        writer_waiting = threading.Event()
        flock = fcntl.flock
        lock_path = self.state / "locks" / "network" / "state.lock"
        def observed_flock(fd, operation):
            if entered.is_set() and os.fstat(fd).st_ino == lock_path.stat().st_ino:
                writer_waiting.set()
            return flock(fd, operation)
        replace = os.replace
        def slow_replace(source, target):
            if str(target) == str(self.state / 'network' / 'devices.json'):
                entered.set()
                if not release.wait(5):
                    raise OSError('fixture write was not released')
            return replace(source, target)
        # Drive the minute scheduler's public flush boundary without sleeping a minute.
        with patch('os.replace', side_effect=slow_replace), patch('fcntl.flock', side_effect=observed_flock), \
                ThreadPoolExecutor(max_workers=2) as pool:
            flushing = pool.submit(service.network.flush_last_seen)
            try:
                self.assertTrue(entered.wait(2))
                # A device write waiting for the disk lock must not own the read lock.
                writing = pool.submit(self.request, '/v1/devices/' + device['device_id'] + '/session-access',
                                      'POST', {'enabled': True}, key='concurrent-write')
                self.assertTrue(writer_waiting.wait(2))
                durations = []
                with patch('remote_dispatch.rd_messages.utc_now', return_value='2099-01-01T00:00:00+00:00'):
                    for _ in range(10):
                        started = time.monotonic()
                        self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
                        durations.append(time.monotonic() - started)
                started = time.monotonic()
                self.assertEqual(self.request('/v1/devices')[1]['devices'][0]['last_seen_at'],
                                 '2099-01-01T00:00:00+00:00')
                durations.append(time.monotonic() - started)
                print('blocked flush foreground milliseconds:', [round(value * 1000, 2) for value in durations])
                self.assertLess(max(durations), 0.3)
            finally:
                release.set()
                flushing.result(5)
                self.assertEqual(writing.result(5)[0], 200)
        # A newer observation during a flush must remain pending for the next flush.
        service.network.flush_last_seen()
        service.close()
        self.start()
        self.assertEqual(self.request('/v1/devices')[1]['devices'][0]['last_seen_at'],
                         '2099-01-01T00:00:00+00:00')

    def test_legacy_device_timestamps_are_unknown_until_successful_authentication(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        _, headers = self.pair(info)
        service.close()
        path = self.state / 'network' / 'devices.json'
        data = json.loads(path.read_text())
        for row in data['devices'].values():
            row.pop('paired_at', None)
            row.pop('last_seen_at', None)
        path.write_text(json.dumps(data))
        self.start()
        self.assertIsNone(self.request('/v1/devices')[1]['devices'][0]['paired_at'])
        self.assertIsNone(self.request('/v1/devices')[1]['devices'][0]['last_seen_at'])
        info = self.network_info()
        self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
        self.assertIsNotNone(self.request('/v1/devices')[1]['devices'][0]['last_seen_at'])
        self.assertIsNone(self.request('/v1/devices')[1]['devices'][0]['paired_at'])

    def test_device_times_track_only_authenticated_requests_and_survive_restart(self):
        self.configure_network()
        service = self.start()
        info = self.network_info()
        with patch('remote_dispatch.rd_messages.utc_now', return_value='2026-10-27T10:00:00+00:00'):
            device, headers = self.pair(info)
        row = self.request('/v1/devices')[1]['devices'][0]
        self.assertEqual(row['paired_at'], '2026-10-27T10:00:00+00:00')
        self.assertIsNone(row['last_seen_at'])
        persisted = (self.state / 'network' / 'devices.json').read_bytes()
        with patch('remote_dispatch.rd_messages.utc_now', return_value='2026-10-27T10:01:00+00:00'):
            self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
        row = self.request('/v1/devices')[1]['devices'][0]
        self.assertEqual(row['last_seen_at'], '2026-10-27T10:01:00+00:00')
        example = json.loads((hosts.serve_tests.CONTRACTS / 'network-devices.json').read_text())
        expected = example['response']['body']
        expected['devices'][0]['device_id'] = device['device_id']
        self.assertEqual(self.request('/v1/devices'), (example['response']['status'], expected))
        self.assertEqual((self.state / 'network' / 'devices.json').read_bytes(), persisted)
        with patch('remote_dispatch.rd_messages.utc_now', return_value='2026-10-27T10:02:00+00:00'):
            invalid = dict(headers, Authorization='Bearer invalid')
            self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=invalid)[0], 401)
        self.assertEqual(self.request('/v1/devices')[1]['devices'][0], row)
        with patch('remote_dispatch.rd_messages.utc_now', return_value='2026-10-27T10:03:00+00:00'):
            for _ in range(5):
                self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
        self.assertEqual((self.state / 'network' / 'devices.json').read_bytes(), persisted)
        service.close()
        self.start()
        row = self.request('/v1/devices')[1]['devices'][0]
        self.assertEqual(row['paired_at'], '2026-10-27T10:00:00+00:00')
        self.assertEqual(row['last_seen_at'], '2026-10-27T10:03:00+00:00')

    def test_closing_pairing_window_invalidates_code_and_preserves_paired_device(self):
        self.configure_network()
        self.start()
        info = self.network_info()
        device, headers = self.pair(info)
        window = self.request('/v1/pairing-window', 'POST', {}, key='second-window')[1]
        for path, method, body in (('/v1/pairing-window', 'DELETE', None),
                                   ('/v1/config/session-access', 'GET', None),
                                   ('/v1/config/session-access', 'PUT', {'enabled': False})):
            self.assertEqual(self.request(path, method, body, key='remote-admin',
                             connection=self.tls(info), headers=headers)[0], 403)
        example = json.loads((hosts.serve_tests.CONTRACTS / 'network-pairing-window-close.json').read_text())
        self.assertEqual(self.request('/v1/pairing-window', 'DELETE', key='close'),
                         (example['response']['status'], example['response']['body']))
        self.assertEqual(self.request('/v1/pair', 'POST', {'code': window['code']}, key='closed-code'),
                         (403, {'error': {'reason': 'pairing_closed'}}))
        self.assertEqual(self.request('/v1/jobs', connection=self.tls(info), headers=headers)[0], 200)
        # Replaying an old close must not close a newly opened window.
        reopened = self.request('/v1/pairing-window', 'POST', {}, key='reopen')[1]
        self.assertEqual(self.request('/v1/pairing-window', 'DELETE', key='close')[0], 200)
        self.assertEqual(self.request('/v1/pair', 'POST', {'code': reopened['code']}, key='new-pair')[0], 200)


class AdminSessionTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    start = sessions.SessionTests.start
    agent = sessions.SessionTests.agent
    save_fixture = sessions.SessionTests.save_fixture
    request = sessions.SessionTests.request
    eventually = sessions.SessionTests.eventually
    open_events = sessions.SessionTests.open_events
    write = hosts.HostConfigTests.write

    def gated_frames(self):
        gate, observer = self.root / 'frame-gate', self.root / 'observer.pid'
        self.data.update(frame_gate=str(gate), observer_pid=str(observer),
                         frames=[json.dumps(dict(type='terminal.frame', data='c2VjcmV0',
                                                 seq=1, full=True, width=149, height=45))])
        self.save_fixture()
        self.start(poll_interval=3600)
        return gate, observer

    def test_disabling_global_access_stops_existing_terminal_stream_before_next_frame(self):
        gate, observer = self.gated_frames()
        stream = self.open_events('/v1/sessions/w1:p1/terminal')
        try:
            self.eventually(observer.exists, bool)
            self.assertEqual(self.write('/v1/config/session-access', {'enabled': False}, method='PUT')[0], 200)
            gate.touch()
            self.assertEqual(stream.read(), b'')
        finally:
            gate.touch()
            stream.close()

    def test_disabling_global_access_denies_screen_already_waiting_for_a_frame(self):
        gate, observer = self.gated_frames()
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(self.request, '/v1/sessions/w1:p1/screen')
            try:
                self.eventually(observer.exists, bool)
                self.assertEqual(self.write('/v1/config/session-access', {'enabled': False}, method='PUT')[0], 200)
                gate.touch()
                self.assertEqual(reading.result(5), (403, {'error': {'reason': 'session_access_disabled'}}))
            finally:
                gate.touch()


if __name__ == '__main__':
    unittest.main()
