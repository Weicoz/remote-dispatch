"""控制端按用户保存的会话派单默认值；读取不创建文件。"""
import configparser
from pathlib import Path

from . import rd_agents as agents
from . import rd_events as events
from . import rd_messages as messages
from .rd_lock import JobLock
from .rd_profiles import user_id


EMPTY = dict(host=None, harness=None)


def identity(session=None, pane=None):
    value = session or pane
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError('会话标识无效。')
    return ('session:' if session else 'pane:') + value


def validate(values):
    if not isinstance(values, dict) or set(values) - EMPTY.keys():
        raise ValueError('派单默认值只允许 host、harness 字段。')
    for key, value in values.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > 128 or not all(
                c.isascii() and (c.isalnum() or c in '._-') for c in value):
            raise ValueError('目标机或 harness 名称无效。')
        if key == 'harness' and value not in agents.SPECS:
            raise ValueError('派单默认值的 harness 不受支持。')
    return values


def pane_record(rows, pane, *, include_closed=False):
    matches = [row for row in rows.values() if row.get('pane_id') == pane and not row.get('closed_at')]
    if not matches and include_closed:
        matches = [row for row in rows.values() if row.get('pane_id') == pane]
    return matches[0] if len(matches) == 1 else {}


class SessionDefaults:
    def __init__(self, state, uid):
        self.state = Path(state)
        self.uid = user_id(uid)
        self.path = self.state / 'session-defaults' / (self.uid + '.json')

    def lock(self):
        lock = JobLock(self.state, self.uid)
        lock.path = self.state.resolve() / 'locks' / 'session-defaults' / (self.uid + '.lock')
        return lock.acquire(blocking=True)

    def read(self):
        return messages.read_json(self.path) or {}

    def get(self, session=None, pane=None):
        rows = self.read()
        row = rows.get(identity(session, pane), {})
        if not session and not row:
            row = pane_record(rows, pane)
        return {key: row.get(key) for key in EMPTY}

    def update(self, values, session=None, pane=None, *, saved=None, request_hash=None):
        validate(values)
        key = identity(session, pane)
        lock = self.lock()
        try:
            if saved is not None:
                previous = messages.read_json(saved)
                if previous is not None:
                    if previous['fingerprint'] != request_hash:
                        return 422, {'error': {'reason': 'idempotency_key_reused'}}
                    return previous['status'], previous['body']
            rows = self.read()
            before = self.get(session, pane)
            result = dict(before, **values)
            row = rows.get(key, {})
            rows[key] = dict(row, **result, session_id=session, pane_id=pane or row.get('pane_id'),
                             closed_at=row.get('closed_at'))
            paths = [self.path] + ([saved] if saved is not None else [])
            with events.write_transaction(paths):
                messages.atomic_json(self.path, rows, limit=None)
                if before != result:
                    events.emit(self.state, 'session.changed', dict(user_id=self.uid,
                        pane_id=rows[key]['pane_id'], session_id=session, dispatch_defaults=result))
                if saved is not None:
                    messages.atomic_json(saved, dict(status=200, body=result, fingerprint=request_hash))
            return 200, result
        finally:
            lock.release()

    def observe(self, sessions):
        """只在 Herdr 完整采集成功后确认关闭；服务停机时间不推测为关闭时间。"""
        from datetime import datetime, timedelta
        now = messages.utc_now()
        instant = datetime.fromisoformat(now)
        active = {}
        for row in sessions or []:
            session = row.get('session_id') if not row.get('probable') else None
            active[identity(session, row['pane_id'])] = row['pane_id']
        lock = self.lock()
        try:
            before = self.read()
            rows = {key: dict(row) for key, row in before.items()}
            for key, row in list(rows.items()):
                if key in active:
                    row.update(pane_id=active[key], closed_at=None)
                elif not row.get('closed_at'):
                    if sessions is not None:
                        row['closed_at'] = now
                elif instant - datetime.fromisoformat(row['closed_at']) >= timedelta(days=30):
                    del rows[key]
            if rows != before:
                messages.atomic_json(self.path, rows, limit=None)
        finally:
            lock.release()


