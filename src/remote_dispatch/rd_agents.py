"""代理登记：启动模板、可派平台、配置隔离方式、目标机预检与参数构建。

rd 只派 SPECS 中登记且完成员工配置隔离的代理；AGENTS 里其余模板仅作保留，派单前拒绝。
"""
import shlex

SESSION_RE = r'"(?:session_id|sessionId|thread_id)":\s*"([^"]+)"'

# AGY 不读 stdin 文本：把提示词编码成一条 stream-json 用户消息，避开命令行长度上限。
AGY_PROMPT = ("python3 -c 'import json,sys; print(json.dumps({\"event\": \"user\", \"message\": "
              "{\"role\": \"user\", \"content\": sys.stdin.read()}}, ensure_ascii=False))' < \"$RD_PROMPT\"")

# Templates run on the worker inside the job's worktree. Available variables:
# $RD_WT (worktree), $RD_JOB_DIR (logs), $RD_PROMPT (prompt file), $RD_ROUND.
# {model_args} and {session} are filled in by rd.
AGENTS = {
    "codex": {
        "start": 'codex exec -C "$RD_WT" --json {model_args} '
        '-o "$RD_JOB_DIR/last.r$RD_ROUND.txt" - < "$RD_PROMPT"',
        "resume": 'codex exec resume {session} --json {model_args} -o "$RD_JOB_DIR/last.r$RD_ROUND.txt" - < "$RD_PROMPT"',
        "model": "-m {model}",
        "effort": "-c model_reasoning_effort={effort}",
    },
    "dsh": {
        "start": 'dsh headless --json {model_args} - < "$RD_PROMPT"',
        "resume": 'dsh headless --json --session-id {session} {model_args} - < "$RD_PROMPT"',
    },
    "agy": {
        "start": AGY_PROMPT + ' | agy {model_args} --input-format stream-json --output-format stream-json --print=""',
        "resume": AGY_PROMPT + ' | agy --conversation {session} {model_args} '
        '--input-format stream-json --output-format stream-json --print=""',
        "model": "--model {model}",
        "effort": "--effort {effort}",
        "session_regex": r'"conversation_id":\s*"([^"]+)"',
        "bin": "agy",
    },
    "kimi": {
        "start": 'kimi --auto {model_args} --output-format stream-json -p "$(cat "$RD_PROMPT")"',
        "resume": 'kimi --auto -S {session} {model_args} --output-format stream-json -p "$(cat "$RD_PROMPT")"',
        "model": "-m {model}",
    },
    "claude": {
        "start": "claude -p --output-format stream-json --verbose --dangerously-skip-permissions "
        '{model_args} < "$RD_PROMPT"',
        "resume": "claude -p --resume {session} --output-format stream-json --verbose "
        '--dangerously-skip-permissions {model_args} < "$RD_PROMPT"',
        "model": "--model {model}",
    },
}

DSH_CHECK = 'command -v dsh >/dev/null 2>&1 && dsh headless --help >/dev/null 2>&1'
CODEX_CHECK = r'''python3 -c "import tomllib" || exit 1
codex sandbox --help | grep -q -- --permission-profile || exit 1
flags=$(codex features list) || exit 1
for flag in hooks plugins memories external_agent_memory_import; do
  echo "$flags" | awk '{print $1}' | grep -qx "$flag" || exit 1
done
'''
AGY_CHECK = ('command -v python3 >/dev/null 2>&1 && command -v agy >/dev/null 2>&1 && '
             'agy --help 2>&1 | grep -q -- --input-format')

# AGY 无人值守时无法弹出权限确认：plan/accept-edits 会自动拒绝 shell 命令（含 git 提交），只有 skip 放行。
AGY_PERMISSIONS = {
    "plan": "--mode=plan",
    "accept-edits": "--mode=accept-edits",
    "auto": "--mode=auto",
    "skip": "--dangerously-skip-permissions",
}

# 可派代理的能力。platforms：目标机平台；auth_key：host 配置里的凭据项；
# auth_required：缺凭据即拒绝；check：目标机预检；permissions：必须显式选择的权限档位。
SPECS = {
    "codex": {"platforms": ("posix", "windows"), "isolation": "profile", "auth_key": "codex_auth_file",
              "auth_required": False, "check": CODEX_CHECK,
              "check_error": "目标机必须安装 Python 3.11+ 和支持功能开关的 Codex CLI；未推送任务分支。"},
    "dsh": {"platforms": ("posix",), "isolation": "dsh", "auth_key": "dsh_auth_file",
            "auth_required": True, "check": DSH_CHECK,
            "check_error": "目标机必须安装 dsh 且 dsh headless --help 可用；未推送任务分支。"},
    "agy": {"platforms": ("posix",), "isolation": "agy", "auth_key": "agy_auth_file",
            "auth_required": True, "check": AGY_CHECK, "permissions": AGY_PERMISSIONS,
            "check_error": "目标机必须安装 python3 和支持 --input-format 的 agy CLI；未推送任务分支。"},
}


def spec(name):
    """返回可派代理的能力；模板存在但未完成隔离适配的代理一律拒绝。"""
    if name not in AGENTS:
        raise ValueError(f"未知代理 '{name}'；内置：{', '.join(AGENTS)}。")
    if name not in SPECS:
        raise ValueError("该代理模板未实现员工配置隔离；完成专用适配前不得继承宿主个人配置。")
    return SPECS[name]


def permission_args(name, mode):
    """权限档位只对登记了 permissions 的代理有效，且必须显式给出。"""
    modes = SPECS.get(name, {}).get("permissions")
    if not modes:
        if mode:
            raise ValueError(f"{name} 不支持 --permission-mode；权限取自员工配置。")
        return []
    if not mode:
        raise ValueError(f"{name} 派单必须显式指定 --permission-mode（{'/'.join(modes)}）；"
                         "无人值守时只有 skip 能执行 shell 命令和 git 提交。")
    if mode not in modes:
        raise ValueError(f"{name} 的 --permission-mode 只能是 {'/'.join(modes)}。")
    return [modes[mode]]


def command(agent, model, effort, session=None, permission=None):
    args = []
    if agent.get("kind") == "codex":
        args += ['--disable hooks --disable plugins --disable memories --disable external_agent_memory_import -c "$RD_CODEX_SQLITE" -c "$RD_CODEX_LOG" -c \'cli_auth_credentials_store="file"\'']
    args += permission_args(agent.get("kind"), permission)
    if model:
        if "model" not in agent:
            raise ValueError("this agent has no model flag; set the model in its own config on the worker")
        args.append(agent["model"].replace("{model}", shlex.quote(model)))
    if effort:
        if "effort" not in agent:
            raise ValueError("this agent has no reasoning-effort flag")
        args.append(agent["effort"].replace("{effort}", shlex.quote(effort)))
    tpl = agent["resume"] if session else agent["start"]
    return tpl.replace("{model_args}", " ".join(args)).replace("{session}", shlex.quote(session or ""))
