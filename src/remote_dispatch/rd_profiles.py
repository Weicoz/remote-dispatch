"""Remote Dispatch 的员工配置包；仅用标准库，配置操作要求 Python 3.11+。"""
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

try:
    import tomllib
except ImportError:
    tomllib = None

WEEK = 7 * 24 * 60 * 60
MAX_BYTES = 32 * 1024 * 1024
TOP_FILES = {"config.toml", "AGENTS.md", "git.json"}
TOP_DIRS = {"skills", "agents", "rules", "tools"}
CONFIG_KEYS = {
    "model", "model_provider", "model_providers", "model_reasoning_effort",
    "model_reasoning_summary", "model_verbosity", "service_tier", "web_search",
    "approval_policy", "sandbox_mode", "sandbox_workspace_write", "agents",
    "mcp_servers", "features", "project_doc_max_bytes", "project_doc_fallback_filenames",
    "tool_output_token_limit",
}


def portable_name(value):
    stem = value.split(".", 1)[0].lower()
    return bool(value) and not re.search(r'[<>:"\\|?*\x00-\x1f]', value) and not value.endswith((".", " ")) and stem not in {"con", "prn", "aux", "nul", *{"com" + str(n) for n in range(1, 10)}, *{"lpt" + str(n) for n in range(1, 10)}}


def first_json(path, create):
    """原子发布首次配置；并发创建时沿用先落盘的完整记录。"""
    path = Path(path)
    try:
        if not path.exists():
            value = create()
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                pending = Path(stream.name)
                json.dump(value, stream)
            try:
                try:
                    os.link(pending, path)
                except FileExistsError:
                    pass
            finally:
                pending.unlink()
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("用户身份配置无法读取或保存；请检查配置文件及目录权限。") from None


def user_id(value=None, config_dir=None):
    if value is None:
        value = os.environ.get("RD_USER_ID", "")
        if not value:
            directory = Path(config_dir) if config_dir is not None else Path.home() / ".config/remote-dispatch"
            def default():
                try:
                    username = getpass.getuser()
                except (OSError, KeyError):
                    raise ValueError("无法取得电脑用户名；请手动设置 RD_USER_ID。") from None
                normalized = re.sub(r"[^a-z0-9_-]", "-", username.lower()).strip("-")[:64]
                return {"user_id": user_id(normalized)}
            saved = first_json(directory / "default-user.json", default)
            value = saved.get("user_id") if isinstance(saved, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) or not portable_name(value):
        raise ValueError("用户 ID 无效；请手动设置 RD_USER_ID：1–64 位小写英文、数字、下划线或连字符，以英文或数字开头，不能使用 Windows 保留名。")
    return value


