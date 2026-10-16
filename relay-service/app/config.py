from __future__ import annotations

import math
import os
from dataclasses import dataclass, field


# 核心拦截（正则，对命令小写后匹配）：拦"不可逆 / 会炸服务器 / 批量毁数据 / 动中转自身"。
# 原则：保证安全的基础上最大程度放开——普通（非递归）rm、iptables、curl|sh 等仍放行；
# git 只读（log/diff/status/clone）、systemctl 只读（status/is-active/cat）不拦。
# 递归 rm 由 executor._rm_violation 按路径单独判断：rm_safe_prefixes 下放行，其余拦截。
# 注意：本清单是审计+审批层（可被 python -c 等绕过、可人工批准豁免）；对"中转自身"
# 的硬边界在 relay.service 挂载命名空间（ProtectSystem=strict + InaccessiblePaths），
# 硬边界内即使批准也写不动（EROFS），例外只能由人在宿主机操作。
# 系统／中转自身保护类：不可逆或影响服务存续的操作。命中走**人工审批**——运维任务
# 可能合法需要它们（如超管让 agent 重启服务），不能自动拒。
_SYSTEM_PROTECT_PATTERNS = [
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
# ---- 凭据枚举／系统侦察（2026-09-28 加固，2026-09-29 归入自动拒绝类）----
# 任务环境本就没有 relay 凭据（executor._ENV_DROP_PREFIXES 剔除 RELAY_*），但
# agent 不知道，会花几分钟刨 env / /opt/team / relay 源码找 token：#278 为
# 「现在有几个定时任务？」这一句话烧掉 5.5 分钟 / 32 次 Bash。（本机并无
# local cron 层，定时任务唯一真源是 relay /plans，正规入口是 ```plan``` 标记。）
#
# 为什么单列成一组：**这几条没有任何合法业务用途**，拿去问人毫无意义——业务用户
# 无法判断 `ls -laR /opt/team/relay-avatar/` 安不安全，理性反应只有「点确认让它
# 闭嘴」（门禁形同虚设）或「点拒绝」（整个任务作废：#299 卡了 76 分钟后被拒，
# 而它的正事其实早已办完）。故命中即**自动拒绝 + 回注提示**让 agent 改道续跑，
# 不打扰用户。其余模式（删共享区、对外发送、系统操作）仍走人工审批。
#
# 用 [^;\n] 而非 [^|]：目标命令形如 `env | grep -iE "relay|ta_|token"`，引号内
# 含 `|`，用 [^|] 会在第一个竖线处截断而漏判；排除 `;` 与换行则避免跨命令误伤
# （如 `env | grep PATH; grep token notes.txt` 不应命中）。
RECON_BLOCKED_PATTERNS = [
    r"\b(?:env|printenv)\b[^;\n]{0,60}?\|\s*(?:grep|egrep|fgrep|rg)\b[^;\n]{0,120}?"
    r"\b(?:token|secret|passw|api[_-]?key|auth|credential|device)\b",
    r"/proc/(?:\d+|self)/environ",                   # 读进程环境（AGENTS.md 已明禁）
    # 分身目录与 avatar-cli（持 dws 凭据、policy.toml、run/ 状态）。
    # **不含 relay-boot**：`cat /opt/team/relay-boot/boot.sh` 是既有放行项
    # （tests/test_hardening.py），boot.sh 是可读脚本而非凭据，拦它会回归。
    r"/opt/team/relay-avatar\b|\bavatar-cli\b",
    # 具体凭据标识符（非泛词，误伤面极小）：出现即说明在找 relay 设备/管理员凭据
    r"\brelay_(?:device|admin)[a-z_]*\b|\bdevice_secret\b|\bx-device-id\b",
]
DEFAULT_BLOCKED_PATTERNS = _SYSTEM_PROTECT_PATTERNS + RECON_BLOCKED_PATTERNS
# 自动拒绝类的原因集合。分类靠**比对已算出的 pattern 字符串**（frozenset O(1)），
# 不新增任何正则、不在热路径上多跑一次匹配——executor._is_blocked 本就返回命中原因。
# 运维若用 RELAY_BLOCKED_PATTERNS 覆盖掉某条侦察模式，它自然不再触发自动拒绝；
# 新增的模式默认落人工审批（保守取向）。
AUTO_DENY_REASONS = frozenset(RECON_BLOCKED_PATTERNS)


def block_category(reason: str) -> str:
    """拦截原因 → 短类别标识（recon/rm/dws/system/other）。

    供 error 字段编码用（`blocked:<cat>: <cmd>`），让分身侧能用**业务语言**向超管
    转述，而不必把 shell 原文甩给业务用户。只做字符串前缀/集合比对，**不跑正则**。
    """
    if not reason:
        return "other"
    if reason in AUTO_DENY_REASONS:
        return "recon"
    if reason.startswith("recursive rm outside safe dirs:"):
        return "rm"
    if reason.startswith("dws "):
        return "dws"
    if reason in _SYSTEM_PROTECT_PATTERNS:
        return "system"
    return "other"


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
    # ── 单轮 LLM 调用保护（claude 引擎专用，透传为 claude-code 环境变量）──
    #
    # 为什么需要：task_timeout 只管**整个任务**，管不到单轮。2026-10-13 实测
    # T769/T770/T771/T775/T782 五任务 145 轮，其中 20 轮是「停顿轮」（输出
    # <2000 字符却耗时 >60s），合计 2101s ＝ LLM 总时间的 35%。最极端一轮
    # （T775 R25）tool_result 落在 02:48:45.307、下一条 assistant 消息
    # 02:53:14.707——单次 API 调用挂了 269 秒只产出 1150 字符（4 字符/s，
    # 而正常生成速率是 116~201 字符/s）。claude-code 的 API_TIMEOUT_MS 默认
    # 600000ms(10min)，所以这种停顿完全在默认阈值内，不会触发任何重试。
    #
    # 取值依据（不能拍脑袋设小）：同批实测**合法**轮最长 133.5s（一次 Write
    # 生成 26532 字符 HTML）。设 120s 会误杀 2 个合法轮，故取 180s——比合法
    # 最长值留 35% 余量，同时仍能截掉 269s/236s 两个最坏停顿，且比默认 600s
    # 紧 3.3 倍。0/负值＝不设限（沿用 claude-code 默认）。
    api_timeout_ms: float = 180000.0
    # 首字节窗口：等响应头的上限。与 api_timeout_ms 互补——后者管整次调用，
    # 前者专管「连上了但迟迟不吐第一个字节」，正是停顿轮的典型形态。claude-code
    # 对该原因有独立重试上限（不占会话重试预算），故可以设得比 api_timeout_ms 紧。
    # 内部钳位区间 [10s, 1800s]。0/负值＝不设。
    api_first_byte_timeout_ms: float = 60000.0
    # 重试次数。注意环境变量名是 CLAUDE_CODE_MAX_RETRIES，**不是** MAX_RETRIES
    # （后者在 claude 二进制里 0 命中，只作为前者的后缀存在）；内部钳位上限 15。
    api_max_retries: int = 3
    engine: str = "opencode"  # opencode | claude
    agent_bin: str = ""
    model: str = ""
    log_path: str = ""
    xdg_config_home: str = ""
    dashboard_password: str = ""
    event_history_max: int = 500
    stale_timeout_hours: float = 24.0   # review 停留超时 → 自动 done
    # 待审批（pending_approval）停留超时 → 自动 **failed**（不是 done）。
    # 为什么与 review 分开：review 是"活已干完、等人过目"，超时置 done 无损；
    # pending_approval 是"活干到一半被门禁拦住"，超时置 done 等于把**没办完的
    # 任务标成办完了**（#299 若无人手工拒批，24h 后就会被标 done）。
    # 默认 60min：无 blocked_at 列（不迁移 schema），超时从 created_at 起算，故要
    # 覆盖「任务本身跑掉的时间 + 留给人决策的时间」。任务时限 1800s(30min)，60min
    # 意味着最坏情况（跑满 30min 才被拦）仍有 30min 决策窗。#299 从 09:29 卡到
    # 10:45（76 分钟）无人处理，60min 足以让超管看到，又不至于让发起人干等一上午。
    approval_timeout_minutes: float = 60.0
    # 侦察类命令的自动拒绝续跑上限（Tier 2）。超过即转人工审批，防 agent 反复
    # 试探把续跑变成死循环——每次续跑都是一次完整 claude 调用，代价不低。
    auto_deny_max: int = 2
    # ── 长任务异步化：T 升级 ＋ 分段续跑 ＋ long lane ──
    #
    # 依据（现役 PG 台账实测 n=108、跨度 41 天）：P50=1.9m、52% 的任务 2 分钟内就
    # 结束，但 >15m 的占 11.1% 且吃掉 **49.7%** 的槽位时间；3 个任务死在 30.0m 整
    # （task_timeout 斩立决），产出全丢、tokens 记 0。#108 被砍后重跑为 #111，同样
    # 工作 24.3m 就完成——差的不是能力，是时间预算。
    #
    # 为什么判据放在**运行时**而不是提交时预测：提交时静态特征可分性差（desc≥1000 字
    # 精确率仅 16.9%、长活关键词 15.9%；要抓 91.7% 召回得标记 60% 的任务，且 desc
    # 长度被 marker 铁律样板约 1100 字污染）。而条件概率 P(>15m｜已跑到 T 仍在跑)
    # 在 T=10m 达 **75% 精确率、100% 召回**（历史上没有任何长任务在 10m 前结束过），
    # 只打扰 14.8% 的任务、误报 3.7%。T=3m 会打扰 41.7% 且精确率仅 26.7%，提示沦为噪音。
    long_task_mode: str = "shadow"          # off | shadow | notify | on
    long_task_threshold_sec: float = 600.0  # T：单段跑到此时长即判为长任务
    long_task_max_segments: int = 3         # 单任务最多跑几段（含首段）；3 段＝上限 90m
    # long lane：为短任务保留槽位，防长任务吃光并发（移除 30m 上限的必要配套——
    # 分段续跑让单个长任务最坏占槽 90m，是原来的 3 倍）。
    # **比例式而非固定值**：固定 reserve=1 只在 max_concurrent=3 时合适；扩到 8 后
    # 只占 12.5%、护栏形同虚设（8 倍流量重放下短任务 P90 仍等 39m）。1/3 比例在
    # 3 槽（cap=2）与 8 槽（cap=5）两个尺度上同为重放拐点，并发怎么扩都不退化成死代码。
    long_lane_reserve_ratio: float = 1.0 / 3.0
    # aging 逃生：长任务在 lane 里等待超过此时长即放行，杜绝无界饥饿。
    # cap=1 的极端情形重放出过 8.5h 等待，aging 是它的上界保险。
    long_wait_promote_sec: float = 1200.0
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

    @property
    def long_lane_cap(self) -> int:
        """同时可运行的长任务上限＝max_concurrent − 保留给短任务的槽位数。

        保留数按比例算并下限 1：max_concurrent=3 → reserve 1 → cap 2；
        =8 → reserve 3 → cap 5。cap 再下限 1，防比例配得过大时把长任务全禁掉。
        """
        reserve = max(1, math.ceil(self.max_concurrent * self.long_lane_reserve_ratio))
        return max(1, self.max_concurrent - reserve)

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
            api_timeout_ms=float(os.environ.get("RELAY_API_TIMEOUT_MS", "180000")),
            api_first_byte_timeout_ms=float(
                os.environ.get("RELAY_API_FIRST_BYTE_TIMEOUT_MS", "60000")),
            api_max_retries=int(os.environ.get("RELAY_API_MAX_RETRIES", "3")),
            engine=os.environ.get("RELAY_ENGINE", "opencode"),
            agent_bin=os.environ.get("RELAY_AGENT_BIN", ""),
            model=os.environ.get("RELAY_MODEL", ""),
            log_path=os.environ.get("RELAY_LOG", f"{root}/relay.log"),
            xdg_config_home=os.environ.get("RELAY_XDG_CONFIG_HOME", ""),
            dashboard_password=os.environ.get("RELAY_DASHBOARD_PASSWORD", ""),
            stale_timeout_hours=float(os.environ.get("RELAY_STALE_TIMEOUT_HOURS", "24")),
            approval_timeout_minutes=float(
                os.environ.get("RELAY_APPROVAL_TIMEOUT_MINUTES", "60")),
            auto_deny_max=int(os.environ.get("RELAY_AUTO_DENY_MAX", "2")),
            long_task_mode=os.environ.get("RELAY_LONG_TASK_MODE", "shadow").strip().lower(),
            long_task_threshold_sec=float(
                os.environ.get("RELAY_LONG_TASK_THRESHOLD_SEC", "600")),
            long_task_max_segments=int(
                os.environ.get("RELAY_LONG_TASK_MAX_SEGMENTS", "3")),
            long_lane_reserve_ratio=float(
                os.environ.get("RELAY_LONG_LANE_RESERVE_RATIO", str(1.0 / 3.0))),
            long_wait_promote_sec=float(
                os.environ.get("RELAY_LONG_WAIT_PROMOTE_SEC", "1200")),
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
            sign_window=int(os.environ.get("RELAY_SIGN_WINDOW", "900")),
            cleanup_runtime=os.environ.get("RELAY_CLEANUP_RUNTIME", "1").strip().lower()
            in ("1", "true", "on", "yes"),
            shared_venv=os.environ.get("RELAY_SHARED_VENV", "").strip(),
            backup_max_mb=float(os.environ.get("RELAY_BACKUP_MAX_MB", "50")),
        )
