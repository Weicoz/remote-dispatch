"""目标机配置的默认值。独立成模块，避免配置读写模块反向依赖 cli。"""

DEFAULTS = {
    "workdir": "~/remote-dispatch",
    "sync": "origin",
    "launcher": "nohup",
    "path": "$HOME/.local/bin:$HOME/.kimi-code/bin:/opt/homebrew/bin:/usr/local/bin",
    "ssh_opts": "-o BatchMode=yes -o ConnectTimeout=8",
    "env_file": "",
    # 缓存目录声明：NAME=目录，多项用分号分隔。相对目录落在用户根目录的 cache/ 下；
    # 绝对目录必须含 {user}（展开为用户 ID），且不得落在任何 worktree 内。空表示不注入。
    "cache_dirs": "",
    "codex_auth_file": "",
    "dsh_auth_file": "",
    "agy_auth_file": "~/.gemini/antigravity-cli/antigravity-oauth-token",
    "platform": "posix",
    "python": "python",
    "max_cpu_pct": "85",
    "warn_cpu_pct": "70",
    "tags": "",
    "enabled": "true",
    "max_heavy": "1",
    "max_load_per_core": "1.5",
    "min_free_mem_pct": "10",
    "min_free_disk_gb": "10",
    "warn_load_per_core": "1.0",
    "warn_free_mem_pct": "20",
    "warn_free_disk_gb": "20",
    # 交换区已用量只反映历史累积（macOS 不主动回收冷页），超过它只提示不拦；
    # 拦派单看换页速率：持续高速换页，或交换区快用尽且仍在换页。
    "max_swap_used_gb": "15",
    # 已停用：下面三项仍可加载，但不再参与接单判定
    "max_swap_io_mb_s": "10",
    "min_swap_free_gb": "1",
    "swap_active_mb_s": "1",
    "timeout": "3600",
    # worktree 总量上限（GB）；0 表示不限。仅 serve 定时回收使用，见 docs/cache-retention.md。
    "max_worktree_gb": "0",
}
