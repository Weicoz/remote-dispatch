"""会话派单默认值：通过 CLI、HTTP 和事件公开接口验证。"""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from remote_dispatch import cli
from tests import test_rd_serve_sessions as sessions
from tests import test_rd_serve as serve


class DefaultsTests(unittest.TestCase):
    setUp = sessions.SessionTests.setUp
    agent = sessions.SessionTests.agent
    save_fixture = sessions.SessionTests.save_fixture
    saved_session = sessions.SessionTests.saved_session
    transcript = sessions.SessionTests.transcript
    start = sessions.SessionTests.start
    request = serve.ServeTests.request
    eventually = serve.ServeTests.eventually

    def command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(['session-default', *args])
        return json.loads(output.getvalue())

    def test_cli_sets_reads_and_clears_defaults_per_user_and_session(self):
        self.assertEqual(self.command('one', '--host', 'target', '--harness', 'codex'),
                         {'host': 'target', 'harness': 'codex'})
        self.assertEqual(self.command('one'), {'host': 'target', 'harness': 'codex'})
        self.assertEqual(self.command('two'), {'host': None, 'harness': None})
        with patch.dict(os.environ, RD_USER_ID='other'):
            self.assertEqual(self.command('one'), {'host': None, 'harness': None})
        self.assertEqual(self.command('one', '--clear-host'), {'host': None, 'harness': 'codex'})
        self.assertEqual(self.command('one', '--clear'), {'host': None, 'harness': None})
        self.assertTrue((self.state / 'session-defaults' / 'fixture.json').exists())

    def test_http_reads_and_idempotently_replaces_defaults_and_emits_only_changes(self):
        from remote_dispatch import rd_events
        self.saved_session()
        self.start(poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        self.assertEqual(self.request(path), (200, {'host': None, 'harness': None}))
        body = json.dumps({'host': 'target', 'harness': 'codex'})
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': 'default-one'}
        self.assertEqual(self.request(path, 'PUT', body, {'Content-Type': 'application/json'})[0], 400)
        expected = (200, {'host': 'target', 'harness': 'codex'})
        self.assertEqual(self.request(path, 'PUT', body, headers), expected)
        self.assertEqual(self.request(path, 'PUT', body, headers), expected)
        self.assertEqual(self.command('session-one'), expected[1])
        changes = [e for e in rd_events.read_after(self.state, 0) if 'dispatch_defaults' in e['data']]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['data']['dispatch_defaults'], expected[1])
        self.assertEqual(self.request(path, 'PUT', '{}', headers)[0], 422)
        headers['Idempotency-Key'] = 'default-clear'
        self.assertEqual(self.request(path, 'PUT', '{"host": null, "harness": null}', headers),
                         (200, {'host': None, 'harness': None}))
        self.assertEqual(self.request(path), (200, {'host': None, 'harness': None}))

    def test_closed_defaults_survive_restart_until_thirty_days_after_observed_close(self):
        from remote_dispatch import rd_messages
        self.saved_session()
        service = self.start(poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': 'retain'}
        self.request(path, 'PUT', '{"host":"target"}', headers)
        self.data['agents'] = []
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-10-26T00:00:00+00:00'):
            service.sessions.collect()
        service.close()
        with patch.object(rd_messages, 'utc_now', return_value='2026-11-24T23:59:59+00:00'):
            service = serve.ServeTests.start(self, poll_interval=3600)
            service.sessions.collect()
        self.assertEqual(self.command('session-one')['host'], 'target')
        with patch.object(rd_messages, 'utc_now', return_value='2026-11-25T00:00:00+00:00'):
            service.sessions.collect()
        self.assertEqual(self.command('session-one')['host'], None)


    def test_http_contracts_and_sensitive_field_projection(self):
        from remote_dispatch import rd_events
        self.saved_session()
        service = self.start(poll_interval=3600)
        for name in ('get', 'put', 'clear'):
            example = json.loads((serve.CONTRACTS / ('session-dispatch-defaults-' + name + '.json')).read_text())
            for request in filter(None, [example.get('previous_request'), example['request']]):
                result = self.request(request['path'], request['method'],
                    json.dumps(request['body']) if 'body' in request else None, request.get('headers'))
            self.assertEqual(result, (example['response']['status'], example['response']['body']))
            for expected in example['events']:
                changes = [event for event in rd_events.read_after(self.state, 0) if 'dispatch_defaults' in event['data']]
                self.assertEqual({key: changes[-1][key] for key in ('type', 'data')}, expected)
        rd_events.emit(self.state, 'session.changed', dict(user_id='fixture', pane_id='w1:p1',
            session_id='session-one', auth_file='PRIVATE_MARKER', dispatch_defaults=dict(
                host='target', harness='codex', token='PRIVATE_MARKER', env_file='PRIVATE_MARKER')))
        serialized = json.dumps([self.request('/v1/sessions/w1:p1/dispatch-defaults'),
                                 service.redact(rd_events.read_after(self.state, 0))])
        for forbidden in ('PRIVATE_MARKER', 'auth_file', 'env_file', 'token'):
            self.assertNotIn(forbidden, serialized)
        before = serve.ServeTests.snapshot(self)
        self.request('/v1/sessions/w1:p1/dispatch-defaults')
        self.assertEqual(serve.ServeTests.snapshot(self), before)

    def test_disabled_session_access_applies_to_default_reads_and_writes(self):
        self.config.write_text('[serve]\nsession_access=false\n')
        serve.ServeTests.start(self, poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        self.assertEqual(self.request(path)[0], 403)
        self.assertEqual(self.request(path, 'PUT', '{}', {'Content-Type': 'application/json',
                         'Idempotency-Key': 'disabled'})[0], 403)

    def test_no_session_identity_keeps_pane_defaults_and_validates_writes(self):
        self.start(poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': 'pane'}
        self.assertEqual(self.request(path, 'PUT', '{"host":"target"}', headers),
                         (200, {'host': 'target', 'harness': None}))
        self.assertEqual(self.command('w1:p1', '--pane'), {'host': 'target', 'harness': None})
        for number, body in enumerate(({'harness':'unsupported'}, {'host':'../bad'}, {'token':'private'})):
            headers['Idempotency-Key'] = 'invalid-' + str(number)
            self.assertEqual(self.request(path, 'PUT', json.dumps(body), headers)[0], 400)
        self.assertEqual(self.request(path)[1], {'host': 'target', 'harness': None})

    def test_discovery_failure_does_not_start_retention_and_reopening_resets_close_time(self):
        from remote_dispatch import rd_messages
        self.saved_session()
        service = self.start(poll_interval=3600)
        self.command('session-one', '--host', 'target')
        self.data['unavailable'] = True
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-10-26T00:00:00+00:00'):
            service.sessions.collect()
        self.data['unavailable'] = False
        self.data['agents'] = []
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-12-01T00:00:00+00:00'):
            service.sessions.collect()
        self.assertEqual(self.command('session-one')['host'], 'target')
        self.data['agents'] = [self.agent()]
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-12-02T00:00:00+00:00'):
            service.sessions.collect()
        self.data['agents'] = []
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-12-31T00:00:00+00:00'):
            service.sessions.collect()
        with patch.object(rd_messages, 'utc_now', return_value='2027-01-29T23:59:59+00:00'):
            service.sessions.collect()
        self.assertEqual(self.command('session-one')['host'], 'target')


    def test_cli_forwarding_and_http_replay_survive_service_restart(self):
        self.saved_session()
        service = self.start(poll_interval=3600)
        with patch.dict(os.environ, RD_SERVE_SOCKET=str(self.path)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                cli.main(['session-default', 'session-one', '--host', 'first-target'])
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(json.loads(output.getvalue())['host'], 'first-target')
        service.sessions.collect()
        # Exact pane fallback also locates defaults saved under the known session ID.
        self.assertEqual(self.command('w1:p1', '--pane')['host'], 'first-target')
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        headers = {'Content-Type':'application/json', 'Idempotency-Key':'durable'}
        self.request(path, 'PUT', '{"host":"target"}', headers)
        self.command('session-one', '--host', 'new-target')
        service.close()
        self.start(poll_interval=3600)
        self.assertEqual(self.request(path, 'PUT', '{"host":"target"}', headers),
                         (200, {'host':'target', 'harness':None}))
        self.assertEqual(self.request(path)[1]['host'], 'new-target')

    def test_event_failure_rolls_back_defaults_and_idempotency_result(self):
        from remote_dispatch import rd_events
        self.saved_session()
        self.start(poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        headers = {'Content-Type':'application/json', 'Idempotency-Key':'rollback'}
        replace = os.replace
        def fail_events(source, destination):
            if Path(destination) == self.state / 'events.jsonl':
                raise OSError('fixture disk full')
            return replace(source, destination)
        with patch.object(os, 'replace', side_effect=fail_events):
            self.assertEqual(self.request(path, 'PUT', '{"host":"target"}', headers)[0], 503)
        self.assertEqual(self.request(path)[1], {'host':None, 'harness':None})
        self.assertFalse(any('dispatch_defaults' in event['data'] for event in rd_events.read_after(self.state, 0)))
        self.assertEqual(self.request(path, 'PUT', '{"host":"target"}', headers)[0], 200)


    def test_reused_closed_pane_does_not_choose_an_arbitrary_previous_session(self):
        self.saved_session('session-one')
        service = self.start(poll_interval=3600)
        path = '/v1/sessions/w1:p1/dispatch-defaults'
        self.request(path, 'PUT', '{"host":"first"}',
                     {'Content-Type':'application/json', 'Idempotency-Key':'first'})
        self.saved_session('session-two')
        service.sessions.collect()
        self.request(path, 'PUT', '{"host":"second"}',
                     {'Content-Type':'application/json', 'Idempotency-Key':'second'})
        self.data['agents'] = []
        self.save_fixture()
        service.sessions.collect()
        self.assertEqual(self.request(path)[1], {'host':None, 'harness':None})
        self.assertEqual(self.command('session-one')['host'], 'first')
        self.assertEqual(self.command('session-two')['host'], 'second')


    def test_disabled_discovery_still_cleans_records_with_known_close_time(self):
        from remote_dispatch import rd_messages
        self.saved_session()
        service = self.start(poll_interval=3600)
        self.command('session-one', '--host', 'target')
        self.data['agents'] = []
        self.save_fixture()
        with patch.object(rd_messages, 'utc_now', return_value='2026-10-26T00:00:00+00:00'):
            service.sessions.collect()
        service.sessions.enabled = False
        with patch.object(rd_messages, 'utc_now', return_value='2026-11-25T00:00:00+00:00'):
            service.sessions.collect()
        self.assertEqual(self.command('session-one')['host'], None)



from tests import test_rd_dispatch as dispatch


class RunDefaultsTests(unittest.TestCase):
    setUp = dispatch.DispatchTests.setUp
    git = dispatch.DispatchTests.git
    local_shell = dispatch.DispatchTests.local_shell
    local_rpc = dispatch.DispatchTests.local_rpc
    finish = dispatch.DispatchTests.finish
    args = dispatch.DispatchTests.args
    dispatch = dispatch.DispatchTests.dispatch

    def defaults(self, *args):
        cli.main(['session-default', 'session-one', *args])

    def test_run_uses_cli_then_session_then_configuration_for_each_field(self):
        import sys
        binaries = self.root / 'bin'
        binaries.mkdir()
        for name in ('dsh', 'agy'):
            executable = binaries / name
            executable.write_text('#!' + sys.executable + '\nprint("--input-format")\n')
            executable.chmod(0o700)
        auth = self.root / 'fixture-auth'
        auth.write_text('{}')
        config = Path(cli.CONFIG)
        config.write_text(config.read_text() + f'\ndsh_auth_file={auth}\nagy_auth_file={auth}\n\n[defaults]\nhost=fixture\nharness=dsh\n'
                          f'\n[host:session-target]\nssh=fixture.invalid\nworkdir={self.root / "worker"}\nsync=direct\nagy_auth_file={auth}\ndsh_auth_file={auth}\npath={binaries}:{os.environ["PATH"]}\n')
        # Built-in harness readiness executes in the fake target's shell.
        with patch.dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH']):
            self.cfg = cli.Config()
            prompt = self.root / 'prompt.md'
            prompt.write_text('fixture')
            options = dict(host=None, agent=None, cmd=None, prompt_file=str(prompt), initiator_session='session-one')
            configured = self.dispatch(**options)
            self.assertEqual((configured['host'], configured['agent']), ('fixture', 'dsh'))
            self.defaults('--host', 'session-target', '--harness', 'agy')
            defaulted = self.dispatch(**dict(options, permission_mode='skip'))
            self.assertEqual((defaulted['host'], defaulted['agent']), ('session-target', 'agy'))
            explicit = self.dispatch(**dict(options, host='fixture', agent='dsh'))
            self.assertEqual((explicit['host'], explicit['agent']), ('fixture', 'dsh'))
            host_override = self.dispatch(**dict(options, host='fixture', permission_mode='skip'))
            self.assertEqual((host_override['host'], host_override['agent']), ('fixture', 'agy'))
            harness_override = self.dispatch(**dict(options, agent='dsh'))
            self.assertEqual((harness_override['host'], harness_override['agent']), ('session-target', 'dsh'))
            command = self.dispatch(host=None, initiator_session='session-one')
            self.assertEqual((command['host'], command['agent'], command['cmd']), ('session-target', None, 'true'))

    def test_missing_session_uses_herdr_pane_and_unmatched_pane_reads_config(self):
        self.defaults('--host', 'missing-target')
        # CLI can address a pane when the harness cannot expose a session ID.
        cli.main(['session-default', 'pane-one', '--pane', '--host', 'fixture'])
        with patch.dict(os.environ, RD_INITIATOR_SESSION='', HERDR_PANE_ID='pane-one'), \
             patch.object(cli.initiators, 'read_process', return_value=None):
            record = self.dispatch(host=None)
        self.assertEqual(record['host'], 'fixture')
        cli.main(['session-default', 'pane-one', '--pane', '--host', 'missing-target'])
        with patch.dict(os.environ, RD_INITIATOR_SESSION='', HERDR_PANE_ID='other-pane'), \
             patch.object(cli.initiators, 'read_process', return_value=None):
            self.cfg.cp['defaults'] = {'host': 'fixture'}
            self.assertEqual(self.dispatch(host=None)['host'], 'fixture')

    def test_missing_and_offline_session_target_fail_without_fallback(self):
        import subprocess
        self.defaults('--host', 'missing-target')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            self.dispatch(host=None, initiator_session='session-one', force_host=False)
        self.assertEqual(result.exception.code, 4)
        self.assertIn('目标机不存在：missing-target', errors.getvalue())
        self.defaults('--host', 'fixture')
        original = self.local_shell
        def offline(host, script, **kwargs):
            if script == cli.PROBE:
                return subprocess.CompletedProcess([], 255, '', 'fixture offline')
            return original(host, script, **kwargs)
        errors = io.StringIO()
        with patch.object(cli, 'ssh', side_effect=offline), contextlib.redirect_stderr(errors), \
             self.assertRaises(SystemExit) as result:
            self.dispatch(host=None, initiator_session='session-one', force_host=False)
        self.assertEqual(result.exception.code, 3)
        self.assertIn('目标机 fixture 离线或暂时无法接单', errors.getvalue())
        self.assertEqual(cli.all_jobs(), [])