def controller_fingerprint(config_dir):
    saved = first_json(Path(config_dir) / "controller.json",
                       lambda: {"fingerprint": hashlib.sha256(os.urandom(32)).hexdigest()})
    fingerprint = saved.get("fingerprint") if isinstance(saved, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("控制端指纹配置无效；请检查 controller.json。")
    return fingerprint


def register_controller(root, fingerprint):
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("控制端指纹无效。")
    saved = first_json(Path(root) / "controller.json", lambda: {"fingerprint": fingerprint})
    original = saved.get("fingerprint") if isinstance(saved, dict) else None
    if not isinstance(original, str) or not re.fullmatch(r"[a-f0-9]{64}", original):
        raise ValueError("目标机控制端指纹记录无效；请检查用户目录下的 controller.json。")
    return original != fingerprint


def validate_config(text, files):
    if tomllib is None:
        raise ValueError("员工配置操作要求 Python 3.11+（tomllib）。")
    try:
        config = tomllib.loads(text)
    except Exception:
        raise ValueError("config.toml 格式错误；为避免泄露配置内容，不输出解析器原文。") from None
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ValueError("配置包不支持这些顶层字段：" + ", ".join(sorted(unknown)))
    if config.get("approval_policy") != "never":
        raise ValueError('后台执行要求 config.toml 明确设置 approval_policy = "never"。')

    def inspect(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in {"api_key", "token", "access_token", "refresh_token", "password", "bearer_token", "env", "http_headers"}:
                    raise ValueError("配置含凭据或内联环境字段；改用远端环境变量引用，不同步凭据。")
                inspect(v, k)
        elif isinstance(node, list):
            for v in node:
                inspect(v, key)
        elif isinstance(node, str):
            if node.startswith(("/", "~/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", node):
                raise ValueError("配置包含本机绝对路径；包内资源使用 @RD_PROFILE@，目标机命令使用 PATH。")
            if key == "config_file" and node not in files:
                raise ValueError("代理 config_file 必须指向配置包内的相对路径。")
    inspect(config)
    for name, content in files.items():
        if name.startswith("agents/") and name.endswith(".toml"):
            try:
                inspect(tomllib.loads(content.decode()))
            except (UnicodeError, tomllib.TOMLDecodeError):
                raise ValueError("代理配置 TOML 格式错误。") from None
    return config


def bundle(source, uid, state):
    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("本地配置包目录不存在或是软链接；请先准备独立、可移植的配置包。")
    files, modes, latest = {}, {}, 0.0
    total = 0
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source).as_posix()
        if not all(portable_name(part) for part in path.relative_to(source).parts):
            raise ValueError("配置文件名不能跨平台使用；避免 Windows 保留名、控制字符和特殊字符。")
        top = rel.split("/", 1)[0]
        if path.is_symlink():
            raise ValueError("配置包含软链接；先将选定资源复制进包，避免引用本机私有路径。")
        if path.is_dir():
            if top not in TOP_DIRS:
                raise ValueError("配置包包含未允许的目录；不得整份同步个人 HOME 或 .codex。")
            continue
        if not path.is_file() or (rel not in TOP_FILES and top not in TOP_DIRS):
            raise ValueError("配置包包含未允许的文件；只接收 config.toml、AGENTS.md、git.json 和资源目录。")
        if path.name.startswith(".") or path.suffix in {".key", ".pem", ".p12", ".pyc"} or path.name in {"auth.json", "credentials.json", "history.jsonl"}:
            raise ValueError("配置包含凭据、隐藏文件或运行状态；移出后再检查。")
        if path.stat().st_size > MAX_BYTES - total:
            raise ValueError("配置包超过 32 MiB；只保留本任务需要的资源。")
        with path.open("rb") as stream:
            files[rel] = stream.read(MAX_BYTES - total + 1)
        total += len(files[rel])
        if total > MAX_BYTES:
            raise ValueError("配置包超过 32 MiB；只保留本任务需要的资源。")
        modes[rel] = 0o755 if path.stat().st_mode & 0o111 else 0o644
        latest = max(latest, path.stat().st_mtime)
    if not files.get("AGENTS.md", b"").strip() or "config.toml" not in files:
        raise ValueError("配置包必须包含 config.toml 和非空 AGENTS.md（公司规则＋员工规则）。")
    config = validate_config(files["config.toml"].decode(), files)
    if latest > time.time() + 300:
        raise ValueError("本地配置文件时间领先当前时钟；先核对时钟。")
    if "git.json" in files:
        identity = json.loads(files["git.json"])
        if set(identity) != {"name", "email"} or not all(isinstance(v, str) and v.strip() and "\n" not in v for v in identity.values()):
            raise ValueError("git.json 必须只含非空 name、email。")
    entries = {name: {"sha256": hashlib.sha256(data).hexdigest(), "mode": modes[name]} for name, data in files.items()}
    fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    previous_path = Path(state) / "profiles" / uid / "source.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else None
    # shortcut: 首次用文件最新 mtime，后续用内容变化发现时间；需要跨机精确编辑时间时引入版本发布记录。
    updated = previous["source_updated_at"] if previous and previous["fingerprint"] == fingerprint else (time.time() if previous else latest)
    manifest = {"user_id": uid, "fingerprint": fingerprint, "source_updated_at": updated, "files": entries}
    # 回执模型兜底读此快照，不在读取回执时访问目标机；配置未写 model 则不记。
    if isinstance(config.get("model"), str) and config["model"].strip():
        manifest["model"] = config["model"].strip()
    previous_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=previous_path.parent, delete=False) as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False))
        pending = stream.name
    os.replace(pending, previous_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in dict(files, **{"manifest.json": json.dumps(manifest).encode()}).items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), modes.get(name, 0o600)
            archive.addfile(info, io.BytesIO(data))
    return manifest, buffer.getvalue()


