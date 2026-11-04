"""启动：运行脚本生成、启动文件打包上传、缓存目录环境、员工配置预检。

拆自 cli.py，行为不变。引用 cli 里定义的名字一律写成 cli.<名字>：测试和调用方对 cli.<名字> 的补丁
对这里的代码同样生效。请先导入 cli（cli 末尾会把本模块的名字重新导出）。
"""

import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import posixpath
import shlex
import tempfile
import tarfile
import time
from . import rd_native as native
from . import rd_agents as agents
from . import rd_line as workline
from .rd_host_config import parse_cache_dirs
from . import cli

__all__ = [
    "agent_command",
    "agent_preflight",
    "build_run_script",
    "bundle_script",
    "cache_env",
    "cache_env_script",
    "launch",
    "native_init",
    "push_task_branch",
    "put_file",
    "read_text_file",
    "tar_stream",
    "upload_and_start",
    "worker_path",
]


def build_run_script(host, rec, round_no, timeout):
    push = ""
    if rec["sync"] == "origin":
        push = (f'git push origin {shlex.quote("HEAD:refs/heads/" + rec["branch"])} > "$RD_JOB_DIR/push.log" 2>&1; '
                'echo $? > "$RD_JOB_DIR/push_rc"')
    env_file = ""
    if host["env_file"]:
        ef = host["env_file"].replace("~/", "$HOME/", 1)
        env_file = f'[ -f "{ef}" ] && . "{ef}"'
    shared = dict(host, workdir=host.get("shared_workdir", host["workdir"]))
    identity = 'export RD_USER_ID=' + shlex.quote(rec["user_id"]) if rec.get("user_id") else ""
    command = 'sh "$RD_JOB_DIR/cmd.r$RD_ROUND.sh"'
    if rec.get("profile") or rec.get("isolated"):
        command = ('python3 "$RD_JOB_DIR/rd_profiles.py" "$RD_JOB_DIR" "$RD_JOB_DIR/cmd.r$RD_ROUND.sh" '
                   + shlex.quote(rec.get("auth_file") or host["codex_auth_file"]) + " " + shlex.quote(rec.get("agent_kind") or "codex")
                   + (" refresh" if rec.get("refresh_auth_round") == round_no else ""))
    report_env, report_finish = "", ":"
    if rec.get("handoff_version"):
        report_env = 'export RD_REPORTER="$RD_JOB_DIR/rd_messages.py" RD_AGENT=' + shlex.quote(rec.get("agent") or "")
        if rec.get("test") and not rec.get("agent") and rec.get("cmd"):
            report_env += " RD_TEST_COMMAND=" + shlex.quote(rec["cmd"])
        report_finish = ('printf "%s\\n" "$rc" > "$RD_JOB_DIR/exit.r$RD_ROUND"; '
                         'if command -v python3 >/dev/null 2>&1; then python3 "$RD_REPORTER" finish --exit "$rc" '
                         '>> "$RD_JOB_DIR/report.r$RD_ROUND.log" 2>&1; fi')
    rollout_env = "export RD_ROLLOUT_STATS=1" if str(host.get("rollout_stats", "0")).lower() in ("1", "true", "yes") else ""
    subs = {"JOB": rec["job"], "BASE": shlex.quote(workline.round_base(rec, round_no) or ""), "ROLLOUT_ENV": rollout_env, "ROUND": str(round_no), "PATH": host["path"], "ROOT": cli.root_expr(host),
            "MAX_HEAVY": host["max_heavy"], "ENV_FILE": env_file, "KILLTREE": cli.KILLTREE,
            "TIMEOUT": str(int(timeout)), "PUSH": push, "SHARED_ROOT": cli.root_expr(shared),
            "CACHE_ENV": cli.cache_env_script(host, rec.get("user_id")), "IDENTITY": identity, "EXEC": command, "REPORT_ENV": report_env, "REPORT_FINISH": report_finish}
    subs["REGEN"] = "\n".join(
        f'rd_sz=$(du -sk "$RD_WT"/{shlex.quote(p)} 2>/dev/null | cut -f1); '
        f'printf "path_size_kb=%s\\t%s\\n" "${{rd_sz:-0}}" {shlex.quote(p)} >> "$RD_JOB_DIR/timing.r$RD_ROUND"'
        for p in rec.get("regenerable_paths") or [])
    text = cli.RUN_TEMPLATE
    for k, v in subs.items():
        text = text.replace(f"@@{k}@@", v)
    return text


