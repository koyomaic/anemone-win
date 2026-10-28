from __future__ import annotations

import math
import os
import re
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
    # 增量任务的水位文件（约定路径 shared/report/<job>/state/<name>.watermark.txt）。
    # 为什么必须豁免：这类文件**本来就该每轮重写**——它记的是"上次读到哪"，不是产出。
    # 但冲突门禁只看"是否改了同一路径"，于是定时增量任务会跟自己上一轮撞车。
    # 实测「财务智脑攻坚组群消息即时提炼」计划每 15 分钟触发一次，累计 32 单里
    # 27 单（84%）判 conflict，冲突记录里该文件被命中 1140 次，而每单的
    # changed_files **只有这一个水位文件**（无新消息时它不产出任何报告）——
    # 即纯伪冲突：白干 1.2h 机时、堆了 68MB 冲突备份，还占并发槽跟真实用户任务抢。
    # 豁免后 scan["all"] 为空 → 干净 done；有真实产出时那些产出照常参与冲突判定。
    "*/state/*.watermark*",
    "*.watermark.txt",   # 兜住没放在 state/ 下的写法
]

# 低风险自动完成前缀：任务**仅新建**这些目录下的文件时，不再进入 review。
# 为什么需要：现役 review 的主要来源不是冲突，而是“任何实质文件产出都要人确认”。
# 2026-10-16 现役 PG 抽样显示，当前 9 个 review 任务的产出全部落在报告/临时/系统
# 盘点目录，且 conflicts 基本为空；逐条确认没有增加安全性，只增加等待。
# 保守边界：只放新建、不放修改/删除；只放报告、临时、数据与系统盘点目录，
# 不放 knowledge/memory/skills/mcp/tools/secrets 等会影响后续行为或权限的目录。
DEFAULT_AUTO_DONE_PREFIXES = [
    "shared/report/",
    "shared/tmp/",
    "shared/data/",
    "shared/_sync/",
    "shared/_migrate/",
    "shared/_inventory/",
    "shared/_upgrade-backup/",
]

# 「再生型固定名产物」豁免名单（fnmatch，匹配工作区相对路径）。
#
# 为什么需要：auto_done_reason 原本只要 scan 里有 modified 就一律退回 review，
# 而报告类技能按约定还会写一份**固定名指针件**（shared/report/<job>/latest.html
# ＝「最新版」入口）。它每轮必然被覆盖 → 每轮都算 modified → 每轮都进 review 等
# 人确认。2026-10-27 #475（大脑资产盘点）即如此：15:57 办完、结果随卡片定格与
# 附件当场送达，只因 latest.html 是覆盖而非新建就挂进 review；发起人没回「确认」，
# 8 小时后（RELAY_STALE_TIMEOUT_HOURS=8，阈值按 created_at 起算）被清扫自动 done，
# 23:58 又给用户推了一遍全文——深夜幽灵回复。历史 21 单超时自动 done 里 10 单
# 落在 22:00–07:00。
#
# 边界（刻意收窄）：
#   · 只豁免 latest.* 这类「每轮重写的最新版指针」，且必须**全部** modified 命中；
#     任何其它 modified、任何 deleted、跨任务冲突、只读覆盖仍照旧退回人工核查。
#   · 只作用于「是否自动 done」，不改冲突判定——豁免件仍进 changed_files、仍参与
#     撞车检测（与 volatile 的区别：volatile 是彻底不算产出）。
#   · 台账类累积件（如 shared/report/<job>/ledger.md）虽也每轮重写，但内容是实质
#     记录，默认**不**豁免；确有需要逐机用 RELAY_AUTO_DONE_MODIFIED_OK 追加。
DEFAULT_AUTO_DONE_MODIFIED_OK = [
    "shared/report/latest.*",
    "shared/report/*/latest.*",   # fnmatch 的 * 跨 /，故 asset-map/latest.html 及更深层均命中
]

