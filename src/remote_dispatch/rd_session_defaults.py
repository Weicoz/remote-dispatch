"""控制端按用户保存的会话派单默认值；读取不创建文件。"""
import configparser
from pathlib import Path

from . import rd_agents as agents
from . import rd_events as events
from . import rd_messages as messages
from .rd_lock import JobLock
from .rd_profiles import user_id


EMPTY = dict(host=None, harness=None)
MODE_FIELD = dict(dispatch_mode=None)
MODES = ('suggest', 'line', 'ticket')
DEFAULT_MODE = 'suggest'


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


def validate_mode(value):
    if value is not None and value not in MODES:
        raise ValueError('派单模式只允许 ' + '、'.join(MODES) + '。')
    return value


def configured_mode(parser):
    """[defaults] dispatch_mode；缺省或空值为 suggest，非法值报错。"""
    value = parser.get('defaults', 'dispatch_mode', fallback=None) or DEFAULT_MODE
    if value not in MODES:
        raise ValueError('配置 [defaults] dispatch_mode 无效：' + value + '；只允许 ' + '、'.join(MODES) + '。')
    return value


def resolve_mode(option, session_value, parser):
    """命令行 > 会话默认值 > [defaults] dispatch_mode。"""
    validate_mode(option)
    return option or session_value or configured_mode(parser)


def pane_record(rows, pane, *, include_closed=False):
    matches = [row for row in rows.values() if row.get('pane_id') == pane and not row.get('closed_at')]
    if not matches and include_closed:
        matches = [row for row in rows.values() if row.get('pane_id') == pane]
    return matches[0] if len(matches) == 1 else {}


