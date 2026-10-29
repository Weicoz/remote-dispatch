"""Windows 原生 worker 与 SSH JSON/二进制传输；不要求 Git Bash 或 WSL。"""
import base64
import ctypes
import io
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import fnmatch
import tempfile
import time

if "profiles" not in globals():
    try:
        from . import rd_profiles as profiles
    except ImportError:  # standalone copy on the worker
        import rd_profiles as profiles
if "messages" not in globals():
    try:
        from . import rd_messages as messages
    except ImportError:
        import rd_messages as messages
if "health" not in globals():
    try:
        from . import rd_health as health
    except ImportError:
        import rd_health as health


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def request(host, operation, data=None, payload=None, output=None, timeout=60):
    """源代码和请求走 stdin，避免 Windows 命令行长度限制和 shell 插值。"""
    here = Path(__file__).parent
    message = dict(data or {}, operation=operation, workdir=host["workdir"],
                   shared_workdir=host.get("shared_workdir", host["workdir"]),
                   path=host["path"], worker_code=(here / "rd_native.py").read_text(encoding="utf-8"),
                   profile_code=(here / "rd_profiles.py").read_text(encoding="utf-8"),
                   health_code=(here / "rd_health.py").read_text(encoding="utf-8"),
                   messages_code=(here / "rd_messages.py").read_text(encoding="utf-8"))
    boot = "import json,sys,types; q=json.loads(sys.stdin.buffer.readline()); p=types.ModuleType('rd_profiles'); exec(q['profile_code'],p.__dict__); m=types.ModuleType('rd_messages'); exec(q['messages_code'],m.__dict__); h=types.ModuleType('rd_health'); exec(q['health_code'],h.__dict__); n={'__name__':'rd_remote','profiles':p,'messages':m,'health':h}; exec(q['worker_code'],n); n['serve'](q)"
    prefix = "$env:PATH=[Environment]::ExpandEnvironmentVariables(" + ps_quote(host["path"]) + ")+';'+$env:PATH; " if host["path"] else ""
    script = prefix + "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); & " + ps_quote(host["python"]) + " '-c' " + ps_quote(boot) + "; exit $LASTEXITCODE"
    if host.get("platform") == "posix":
        script = "python3 -c " + shlex.quote(boot)
        if host["path"]:
            script = 'export PATH="' + host["path"] + ':$PATH"; ' + script
        cmd = (["sh", "-c", script] if host["ssh"] == "local" else
               ["ssh", *shlex.split(host["ssh_opts"]), host["ssh"], "sh -c " + shlex.quote(script)])
    else:
        cmd = ["ssh", *shlex.split(host["ssh_opts"]), host["ssh"], " ".join(powershell(script))]
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
        incoming.write(json.dumps(message).encode() + b"\n")
        if payload is not None:
            message["payload_encoding"] = "base64"
            incoming.seek(0)
            incoming.truncate()
            incoming.write(json.dumps(message).encode() + b"\n")
            if isinstance(payload, bytes):
                payload = io.BytesIO(payload)
            while True:
                chunk = payload.read(49152)
                if not chunk:
                    break
                incoming.write(base64.b64encode(chunk) + b"\n")
        incoming.seek(0)
        result = subprocess.run(cmd, stdin=incoming, stdout=outgoing, stderr=subprocess.PIPE, timeout=timeout)
        outgoing.seek(0)
        try:
            header = json.loads(outgoing.readline())
        except (ValueError, UnicodeError):
            raise ValueError("Windows SSH/Python 响应无效；检查 OpenSSH、PowerShell、python 路径，不输出原始配置。") from None
        if result.returncode or not header.get("ok"):
            raise ValueError(header.get("error", "Windows worker 操作失败。"))
        if output is not None:
            if header.get("binary_encoding") != "base64":
                raise ValueError("Windows 二进制响应编码无效。")
            for line in outgoing:
                output.write(base64.b64decode(line.strip(), validate=True))
        return header["result"]


