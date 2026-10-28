"""app/config.py —— 统一配置：env 凭据 + policy.toml 策略（唯一真源）。

- 凭据只进 env 文件（shared/secrets/relay-avatar.env，600），policy.toml 无明文。
- policy.toml 缺失时用内置默认（与 policy.example.toml 同构），保证首启不炸。
- 禁读 relay.env 与任何 /proc/<pid>/environ（凭据边界硬性，见 workspace AGENTS.md）。
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE = "/mnt/vol-eltaah12/workspace"

DEFAULTS: dict[str, Any] = {
    "identity": {"avatar_name": "智微办", "owner_name": "王洪彬",
                 "owner_userid": "", "self_open_id": "", "dws_profile": "",
                 "sign": ""},
    "ingress": {"event_keys": ["user_im_message_receive_o2o_all"],
                "idle_watchdog_sec": 270,                 "debounce_sec": 20,
                # 纯文本消息的聚合窗（缓冲里一旦出现附件标记就自动切回 debounce_sec）。
                # 为什么不把 debounce_sec 直接调小：实测 1271 对真人相邻消息里 23.2%
                # 间隔 ≤20s，全是「文字＋图片」「分条补充」这类真实组合请求，一刀切
                # 缩短会把它们劈成两个任务、agent 没看到图就作答。纯文本劈开则只是
                # 退化成 followup 追问（会 resume 上一条会话并注入摘要，可恢复），
                # 代价小得多，故只对纯文本收紧。取值依据见 session.py 的间隔分布。
                "debounce_text_sec": 12,
                "followup_debounce_sec": 30, "session_ttl_sec": 1800,
                # 重复请求去重：同一发起人在多个会话里发同一需求时只跑一次，
                # 其余会话绑成额外投递目标（结果送到每一处）。
                # 为什么必须有：2026-10-10 同一份周报需求 6 分钟内从单聊+两个群
                # 各触发一个任务，三者收敛出字节相同的报告并写同一路径 → 触发
                # 冲突守卫；并发抢资源还让其中一个撞上 1800s 超时。
                # dedup_similarity 用归一化后的 SequenceMatcher 比值；0＝关闭去重。
                "dedup_window_sec": 900, "dedup_similarity": 0.82,
                "dedup_min_chars": 12,
                # resume 上下文预算：链深/累计 tokens 任一触顶即断链另起新会话，
                # 并注入上一轮结果摘要（resume_digest_chars 截断）保持话题连续。
                # 为什么必须有：此前 resume 只看 TTL，链可无限续——claude 每轮重放
                # 整段历史。2026-09-28 实测 #278→#281 单轮往返 7.3s→32.6s（4.5 倍），
                # 模型往返占执行时间 75%。0/负值＝该项不设限。
                #
                # resume_max_tokens 的口径在 2026-10-23 变过，阈值随之从 60000 抬到
                # 1500000：relay 的 tokens 取自 claude 的 result.usage，而 claude-proxy
                # 此前在流式收尾只回 output_tokens、把 input_tokens 丢了，tokens 于是
                # 是**纯输出**；补回后它是**任务内所有 API 调用的 input+output 累加**
                # （实测单轮基线 ~19.8k，一个 4 轮任务 result.usage.input_tokens=79734）。
                # 同一个 60000 阈值会让**任何一个普通任务跑完就触顶**，追问链全断、
                # 用户每轮都丢上下文。1500000 ≈ 两个 #280 量级任务，与旧阈值「约 2 轮」
                # 的原意对齐。真正稳的闸门是下面的转写字节数（不依赖遥测口径）。
                "resume_max_depth": 3, "resume_max_tokens": 1500000,
                # 转写字节闸门：链上会话转写（--resume 要整段重放的东西）超过此
                # 字节数即断链。为什么 token 闸门不够：claude-proxy 曾把流式 usage
                # 的 input_tokens 丢掉，relay 的 tokens 退化成纯输出——2026-10-23
                # 宋连杰一个 181s 录屏经 378→380→382→384→386 五任务链累积 15 个
                # 图片块 / 6.11MB base64，转写 12.66MB，而 chain_tokens 只有 48387，
                # 60000 的闸门纹丝不动，一句话追问（#382）跑满 134s。字节数不依赖
                # 任何遥测，量的一定是会被重放的载荷。0/负值＝不设限。
                "resume_max_transcript_bytes": 2000000,
                "resume_digest_chars": 2000,
                "poll_sec": 3, "result_max_chars": 3000,
                "ack_emoji": "奋斗", "submit_emoji": "",
                "noise_emoji": "打招呼", "followup_emoji": "OK",
                 "ack_text_emotion": {}, "done_text_emotion": {},
                 "fail_text_emotion": {}, "use_card": True,
                 "card_progress_sec": 30,
                 "card_progress_mode": "rich",
                 "card_progress_fast_sec": 15,
                 "card_progress_slow_sec": 60,
                 "card_progress_actions": 3,
                 "card_progress_detail_in_group": False,
                 "card_real_summary_sec": 900,
                 "card_real_summary_max": 2,
                 "card_orphan_sweep_sec": 900,
                 "card_orphan_ttl_sec": 3600,
                 "card_orphan_sweep_batch": 5,
                 "relay_progress_enabled": True,
                 "relay_progress_retry_sec": 1800,
                 "relay_segment_budget_sec": 1800,
                 "relay_max_segments": 3,

                 # 长任务提示：与 relay 侧同名参数对齐（relay 管执行、分身管告知）。
                 # 分身**自己按 row.started_at 算 elapsed**，不依赖 relay 新增字段——
                 # poll 拿到的 row 里本来就有 started_at/resume_hint，故零新增 HTTP
                 # 请求、零 API 变更。mode: off|shadow|notify|on（shadow 只记日志）。
                 "long_task_mode": "on", "long_task_threshold_sec": 600,
                "noise_ignore": [], "self_poll": False,
                "self_poll_interval_sec": 5},
    "keywords": {"submit": ["提交", "开始执行"],
                 "cancel": ["取消", "停止", "停", "停下", "终止", "别跑了"],
                 "confirm": ["确认", "同意", "确认完成", "上一件任务：确认",
                             "上一件任务:确认", "上一件确认", "通过", "没问题"],
                 "deny": ["拒绝", "不同意", "否决"],
                 "conflict": ["冲突"],
                 # 查询类指令：必须与 submit/cancel 同级识别，否则 RUNNING 时会被
                 # 当追问吞进 followups、IDLE 时被当暂存笔记存起来（#149 实测）。
                  "progress": ["进度", "进展", "状态", "进度？", "进度?",
                               "查进度", "看进度", "任务还在进行吗", "任务还在进行吗？",
                               "这个任务在执行吗", "这个任务在执行吗？", "在执行吗",
                               "在执行吗？", "还在吗", "还在吗？", "任务还在吗"]},
    "group": {"enabled": False},
    "permissions": {"default_mode": "whitelist", "guest_mode": "silent",
                    "superadmin": [],
                    "admins": [], "whitelist": [], "blacklist": [],
                    "file_uploaders": [], "grant_valid_days": 90},
    "execution": {"default_backend": "relay", "local_enabled": False,
                  "local_allowed_for": ["self", "l3"],
                  "l2_backend": "relay", "super_backend": "relay",
                   # 仅用于审批通知文案里告知超管"多久没人管会自动终止"。
                   # **必须与 relay 侧 RELAY_APPROVAL_TIMEOUT_MINUTES 保持一致**
                   # （relay 默认 360，且自 blocked_at 起算）；不一致只是文案数字
                   # 不准，不影响实际清扫——真正执行超时的是 relay 的 sweep_stale。
                    "approval_timeout_minutes": 360,
                    # 全局门禁看板：轮询 relay 所有 pending_approval，覆盖直连 bridge
                    # 任务（它们不经分身会话，此前只有 dashboard 能看见）。
                    "gate_watch_enabled": True,
                    "gate_watch_poll_sec": 60,
                    "gate_watch_limit": 50,
                    # 计划投递绑定：创建计划时把当前会话写入 action.delivery，
                   # 计划任务结束后由 avatar 按绑定目标代投结果/文件。
                    "plan_delivery_enabled": True,
                    "plan_delivery_poll_sec": 15,
                    "plan_delivery_max_attempts": 3,
                    "plan_health_scan_sec": 3600,
                    "plan_health_repeat_sec": 604800},
    "claude": {"permission_mode_super": "bypassPermissions",
               "permission_mode_l2": "default",
               "l2_allowed_tools": ["Read", "Glob", "Grep", "LS"],
               "local_orchestrate_tools": ["Read", "Glob", "Grep", "LS", "Task"],
               "local_orchestrate_disallowed":
                   "Bash Write Edit MultiEdit WebFetch WebSearch",
               "permission_mode_l3": "manual",
               "l3_permission_prompts": "none",
               "l3_disallowed_tools": "Bash Write Edit MultiEdit WebFetch WebSearch",
                "session_ttl_sec": 3600, "reply_timeout_sec": 600,
                 # 重报告/装配/定时播报类任务在 local 通路的短预算：超过即带阶段摘要
                 # 升级 relay，避免 local 白烧 10 分钟后 relay 再从零探索
                 # （2026-10-21 #1173→#1175）。
                 # 2026-10-27：180→300。原 180s 过紧——当日 4 条 heavy 任务全在
                 # 183~186s 触顶（#1474/1492/1524/1541），几乎必然超时升级、local
                 # 白烧。300s 给 heavy 任务真正跑完的机会，仍短于 normal 600s，
                 # 保留「尽早升级」本意。同时收窄两条过贪模式（见下），把纯数据
                 # 处理件（归档/解析/审核/数据表名）剔出 heavy、还给 600s 预算。
                 "heavy_local_timeout_sec": 300,
                 "heavy_local_patterns": [
                     r"(整理|生成|制作|输出|装配|优化).{0,16}(报告|报表|方案|卡片|汇报|台账)",
                     r"项目进度",
                     r"使用情况报告",
                     # 去掉裸 html|模板：会误伤「审核 .html 文档」「加入到模板」等
                     # 数据处理/文档审阅件（#1474/#1524）。报告生成仍由模式1兜住
                     # （生成…报告）；此处只留确定的渲染/质检术语。
                     r"(?i)(render_report|骨架|质检|机检)",
                     # 去掉 报告|报表|计划|任务：会误伤数据表名「每日计划量分配表」
                     # 「每日市场研判报告…价格曲线」等（#1492/#1524/#1541）。定时
                     # 播报仍由 播报|群播|推送|发送|发|提醒 兜住。
                     r"(每日|每天|每周|每月|定时|周期|到点).{0,24}(播报|群播|推送|发送|发|提醒)",
                     r"(播报|群播|推送|发送|发|提醒).{0,24}(每日|每天|每周|每月|定时|周期|到点)",
                     r"(创建|新建|建立|设置|配置|加入|添加|修改|调整|删除|停用|启用|绑定).{0,20}(计划|定时|任务|提醒|播报|群播|推送)",
                     r"(?i)(cron|timer|schedule|broadcast)",
                 ],
                "local_partial_summary_chars": 1500},
    "security": {"l2_lockdown": True,
                 "l2_sandbox_dir": f"{DEFAULT_WORKSPACE}/shared/work",
                 "l2_add_dirs": [],
                 # local 通路可写目录（作用域写）。默认空＝维持纯只读，
                 # 需逐机显式开启；非空时 exec_local 会发 Edit(//abs/**) 规则。
                 "local_write_dirs": [],
                 # local 通路**可删**目录（代删标记 delete_file 的作用域）。
                 # 默认空＝不开放代删。应配得比 local_write_dirs **更窄**：
                 # 只给草稿区（shared/work），正式交付区（shared/report）无 baseline
                 # 快照、删了不可追溯，一律不放进这里。
                 "local_delete_dirs": [],
                 # 受保护的定时计划名单：非 super 一律不得删除/停用/启用。
                 # 为什么需要（2026-09-29）：`_plan_gate` 只校验 tier、不校验归属，
                 # 而 relay 侧 `_plan_owner_ok` 比的是 `principal["name"] ==
                 # plan["owner_name"]`——经分身建的计划 owner_name **全是分身 token
                 # 主体名（鲁大师）**，该检查形同虚设。于是任何 L2 成员说一句
                 # 「删掉计划 每日原油」就能让 07:50 的日报静默消失。用户已明确
                 # **不动 relay schema**（不加 owner_uid 列），故在分身侧用名单止血。
                 # 名单按 plan.name 精确匹配；空列表＝不保护任何计划。
                  "plan_protected": [],
                  "plan_max_per_user": 20,
                  "plan_max_total": 50,
                  "profile_enforce": "strict", "sanitize": "on",
                 "rate_limit_daily_tasks": 100, "reprompt_cooldown_sec": 60,
                 "review_reprompt_cooldown_sec": 1800,
                 "review_reprompt_max": 2,
                 "bot_blocklist": [], "media_max_size_mb": 20,
                 "media_allowed_ext": []},
    "store": {"root": f"{DEFAULT_WORKSPACE}/shared",
              "cli": "/usr/local/bin/store", "least_privilege": True,
              "staging_fallback": True},
    "knowledge": {"local_dir": f"{DEFAULT_WORKSPACE}/shared/knowledge",
                  "wiki_enabled": False, "recall_max_files": 6,
                  "recall_char_budget": 3000,
                  "wiki_env": f"{DEFAULT_WORKSPACE}/shared/secrets/jingbowiki.env",
                  "wiki_kb_id": "", "wiki_sync_scope": "all",
                  "wiki_sync_paths": ""},
    "group_watch": {"enabled": False, "retention_days": 30},
    "monitor": {"heartbeat_sec": 30, "probe_interval_sec": 300,
                "dashboard_enabled": True, "dashboard_port": 8791,
                "alert_to": "", "alert_repeat_hours": 6},
}


def workspace_dir() -> Path:
    return Path(os.environ.get("AVATAR_WORKSPACE", DEFAULT_WORKSPACE))


def state_dir() -> Path:
    """运行态目录 $WORKSPACE/avatar/（与 shared/ 平级，不在 store 扫描根内）。"""
    env = os.environ.get("AVATAR_STATE_DIR", "").strip()
    return Path(env) if env else workspace_dir() / "avatar"


def env_file() -> Path:
    """凭据文件：显式 AVATAR_ENV_FILE > shared/secrets/relay-avatar.env > 状态目录内。"""
    env = os.environ.get("AVATAR_ENV_FILE", "").strip()
    if env:
        return Path(env)
    p = workspace_dir() / "shared" / "secrets" / "relay-avatar.env"
    if p.exists():
        return p
    return state_dir() / "relay-avatar.env"


def load_env(path: Path | None = None) -> dict[str, str]:
    p = path or env_file()
    env: dict[str, str] = {}
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


class Config:
    """policy.toml + env 合并视图。点号分段访问，带默认值兜底。"""

    def __init__(self, policy: dict, env: dict, state: Path):
        self._p = policy
        self.env = env
        self.state = state

    def get(self, section: str, key: str, default: Any = None) -> Any:
        sec = self._p.get(section)
        if isinstance(sec, dict) and key in sec:
            return sec[key]
        d = DEFAULTS.get(section, {})
        if key in d:
            return d[key]
        return default

    def section(self, name: str) -> dict:
        merged = dict(DEFAULTS.get(name, {}))
        s = self._p.get(name)
        if isinstance(s, dict):
            merged.update(s)
        return merged

    # ── 常用派生 ──
    @property
    def avatar_name(self) -> str:
        return str(self.get("identity", "avatar_name", "分身"))

    @property
    def owner_userid(self) -> str:
        return str(self.get("identity", "owner_userid", "") or "")

    @property
    def self_open_id(self) -> str:
        return str(self.get("identity", "self_open_id", "") or "")

    @property
    def dws_profile(self) -> str:
        return str(self.get("identity", "dws_profile", "") or "")

    @property
    def store_root(self) -> Path:
        return Path(str(self.get("store", "root")))

    @property
    def run_dir(self) -> Path:
        return self.state / "run"


def _deep_merge(base: dict, over: dict) -> dict:
    """深合并，且**必须深拷贝**。

    原实现 `out = dict(base)` 是浅拷贝：未被 over 覆盖的嵌套 dict 会直接别名
    base（即模块级 DEFAULTS）。policy.toml 里缺哪个节，哪个节的默认 dict 就被
    共享——之后任何对 cfg._p 的就地修改都会改写全进程默认值，导致同进程内二次
    加载的配置互相串味、测试变成顺序相关（实测：POLICY_TMPL 无 [execution]，
    一个测试设 l2_backend=local 后，后续所有测试的默认后端都变成了 local）。
    """
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(state: Path | None = None, policy_path: Path | None = None) -> Config:
    """加载配置。policy.toml 解析失败不致命：用默认值继续并在审计里留痕。"""
    st = state or state_dir()
    pp = policy_path or (st / "policy.toml")
    data: dict = {}
    if pp.exists():
        try:
            with open(pp, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            print(f"[config] policy.toml 解析失败，用内置默认: {e}", flush=True)
    return Config(_deep_merge(DEFAULTS, data), load_env(), st)
