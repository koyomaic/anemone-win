from __future__ import annotations

import os
from dataclasses import dataclass, field


# 核心拦截（正则，对命令小写后匹配）：拦"不可逆 / 会炸服务器 / 批量毁数据 / 动中转自身"。
# 原则：保证安全的基础上最大程度放开——普通（非递归）rm、iptables、curl|sh 等仍放行；
# git 只读（log/diff/status/clone）、systemctl 只读（status/is-active/cat）不拦。
# 递归 rm 由 executor._rm_violation 按路径单独判断：rm_safe_prefixes 下放行，其余拦截。
# 注意：本清单是审计+审批层（可被 python -c 等绕过、可人工批准豁免）；对"中转自身"
# 的硬边界在 relay.service 挂载命名空间（ProtectSystem=strict + InaccessiblePaths），
# 硬边界内即使批准也写不动（EROFS），例外只能由人在宿主机操作。
DEFAULT_BLOCKED_PATTERNS = [
    r"\bmkfs",                                      # 格式化文件系统（不可逆）
    r"\bof=/dev/(sd|vd|nvme|hd|xvd|dm-|md)\w*",     # dd 写裸盘
    r">\s*/dev/(sd|vd|nvme|hd|xvd|dm-|md)\w*",      # 重定向写裸盘
    r"\b(shutdown|reboot|poweroff|halt)\b",         # 服务器电源操作
    r"\bkill\s+(-9|-kill)\s+1(?!\d)",               # 杀 init（PID 1）
    r"\bchmod\s+-r\s+777\s+(?:~|/(?!\w))",          # 根/家目录递归 777
    # ---- 中转自身保护（2026-09-17 加固）----
    # 选项跳跃组 (?:...)*?：覆盖 git -C <path> push / git -c k=v commit /
    # systemctl --no-pager restart 等全局选项间隔写法（冒烟实测发现的绕过面）。
    r"\bgit\s+(?:-{1,2}[a-z-]+(?:[= ]\S+)?\s+)*?(push|commit|am|rebase|merge|reset|clean|tag|remote|cherry-pick|apply|format-patch|revert)\b",
    r"\bsystemctl\s+(?:--?\S+\s+)*?(start|stop|restart|try-restart|reload|kill|enable|disable|mask|unmask|edit|daemon-reload|daemon-reexec|isolate|preset)\b",
    r"\bservice\s+[\w.@-]+\s+(start|stop|restart|reload)\b",
    r"\b(pkill|killall)\s+.*\b(relay|app\.serve|uvicorn|claude-proxy)\b",
]
DEFAULT_RM_SAFE_PREFIXES = ["/tmp", "/var/tmp"]

# dws 消息发送二次确认门控的敏感接收人（executor._confirm_violation 精确字符串匹配）。
# "1" 是马韵升（董事局主席）的 userId，必须整 token 精确相等，绝不子串匹配
# （避免误伤 37505774 等含 1 的 userId）。
DEFAULT_CONFIRM_RECIPIENTS = ["马韵升", "1", "D3QFnNNkA4zsa6fEkz0vxWVZZG4c4SxTc"]

# 工具自动重写的运行时产物（fnmatch 通配，匹配工作区相对路径）：不是任务产出。
# 典型如 dws CLI 每次调用都刷新的凭据缓存——若计入 changed_files，则每个用过 dws 的
# 任务都会与历史上所有用过 dws 的任务"撞文件"，把成功任务误判成 conflict。
# 命中的路径不计产出、不参与冲突判定（scan 结果里单列 volatile 供排查）。
DEFAULT_VOLATILE_PATTERNS = [
    "*auth-token*",      # dws CLI 凭据缓存（shared/secrets/dws-cli-seed/...）
    "*.token_cache",     # 接口技能 token 缓存
    ".token_cache",
    "*.bak-*",           # 冲突备份自身产物（Workspace.backup_version 生成）
    "*.pyc",
]


def _blocked_patterns_from_env() -> list[str]:
    raw = os.environ.get("RELAY_BLOCKED_PATTERNS", "").strip()
    if not raw:
        return list(DEFAULT_BLOCKED_PATTERNS)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _rm_safe_prefixes_from_env() -> list[str]:
    raw = os.environ.get("RELAY_RM_SAFE_PREFIXES", "").strip()
    if not raw:
        return list(DEFAULT_RM_SAFE_PREFIXES)
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