def validate_manifest(manifest, uid):
    if not isinstance(manifest, dict) or manifest.get("user_id") != uid:
        raise ValueError("远端配置的员工 ID 不匹配。")
    if not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("fingerprint", ""))):
        raise ValueError("远端配置指纹无效。")
    stamp = manifest.get("source_updated_at")
    if not isinstance(stamp, (int, float)) or not 0 < stamp <= time.time() + 300:
        raise ValueError("远端配置时间无效或时钟领先；先核对时钟。")
    if not isinstance(manifest.get("files"), dict):
        raise ValueError("远端配置清单无效。")
    for name, entry in manifest["files"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or (name not in TOP_FILES and path.parts[0] not in TOP_DIRS):
            raise ValueError("远端配置清单含越界路径。")
        if not isinstance(entry, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", ""))) or entry.get("mode") not in {0o644, 0o755}:
            raise ValueError("远端配置文件指纹或权限无效。")
    return manifest


def comparison(local, remote):
    if remote is None:
        return "missing"
    if local is None:
        return "unknown"
    if local["fingerprint"] == remote["fingerprint"]:
        return "same"
    delta = local["source_updated_at"] - remote["source_updated_at"]
    return "stale" if delta > WEEK else "remote-newer" if delta < 0 else "different"


def host_skill_paths():
    roots = [Path.home() / ".agents/skills", Path.home() / ".codex/skills", Path("/etc/codex/skills")]
    paths, visited = set(), set()
    for root in roots:
        for base, dirs, names in os.walk(root, followlinks=True):
            real = os.path.realpath(base)
            if real in visited:
                dirs[:] = []
                continue
            visited.add(real)
            if "SKILL.md" in names:
                paths.add(str(Path(base) / "SKILL.md"))
                paths.add(str(Path(real) / "SKILL.md"))
                dirs[:] = []
    return sorted(paths)


def toml_value(value):
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k, ensure_ascii=False) + " = " + toml_value(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise ValueError("员工配置包含无法保存的 TOML 类型。")


def allow_task_git(runtime, job_dir):
    """workspace-write 保留文件保护，开放任务 Git 元数据、重任务队列和默认网络。"""
    path = runtime / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config.get("sandbox_mode") != "workspace-write":
        return
    root = job_dir.parent.parent
    if root.parent.name != "users":
        raise ValueError("任务目录不在员工 users 命名空间；停止授权共享队列。")
    wt = root / "wt" / job_dir.name
    if not (wt / ".git").exists():
        return
    def git_path(flag):
        result = subprocess.run(["git", "-C", str(wt), "rev-parse", flag], capture_output=True, text=True, encoding="utf-8")
        if result.returncode or not result.stdout.strip():
            raise ValueError("无法定位任务 Git 元数据目录。")
        return (wt / result.stdout.strip()).resolve()
    directory, common = git_path("--absolute-git-dir"), git_path("--git-common-dir")
    if not all(p.is_relative_to((root / "repos").resolve()) for p in (directory, common)):
        raise ValueError("任务 Git 元数据不在当前员工 repos 内；停止启动。")
    previous = config.pop("sandbox_workspace_write", {})
    filesystem = {str(common): "write", str(directory): "write"}
    filesystem[str((root.parent.parent / "locks").resolve())] = "write"
    reports = Path(job_dir) / "reports"
    if reports.is_symlink():
        raise ValueError("本任务报告目录不得是符号链接。")
    if reports.is_dir():
        filesystem[str(reports.resolve())] = "write"
    for extra in previous.get("writable_roots", []):
        filesystem[extra] = "write"
    if previous.get("exclude_tmpdir_env_var"):
        filesystem[":tmpdir"] = "read"
    if previous.get("exclude_slash_tmp"):
        filesystem[":slash_tmp"] = "read"
    config.pop("sandbox_mode")
    config["default_permissions"] = "rd_workspace"
    config["permissions"] = {"rd_workspace": {"extends": ":workspace", "filesystem": filesystem,
                                                "network": {"enabled": True}}}
    path.write_text("\n".join(json.dumps(k) + " = " + toml_value(v) for k, v in config.items()) + "\n", encoding="utf-8")


def copy_credential(source, target):
    shutil.copyfile(source, target)
    target.chmod(0o600)


def prepare_runtime(job_dir, expected, auth_file="", refresh=False):
    """首轮建立任务配置并复制认证文件；续跑沿用，只有 refresh=True 才重新复制认证文件。"""
    job_dir = Path(job_dir).resolve()
    runtime = job_dir / "codex"
    marker = runtime / "rd-profile.json"
    if marker.exists():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if saved["fingerprint"] != expected["fingerprint"] or saved["user_id"] != expected["user_id"]:
            raise ValueError("续跑配置版本不匹配；不得切换原任务配置。")
        if not (runtime / "config.toml").is_file():
            raise ValueError("原任务配置已丢失；不得回退系统账号配置。")
        if refresh:
            auth = Path(os.path.expandvars(os.path.expanduser(auth_file))) if auth_file else None
            if auth is None or not auth.is_file():
                raise ValueError("--refresh-auth 需要目标机认证文件；指定的文件不存在，不得回退默认账号凭据。")
            copy_credential(auth, runtime / "auth.json")
        allow_task_git(runtime, job_dir)
        return runtime
    if runtime.exists():
        raise ValueError("任务配置初始化不完整；检查后重派，不覆盖现有目录。")
    source = job_dir.parent.parent / "profiles" / expected["fingerprint"]
    actual = validate_manifest(json.loads((source / "manifest.json").read_text(encoding="utf-8")), expected["user_id"])
    if actual["fingerprint"] != expected["fingerprint"]:
        raise ValueError("配置版本不匹配。")
    for name, entry in actual["files"].items():
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("远端配置内容与指纹不符；停止启动。")
    text = (source / "config.toml").read_text(encoding="utf-8")
    validate_config(text, {name: (source / name).read_bytes() for name in actual["files"]})
    auth = Path(os.path.expandvars(os.path.expanduser(auth_file))) if auth_file else None
    if auth is not None and not auth.is_file():
        raise ValueError("指定的远端认证文件不存在；不得回退默认账号凭据。")
    runtime.mkdir(mode=0o700)
    for name in actual["files"]:
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
        target.chmod(actual["files"][name]["mode"])
    runtime.chmod(0o700)
    # TOML 字符串里的占位符映射到当前任务目录，不沿用派单端绝对路径。
    text = text.replace("@RD_PROFILE@", json.dumps(str(runtime))[1:-1])
    for path in (runtime / "agents").rglob("*.toml"):
        path.write_text(path.read_text(encoding="utf-8").replace("@RD_PROFILE@", json.dumps(str(runtime))[1:-1]), encoding="utf-8")
    prefix = 'cli_auth_credentials_store = "file"\nlog_dir = ' + json.dumps(str(runtime / "logs")) + '\nsqlite_home = ' + json.dumps(str(runtime)) + '\n'
    text = prefix + text
    # 当前 Codex 按 SKILL.md 路径匹配禁用项，目录路径不足以关闭宿主个人 skills。
    for path in host_skill_paths():
        text += '\n[[skills.config]]\npath = ' + json.dumps(path) + '\nenabled = false\n'
    (runtime / "config.toml").write_text(text, encoding="utf-8")
    index = []
    for skill in sorted((runtime / "skills").glob("*/SKILL.md")):
        index.append("- " + skill.parent.name + ": " + str(skill))
    if index:
        with (runtime / "AGENTS.md").open("a", encoding="utf-8") as stream:
            stream.write("\n## 本任务员工 skills\n根据任务选择以下技能，使用前读取对应 SKILL.md；技能资源以该文件目录为基准。\n" + "\n".join(index) + "\n")
    if auth is not None:
        shutil.copyfile(auth, runtime / "auth.json")
        (runtime / "auth.json").chmod(0o600)
    allow_task_git(runtime, job_dir)
    marker.write_text(json.dumps(expected), encoding="utf-8")
    return runtime


# 预检用目标机环境文件里的代理访问模型；代理进程必须拿到同一组网络变量，否则出现“预检通过、模型连不上”。
NETWORK_KEEP = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")


def runtime_environment(runtime, inherited=None):
    config = tomllib.loads((runtime / "config.toml").read_text(encoding="utf-8"))
    keep = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "OPENAI_API_KEY", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "COMSPEC", "ComSpec", "PATHEXT", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERNAME", "USERDOMAIN", *NETWORK_KEEP}
    for provider in config.get("model_providers", {}).values():
        if provider.get("env_key"):
            keep.add(provider["env_key"])
    for server in config.get("mcp_servers", {}).values():
        keep.update(server.get("env_vars", []))
        if server.get("bearer_token_env_var"):
            keep.add(server["bearer_token_env_var"])
    allowed = {k.upper() for k in keep} if os.name == "nt" else keep
    env = {k: v for k, v in (os.environ if inherited is None else inherited).items() if (k.upper() if os.name == "nt" else k) in allowed or k.upper().startswith("RD_")}
    env["CODEX_HOME"] = str(runtime)
    env["RD_CODEX_SQLITE"] = "sqlite_home=" + json.dumps(str(runtime))
    env["RD_CODEX_LOG"] = "log_dir=" + json.dumps(str(runtime / "logs"))
    identity = runtime / "git.json"
    if identity.exists():
        author = json.loads(identity.read_text(encoding="utf-8"))
        for role in ("AUTHOR", "COMMITTER"):
            env["GIT_" + role + "_NAME"] = author["name"]
            env["GIT_" + role + "_EMAIL"] = author["email"]
    return env


DSH_KEEP = ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TERM",
            "USERPROFILE", "SystemRoot", "SYSTEMROOT", "COMSPEC", "ComSpec", "PATHEXT",
            "APPDATA", "LOCALAPPDATA", "USERNAME", "USERDOMAIN", *NETWORK_KEEP)
DSH_ENV_PREFIXES = ("RD_", "DSH_", "DEEPSEEK_", "OPENAI_", "ANTHROPIC_")
DSH_AGENTS_PLACEHOLDER = """# 本任务员工规范

由 Remote Dispatch 建立的隔离工作区；宿主个人 ~/.dsh 配置、skills、MCP 与会话不得继承。

- 只完成本轮指令范围内的工作；结果回传后等待发起方新指令。
- 密码、私钥、令牌和敏感正文不写回执或日志。
"""


def prepare_dsh_runtime(job_dir, auth_file="", refresh=False):
    """首轮复制凭据；续跑沿用任务内副本，只有 refresh=True 才重新复制。"""
    job_dir = Path(job_dir).resolve()
    home = job_dir / "dsh-home"
    marker = home / "rd-isolation.json"
    if home.exists() and not marker.exists():
        raise ValueError("任务 DSH 配置目录存在但缺少隔离标记；检查后重派，不覆盖现有目录。")
    first = not marker.exists()
    auth = Path(os.path.expandvars(os.path.expanduser(auth_file))) if auth_file and (first or refresh) else None
    if auth is not None and not auth.is_file():
        raise ValueError("指定的 DSH 认证文件不存在；不得回退宿主个人登录态。")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    if auth is not None:
        copy_credential(auth, home / ".credentials.yaml")
    agents = home / "AGENTS.md"
    if not agents.exists():
        agents.write_text(DSH_AGENTS_PLACEHOLDER, encoding="utf-8")
    marker.write_text(json.dumps({"agent": "dsh", "home": str(home)}), encoding="utf-8")
    return home


def dsh_environment(home, inherited=None):
    source = os.environ if inherited is None else inherited
    env = {k: v for k, v in source.items()
           if k in DSH_KEEP or k.upper().startswith(DSH_ENV_PREFIXES)}
    env["DSH_HOME"] = str(home)
    identity = home.parent / "dsh-git.json"
    if identity.exists():
        author = json.loads(identity.read_text(encoding="utf-8"))
        for role in ("AUTHOR", "COMMITTER"):
            env["GIT_" + role + "_NAME"] = author["name"]
            env["GIT_" + role + "_EMAIL"] = author["email"]
    return env


AGY_KEEP = ("PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME", *NETWORK_KEEP)
AGY_TOKEN = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"


def prepare_agy_runtime(job_dir, auth_file="", refresh=False):
    """AGY 无配置目录变量：给任务独立 HOME，只放目标机自己的登录凭据，隔离个人 MCP、技能、插件与会话。
    首轮复制凭据；续跑沿用任务内副本，只有 refresh=True 才重新复制；副本缺失时报错，不自动补复制。"""
    job_dir = Path(job_dir).resolve()
    home = job_dir / "agy-home"
    marker = home / "rd-isolation.json"
    if home.exists() and not marker.exists():
        raise ValueError("任务 AGY 配置目录存在但缺少隔离标记；检查后重派，不覆盖现有目录。")
    token = home / AGY_TOKEN
    if marker.exists() and not refresh:
        if token.is_file():
            return home
        raise ValueError("任务内 AGY 登录凭据缺失；不会自动重新复制。确认后运行 rdispatch resume <job> --refresh-auth。")
    auth = Path(os.path.expandvars(os.path.expanduser(auth_file))) if auth_file else None
    if auth is None or not auth.is_file():
        raise ValueError("目标机没有 AGY 登录凭据；先在目标机运行 agy 登录，不得回退其他账号。")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    token.parent.mkdir(parents=True, exist_ok=True)
    copy_credential(auth, token)
    marker.write_text(json.dumps({"agent": "agy", "home": str(home)}), encoding="utf-8")
    return home


def agy_environment(home, inherited=None):
    source = os.environ if inherited is None else inherited
    env = {k: v for k, v in source.items() if k in AGY_KEEP or k.startswith("RD_")}
    env["HOME"] = str(home)
    # HOME 换成任务目录后仍沿用目标机账号的 git 全局配置（身份、凭据助手）。
    real_home = Path(source.get("HOME") or os.path.expanduser("~"))
    for candidate in (real_home / ".gitconfig", real_home / ".config" / "git" / "config"):
        if candidate.is_file():
            env["GIT_CONFIG_GLOBAL"] = str(candidate)
            break
    identity = home.parent / "agy-git.json"
    if identity.exists():
        author = json.loads(identity.read_text(encoding="utf-8"))
        for role in ("AUTHOR", "COMMITTER"):
            env["GIT_" + role + "_NAME"] = author["name"]
            env["GIT_" + role + "_EMAIL"] = author["email"]
    return env


def execute(job_dir, command, auth_file="", agent="codex", refresh=""):
    refresh = refresh == "refresh"
    if agent == "agy":
        home = prepare_agy_runtime(job_dir, auth_file, refresh)
        print("Remote Dispatch: AGY isolated home=" + str(home), flush=True)
        os.execvpe("sh", ["sh", command], agy_environment(home))
    if agent == "dsh":
        home = prepare_dsh_runtime(job_dir, auth_file, refresh)
        print("Remote Dispatch: DSH isolated home=" + str(home), flush=True)
        os.execvpe("sh", ["sh", command], dsh_environment(home))
    job_dir = Path(job_dir)
    expected = json.loads((job_dir / "profile.json").read_text(encoding="utf-8"))
    runtime = prepare_runtime(job_dir, expected, auth_file, refresh)
    env = runtime_environment(runtime)
    print("Remote Dispatch: ID=" + expected["user_id"] + " config=" + str(runtime) + " version=" + expected["fingerprint"][:12], flush=True)
    os.execvpe("sh", ["sh", command], env)


if __name__ == "__main__":
    try:
        execute(*sys.argv[1:])
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else "员工配置初始化失败；检查配置目录、Python 版本和认证文件。"
        print("Remote Dispatch: " + message, file=sys.stderr)
        sys.exit(98)