def agent_command(agent, model, effort, session=None, permission=None):
    try:
        return agents.command(agent, model, effort, session, permission)
    except ValueError as error:
        cli.die(str(error), 4)


def put_file(host, rel, content, mode="644"):
    if host["platform"] == "windows":
        native.request(host, "upload", {"relative": rel}, payload=content.encode())
        return
    cli.ssh(host, f'mkdir -p "$(dirname "$ROOT/{rel}")" && cat > "$ROOT/{rel}" && chmod {mode} "$ROOT/{rel}"',
        stdin_data=content)


def push_task_branch(repo, destination, branch, sha, env=None):
    pushed = cli.git(["push", "--porcelain", "--no-follow-tags", f"--force-with-lease=refs/heads/{branch}:",
                  destination, f"{sha}:refs/heads/{branch}"], repo, env=env)
    if not any(line.startswith("*\t") for line in pushed.stdout.splitlines()):
        cli.die(f"任务分支已存在：{branch}；保留原分支，请换用新的 --branch。")


def native_init(host, rec, repo, sha, origin):
    data = {k: rec[k] for k in ("job", "bare", "branch", "sync")}
    if rec["sync"] == "origin":
        if not origin:
            cli.die("Windows origin 同步需要仓库 origin。")
        cli.push_task_branch(repo, "origin", rec["branch"], sha)
        rec["owns_origin_branch"] = True
        cli.save_job(rec)
        result = native.request(host, "init", dict(data, origin=origin), timeout=300)
    elif rec["sync"] == "direct":
        reference = "refs/rd-transfer/" + rec["job"]
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "source.bundle"
            cli.git(["update-ref", reference, sha], repo)
            try:
                cli.git(["bundle", "create", str(bundle), reference], repo)
            finally:
                cli.git(["update-ref", "-d", reference, sha], repo)
            with bundle.open("rb") as stream:
                result = native.request(host, "init", dict(data, transfer_ref=reference), payload=stream, timeout=300)
    else:
        cli.die("sync 必须是 origin 或 direct。")
    host["workdir"] = result["root"]
    rec["worker_root"] = result["root"]


def tar_stream(entries):
    """entries: [(包内路径, bytes, 八进制权限字符串)] -> 已回绕到开头的临时文件（GNU tar 格式）。"""
    stream = tempfile.TemporaryFile()
    now = time.time()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), int(mode, 8), now
            archive.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream


def bundle_script(files, libs, start):
    """一次 ssh 里完成：解包到 $ROOT 下的临时目录 -> 公共脚本库按摘要就位 -> 逐个原子 mv 发布 -> 启动。
    files: [(相对 $ROOT 的路径, bytes, 权限)]；libs: [(目标相对路径, bytes, 权限)]，按内容摘要存放在 $ROOT/lib/<摘要>/。"""
    q = shlex.quote
    lines = ['set -e', 'mkdir -p "$ROOT"', 'T=$(mktemp -d "$ROOT/.up.XXXXXX")', "trap 'rm -rf \"$T\"' EXIT",
             'tar -xpf - -C "$T"']
    for dest, data, mode in libs:
        name = posixpath.basename(dest)
        lib = f'"$ROOT"/lib/{hashlib.sha256(data).hexdigest()[:16]}/{q(name)}'
        lines.append(f'if [ ! -f {lib} ] || [ "$(wc -c < {lib} | tr -d " ")" != {len(data)} ]; then '
                     f'if [ -f "$T"/.lib/{q(name)} ]; then mkdir -p "$(dirname {lib})" && mv -f "$T"/.lib/{q(name)} {lib}; '
                     f'else exit {cli.LIB_MISSING}; fi; fi')
    for dest in sorted({posixpath.dirname(rel) for rel, _, _ in files + libs} - {""}):
        lines.append(f'mkdir -p "$ROOT"/{q(dest)}')
    for rel, _, _ in files:
        lines.append(f'mv -f "$T"/{q(rel)} "$ROOT"/{q(rel)}')
    for dest, data, mode in libs:
        lib = f'"$ROOT"/lib/{hashlib.sha256(data).hexdigest()[:16]}/{q(posixpath.basename(dest))}'
        target = f'"$ROOT"/{q(dest)}'
        lines.append(f'cmp -s {lib} {target} 2>/dev/null || {{ cp {lib} {target}.new.$$ && chmod {mode} {target}.new.$$ '
                     f'&& mv -f {target}.new.$$ {target}; }}')
    lines.append(start)
    return "\n".join(lines)