def checked_path(root, relative):
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("worker 请求含越界路径。")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError("worker 请求含越界路径。") from None
    return candidate


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(repo), *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise ValueError("worker Git 操作失败，退出码 " + str(result.returncode) + "；检查仓库、分支和访问权限。")
    return result


def alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x100000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            running = kernel.WaitForSingleObject(handle, 0) == 258
            kernel.CloseHandle(handle)
            return running
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def stop_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(int(pid), 15)


def win_pagefile_used():
    """页面文件已用量，格式与 POSIX 探针一致（如 "2048M"）；取不到返回 "?"，不当作 0。"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "(Get-CimInstance Win32_PageFileUsage | Measure-Object CurrentUsage -Sum).Sum"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return f"{int(float(out))}M" if out else "?"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "?"


def win_metrics(root, shared):
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [(k, ctypes.c_ulonglong) for k in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ValueError("Windows 内存探针失败；不猜测可用内存。")
    def cpu_times():
        idle, system, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise ValueError("Windows CPU 探针失败。")
        to_int = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
        return to_int(idle), to_int(system) + to_int(user)
    old_idle, old_total = cpu_times()
    time.sleep(0.2)
    idle, total = cpu_times()
    if total <= old_total:
        raise ValueError("Windows CPU 采样无效。")
    cpu = max(0.0, min(100.0, 100 * (1 - (idle - old_idle) / (total - old_total))))
    jobs = list(shared.glob("users/*/jobs/*")) + list(shared.glob("jobs/*"))
    running = sum(1 for j in jobs if (j / "pid").exists() and not (j / "exit_code").exists() and alive((j / "pid").read_text(encoding="utf-8")))
    heavy = sum(1 for p in shared.glob("locks/heavy.*/pid") if alive(p.read_text(encoding="utf-8")))
    disk = shutil.disk_usage(root if root.exists() else Path.home())
    return {"os": "Windows", "ncpu": str(os.cpu_count() or 1), "cpu_pct": str(round(cpu, 1)),
            "memfree": str(round(memory.available * 100 / memory.total, 1)), "swap": win_pagefile_used(),
            "disk": str(int(disk.free / 1024 ** 3)), "disk_total": str(int(disk.total / 1024 ** 3)),
            "running": str(running), "heavy": str(heavy)}


def extract(stream, destination):
    """只接收目录和普通文件；兼容 Python 3.11，不依赖新版 tar filter。"""
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for entry in archive:
            target = checked_path(destination, entry.name)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(entry.mode & 0o777)
            else:
                raise ValueError("上传包含软链接或设备文件；仅支持普通文件。")


def export_resume(root, job, destination):
    """Transfer Git history/index and worktree, excluding common credential paths."""
    wt = checked_path(root, "wt/" + messages.job_id(job))
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / "source.bundle"
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        tree = git(wt, "write-tree").stdout.strip()
        snapshot = git(wt, "-c", "user.name=Remote Dispatch", "-c", "user.email=noreply@local.invalid",
                       "commit-tree", tree, "-p", head, "-m", "Resume index transfer").stdout.strip()
        reference = "refs/rd-transfer/" + job
        git(wt, "update-ref", reference, snapshot, "")
        try:
            git(wt, "bundle", "create", bundle, reference)
        finally:
            git(wt, "update-ref", "-d", reference, snapshot)
        index = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
        with tarfile.open(destination, "w") as archive:
            archive.add(bundle, arcname="source.bundle")
            archive.add(index, arcname="index")
            for child in wt.iterdir():
                if child.name != ".git":
                    archive.add(child, arcname="workspace/" + child.name, filter=resume_member)
            directory = checked_path(root, "jobs/" + job)
            for name in ("setup.sh", "setup.ps1"):
                if (directory / name).is_file():
                    archive.add(directory / name, arcname=name)


def resume_member(member):
    """Apply exclusions recursively, including directories and symlink names."""
    patterns = (".env*", "*.pem", "*.key", "id_*", ".ssh", ".aws", ".gnupg")
    if any(fnmatch.fnmatchcase(part.lower(), pattern)
           for part in Path(member.name).parts[1:] for pattern in patterns):
        return None
    return member


def import_resume(root, q, incoming):
    job = messages.job_id(q["job"])
    bare, wt = checked_path(root, q["bare"]), checked_path(root, "wt/" + job)
    directory = checked_path(root, "jobs/" + job)
    if wt.exists() or directory.exists():
        raise ValueError("resume_target_conflict: 目标机已有同名派单；请先核对。")
    bare.parent.mkdir(parents=True, exist_ok=True)
    if not bare.exists():
        bare.mkdir()
        git(bare, "init", "--bare")
    reference = "refs/heads/" + q["branch"]
    git(bare, "check-ref-format", reference)
    owned = False
    try:
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary)
            extract(incoming, staged)
            git(bare, "fetch", staged / "source.bundle", "refs/rd-transfer/" + job)
            sha = git(bare, "rev-parse", "FETCH_HEAD^").stdout.strip()
            git(bare, "update-ref", reference, sha, "")
            owned = True
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(bare, "worktree", "add", wt, q["branch"])
            for child in wt.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            workspace = staged / "workspace"
            if workspace.exists():
                for child in workspace.iterdir():
                    if child.name == ".git":
                        raise ValueError("迁移工作树不得覆盖 Git 元数据。")
                    shutil.move(str(child), str(wt / child.name))
            index = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
            shutil.copyfile(staged / "index", index)
            directory.mkdir(parents=True)
            for name in ("setup.sh", "setup.ps1"):
                if (staged / name).exists():
                    shutil.copyfile(staged / name, directory / name)
        return dict(root=str(root))
    except (Exception, SystemExit):
        if owned:
            git(bare, "worktree", "remove", "--force", wt, check=False)
            git(bare, "update-ref", "-d", reference, check=False)
            if directory.exists():
                shutil.rmtree(directory)
        raise


def job_state(root, job):
    directory = checked_path(root, "jobs/" + job)
    read = lambda name: (directory / name).read_text(encoding="utf-8").strip() if (directory / name).exists() else ""
    state = "missing" if not directory.exists() else "done" if read("exit_code") else "running" if alive(read("pid")) else "starting" if not read("pid") else "dead"
    rounds = max((int(p.name.split(".")[1][1:]) for p in directory.glob("run.r*.json")), default=0)
    logs = directory / ("stdout.r" + str(rounds) + ".log")
    tail = []
    if logs.exists():
        with logs.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, 2) - 4096))
            tail = stream.read(4096).decode("utf-8", errors="replace").splitlines()[-1:]
    head, dirty, head_source = read("head"), read("dirty"), "recorded"
    wt = root / "wt" / job
    if state == "running" and wt.is_dir():
        current = git(wt, "rev-parse", "HEAD", check=False)
        if current.returncode == 0:
            head, head_source = current.stdout.strip(), "live"
            dirty = str(len(git(wt, "status", "--porcelain", check=False).stdout.splitlines()))
    return {"job": job, "state": state, "exit": read("exit_code"), "timed_out": "yes" if (directory / "timed_out").exists() else "",
            "push_rc": read("push_rc"), "head": head, "dirty": dirty, "round": str(rounds),
            "head_source": head_source, "observed_at": messages.utc_now(), "started_at": read("started.r" + str(rounds)),
            "setup": "done" if (directory / "setup.done").exists() else "pending" if (directory / "setup.ps1").exists() else "", "last": tail[0] if tail else "",
            "network_sample": health.log_sample(directory, rounds)}


def shell_command(script):
    if os.name == "nt":
        return powershell("$ErrorActionPreference='Stop'; $OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script + "; if($LASTEXITCODE){exit $LASTEXITCODE}")
    return ["sh", "-c", script]


def codex_command(rec, runtime, prompt):
    args = ["exec"]
    if rec.get("session"):
        args += ["resume", rec["session"]]
    else:
        args += ["-C", rec["wt"]]
    args += ["--json", "-o", rec["last"], "--disable", "hooks", "--disable", "plugins", "--disable", "memories", "--disable", "external_agent_memory_import",
             "-c", "sqlite_home=" + json.dumps(str(runtime)), "-c", "log_dir=" + json.dumps(str(runtime / "logs")), "-c", 'cli_auth_credentials_store="file"']
    if rec.get("model"):
        args += ["-m", rec["model"]]
    if rec.get("effort"):
        args += ["-c", "model_reasoning_effort=" + rec["effort"]]
    args += ["-"]
    if os.name != "nt":
        return ["codex", *args]
    script = "$p=Get-Content -LiteralPath " + ps_quote(prompt) + " -Raw -Encoding UTF8; $p | & 'codex' " + " ".join(map(ps_quote, args)) + "; exit $LASTEXITCODE"
    return powershell("$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script)


def heavy_slot(directory, maximum):
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        for number in range(1, int(maximum) + 1):
            slot = directory / ("heavy." + str(number))
            try:
                slot.mkdir()
            except FileExistsError:
                pid = slot / "pid"
                if pid.exists() and not alive(pid.read_text(encoding="utf-8")):
                    shutil.rmtree(slot)
                continue
            (slot / "pid").write_text(str(os.getpid()), encoding="utf-8")
            return slot
        time.sleep(15)


def run_job(spec_file):
    spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    directory, wt = Path(spec["directory"]), Path(spec["wt"])
    for name in ("exit_code", "timed_out"):
        (directory / name).unlink(missing_ok=True)
    (directory / "pid").write_text(str(os.getpid()), encoding="utf-8")
    (directory / ("started.r" + str(spec["round"]))).write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8")
    env = dict(os.environ)
    if spec.get("env_file"):
        values = json.loads(Path(os.path.expandvars(os.path.expanduser(spec["env_file"]))).read_text(encoding="utf-8"))
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise ValueError("Windows env_file 必须是字符串键值的 JSON 文件。")
        env.update(values)
    env.update(RD_USER_ID=spec["user_id"], RD_JOB=spec["job"], RD_WT=str(wt), RD_JOB_DIR=str(directory),
               RD_LOCK_DIR=spec["lock_dir"], RD_MAX_HEAVY=str(spec["max_heavy"]), RD_ROUND=str(spec["round"]),
               PATH=spec["bin"] + os.pathsep + env.get("PATH", ""))
    if spec.get("handoff_version"):
        env["RD_REPORTER"] = str(directory / "rd_messages.py")
    if str(spec.get("rollout_stats", "0")).lower() in ("1", "true", "yes"):
        os.environ["RD_ROLLOUT_STATS"] = "1"  # messages.finish 在本进程内读取
    os.chdir(wt)
    setup = directory / "setup.ps1"
    timing = directory / ("timing.r" + str(spec["round"]))
    if setup.exists() and not (directory / "setup.done").exists():
        began = time.monotonic()
        with (directory / "setup.log").open("wb") as output:
            result = subprocess.run(shell_command(setup.read_text(encoding="utf-8")), stdout=output, stderr=subprocess.STDOUT, env=env)
        timing.write_text("setup_seconds=%d\n" % (time.monotonic() - began), encoding="utf-8")
        if result.returncode:
            (directory / "exit_code").write_text("97", encoding="utf-8")
            return
        (directory / "setup.done").touch()
    else:
        timing.write_text("setup_seconds=0\nsetup_note=已跳过\n", encoding="utf-8")
    runtime = None
    if spec.get("profile"):
        runtime = profiles.prepare_runtime(directory, spec["profile"], spec.get("auth_file", ""))
        env = profiles.runtime_environment(runtime, env)
    prompt = directory / ("prompt.r" + str(spec["round"]) + ".md")
    command = codex_command(spec, runtime, prompt) if runtime else shell_command(spec["cmd"])
    slot = heavy_slot(Path(spec["lock_dir"]), spec["max_heavy"]) if spec.get("heavy") else None
    rc = 98
    began = time.monotonic()
    try:
        with (directory / ("stdout.r" + str(spec["round"]) + ".log")).open("wb") as output, (directory / ("stderr.r" + str(spec["round"]) + ".log")).open("wb") as error, prompt.open("rb") as incoming:
            child = subprocess.Popen(command, stdin=incoming if runtime else subprocess.DEVNULL, stdout=output, stderr=error,
                                     env=env, start_new_session=os.name != "nt")
            try:
                rc = child.wait(timeout=spec["timeout"])
            except subprocess.TimeoutExpired:
                (directory / "timed_out").touch()
                stop_tree(child.pid)
                child.wait()
                rc = 124
    finally:
        with timing.open("a", encoding="utf-8") as stream:
            stream.write("body_seconds=%d\n" % (time.monotonic() - began))
        if slot:
            shutil.rmtree(slot)
        (directory / "head").write_text(git(wt, "rev-parse", "HEAD", check=False).stdout.strip(), encoding="utf-8")
        (directory / "dirty").write_text(str(len(git(wt, "status", "--porcelain", check=False).stdout.splitlines())), encoding="utf-8")
        if spec["sync"] == "origin":
            pushed = git(wt, "push", "origin", "HEAD:refs/heads/" + spec["branch"], check=False)
            (directory / "push.log").write_text(pushed.stdout + pushed.stderr, encoding="utf-8")
            (directory / "push_rc").write_text(str(pushed.returncode), encoding="utf-8")
        if spec.get("handoff_version"):
            (directory / ("exit.r" + str(spec["round"]))).write_text(str(rc), encoding="utf-8")
            try:
                messages.finish(directory, spec["job"], spec["round"], rc, agent=spec.get("agent"))
            except (ValueError, OSError):
                (directory / ("report.r" + str(spec["round"]) + ".log")).write_text("结果回执生成失败；原执行退出码保留。", encoding="utf-8")
        (directory / "exit_code").write_text(str(rc), encoding="utf-8")


def handle(q, incoming):
    root = Path(os.path.expandvars(os.path.expanduser(q["workdir"]))).resolve()
    shared = Path(os.path.expandvars(os.path.expanduser(q["shared_workdir"]))).resolve()
    if q.get("path"):
        os.environ["PATH"] = os.path.expandvars(q["path"]) + os.pathsep + os.environ.get("PATH", "")
    op = q["operation"]
    if op == "resume-import":
        return import_resume(root, q, incoming)
    if op in ("resume-checkpoint", "resume-rollback"):
        directory = checked_path(root, "jobs/" + messages.job_id(q["job"]))
        names = ("pid", "exit_code", "timed_out", "head", "dirty", "push_rc")
        if op == "resume-checkpoint":
            return {name: base64.b64encode((directory / name).read_bytes()).decode()
                    for name in names if (directory / name).is_file()}
        saved = q["checkpoint"]
        pid = directory / "pid"
        if pid.exists() and base64.b64encode(pid.read_bytes()).decode() != saved.get("pid"):
            value = pid.read_text().strip()
            if value.isdigit() and int(value) > 0 and alive(value):
                stop_tree(int(value))
        token = "r" + str(messages.round_id(q["round"]))
        for folder in (directory, directory / "reports"):
            if folder.exists():
                for path in folder.iterdir():
                    if token in path.name.split(".") and not path.is_dir():
                        path.unlink()
        for name in names:
            path = directory / name
            if name in saved:
                path.write_bytes(base64.b64decode(saved[name], validate=True))
            else:
                path.unlink(missing_ok=True)
        return None
    if op == "probe":
        info = win_metrics(root, shared)
        if q.get("env_file"):
            values = json.loads(Path(os.path.expandvars(os.path.expanduser(q["env_file"]))).read_text(encoding="utf-8"))
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
                raise ValueError("Windows env_file 必须是字符串键值的 JSON 文件。")
            os.environ.update(values)
        info["network"] = health.network_probe(q["network_options"], shared, q.get("user_id", ""), q.get("agent", "codex"))
        return info
    if op == "doctor":
        tools = {k: shutil.which(k) for k in ("git", "codex", "powershell.exe")}
        flags = subprocess.run(shell_command("& 'codex' features list" if os.name == "nt" else "codex features list"), capture_output=True, text=True)
        sandbox_help = subprocess.run(shell_command("& 'codex' sandbox --help" if os.name == "nt" else "codex sandbox --help"), capture_output=True, text=True)
        names = {line.split()[0] for line in flags.stdout.splitlines() if line.split()}
        return {"tools": tools, "python": sys.version.split()[0], "features": set(("hooks", "plugins", "memories", "external_agent_memory_import")) <= names and "--permission-profile" in sandbox_help.stdout, "ok": all(tools.values()) and sys.version_info >= (3, 11)}
    if op == "controller-register":
        return profiles.register_controller(root, q["fingerprint"])
    if op == "profile-read":
        current = root / "profiles/current.json"
        return {"root": str(root), "manifest": json.loads(current.read_text(encoding="utf-8")) if current.exists() else None}
    if op == "profile-sync":
        directory = root / "profiles"
        directory.mkdir(parents=True, exist_ok=True)
        lock = directory / ".sync-lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError("另一次员工配置同步进行中。") from None
        try:
            current = directory / "current.json"
            actual = json.loads(current.read_text(encoding="utf-8")) if current.exists() else None
            if actual != q["expected"]:
                raise ValueError("远端配置已变化，请重新检查并确认。")
            manifest = profiles.validate_manifest(q["manifest"], q["user_id"])
            version = directory / manifest["fingerprint"]
            with tempfile.TemporaryDirectory(prefix=".incoming-", dir=directory) as temporary:
                staged = Path(temporary)
                extract(incoming, staged)
                if json.loads((staged / "manifest.json").read_text(encoding="utf-8")) != manifest:
                    raise ValueError("配置包指纹与已确认清单不一致。")
                if not version.exists():
                    staged.rename(version)
            pending = directory / "current.tmp"
            pending.write_text(json.dumps(manifest), encoding="utf-8")
            os.replace(pending, current)
            return manifest
        finally:
            lock.rmdir()
    if op == "upload":
        destination = checked_path(root, q["relative"])
        if q.get("archive"):
            destination.mkdir(parents=True, exist_ok=True)
            extract(incoming, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                shutil.copyfileobj(incoming, output)
        return None
    if op == "init":
        directory = checked_path(root, "jobs/" + q["job"])
        directory.mkdir(parents=True, exist_ok=True)
        bare = checked_path(root, q["bare"])
        bare.parent.mkdir(parents=True, exist_ok=True)
        if not bare.exists():
            bare.mkdir()
            git(bare, "init", "--bare")
            if q["sync"] == "origin":
                git(bare, "remote", "add", "origin", q["origin"])
        transfer = "refs/rd-transfer/" + q["job"]
        try:
            if q["sync"] == "origin":
                git(bare, "fetch", "origin", "refs/heads/" + q["branch"] + ":" + transfer)
            else:
                with tempfile.TemporaryDirectory(dir=root) as temporary:
                    bundle = Path(temporary) / "incoming.bundle"
                    with bundle.open("wb") as output:
                        shutil.copyfileobj(incoming, output)
                    git(bare, "fetch", bundle, q["transfer_ref"] + ":" + transfer)
            sha = git(bare, "rev-parse", transfer).stdout.strip()
            git(bare, "update-ref", "refs/heads/" + q["branch"], sha, "")
            (directory / "branch-created").touch()
        finally:
            git(bare, "update-ref", "-d", transfer, check=False)
        wt = checked_path(root, "wt/" + q["job"])
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(bare, "worktree", "add", wt, q["branch"])
        return {"root": str(root)}
    if op == "apply-wip":
        wt = checked_path(root, "wt/" + q["job"])
        result = subprocess.run(["git", "-C", str(wt), "apply", "--index", "--binary", "-"],
                                input=incoming.read(), capture_output=True)
        if result.returncode:
            raise ValueError("--wip 补丁应用失败：" + result.stderr.decode(errors="replace"))
        return None
    if op == "branch-owned":
        return checked_path(root, "jobs/" + q["job"] + "/branch-created").is_file()
    if op == "state":
        return {job: job_state(root, job) for job in q["jobs"]}
    if op == "report-read":
        number = messages.round_id(q["round"])
        if q["kind"] not in ("progress", "result"):
            raise ValueError("无效回执类型。")
        path = checked_path(root, "jobs/" + messages.job_id(q["job"]) + "/reports/" + q["kind"] + ".r" + str(number) + ".json")
        data = messages.read_json(path)
        if q["kind"] == "progress" and data is not None:
            data["history"] = messages.progress_rows(path.parent.parent, q["job"], number)
        return messages.validate_report(data, q["job"], number, q["kind"]) if data is not None else None
    if op in ("logs", "sessions"):
        directory = checked_path(root, "jobs/" + q["job"])
        if op == "sessions":
            if q.get("round") is not None:
                path = checked_path(directory, "stdout.r" + str(messages.round_id(q["round"])) + ".log")
                return path.read_bytes()[:200000].decode("utf-8", errors="replace") if path.exists() else ""
            return "".join(p.read_bytes()[:200000].decode("utf-8", errors="replace") for p in sorted(directory.glob("stdout.r*.log")))
        path = checked_path(directory, q["file"])
        return "\n".join(path.read_bytes()[-1024 * 1024:].decode("utf-8", errors="replace").splitlines()[-q["lines"]:]) if path.exists() else "(日志尚未生成)"
    if op == "clean-check":
        wt = checked_path(root, "wt/" + q["job"])
        if q.get("detailed"):
            return {"head": git(wt, "rev-parse", "HEAD").stdout.strip(),
                    "status": git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout}
        return {"dirty": len(git(wt, "status", "--porcelain", check=False).stdout.splitlines()), "head": git(wt, "rev-parse", "HEAD", check=False).stdout.strip()}
    if op == "clean":
        bare = checked_path(root, q["bare"])
        wt = checked_path(root, "wt/" + q["job"])
        if wt.exists():
            git(bare, "worktree", "remove", "--force", wt)
        if q.get("delete_branch") and git(bare, "show-ref", "--verify", "--quiet", "refs/heads/" + q["branch"], check=False).returncode == 0:
            git(bare, "branch", "-D", q["branch"])
        directory = checked_path(root, "jobs/" + q["job"])
        if directory.exists():
            shutil.rmtree(directory)
        return None
    if op == "stop":
        directory = checked_path(root, "jobs/" + q["job"])
        pid = directory / "pid"
        if pid.exists() and alive(pid.read_text(encoding="utf-8")):
            stop_tree(int(pid.read_text(encoding="utf-8")))
            (directory / "exit_code").write_text("143", encoding="utf-8")
        return None
    if op == "launch":
        spec = q["spec"]
        directory = checked_path(root, "jobs/" + spec["job"])
        directory.mkdir(parents=True, exist_ok=True)
        spec.update(directory=str(directory), wt=str(checked_path(root, "wt/" + spec["job"])), lock_dir=str(shared / "locks"), bin=str(root / "bin"))
        spec["last"] = str(directory / ("last.r" + str(spec["round"]) + ".txt"))
        if not (directory / "rd_native.py").exists():
            (directory / "rd_profiles.py").write_text(q["profile_code"], encoding="utf-8")
            (directory / "rd_native.py").write_text(q["worker_code"], encoding="utf-8")
            if spec.get("profile"):
                (directory / "profile.json").write_text(json.dumps(spec["profile"]), encoding="utf-8")
        message_code = q.get("messages_code") or Path(messages.__file__).read_text(encoding="utf-8")
        health_code = q.get("health_code") or Path(health.__file__).read_text(encoding="utf-8")
        (directory / "rd_messages.py").write_text(message_code, encoding="utf-8")
        (directory / "rd_health.py").write_text(health_code, encoding="utf-8")
        (directory / ("prompt.r" + str(spec["round"]) + ".md")).write_text(q["prompt"] or "", encoding="utf-8")
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "rd_native.py").write_text(q["worker_code"], encoding="utf-8")
        (bin_dir / "rd_profiles.py").write_text(q["profile_code"], encoding="utf-8")
        (bin_dir / "rd_messages.py").write_text(message_code, encoding="utf-8")
        (bin_dir / "rd_health.py").write_text(health_code, encoding="utf-8")
        (bin_dir / "rd-heavy.cmd").write_text('@"' + sys.executable + '" "' + str(bin_dir / "rd_native.py") + '" --heavy %*\r\n', encoding="utf-8")
        spec_file = directory / ("run.r" + str(spec["round"]) + ".json")
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        for name in ("pid", "exit_code"):
            (directory / name).unlink(missing_ok=True)
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB if os.name == "nt" else 0
        with (directory / "bootstrap.log").open("ab") as log:
            try:
                child = subprocess.Popen([sys.executable, str(directory / "rd_native.py"), "--run", str(spec_file)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, creationflags=flags, start_new_session=os.name != "nt")
            except OSError:
                (directory / "exit_code").write_text("98", encoding="utf-8")
                raise ValueError("Windows 不能脱离 SSH Job Object 启动任务；检查 OpenSSH 的后台进程策略。未回退到随 SSH 断开的进程。") from None
        return {"pid": child.pid}
    raise ValueError("未知 worker 操作。")


def serve(q):
    try:
        if q["operation"] in ("fetch-bundle", "reports-bundle", "resume-export"):
            root = Path(os.path.expandvars(os.path.expanduser(q["workdir"]))).resolve()
            with tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "result.bundle"
                if q["operation"] == "fetch-bundle":
                    git(checked_path(root, q["bare"]), "bundle", "create", bundle, "refs/heads/" + q["branch"])
                elif q["operation"] == "resume-export":
                    export_resume(root, q["job"], bundle)
                else:
                    reports = checked_path(root, "jobs/" + messages.job_id(q["job"]) + "/reports")
                    if reports.is_symlink() or (reports.exists() and not reports.is_dir()):
                        raise ValueError("reports 必须是普通目录。")
                    with tarfile.open(bundle, "w") as archive:
                        if reports.exists():
                            archive.add(reports, arcname=".")
                sys.stdout.buffer.write(json.dumps({"ok": True, "result": None, "binary_encoding": "base64"}).encode() + b"\n")
                with bundle.open("rb") as source:
                    while True:
                        chunk = source.read(49152)
                        if not chunk:
                            break
                        sys.stdout.buffer.write(base64.b64encode(chunk) + b"\n")
            return
        if q.get("payload_encoding") == "base64":
            root = Path(os.path.expandvars(os.path.expanduser(q["workdir"]))).resolve()
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=root) as incoming:
                for line in sys.stdin.buffer:
                    incoming.write(base64.b64decode(line.strip(), validate=True))
                incoming.seek(0)
                value = handle(q, incoming)
        else:
            value = handle(q, sys.stdin.buffer)
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=True))
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else "Windows worker 操作失败；检查环境、路径和权限。"
        print(json.dumps({"ok": False, "error": message}))
        sys.exit(1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--heavy"]:
        slot = heavy_slot(Path(os.environ["RD_LOCK_DIR"]), os.environ.get("RD_MAX_HEAVY", "1"))
        try:
            command = "& " + " ".join(map(ps_quote, sys.argv[2:])) if os.name == "nt" else " ".join(sys.argv[2:])
            sys.exit(subprocess.call(shell_command(command)))
        finally:
            shutil.rmtree(slot)
    elif sys.argv[1:2] == ["--run"]:
        try:
            run_job(sys.argv[2])
        except Exception:
            spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            (Path(spec["directory"]) / "exit_code").write_text("98", encoding="utf-8")
            print("Windows 员工配置或认证准备失败，检查配置、环境文件和 bootstrap.log。", file=sys.stderr)
