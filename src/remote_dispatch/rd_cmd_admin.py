"""管理类命令：目标机配置、员工配置包（profile）、体检、serve/service/device。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import configparser
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import tempfile
import time
from urllib.parse import quote
import uuid
from . import rd_profiles as profiles
from . import rd_native as native
from . import rd_messages as messages
from . import rd_artifacts as artifacts
from . import rd_agents as agents
from . import rd_query as query
from .rd_lock import JobBusy, JobLock
from . import cli

__all__ = [
    "cmd_device",
    "cmd_doctor",
    "cmd_host",
    "cmd_hosts",
    "cmd_pair",
    "cmd_profile",
    "cmd_serve",
    "cmd_service",
    "cmd_session_access",
    "config_error",
    "config_lock",
    "config_path",
    "config_reply",
    "config_unavailable",
    "die_active_dispatches",
    "die_invalid_field",
    "doctor_fingerprint",
    "host_fields",
    "host_row",
    "host_secrets",
    "local_job_states",
    "local_profile",
    "mask_ssh",
    "prepare_local_profile",
    "print_hosts",
    "print_json",
    "print_service_status",
    "profile_report",
    "redact_public",
    "remote_profile",
    "serve_admin",
    "serve_config_request",
    "serve_reply",
    "service_environment",
    "service_hint",
    "sync_profile",
    "table_cell",
    "validate_host_fields",
]


def config_path():
    return Path(cli.CONFIG).absolute()


def config_lock():
    lock = JobLock(cli.STATE, "host-config")
    lock.path = cli.config_path().with_name(cli.config_path().name + ".lock")
    return lock


def serve_config_request(method, path, body=None):
    """Ask the running serve before falling back; only pre-send absence can fall back."""
    from .rd_host_config import config_digest
    from .rd_serve import ServeFailed, ServeUnavailable, serve_call
    headers = {"X-Rdispatch-Config-SHA256": config_digest(cli.config_path())}
    if method in ("POST", "PUT", "DELETE"):
        headers["Idempotency-Key"] = uuid.uuid4().hex
    try:
        return serve_call(method, path, body, headers=headers, timeout=cli.CONFIG_REQUEST_TIMEOUT)
    except ServeUnavailable as error:
        if error.absent:
            return None
        raise ValueError("cli_forward_failed: command may have run; check state before retrying") from error
    except ServeFailed as error:
        raise ValueError("cli_forward_failed: command may have run; check state before retrying") from error


def config_error(error, status):
    reason = error.get("reason") if isinstance(error.get("reason"), str) else "invalid_response"
    field = error.get("field")
    if reason in ("invalid_host_field", "host_field_required"):
        message = reason + ": 字段 " + str(field or "?")
        if reason == "host_field_required":
            message += " 不能为空"
        elif field == "max_heavy":
            message += " 必须是大于等于 1 的整数"
        else:
            message += " 取值无效"
    elif reason == "host_has_active_dispatches":
        ids = error.get("running_job_ids") or []
        message = (reason + ": running_count=" + str(error.get("running_count", 0))
                   + " running_job_ids=" + ",".join(map(str, ids[:20])))
    elif reason == "host_config_busy":
        message = reason + ": 配置正在写入，请稍后重试"
    elif reason == "not_found":
        message = "not_found: 目标机不存在"
    elif reason == "host_exists":
        message = "host_exists: 目标机名称已存在"
    elif reason == "cli_config_mismatch":
        message = "cli_config_mismatch: CLI 与 serve 使用不同的配置文件"
    elif reason == "invalid_host_request":
        message = "invalid_host_request: 目标机配置请求格式无效"
    elif reason == "method_not_allowed":
        message = "method_not_allowed: 此路径不支持该操作"
    elif reason == "idempotency_key_required":
        message = "idempotency_key_required: 请提供有效的请求标识"
    elif reason == "idempotency_key_reused":
        message = "idempotency_key_reused: 请求标识已用于其他操作"
    elif reason == "host_config_unavailable":
        message = "host_config_unavailable: 无法读取或保存目标机配置"
    elif reason == "host_config_outcome_unknown":
        message = "host_config_outcome_unknown: 操作结果未确认，请刷新后核对"
    else:
        message = reason
        if field:
            message += " field=" + str(field)
        if error.get("text"):
            message += "：" + str(error["text"])
    return message, 4 if status in (400, 422) else 1


def config_reply(method, path, body=None):
    reply = cli.serve_config_request(method, path, body)
    if reply is None:
        return None
    status, payload = reply
    if status >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        error = error if isinstance(error, dict) else {}
        message, code = cli.config_error(error, status)
        cli.die(message, code)
    return payload


def mask_ssh(value):
    return re.sub(r"(://)[^/@\s]+@", r"\1[redacted]@", value or "")


def host_secrets(cp, module):
    return {value for section in cp.sections() if section.startswith("host:")
            for field, value in module.effective(cp, section[5:]).items()
            if field not in module.PUBLIC_FIELDS and value}


def redact_public(value, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[redacted]", value, flags=re.S)


def host_row(cp, name, module, secrets=()):
    values = module.effective(cp, name)
    public = {key: cli.redact_public(values[key], secrets) for key in module.PUBLIC_FIELDS if key != "ssh"}
    connection = values.get("ssh", "")
    public["ssh"] = cli.mask_ssh(cli.redact_public(connection, secrets - {connection}))
    return dict(name=name, values=public,
                protected_fields=[dict(field=key, is_set=bool(value)) for key, value in values.items()
                                  if key not in module.PUBLIC_FIELDS])


def local_job_states(records):
    states = {}
    for rec in records:
        number = int(rec.get("rounds") or 1)
        try:
            receipt = messages.read_json(query.result_path(cli.STATE, rec, number)) or {}
        except (OSError, ValueError):
            # A damaged receipt cannot prove the dispatch ended.
            receipt = {}
        states[rec["job"]] = dict(
            state=receipt.get("execution_state") or rec.get("execution_state")
            or ("dead" if rec.get("dispatch_state") == "preparation_failed" else "starting"))
    return states


def host_fields(a):
    fields = {}
    for field in ("ssh", "workdir", "sync", "platform", "enabled", "launcher", "tags"):
        value = getattr(a, field, None)
        if value is not None:
            fields[field] = value
    for item in getattr(a, "set_fields", None) or []:
        if "=" not in item:
            cli.die("--set 需要 FIELD=VALUE。", 4)
        field, value = item.split("=", 1)
        if field == "name":
            cli.die("名称用位置参数指定，不能通过 --set 修改。", 4)
        fields[field] = value
    for field in getattr(a, "unset_fields", None) or []:
        if field == "name":
            cli.die("名称用位置参数指定，不能通过 --unset 修改。", 4)
        if field in fields:
            cli.die(f"--set 和 --unset 同时给出字段 {field}。", 4)
        fields[field] = None
    return fields


def validate_host_fields(module, fields):
    for field, value in fields.items():
        try:
            module.validate_field(field, value)
        except module.InvalidField:
            cli.die(f"invalid_host_field: 字段 {field} 取值无效", 4)
        except ValueError:
            cli.die("invalid_host_request: 字段名必须是小写字母开头的标识符", 4)


def die_invalid_field(error):
    message, code = cli.config_error(dict(reason=error.reason, field=error.field), 400)
    cli.die(message, code)


def die_active_dispatches(running):
    message, code = cli.config_error(dict(reason="host_has_active_dispatches",
                                      running_count=len(running), running_job_ids=running), 409)
    cli.die(message, code)


def config_unavailable(message="host_config_unavailable: 无法读取或保存目标机配置"):
    cli.die(message, 1)


def print_hosts(reply, as_json):
    if as_json:
        print(json.dumps(reply, ensure_ascii=False))
        return
    rows = reply.get("hosts") or []
    if not rows:
        print("no hosts configured.")
        return
    for row in rows:
        values = row.get("values") or {}
        order = ("ssh", "workdir", "sync", "platform", "launcher", "enabled", "tags")
        shown = [f"{key}={values[key]}" for key in order if key in values]
        shown += [f"{key}={values[key]}" for key in sorted(set(values) - set(order))]
        print("  ".join([row.get("name", "?")] + shown))


def cmd_session_access(a, cfg):
    from . import rd_host_config as host_config
    if a.action == "show":
        reply = cli.config_reply("GET", host_config.SESSION_ACCESS)
        if reply is None:
            try:
                _, cp = host_config.load(cli.config_path())
                reply = dict(enabled=host_config.session_access(cp))
            except (OSError, UnicodeError, configparser.Error):
                cli.config_unavailable()
    else:
        enabled = a.action == "on"
        reply = cli.config_reply("PUT", host_config.SESSION_ACCESS, {"enabled": enabled})
        if reply is None:
            try:
                with cli.config_lock():
                    raw, _ = host_config.load(cli.config_path())
                    updated, encoded = host_config.session_access_candidate(raw, enabled)
                    host_config.replace(cli.config_path(), raw, encoded)
                    reply = dict(enabled=host_config.session_access(updated))
            except JobBusy:
                cli.die("host_config_busy: 配置正在写入，请稍后重试", 1)
            except (OSError, UnicodeError, configparser.Error, ValueError):
                cli.config_unavailable()
    if a.json:
        print(json.dumps(reply, ensure_ascii=False))
    else:
        print("会话访问：开" if reply.get("enabled") else "会话访问：关")


def cmd_host(a, cfg):
    from . import rd_host_config as host_config
    if a.action == "list":
        reply = cli.config_reply("GET", host_config.COLLECTION)
        if reply is None:
            try:
                _, cp = host_config.load(cli.config_path())
                secrets = cli.host_secrets(cp, host_config)
                reply = dict(hosts=[cli.host_row(cp, section[5:], host_config, secrets)
                                    for section in cp.sections() if section.startswith("host:")])
            except (OSError, UnicodeError, configparser.Error):
                cli.config_unavailable()
        cli.print_hosts(reply, a.json)
        return
    fields = {} if a.action == "delete" else cli.host_fields(a)
    try:
        host_config.validate_name(a.name)
    except host_config.InvalidField as error:
        cli.die_invalid_field(error)
    cli.validate_host_fields(host_config, fields)
    if a.action == "add":
        body = dict(fields, name=a.name)
        reply = cli.config_reply("POST", host_config.COLLECTION, body)
    elif a.action == "update":
        reply = cli.config_reply("PUT", host_config.COLLECTION + "/" + a.name, fields)
    else:
        reply = cli.config_reply("DELETE", host_config.COLLECTION + "/" + a.name, {})
    if reply is None:
        try:
            with cli.config_lock():
                raw, cp = host_config.load(cli.config_path())
                if a.action == "add":
                    if cp.has_section("host:" + a.name):
                        cli.die("host_exists: 目标机 " + a.name + " 已存在", 1)
                    updated, encoded = host_config.host_candidate(raw, a.name, fields)
                    host_config.validate(host_config.effective(updated, a.name))
                elif a.action == "update":
                    if not cp.has_section("host:" + a.name):
                        cli.die("not_found: 目标机 " + a.name + " 不存在", 1)
                    updated, encoded = host_config.host_candidate(raw, a.name, fields)
                    host_config.validate(host_config.effective(updated, a.name))
                else:
                    if not cp.has_section("host:" + a.name):
                        cli.die("not_found: 目标机 " + a.name + " 不存在", 1)
                    records = [rec for rec in cli.all_jobs() if rec.get("host") == a.name]
                    running = host_config.active_job_ids(records, cli.local_job_states(records))
                    if running:
                        cli.die_active_dispatches(running)
                    updated, encoded = host_config.host_candidate(raw, a.name, {}, delete=True)
                host_config.replace(cli.config_path(), raw, encoded)
                reply = (dict(deleted=a.name) if a.action == "delete"
                         else dict(host=cli.host_row(updated, a.name, host_config,
                                                 cli.host_secrets(updated, host_config))))
        except host_config.InvalidField as error:
            cli.die_invalid_field(error)
        except JobBusy:
            cli.die("host_config_busy: 配置正在写入，请稍后重试", 1)
        except (OSError, UnicodeError, configparser.Error, ValueError):
            cli.config_unavailable()
    if a.json:
        print(json.dumps(reply, ensure_ascii=False))
    elif a.action == "delete":
        print("deleted host " + reply["deleted"])
    else:
        print(("added" if a.action == "add" else "updated") + " host " + reply["host"]["name"])


def local_profile(uid, required=False):
    source = os.environ.get("RD_PROFILE_DIR") or os.path.expanduser("~/.config/remote-dispatch/profiles/" + uid)
    if not os.path.isdir(source) and not required:
        return None, None
    return profiles.bundle(source, uid, cli.STATE)


def remote_profile(host, uid):
    if host["platform"] == "windows":
        value = native.request(host, "profile-read")
        host["resolved_workdir"] = value["root"]
        return profiles.validate_manifest(value["manifest"], uid) if value["manifest"] else None
    r = cli.ssh(host, '[ ! -f "$ROOT/profiles/current.json" ] || cat "$ROOT/profiles/current.json"')
    if not r.stdout.strip():
        return None
    try:
        return profiles.validate_manifest(json.loads(r.stdout), uid)
    except (ValueError, TypeError):
        cli.die("远端员工配置元数据无效；先检查 ID、时间和配置清单。", 4)


def profile_report(host, uid, local, remote):
    state = profiles.comparison(local, remote)
    fmt = lambda p: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(p["source_updated_at"])) if p else "未确认"
    print(f"Remote Dispatch: ID={uid} host={host['name']} root={host.get('resolved_workdir', host['workdir'])}")
    print(f"配置源更新时间：本地 {fmt(local)}；远端 {fmt(remote)}；状态 {state}")
    if local:
        print("本地配置版本：" + local["fingerprint"])
        old = remote.get("files", {}) if remote else {}
        changed = [n for n, v in local["files"].items() if old.get(n) != v]
        removed = sorted(set(old) - set(local["files"]))
        print("同步清单：" + ", ".join(local["files"]))
        print("变化：" + (", ".join(changed) or "无") + "；移出新版本：" + (", ".join(removed) or "无"))
    if state == "stale":
        print("提醒：本地配置领先远端超过 7 天。是否更新？未确认时使用现有远端版本；同步需单独确认。")
    elif state == "remote-newer":
        print("提醒：远端配置更新；不得自动用另一台电脑的旧配置覆盖。")
    elif state == "unknown":
        print("未确认：此电脑未准备本地配置包，无法比较更新时间；使用已有远端版本。")
    elif state == "missing":
        print("远端首次使用此用户 ID。请确认是否同步，确认后执行 rdispatch profile sync。")
    return state


def sync_profile(host, uid, local, payload, remote):
    """把本地配置包写入目标机；调用方负责确认与输出。内容一致时不写，返回 None。"""
    if remote and remote["fingerprint"] == local["fingerprint"]:
        return None
    if remote and local["source_updated_at"] < remote["source_updated_at"]:
        cli.die("本地配置比远端旧；先取正确配置，不覆盖远端新版本。", 4)
    version = local["fingerprint"]
    expected_current = remote["fingerprint"] if remote else ""
    if host["platform"] == "windows":
        synced = native.request(host, "profile-sync", {"expected": remote, "manifest": local, "user_id": uid}, payload=payload)
        if synced["fingerprint"] != version:
            cli.die("Windows 配置同步回读版本不匹配。")
        return synced
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        cli.ssh(host, f'''set -e
umask 077
mkdir -p "$ROOT/profiles"
mkdir "$ROOT/profiles/.sync-lock" || {{ echo "另一次同步进行中" >&2; exit 4; }}
trap 'rmdir "$ROOT/profiles/.sync-lock"' EXIT
current=$([ ! -f "$ROOT/profiles/current.json" ] || python3 -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])' < "$ROOT/profiles/current.json")
[ "$current" = {shlex.quote(expected_current)} ] || {{ echo "远端配置已变化，请重新检查并确认" >&2; exit 4; }}
stage=$(mktemp -d "$ROOT/profiles/.incoming.XXXXXX")
trap 'rm -rf "$stage"; rmdir "$ROOT/profiles/.sync-lock"' EXIT
tar -xf - -C "$stage"
cp "$stage/manifest.json" "$ROOT/profiles/current.tmp"
if [ ! -d "$ROOT/profiles/{version}" ]; then mv "$stage" "$ROOT/profiles/{version}"; fi
mv "$ROOT/profiles/current.tmp" "$ROOT/profiles/current.json"
''', stdin=stream)
    synced = cli.remote_profile(host, uid)
    if synced["fingerprint"] != version:
        cli.die("同步回读版本不匹配；停止派单。")
    return synced


def prepare_local_profile(host, uid, local, payload, profile):
    """本机目标的配置源与目的地同机同用户：副本缺失或落后时直接同步，不要求确认。"""
    if profiles.comparison(local, profile) == "remote-newer":
        cli.die("本机目标的员工配置副本比本地配置包新；不覆盖较新副本，先核对配置再派单。未推送任务分支。", 4)
    if not local:
        if profile is not None:
            return profile
        cli.die("此 ID 尚无员工配置副本；本机也没有配置包，无法自动同步。先准备配置包再派单。未推送任务分支。", 4)
    synced = cli.sync_profile(host, uid, local, payload, profile)
    if synced is None:
        return profile
    print(f"本机目标 {host['name']}：已自动同步员工配置副本 version={synced['fingerprint'][:12]}；无需确认。")
    return synced


def cmd_profile(a, cfg):
    uid = cli.require_user()
    host = cli.scoped_host(cfg.host(a.host), uid)
    local, payload = cli.local_profile(uid, required=a.action == "sync")
    remote = cli.remote_profile(host, uid)
    cli.profile_report(host, uid, local, remote)
    if a.action == "check":
        return
    if a.confirm_user_id != uid or not a.confirm_version or a.confirm_version != local["fingerprint"]:
        cli.die("尚未确认同步：向用户展示 ID、清单和版本，明确同意后传 --confirm-user-id 和 --confirm-version。", 4)
    synced = cli.sync_profile(host, uid, local, payload, remote)
    print("配置内容一致；无需更新。" if synced is None
          else f"已同步：ID={uid} version={synced['fingerprint']}；旧版本保留。")


def cmd_hosts(a, cfg):
    hosts = cfg.hosts()
    errors = cfg.host_errors()
    if not hosts and not errors:
        cli.die(f"no hosts configured. Add a [host:NAME] section to {cli.CONFIG}")
    for host, info in cli.probe_all(hosts):
        print(cli.fmt_probe(host, info, cli.assess(host, info)))
    for name, text in errors.items():
        print(f"{name:<12} 配置无效  {text}")
    if errors and not hosts:
        sys.exit(4)


def cmd_doctor(a, cfg):
    host = cfg.host(a.host)
    requested = a.agent or []
    if host["platform"] == "windows":
        unsupported = [name for name in requested if host["platform"] not in agents.SPECS.get(name, {}).get("platforms", ())]
        if unsupported:
            cli.die(f"Windows 目标机只支持 codex 派单；不支持：{', '.join(unsupported)}。", 4)
        info = native.request(host, "doctor")
        print(json.dumps(info, indent=2, ensure_ascii=False))
        status = cli.probe(host)
        print(cli.fmt_probe(host, status, cli.assess(host, status)))
        sys.exit(0 if info["ok"] and info["features"] else 1)
    names = requested or list(cli.AGENTS)
    checks = ['ok() { echo "  ok    $1"; }; bad() { echo "  MISSING  $1"; fail=1; }; fail=0',
              'command -v git >/dev/null && ok "git $(git --version | cut -d" " -f3)" || bad git',
              'command -v pgrep >/dev/null && ok pgrep || bad "pgrep (needed for timeouts and rdispatch stop)"']
    if host["launcher"] == "tmux":
        checks.append('command -v tmux >/dev/null && ok tmux || bad "tmux (launcher = tmux)"')
    for name in names:
        b = cfg.agent(name)["bin"]
        if requested:
            checks.append(f'command -v {shlex.quote(b)} >/dev/null && ok "agent {name}: $(command -v {shlex.quote(b)})" '
                          f'|| bad "agent {name} CLI"')
        else:
            checks.append(f'command -v {shlex.quote(b)} >/dev/null && ok "agent {name}: $(command -v {shlex.quote(b)})" '
                          f'|| echo "  absent   agent {name} (only needed if you dispatch to it)"')
        if name == "codex":
            checks.append("( " + cli.CODEX_CHECK + ' ) >/dev/null 2>&1 && ok "Codex profile runtime" || bad "Python 3.11+ / Codex isolation features"')
        if name == "agy" and requested:
            # 已安装与已登录分开报告；不读取凭据内容。
            checks.append("( " + agents.AGY_CHECK + ' ) >/dev/null 2>&1 && ok "AGY print/stream-json runtime" || bad "python3 / agy --input-format"')
            auth = host["agy_auth_file"]
            if auth:
                checks.append("[ -f " + cli.worker_path(auth) + " ] && ok " + shlex.quote("AGY 登录凭据 " + auth)
                              + " || bad " + shlex.quote("AGY 登录凭据 " + auth + "（先在目标机运行 agy 登录）"))
            else:
                checks.append('bad "agy_auth_file 未配置"')
    url = None
    if host["sync"] == "origin":
        target_dir = cli.get_dir(a)
        cmd = ["git", "-C", target_dir, "rev-parse", "--show-toplevel"] if target_dir else ["git", "rev-parse", "--show-toplevel"]
        p = cli.run(cmd, check=False)
        if p.returncode == 0:
            project, origin_url = cli.project_name(p.stdout.strip())
            url = cfg.project(project).get("origin_url") or origin_url
        if url:
            checks.append(f'GIT_TERMINAL_PROMPT=0 git ls-remote {shlex.quote(url)} HEAD >/dev/null 2>&1 '
                          f'&& ok "can read {url}" || bad "git access to {url} (worker needs its own credentials '
                          f'with push rights, or use sync = direct)"')
        else:
            checks.append('echo "  skipped  origin access (run rdispatch doctor inside the repository to test it)"')
    checks.append("exit $fail")
    label = "本机" if cli.is_local(host) else host["ssh"]
    print(f"{host['name']} ({label}), sync = {host['sync']}, launcher = {host['launcher']}")
    r = cli.ssh(host, "\n".join(checks), check=False, timeout=60)
    if r is None:
        cli.die("timed out")
    sys.stdout.write(r.stdout or r.stderr)
    failed = r.returncode
    failed = cli.doctor_fingerprint(cfg, host, a) or failed
    info = cli.probe(host)
    print(cli.fmt_probe(host, info, cli.assess(host, info)))
    sys.exit(failed)


def doctor_fingerprint(cfg, host, a):
    """项目配置了 fingerprint 时，在本机仓库目录与目标机工作根目录各跑一次并比较；返回非零表示不一致。
    doctor 没有任务 worktree，目标机侧在工作根目录执行：依赖仓库文件的命令以派单时的核对为准。"""
    target_dir = cli.get_dir(a)
    cmd = ["git", "-C", target_dir, "rev-parse", "--show-toplevel"] if target_dir else ["git", "rev-parse", "--show-toplevel"]
    p = cli.run(cmd, check=False)
    if p.returncode != 0:
        return 0
    repo = p.stdout.strip()
    project, _ = cli.project_name(repo)
    command, timeout = cli.fingerprint_settings(cfg.project(project))
    if not command:
        return 0
    try:
        local = cli.run_local_fingerprint(command, repo, timeout)
    except SystemExit:
        print("  MISSING  环境指纹：本机指纹命令失败或超时，无法比较（见上方错误）")
        return 1
    remote, error = cli.run_remote_fingerprint(host, cli.host_fingerprint_command(cfg.project(project), host, command),
                                           timeout, '"$ROOT" 2>/dev/null || cd "$HOME"')
    diff = "" if error else cli.compare_fingerprint(local, remote, host["name"])
    if error:
        print(f"  MISSING  环境指纹：{error}")
        return 1
    if diff:
        print("  MISSING  环境指纹与本机不一致：\n" + diff)
        return 1
    print(f"  ok    环境指纹一致（{artifacts.fingerprint_digest(local)}）")
    return 0


def serve_admin(method, path, body=None, *, action, write=False, uncertain_hint=""):
    """设备与配对窗口只存在于 serve 进程，任何失败都不得回退为直接写状态。"""
    from .rd_serve import ServeFailed, ServeUnavailable, admin_request
    try:
        status, reply = admin_request(method, path, body, key=secrets.token_hex(16) if write else None)
    except ServeUnavailable as error:
        cli.die(f"serve_unavailable: {error}；设备和配对窗口状态只在 serve 进程里，先运行 rdispatch service install。", 4)
    except ServeFailed as error:
        hint = "请求可能已生效，先检查状态再重试。" if write else "稍后重试。"
        cli.die(f"serve_request_failed: {error}；{hint}{uncertain_hint}", 4)
    cli.serve_reply(status, reply, action)
    return reply


def serve_reply(status, reply, action):
    if status == 200:
        return
    detail = reply.get("error") if isinstance(reply, dict) else None
    reason = detail.get("reason") if isinstance(detail, dict) else None
    hint = cli.SERVE_ERROR_HINTS.get(reason, f"{action}失败（serve 返回 HTTP {status}）。")
    cli.die((f"{reason}: " if reason else "") + hint, 4)


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def table_cell(value):
    if value is None:
        return "-"
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in str(value))


def cmd_device(a, cfg):
    if a.action == "list":
        reply = cli.serve_admin("GET", "/v1/devices", action="读取设备列表")
        devices = reply.get("devices") if isinstance(reply, dict) else None
        if not isinstance(devices, list) or not all(isinstance(row, dict) for row in devices):
            cli.die("serve_invalid_response: serve 未返回设备列表。", 4)
        if getattr(a, "json", False):
            cli.print_json(reply)
            return
        if not devices:
            print("no paired devices.")
            return
        print(f"{'ID':<34}{'NAME':<20}{'PAIRED_AT':<28}{'LAST_SEEN_AT':<28}STATUS")
        for row in devices:
            print(f"{cli.table_cell(row.get('device_id')):<34}{cli.table_cell(row.get('name')):<20}"
                  f"{cli.table_cell(row.get('paired_at')):<28}{cli.table_cell(row.get('last_seen_at')):<28}"
                  f"{'revoked' if row.get('revoked') else 'active'}")
        return
    reply = cli.serve_admin("DELETE", "/v1/devices/" + quote(a.device_id, safe=""), action="吊销设备", write=True)
    if getattr(a, "json", False):
        cli.print_json(reply)
    else:
        print("revoked " + a.device_id)


def cmd_pair(a, cfg):
    if a.action == "open":
        reply = cli.serve_admin("POST", "/v1/pairing-window", {}, action="打开配对窗口", write=True,
                            uncertain_hint="如不确定，先运行 rdispatch pair close 关闭可能已打开的窗口。")
        if not isinstance(reply, dict):
            cli.die("serve_invalid_response: serve 未返回配对窗口。", 4)
        if getattr(a, "json", False):
            cli.print_json(reply)
            return
        code = reply.get("code")
        if not isinstance(code, str) or not code:
            cli.die("serve_invalid_response: serve 未返回配对码。", 4)
        try:
            expires = int(reply.get("expires_in"))
        except (TypeError, ValueError):
            cli.die("serve_invalid_response: serve 未返回配对窗口过期时间。", 4)
        print("pairing code  " + code)
        print(f"expires in    {expires}s "
              f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + expires))})")
        if reply.get("spki_sha256"):
            print("fingerprint   " + str(reply["spki_sha256"]))
        candidates = reply.get("candidates") or []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            address = str(row.get("address"))
            endpoint = f"[{address}]:{row.get('port')}" if ":" in address else f"{address}:{row.get('port')}"
            kind = f" ({row['kind']})" if row.get("kind") else ""
            print(f"candidate     {endpoint}{kind}")
        if not candidates:
            print("candidate     none; configure a LAN or VPN listen address before pairing")
        return
    reply = cli.serve_admin("DELETE", "/v1/pairing-window", action="关闭配对窗口", write=True)
    if getattr(a, "json", False):
        cli.print_json(reply)
    else:
        print("pairing window closed")


def cmd_serve(a, cfg):
    from .rd_serve import run_service
    cfg.notify_channels()
    run_service(cfg, a.socket, a.poll_interval)


def service_environment():
    """plist 记录的环境：状态目录、用户、当前 PATH，以及用户设置过的配置文件。"""
    environment = {
        "RD_STATE": os.path.abspath(os.path.expanduser(cli.STATE)),
        "RD_USER_ID": cli.require_user(),
        "PATH": os.environ.get("PATH", ""),
    }
    if os.environ.get("RD_CONFIG"):
        environment["RD_CONFIG"] = os.path.abspath(os.path.expanduser(os.environ["RD_CONFIG"]))
    if os.environ.get("RD_SERVE_SOCKET"):
        # CLI 转交与 App 都读这个变量；serve 必须监听同一路径。
        environment["RD_SERVE_SOCKET"] = os.path.abspath(os.path.expanduser(os.environ["RD_SERVE_SOCKET"]))
    return environment


def print_service_status(state):
    def value(field):
        return cli.table_cell(state.get(field))

    loaded = {True: "yes", False: "no", None: "unknown"}[state["loaded"]]
    if state["pid"] is None:
        process = "-"
    else:
        process = f"{state['pid']} ({'running' if state['running'] else 'not running'})"
    if state["config_reload"] is None:
        config_reload = "-"
    else:
        reload_status = state["config_reload"]
        config_reload = ("ok " if reload_status.get("ok") else "failed ") + cli.table_cell(reload_status.get("at"))
        if reload_status.get("error"):
            config_reload += f" ({reload_status['error']})"
    print(f"{'installed':<15}{'yes' if state['installed'] else 'no'}")
    print(f"{'label':<15}{state['label']}")
    print(f"{'plist':<15}{value('plist')}")
    print(f"{'loaded':<15}{loaded}")
    print(f"{'process':<15}{process}")
    print(f"{'socket':<15}{value('socket')}")
    print(f"{'serve':<15}{'online' if state['serve_online'] else 'offline'}")
    print(f"{'package':<15}{value('package_version')}")
    print(f"{'contract':<15}{value('contract_version')}")
    print(f"{'config reload':<15}{config_reload}")
    print(f"{'executable':<15}{value('executable')}")
    print(f"{'logs':<15}{value('log')}")


def service_hint(state):
    """已安装时的人读排查提示；JSON 只给字段，不掺提示行。"""
    if state["plist_error"]:
        return f"无法读取 plist（{state['plist_error']}）；运行 rdispatch service install 重写。"
    if state["launchctl_error"]:
        return "无法读取 launchd 状态：" + state["launchctl_error"]
    if state["serve_online"]:
        if state["loaded"] is False:
            return "serve 在响应 socket，但 launchd 未加载本任务；运行 rdispatch service restart 交给 launchd 托管。"
        if state["package_version"] != state["cli_version"]:
            return (f"serve 版本 {state['package_version']} 与当前 CLI 版本 {state['cli_version']} 不同；"
                    "运行 rdispatch service restart 使新代码生效。")
        return None
    if not state["running"]:
        log = state["error_log"] or state["log"]
        if log:
            return f"serve 未运行；查看 {log}，或运行 rdispatch service restart。"
        return "serve 未运行；运行 rdispatch service install 重写 plist 后重试。"
    return "serve 未响应 socket；运行 rdispatch service restart 后重试。"


def cmd_service(a, cfg):
    from . import rd_service as service
    if sys.platform != "darwin":
        cli.die("service_unsupported: rdispatch service 只支持 macOS。", 4)
    path = service.plist_path()
    if a.action == "install":
        environment = cli.service_environment()
        try:
            service.prepare(environment["RD_STATE"])
            service.write(path, service.document(service.entry(), environment))
        except OSError as error:
            cli.die(f"service_install_failed: {error}", 4)
        service.reload(path)
        print(f"installed serve {service.LABEL}")
        print(f"plist  {path}")
        print(f"socket {environment.get('RD_SERVE_SOCKET') or Path(environment['RD_STATE']) / service.SOCKET_NAME}")
        print(f"logs   {Path(environment['RD_STATE']) / service.ERROR_LOG_NAME}")
        return
    if a.action == "restart":
        if not path.exists():
            cli.die(f"service_not_installed: {cli.SERVICE_INSTALL_HINT}", 4)
        service.reload(path)
        print(f"reloaded serve {service.LABEL}")
        return
    if a.action == "uninstall":
        if not path.exists():
            service.bootout()
            print("serve not installed.")
            return
        service.unload()
        try:
            path.unlink()
        except OSError as error:
            cli.die(f"service_uninstall_failed: {error}", 4)
        print(f"uninstalled serve {service.LABEL} (state and logs kept)")
        return
    if a.action == "status":
        state = service.status(path)
        if getattr(a, "json", False):
            cli.print_json(state)
        else:
            cli.print_service_status(state)
            hint = cli.service_hint(state)
            if state["installed"] and hint:
                print(f"{'hint':<15}{hint}")
        if not state["installed"]:
            cli.die(f"service_not_installed: {cli.SERVICE_INSTALL_HINT}", 4)
        return
    cli.die("service_action_unknown: 未知的 service 动作。", 4)