def replay(saved, request_hash):
    previous = messages.read_json(saved) if saved is not None else None
    if previous is None:
        return None
    if previous['fingerprint'] != request_hash:
        return 422, {'error': {'reason': 'idempotency_key_reused'}}
    return previous['status'], previous['body']


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

    def get(self, session=None, pane=None, fields=EMPTY):
        rows = self.read()
        row = rows.get(identity(session, pane), {})
        if not session and not row:
            row = pane_record(rows, pane)
        return {key: row.get(key) for key in fields}

    def get_mode(self, session=None, pane=None):
        return self.get(session, pane, MODE_FIELD)['dispatch_mode']

    def update_mode(self, mode, session=None, pane=None, *, saved=None, request_hash=None):
        """独立于 host/harness 的写入口，整体替换 host/harness 不会触及该值。"""
        return self.update(dict(dispatch_mode=validate_mode(mode)), session, pane,
                           saved=saved, request_hash=request_hash, fields=MODE_FIELD)

    def update(self, values, session=None, pane=None, *, saved=None, request_hash=None, fields=EMPTY):
        if fields is EMPTY:
            validate(values)
        key = identity(session, pane)
        lock = self.lock()
        try:
            previous = replay(saved, request_hash)
            if previous is not None:
                return previous
            rows = self.read()
            before = self.get(session, pane, fields)
            result = dict(before, **values)
            row = rows.get(key, {})
            rows[key] = dict(row, **result, session_id=session, pane_id=pane or row.get('pane_id'),
                             closed_at=row.get('closed_at'))
            paths = [self.path] + ([saved] if saved is not None else [])
            with events.write_transaction(paths):
                messages.atomic_json(self.path, rows, limit=None)
                if before != result:
                    shown = result if fields is EMPTY else dict(self.get(session, pane), **result)
                    events.emit(self.state, 'session.changed', dict(user_id=self.uid,
                        pane_id=rows[key]['pane_id'], session_id=session, dispatch_defaults=shown))
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
        return re.fullmatch(r'/v1/sessions/([^/]+)/(dispatch-defaults|dispatch-mode)', path)

    @staticmethod
    def app_match(path):
        """App 会话（hc: 前缀，地址可含 /）的派单默认值 M1 不支持。"""
        import re
        return re.fullmatch(r'/v1/sessions/hc:.+?/(dispatch-defaults|dispatch-mode)', path)

    def read(self, path):
        if self.app_match(path):
            return 409, {'error': 'unsupported_surface', 'surface': 'app'}
        match = self.match(path)
        if match:
            from . import cli
            session, pane = self.target(match[1])
            store = SessionDefaults(self.service.state, self.service.user_id)
            mode = match[2] == 'dispatch-mode'
            values = store.get(session, pane, MODE_FIELD if mode else EMPTY)
            try:
                configured = cli.Config().cp
                config_mode = configured_mode(configured)
            except (OSError, UnicodeError, configparser.Error, ValueError):
                return 503, {'error': {'reason': 'read_unavailable', 'message': '无法读取当前派单默认配置。'}}
            sensitive = set(self.service.sensitive)
            for section in configured.sections():
                for key, value in configured[section].items():
                    if value and (key, value) != ('ssh', 'local') and self.service.is_sensitive_field(key):
                        sensitive.add(value)
            values['config'] = dict(dispatch_mode=config_mode) if mode else dict(
                host=configured.get('defaults', 'host', fallback=None) or 'auto',
                harness=configured.get('defaults', 'harness', fallback=None) or None,
                dispatch_mode=config_mode)
            return 200, self.service.redact(values, sensitive=sensitive)
        return None

    def put(self, handler):
        import hashlib
        import json
        from urllib.parse import unquote, urlsplit
        from .rd_idempotency import fingerprint
        path = unquote(urlsplit(handler.path).path)
        if self.app_match(path):
            try:
                length = int(handler.headers.get('Content-Length', '0'))
                if not handler.headers.get('Transfer-Encoding') and 0 < length <= 8 * 1024 * 1024:
                    handler.rfile.read(length)
            except ValueError:
                pass
            if not self.sessions.allowed(getattr(handler, 'session_device', None)):
                return handler.json(403, {'error': {'reason': 'session_access_disabled'}})
            return handler.json(409, {'error': 'unsupported_surface', 'surface': 'app'})
        match = self.match(path)
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
            mode = match[2] == 'dispatch-mode'
            if mode:
                if not isinstance(body, dict) or set(body) - MODE_FIELD.keys():
                    raise ValueError('派单模式请求只允许 dispatch_mode 字段。')
                validate_mode(body.get('dispatch_mode'))
                values = dict(MODE_FIELD, **body)
            else:
                validate(body)
                # PUT replaces host/harness only; dispatch_mode has its own endpoint.
                values = dict(EMPTY, **body)
            scope = [self.service.user_id, getattr(handler, 'device_id', None), key]
            digest = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            saved = self.service.state / 'session-default-requests' / (digest + '.json')
            request_hash = fingerprint('PUT', match[0], body)
            with self.service.writer:
                if self.service.stop.is_set():
                    raise OSError('服务正在停止。')
                previous = replay(saved, request_hash)
                if previous is not None:
                    return handler.json(previous[0], self.service.redact(previous[1]))
                target = next((item['row'] for item in self.sessions.discover()
                               if item['row']['id'] == match[1]), None)
                if target is None:
                    return handler.json(404, {'error': {'reason': 'session_closed', 'message': '会话已关闭。'}})
                session = target.get('session_id') if not target.get('probable') else None
                pane = match[1]
                store = SessionDefaults(self.service.state, self.service.user_id)
                if mode:
                    status, reply = store.update_mode(values['dispatch_mode'], session, pane,
                                                      saved=saved, request_hash=request_hash)
                else:
                    status, reply = store.update(values, session, pane, saved=saved, request_hash=request_hash)
            handler.json(status, self.service.redact(reply))
        except (ValueError, TypeError, UnicodeError) as exc:
            reason = 'dispatch_defaults_write_failed' if str(exc).startswith('event_write_failed:') else 'invalid_dispatch_defaults'
            handler.json(503 if reason.endswith('failed') else 400, {'error': {'reason': reason, 'message': '无法保存会话派单默认值。' if reason.endswith('failed') else '派单默认值请求无效。'}})
        except OSError:
            handler.json(503, {'error': {'reason': 'dispatch_defaults_write_failed', 'message': '无法保存会话派单默认值。'}})