def upload_and_start(host, files, libs, start):
    """POSIX 启动打包：所有启动文件一次 tar 流上传并原子发布，同一次 ssh 里启动。
    公共脚本先假定远端库里已有同摘要副本（不传）；缺失时返回 87，再带脚本重试一次。"""
    for with_libs in (False, True):
        entries = list(files) + ([(".lib/" + posixpath.basename(d), data, m) for d, data, m in libs] if with_libs else [])
        with cli.tar_stream(entries) as stream:
            result = cli.ssh(host, cli.bundle_script(files, libs, start), check=False, stdin=stream)
        if result.returncode == 0:
            return
        if result.returncode != cli.LIB_MISSING or with_libs:
            cli.die(f"[{host['name']}] step failed ({result.returncode}):\n{(result.stderr or result.stdout).strip()}")


def read_text_file(*parts):
    with open(os.path.join(cli.HERE, *parts), "rb") as stream:
        return stream.read()


def launch(host, rec, round_no, cmd_text, prompt_text, timeout):
    j = rec["job"]
    windows = host["platform"] == "windows"
    files, libs = [], []  # files：本任务专属；libs：跨任务公共脚本（按摘要复用）
    if rec.get("handoff_version"):
        if prompt_text is not None:
            reporter = 'python3 "$RD_REPORTER"' if not windows else '& ' + native.ps_quote(host["python"]) + ' $env:RD_REPORTER'
            prompt_text += ("\n\nRemote Dispatch 本轮交付约定：只完成本轮指令范围内工作，结果回传后等待发起方新指令。"
                           "执行中用 " + reporter + " progress --phase 阶段 --summary 脱敏摘要 记录状态；"
                           "收尾用 " + reporter + " result --outcome completed|partial|blocked|failed "
                           "--checks passed|failed|partial|not_run --summary 脱敏结果 --artifact 证据路径。"
                           "检查状态为执行方报告，最终验收由发起方判断；密码、私钥和敏感正文不写回执。\n")
        libs += [(f"jobs/{j}/rd_messages.py", cli.read_text_file("rd_messages.py"), "600"),
                 (f"jobs/{j}/rd_cache.py", cli.read_text_file("rd_cache.py"), "600")]
        files.append((f"jobs/{j}/reports/dispatch.r{round_no}.json",
                      json.dumps(dict(job=j, round=round_no, initiator_session=rec.get("initiator_session"))).encode(), "600"))
    if windows:
        # Windows 原生 worker：保持逐文件上传。
        for rel, data, _ in libs + files:
            native.request(host, "upload", {"relative": rel}, payload=data)
        spec = dict(rec, round=round_no, base=workline.round_base(rec, round_no), timeout=int(timeout), max_heavy=int(host["max_heavy"]),
                    auth_file=host["codex_auth_file"], env_file=host["env_file"],
                    rollout_stats=host.get("rollout_stats", "0"),
                    cache_env=cli.cache_env(host, rec.get("user_id")))
        native.request(host, "launch", {"spec": spec, "prompt": prompt_text})
        return
    if rec.get("profile"):
        files.append((f"jobs/{j}/profile.json", json.dumps(rec["profile"]).encode(), "600"))
    upload_profiles = (rec.get("profile") or rec.get("isolated")) and not rec.get("profile_script_uploaded")
    if upload_profiles:
        libs.append((f"jobs/{j}/rd_profiles.py", cli.read_text_file("rd_profiles.py"), "600"))
    files.append((f"jobs/{j}/cmd.r{round_no}.sh", (cmd_text + "\n").encode(), "644"))
    if prompt_text is not None:
        files.append((f"jobs/{j}/prompt.r{round_no}.md", prompt_text.encode(), "644"))
    files.append((f"jobs/{j}/run.r{round_no}.sh", cli.build_run_script(host, rec, round_no, timeout).encode(), "755"))
    libs.append(("bin/rd-heavy", cli.read_text_file("bin", "rd-heavy"), "755"))
    script = f'"$ROOT/jobs/{j}/run.r{round_no}.sh"'
    if host["launcher"] == "tmux":
        start = f"tmux new-session -d -s {shlex.quote('rd-' + j + '-r' + str(round_no))} sh {script}"
    elif host["launcher"] == "nohup":
        start = f"nohup sh {script} > /dev/null 2>&1 < /dev/null &"
    else:
        cli.die(f"[host:{host['name']}] launcher must be nohup or tmux")
    cli.upload_and_start(host, files, libs, f'rm -f "$ROOT/jobs/{j}/exit_code" "$ROOT/jobs/{j}/pid"; {start}')
    if upload_profiles:
        rec["profile_script_uploaded"] = True