def _confirm_recipients_from_env() -> list[str]:
    raw = os.environ.get("RELAY_CONFIRM_RECIPIENTS", "").strip()
    if not raw:
        return list(DEFAULT_CONFIRM_RECIPIENTS)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _volatile_patterns_from_env() -> list[str]:
    raw = os.environ.get("RELAY_VOLATILE_PATTERNS", "").strip()
    if not raw:
        return list(DEFAULT_VOLATILE_PATTERNS)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Settings:
    db_path: str
    workspace_root: str
    shared_dir: str
    task_root: str
    users_root: str
    admin_token: str = "change-me"
    max_concurrent: int = 3
    task_timeout: float = 1800.0
    engine: str = "opencode"  # opencode | claude
    agent_bin: str = ""
    model: str = ""
    log_path: str = ""
    xdg_config_home: str = ""
    dashboard_password: str = ""
    event_history_max: int = 500
    stale_timeout_hours: float = 24.0   # review/待审批 停留超时 → 自动 done
    # local 影子记录超期未回报 → 判 failed。默认 15min：avatar 侧 local 硬截止是
    # reply_timeout_sec(600) + 60s 宽限 ≈ 11min，留 4min 余量给回报本身，
    # 既不会抢在 avatar 正常回报前误判，也不会让僵尸行挂太久。
    local_stale_minutes: float = 15.0
    sweep_interval_sec: float = 300.0   # 超时清扫周期
    blocked_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATTERNS))
    rm_safe_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_RM_SAFE_PREFIXES))
    confirm_recipients: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM_RECIPIENTS))
    volatile_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_VOLATILE_PATTERNS))
    conflict_window_hours: float = 24.0  # 同文件冲突判定的时间窗；窗外的先后写入属正常演进，不算冲突
    relay_exclude_providers: list[str] = field(default_factory=list)  # 任务配置中剔除的 provider（本地专用模型不进任务环境）
    boot_cmd: str = "/opt/team/relay-boot/boot.sh"  # bootloader 入口（POST /admin/update 触发）
    min_client_version: str = ""  # 要求的最低客户端版本（空=不校验；/health 透出，客户端软提醒）
    auth_legacy: bool = False     # 旧式 bearer token 认证（生产默认关：token 仅作一次性激活码；过渡需要时置 1）
    sign_window: int = 900        # 设备签名时间窗（秒），防重放；放宽以容忍成员机器时钟漂移
    cleanup_runtime: bool = True  # 任务终态后自动清理 workdir 临时目录（home/data）；置 0 关闭
    shared_venv: str = ""         # 任务级共享 venv（默认 shared_dir/venv）；bin 前置进任务 PATH，
    #                               pip install 落这里——沙箱下系统/服务 venv 只读，安装能力保留但碰不到中转运行时
    backup_max_mb: float = 50.0   # 冲突备份的单文件体积上限；超过则只记 conflicts 不拷贝。
    #                               历史事故：无上限的 copy2 让一个宽 target 任务把 venv/models 整棵树
    #                               备份成 18972 个 .bak、4.92G（单个 model.bin.bak 1.5G 还拷了两份）

    @classmethod
    def from_env(cls) -> "Settings":
        root = os.environ.get("RELAY_WORKSPACE", "/mnt/vol-eltaah12/workspace")
        return cls(
            db_path=os.environ.get("RELAY_DB", f"{root}/relay.db"),
            workspace_root=root,
            shared_dir=os.environ.get("RELAY_SHARED", f"{root}/shared"),
            task_root=os.environ.get("RELAY_TASK_ROOT", f"{root}/tasks"),
            users_root=os.environ.get("RELAY_USERS", f"{root}/users"),
            admin_token=os.environ.get("RELAY_ADMIN_TOKEN", "change-me"),
            max_concurrent=int(os.environ.get("RELAY_MAX_CONCURRENT", "3")),
            task_timeout=float(os.environ.get("RELAY_TASK_TIMEOUT", "1800")),
            engine=os.environ.get("RELAY_ENGINE", "opencode"),
            agent_bin=os.environ.get("RELAY_AGENT_BIN", ""),
            model=os.environ.get("RELAY_MODEL", ""),
            log_path=os.environ.get("RELAY_LOG", f"{root}/relay.log"),
            xdg_config_home=os.environ.get("RELAY_XDG_CONFIG_HOME", ""),
            dashboard_password=os.environ.get("RELAY_DASHBOARD_PASSWORD", ""),
            stale_timeout_hours=float(os.environ.get("RELAY_STALE_TIMEOUT_HOURS", "24")),
            local_stale_minutes=float(os.environ.get("RELAY_LOCAL_STALE_MINUTES", "15")),
            sweep_interval_sec=float(os.environ.get("RELAY_SWEEP_INTERVAL_SEC", "300")),
            blocked_patterns=_blocked_patterns_from_env(),
            rm_safe_prefixes=_rm_safe_prefixes_from_env(),
            confirm_recipients=_confirm_recipients_from_env(),
            volatile_patterns=_volatile_patterns_from_env(),
            conflict_window_hours=float(os.environ.get("RELAY_CONFLICT_WINDOW_HOURS", "24")),
            relay_exclude_providers=[p.strip() for p in os.environ.get("RELAY_EXCLUDE_PROVIDERS", "").split(",") if p.strip()],
            boot_cmd=os.environ.get("RELAY_BOOT_CMD", "/opt/team/relay-boot/boot.sh"),
            min_client_version=os.environ.get("RELAY_MIN_CLIENT_VERSION", "").strip(),
            auth_legacy=os.environ.get("RELAY_AUTH_LEGACY", "0").strip().lower()
            in ("1", "true", "on", "yes"),
            sign_window=int(os.environ.get("RELAY_SIGN_WINDOW", "60")),
            cleanup_runtime=os.environ.get("RELAY_CLEANUP_RUNTIME", "1").strip().lower()
            in ("1", "true", "on", "yes"),
            shared_venv=os.environ.get("RELAY_SHARED_VENV", "").strip(),
            backup_max_mb=float(os.environ.get("RELAY_BACKUP_MAX_MB", "50")),
        )