class DefaultRoutes:
    """会话接口沿用访问开关，PUT 在同一用户文件锁下提交状态、幂等结果和事件。"""
    def __init__(self, sessions):
        self.sessions = sessions
        self.service = sessions.service

    def target(self, pane):
        row = next((row for row in self.sessions.list() if row['id'] == pane), None)
        if row is not None:
            return row.get('session_id') if not row.get('probable') else None, pane
        store = SessionDefaults(self.service.state, self.service.user_id)
        saved = pane_record(store.read(), pane, include_closed=True)
        return (saved.get('session_id'), pane) if saved else (None, pane)

    @staticmethod
    def match(path):
        import re
        return re.fullmatch(r'/v1/sessions/([^/]+)/dispatch-defaults', path)

    def read(self, path):
        match = self.match(path)
        if match:
            from . import cli
            session, pane = self.target(match[1])
            values = SessionDefaults(self.service.state, self.service.user_id).get(session, pane)
            try:
                configured = cli.Config().cp
            except (OSError, UnicodeError, configparser.Error):
                return 503, {'error': {'reason': 'read_unavailable', 'message': '无法读取当前派单默认配置。'}}
            sensitive = set(self.service.sensitive)
            for section in configured.sections():
                for key, value in configured[section].items():
                    if value and (key, value) != ('ssh', 'local') and self.service.is_sensitive_field(key):
                        sensitive.add(value)
            values['config'] = dict(
                host=configured.get('defaults', 'host', fallback=None) or 'auto',
                harness=configured.get('defaults', 'harness', fallback=None) or None)
            return 200, self.service.redact(values, sensitive=sensitive)
        return None

    def put(self, handler):
        import hashlib
        import json
        from urllib.parse import unquote, urlsplit
        from .rd_idempotency import fingerprint
        match = self.match(unquote(urlsplit(handler.path).path))
        if not match:
            return handler.json(404, {'error': {'reason': 'not_found'}})
        try:
            key = handler.headers.get('Idempotency-Key', '')
            length = int(handler.headers.get('Content-Length', '0'))
            if (len(handler.headers.get_all('Idempotency-Key', [])) > 1
                    or handler.headers.get('Transfer-Encoding')
                    or handler.headers.get_content_type() != 'application/json'
                    or not 0 < length <= 8 * 1024 * 1024):
                raise ValueError('派单默认值请求无效。')
            raw = handler.rfile.read(length)
            if not self.sessions.allowed(getattr(handler, 'session_device', None)):
                return handler.json(403, {'error': {'reason': 'session_access_disabled'}})
            body = json.loads(raw)
            if not key.strip() or len(key) > 255:
                return handler.json(400, {'error': {'reason': 'idempotency_key_required'}})
            validate(body)
            # PUT replaces both fields; omitted values mean read configuration.
            values = dict(EMPTY, **body)
            scope = [self.service.user_id, getattr(handler, 'device_id', None), key]
            digest = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            saved = self.service.state / 'session-default-requests' / (digest + '.json')
            request_hash = fingerprint('PUT', match[0], body)
            with self.service.writer:
                if self.service.stop.is_set():
                    raise OSError('服务正在停止。')
                session, pane = self.target(match[1])
                status, reply = SessionDefaults(self.service.state, self.service.user_id).update(
                    values, session, pane, saved=saved, request_hash=request_hash)
            handler.json(status, self.service.redact(reply))
        except (ValueError, TypeError, UnicodeError) as exc:
            reason = 'dispatch_defaults_write_failed' if str(exc).startswith('event_write_failed:') else 'invalid_dispatch_defaults'
            handler.json(503 if reason.endswith('failed') else 400, {'error': {'reason': reason, 'message': '无法保存会话派单默认值。' if reason.endswith('failed') else '派单默认值请求无效。'}})
        except OSError:
            handler.json(503, {'error': {'reason': 'dispatch_defaults_write_failed', 'message': '无法保存会话派单默认值。'}})