def cache_env(host, uid):
    """返回 {环境变量名: 目录}。相对目录在用户根目录 cache/ 下；Windows 用反斜杠路径。"""
    items = parse_cache_dirs(host.get("cache_dirs", ""), host["workdir"])
    windows = host["platform"] == "windows"
    values = {}
    for name, path, absolute in items:
        path = path.replace("{user}", uid or "shared")
        if absolute:
            values[name] = path
        elif windows:
            values[name] = ntpath.join(host["workdir"], "cache", *path.replace("\\", "/").split("/"))
        else:
            values[name] = host["workdir"].rstrip("/") + "/cache/" + path
    return values


def cache_env_script(host, uid):
    """POSIX 运行脚本中的缓存环境变量与目录创建。"""
    lines = []
    for name, path, absolute in parse_cache_dirs(host.get("cache_dirs", ""), host["workdir"]):
        path = path.replace("{user}", uid or "shared")
        target = cli.worker_path(path) if absolute else '"$ROOT"/cache/' + shlex.quote(path)
        lines.append(f'export {name}={target}; mkdir -p "${name}" 2>/dev/null || true')
    return "\n".join(lines)


def worker_path(path):
    """目标机 shell 中的路径表达式；~/ 与 $HOME/ 在目标机展开。"""
    for prefix in ("~/", "$HOME/"):
        if path.startswith(prefix):
            return '"$HOME"/' + shlex.quote(path[len(prefix):])
    return shlex.quote(path)


def agent_preflight(host, uid, name, agent, auto_sync=True):
    """按代理登记的能力检查目标机，不满足即在推送任务分支前停止；返回 Codex 员工配置。

    auto_sync=False 时不改本机目标的配置副本；续跑换目标机必须沿用原派单版本。
    """
    spec = agents.SPECS[name]
    if host["platform"] not in spec["platforms"]:
        cli.die(f"{name} 派单目前只支持 {'、'.join(spec['platforms'])} 目标机。", 4)
    auth = host[spec["auth_key"]]
    if spec["auth_required"]:
        if not auth:
            cli.die(f"[host:{host['name']}] 缺 {spec['auth_key']}；必须给出员工凭据文件，不得回退宿主个人登录态。", 4)
        if not auth.startswith(("/", "~/", "$HOME/")):
            cli.die(f"[host:{host['name']}] {spec['auth_key']} 必须是目标机上的绝对路径或 ~/ 开头路径。", 4)
    profile = None
    if spec["isolation"] == "profile":
        local, payload = cli.local_profile(uid)
        profile = cli.remote_profile(host, uid)
        if cli.is_local(host) and auto_sync:
            profile = cli.prepare_local_profile(host, uid, local, payload, profile)
        else:
            cli.profile_report(host, uid, local, profile)
            if profile is None:
                cli.die("此 ID 尚无远端配置；先询问是否同步、确认 ID，再执行 rdispatch profile sync。未推送任务分支。", 4)
    if host["platform"] == "windows":
        if any(agent[k] != cli.AGENTS[name][k] for k in ("start", "resume")):
            cli.die("Windows 使用内置 Codex 参数适配；自定义 POSIX 启动模板不能直接移植。", 4)
        check = native.request(host, "doctor")
        ready = check["ok"] and check["features"]
    else:
        ready = cli.ssh(host, spec["check"], check=False).returncode == 0
    if not ready:
        cli.die(spec["check_error"], 4)
    if spec["auth_required"] and cli.ssh(host, "[ -f " + cli.worker_path(auth) + " ]", check=False).returncode != 0:
        hint = "先在目标机运行 agy 完成登录。" if name == "agy" else "先在目标机准备该凭据文件。"
        cli.die(f"[host:{host['name']}] 目标机上没有 {spec['auth_key']} 指向的文件；{hint}未推送任务分支。", 4)
    return profile