# claude 引擎默认禁用的内置工具（透传为 --disallowedTools）。
#
# 为什么需要：claude-code 默认挂 26 个工具 + 63 个 slash command + 5 个 agent，
# 光工具定义就占 **19788 token**——实测固定上下文 21539 token 里 89.8% 是它，
# CLAUDE.md 只占 1567、CEO_PROMPT 486、19 个技能合计仅 185（技能只注入名字，
# 描述走 Skill 工具懒加载）。这 21.5k 每一轮 LLM 往返都要重新 prefill 一遍，
# 按实测 ~3000 tok/s 就是每轮 ~7s 纯固定开销；每会话中位 15 轮 → 每单 ~105s。
#
# 取舍依据是 314 个会话 / 6779 次工具调用的**真实使用统计**，不是拍脑袋：
# 下面 7 个在全量历史里调用次数为 0。DesignSync 更是二进制里明写「only available
# with claude.ai authentication... not supported through... other third-party
# providers」——我们走 OpenAI 兼容代理，它根本不可用。
#
# 刻意**不砍**的（都有实测调用，砍了会降质）：
#   TaskCreate(166次/42会话)、TaskUpdate(250次/40会话) —— 多步任务清单，高频
#   Task(转写里名为 Agent，5次/3会话) + SendMessage(5次) —— 子agent 派发与
#     恢复/取消，两者必须成对保留
#   TaskStop/TaskGet/TaskList/TaskOutput —— TaskCreate 的配对，只砍会让后台任务
#     变成停不掉的孤儿
#   Skill(99次/93会话) —— 实测砍掉它 input 反而从 9724 涨到 11715：它是 19 个
#     技能的懒加载入口，没了它 claude-code 会把技能元信息直接摊进系统提示
#   WebFetch(8次)/WebSearch(12次) —— 用户明确要留；且各自只值 206/231 token
# 实测：砍这 7 个后 26→19 工具、21539→15166 token，每轮省 6374（-29.6%）。
DEFAULT_CLAUDE_DISALLOWED_TOOLS = [
    "NotebookEdit",     # Jupyter .ipynb 编辑，无此业务
    "DesignSync",       # 仅 claude.ai 鉴权可用，第三方 provider 下不可用
    "EnterWorktree",    # git worktree 隔离；relay 任务本就各自独立 workdir
    "ExitWorktree",
    "ReportFindings",   # LSP 代码审查发现上报，需先配 LSP server
    "ScheduleWakeup",   # 长驻会话定时唤醒；headless 一次性任务不适用
    "CronDelete",
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


def _auto_done_prefixes_from_env() -> list[str]:
    raw = os.environ.get("RELAY_AUTO_DONE_PREFIXES", "").strip()
    if not raw:
        return list(DEFAULT_AUTO_DONE_PREFIXES)
    out = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        out.append(p if p.endswith("/") else p + "/")
    return out


def _auto_done_modified_ok_from_env() -> list[str]:
    """RELAY_AUTO_DONE_MODIFIED_OK：逗号分隔的 fnmatch 模式。

    未设 → 用 DEFAULT_AUTO_DONE_MODIFIED_OK；显式设 `none`/`-`/`off` → 空列表
    （＝不豁免任何 modified，回到「有修改就进 review」的旧行为）。
    """
    raw = os.environ.get("RELAY_AUTO_DONE_MODIFIED_OK", "").strip()
    if not raw:
        return list(DEFAULT_AUTO_DONE_MODIFIED_OK)
    if raw.lower() in ("none", "-", "off"):
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _claude_disallowed_tools_from_env() -> list[str]:
    """RELAY_CLAUDE_DISALLOWED_TOOLS：逗号或空格分隔的工具名。

    未设 → 用 DEFAULT_CLAUDE_DISALLOWED_TOOLS；显式设 `none`/`-` → 空列表
    （＝一个都不禁，回到 claude-code 原生 26 工具），给排查「某工具被误禁」
    留一条不用改代码的退路。
    """
    raw = os.environ.get("RELAY_CLAUDE_DISALLOWED_TOOLS", "").strip()
    if not raw:
        return list(DEFAULT_CLAUDE_DISALLOWED_TOOLS)
    if raw.lower() in ("none", "-", "off"):
        return []
    return [t for t in re.split(r"[,\s]+", raw) if t]


@dataclass
class Settings:
    db_path: str
    workspace_root: str
    shared_dir: str
    task_root: str
    users_root: str
    admin_token: str = "change-me"
    # 3 → 8 的依据（同批 108 任务真实到达序列重放）：mc=3 在 **2 倍流量即崩**
    # （即使 cap=2，短任务 P90 37m、长任务 P90 215m）；mc=8 + cap=5 在 4 倍流量下
    # 短任务最坏仅 48s。默认值走 CODE 是为了**机队一致**——relay.env 每台机独立、
    # provision 只在缺失时创建且绝不覆盖，逐机改 env 传不出去。上限是天花板不是
    # 地板：现网到达率 0.11 任务/小时，8 槽极少同时占满，小规格机器只在突发时才吃满。
    max_concurrent: int = 8
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
    # ── 管道切碎护栏（2026-10-25；事故与判据见 executor.pipeline_shred_suspect）──
    # 单任务 __unparsedToolInput 签名 ≥ min_count 且占 tool_calls 比 ≥ min_ratio
    # → error/审计标注「疑似平台管道缺陷」＋钉钉告警（带冷却）。基线签名 ~0、
    # 事故期 41-58%；min_count=5 防小样本误报。alert_to 空＝只审计不告警。
    tool_verr_min_count: int = 5
    tool_verr_min_ratio: float = 0.25
    pipeline_alert_to: str = "王洪彬"
    pipeline_alert_cooldown_sec: float = 3600.0
    # dws 种子登录态目录（与 boot.sh notify_failure 同源）；目录不存在时告警
    # 退回本机 root 登录态直发。
    dws_seed_dir: str = "/mnt/vol-eltaah12/workspace/shared/secrets/dws-cli-seed/dws-cli"
    engine: str = "opencode"  # opencode | claude
    agent_bin: str = ""
    model: str = ""
    # claude 引擎禁用的内置工具（见 DEFAULT_CLAUDE_DISALLOWED_TOOLS 的取舍依据）。
    # 空列表＝不禁任何工具。
    claude_disallowed_tools: list[str] = field(
        default_factory=lambda: list(DEFAULT_CLAUDE_DISALLOWED_TOOLS))
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
    # 默认 **on**（用户 2026-10-16 指示：跳过影子期直接启用）。机队一致同样靠 CODE
    # 默认值——relay.env 不随 git 下发。要退回观察态设 RELAY_LONG_TASK_MODE=shadow。
    long_task_mode: str = "on"              # off | shadow | notify | on
    long_task_threshold_sec: float = 600.0  # T：单段跑到此时长即判为长任务
    long_task_max_segments: int = 3         # 单任务最多跑几段（含首段）；3 段＝上限 90m
    # 进展门的"活动量"阈值：本段工具调用数达到即视为有进展。为什么需要这一路——
    # 被砍的段拿不到 result 事件，tokens=0、最终文本也常是空的（#146 第 1 段实测
    # files=0 text=0，其实一直在跑工具查数据）。只看文件与文本会把"正在采集数据"
    # 误判成"毫无进展"而停掉续跑。设 5 而非 1：单次工具调用可能只是探路/重试。
    long_task_min_tool_calls: int = 5
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
    # 低风险产出自动 done：on/off。off 时恢复旧行为——任何实质文件产出都进 review。
    auto_done_mode: str = "on"
    auto_done_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_DONE_PREFIXES))
    # 再生型固定名产物豁免：modified 全部命中时不阻塞自动 done（见 DEFAULT_AUTO_DONE_MODIFIED_OK）
    auto_done_modified_ok: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_DONE_MODIFIED_OK))
    # 单任务新建文件数上限。超过即认为批量写入风险上升，仍走人工 review。
    auto_done_max_files: int = 200
    conflict_window_hours: float = 24.0  # 同文件冲突判定的时间窗；窗外的先后写入属正常演进，不算冲突
    relay_exclude_providers: list[str] = field(default_factory=list)  # 任务配置中剔除的 provider（本地专用模型不进任务环境）
    # ── 外部先进模型 strict 脱敏出口（2026-10-22 方案落地）──
    # required 时，opencode 任务配置里声明为外部的 provider 必须被改写到本机脱敏代理；
    # 代理不健康或未配置外部 provider 时 fail-closed，避免明文公司/人员/基础设施出网。
    external_model_redaction: str = "off"   # off | required
    external_proxy_url: str = ""            # 例：http://127.0.0.1:8792
    external_providers: list[str] = field(default_factory=list)
    external_models: list[str] = field(default_factory=list)
    redaction_health_required: bool = False
    redaction_health_timeout: float = 2.0
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
            max_concurrent=int(os.environ.get("RELAY_MAX_CONCURRENT", "8")),
            task_timeout=float(os.environ.get("RELAY_TASK_TIMEOUT", "1800")),
            api_timeout_ms=float(os.environ.get("RELAY_API_TIMEOUT_MS", "180000")),
            api_first_byte_timeout_ms=float(
                os.environ.get("RELAY_API_FIRST_BYTE_TIMEOUT_MS", "60000")),
            api_max_retries=int(os.environ.get("RELAY_API_MAX_RETRIES", "3")),
            tool_verr_min_count=int(
                os.environ.get("RELAY_TOOL_VERR_MIN_COUNT", "5")),
            tool_verr_min_ratio=float(
                os.environ.get("RELAY_TOOL_VERR_MIN_RATIO", "0.25")),
            pipeline_alert_to=os.environ.get("RELAY_PIPELINE_ALERT_TO", "王洪彬"),
            pipeline_alert_cooldown_sec=float(
                os.environ.get("RELAY_PIPELINE_ALERT_COOLDOWN_SEC", "3600")),
            dws_seed_dir=os.environ.get(
                "RELAY_DWS_SEED_DIR",
                "/mnt/vol-eltaah12/workspace/shared/secrets/dws-cli-seed/dws-cli"),
            engine=os.environ.get("RELAY_ENGINE", "opencode"),
            agent_bin=os.environ.get("RELAY_AGENT_BIN", ""),
            model=os.environ.get("RELAY_MODEL", ""),
            claude_disallowed_tools=_claude_disallowed_tools_from_env(),
            log_path=os.environ.get("RELAY_LOG", f"{root}/relay.log"),
            xdg_config_home=os.environ.get("RELAY_XDG_CONFIG_HOME", ""),
            dashboard_password=os.environ.get("RELAY_DASHBOARD_PASSWORD", ""),
            stale_timeout_hours=float(os.environ.get("RELAY_STALE_TIMEOUT_HOURS", "24")),
            approval_timeout_minutes=float(
                os.environ.get("RELAY_APPROVAL_TIMEOUT_MINUTES", "60")),
            auto_deny_max=int(os.environ.get("RELAY_AUTO_DENY_MAX", "2")),
            long_task_mode=os.environ.get("RELAY_LONG_TASK_MODE", "on").strip().lower(),
            long_task_threshold_sec=float(
                os.environ.get("RELAY_LONG_TASK_THRESHOLD_SEC", "600")),
            long_task_max_segments=int(
                os.environ.get("RELAY_LONG_TASK_MAX_SEGMENTS", "3")),
            long_task_min_tool_calls=int(
                os.environ.get("RELAY_LONG_TASK_MIN_TOOL_CALLS", "5")),
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
            auto_done_mode=os.environ.get("RELAY_AUTO_DONE_MODE", "on").strip().lower(),
            auto_done_prefixes=_auto_done_prefixes_from_env(),
            auto_done_modified_ok=_auto_done_modified_ok_from_env(),
            auto_done_max_files=int(os.environ.get("RELAY_AUTO_DONE_MAX_FILES", "200")),
            conflict_window_hours=float(os.environ.get("RELAY_CONFLICT_WINDOW_HOURS", "24")),
            relay_exclude_providers=[p.strip() for p in os.environ.get("RELAY_EXCLUDE_PROVIDERS", "").split(",") if p.strip()],
            external_model_redaction=os.environ.get("RELAY_EXTERNAL_MODEL_REDACTION", "off").strip().lower(),
            external_proxy_url=os.environ.get("RELAY_EXTERNAL_PROXY_URL", "").strip(),
            external_providers=[p.strip() for p in os.environ.get("RELAY_EXTERNAL_PROVIDERS", "").split(",") if p.strip()],
            external_models=[p.strip() for p in os.environ.get("RELAY_EXTERNAL_MODELS", "").split(",") if p.strip()],
            redaction_health_required=os.environ.get("RELAY_REDACTION_HEALTH_REQUIRED", "0").strip().lower()
            in ("1", "true", "on", "yes"),
            redaction_health_timeout=float(os.environ.get("RELAY_REDACTION_HEALTH_TIMEOUT", "2")),
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
