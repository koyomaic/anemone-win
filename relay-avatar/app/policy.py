"""app/policy.py —— 策略核：权限分层 + 动态白名单审批流 + 限流 + 注入清洗。

分层：guest / L3（文件投递）/ L2（白名单）/ L1（管理员）/ super（超管）。
动态白名单钉钉内闭环：guest 发「申请授权/申请传文件」→ super 收通知回
「同意/拒绝/同意传 <工号或姓名>」→ 热生效，默认 90 天过期（修 fenshen P1 无过期）。
动态数据落 run/（whitelist_dynamic.json / pending_requests.json / rate.json），
不进 policy.toml（TOML 不宜频繁改写）。

tier → 约束表（两后端共读，语义等价，升级不降级）：
  super : 全权
  L2    : 沙箱 + 工具白名单 + 禁 Bash（l2_lockdown 默认开）
  L3    : 仅本人 sender 数据区（shared/data/.../<senderId>/），强制 local 后端（P2 前问答未开放）
"""
from __future__ import annotations

import re
import time

from .storage import atomic_write_json

# ── 注入清洗（不可关；覆盖 user_message/quoted/kb/群消息全部外部内容） ──
_BOUNDARY_TAGS = ("</user_message>", "</quoted>", "</data>", "</context>",
                  "<user_message>", "<quoted", "<data>", "<context>")


def sanitize_injected(s: str) -> str:
    """外部内容注入 prompt/任务描述前：打断边界闭合标签结构，保留原文语义。"""
    s = str(s or "")
    for tag in _BOUNDARY_TAGS:
        s = s.replace(tag, tag[0] + "\\" + tag[1:])
    return s


def sanitize_field(s: str, limit: int = 40) -> str:
    """短字段（发信人名/工号）清洗：去换行/控制字符/引号尖括号，截断。"""
    s = re.sub(r'[\x00-\x1f\x7f"<>]', "", str(s or ""))
    return s.strip()[:limit]


# 路由前置判定：命中即直接走 relay 门禁，不进 local。
#
# **2026-09-25 语义反转（阶段二）**：local 已在内核沙箱内放开 Bash，故「需要跑
# 代码」不再是升级理由——pptx/docx/xlsx 等二进制件 local 自己就能出（实测
# shared/venv/bin/python 带 python-pptx + python-docx，沙箱内只读但可执行）。
# 旧口径把这类任务全推给 relay，是 local 禁 Bash 时代的产物：#204 因此白烧
# 10 分 02 秒（13:02:18 进 local → 13:12:20 才 escalate）。
#
# 现在只判「**relay 独有能力**」：门禁审批、dws 外发凭据、root DSN 任意 SQL、
# 沙箱可写范围外的系统操作、联网检索。
# 宁可误判成 relay（代价：慢一点、过门禁），不可误判成 local（代价：白烧数分钟）。
_NEEDS_RELAY = re.compile(
    # ① 外发：local 无 dws 凭据（shared/secrets 与整个 /root 均被内核隐藏），
    #    发不出去。投递能力只在持有 dws 的 avatar 层（```send_file``` 标记）。
    r"(?:发|发送|推送|群发|外发|转发)[^。；;\n]{0,14}"
    r"(?:钉钉|邮件|邮箱|群里|短信|webhook|机器人)"
    # ② 系统运维：目标在沙箱可写范围外（/opt /etc /usr 与 avatar/ 均只读），
    #    且属需门禁留痕的动作
    r"|(?:安装|卸载|部署|上线|下线|重启|停机|扩容|缩容|迁移|systemctl|service\s+\S+\s+)"
    # ③ 任意 SQL / 直接改库：store_task 只有 store_* 表的 SELECT/INSERT/UPDATE 与
    #    tasks(id,status) 列级授权，其余需 relay 侧的 root DSN
    r"|(?:跑|执行|运行|调用)[^。；;\n]{0,20}(?:sql|数据库|建表|改表|删表|drop|truncate)"
    r"|(?:写入|更新|删除|清空|修改)[^。；;\n]{0,10}(?:数据库|数据表|表结构|字段)"
    # ④ 业务动作：需门禁审批留痕
    r"|(?:下单|付款|转账|开票|审批通过|盖章|签合同)"
    # ⑤ 联网检索：local 仍禁 WebFetch/WebSearch，且沙箱网络白名单只放行 claude
    #    代理与 PG，公网实测不可达（IPAddressDeny=any）
    r"|(?:联网|上网|外网|搜一下|搜索一下|抓(?:取)?[^。；;\n]{0,8}网页|爬取|爬虫"
    r"|(?:打开|访问|请求|拉取|抓取)\s*https?://"
    r"|WebSearch|WebFetch)",
    re.I)


def needs_relay_backend(text: str) -> bool:
    """任务是否需要 relay 独有能力（→ 走门禁，不进 local 沙箱通路）。

    只判「relay 才有」的东西：门禁审批、dws 外发凭据、root DSN 任意 SQL、沙箱
    可写范围外的系统操作、联网检索。纯查询、研判、统计、编排，以及出
    HTML/MD/CSV/PPTX/DOCX/XLSX 等件都返回 False（local 沙箱内能胜任）。
    """
    return bool(_NEEDS_RELAY.search(str(text or "")))


class Policy:
    def __init__(self, cfg, storage, audit):
        self.cfg = cfg
        self.st = storage
        self.audit = audit
        p = cfg.section("permissions")
        self.default_mode = str(p.get("default_mode", "whitelist"))
        self.superadmin = {str(x) for x in p.get("superadmin", [])}
        self.admins = {str(x) for x in p.get("admins", [])}
        self.whitelist = {str(x) for x in p.get("whitelist", [])}
        self.blacklist = {str(x) for x in p.get("blacklist", [])}
        self.file_uploaders = {str(x) for x in p.get("file_uploaders", [])}
        self.grant_valid_days = int(p.get("grant_valid_days", 90))
        sec = cfg.section("security")
        self.bot_blocklist = {str(x) for x in sec.get("bot_blocklist", [])}
        self.rate_limit = int(sec.get("rate_limit_daily_tasks", 100))
        self._dyn_file = storage.run / "whitelist_dynamic.json"
        self._pend_file = storage.run / "pending_requests.json"
        self._rate_file = storage.run / "rate.json"

    # ── 名单读写 ──
    def _load(self, p, default):
        return self.st.load_json(p, default)

    def _save(self, p, d):
        self.st.save_json(p, d)

    # ── 判定 ──
    def is_bot_blocked(self, open_id: str) -> bool:
        return bool(open_id) and open_id in self.bot_blocklist

    def get_level(self, uid: str) -> str:
        """BLOCKED / super / L1 / L2 / L3 / NONE（guest）。"""
        uid = str(uid or "")
        if not uid:
            return "NONE"
        if uid in self.blacklist:
            return "BLOCKED"
        if uid in self.superadmin:
            return "super"
        if uid in self.admins:
            return "L1"
        if uid in self.whitelist:
            return "L2"
        dyn = self._dyn_valid(uid)
        if dyn:
            t = dyn.get("tier")
            return t if t in ("L1", "L3") else "L2"
        if uid in self.file_uploaders:
            return "L3"
        return "NONE"

    def allowed(self, uid: str) -> bool:
        return self.get_level(uid) in ("super", "L1", "L2")

    _ORDER = {"NONE": 0, "L3": 1, "L2": 2, "L1": 3, "super": 4}

    def _static_level(self, ident: str) -> str:
        """单个标识（userId/openId/显示名）只查静态名单。"""
        ident = str(ident or "")
        if not ident:
            return "NONE"
        if ident in self.blacklist:
            return "BLOCKED"
        if ident in self.superadmin:
            return "super"
        if ident in self.admins:
            return "L1"
        if ident in self.whitelist:
            return "L2"
        if ident in self.file_uploaders:
            return "L3"
        return "NONE"

    def level_of(self, uid: str, open_id: str = "", sender: str = "") -> str:
        """运行时定级：动态授权只认 userId；静态名单兼容 openId/显示名匹配
        （im-bridge 时代 config.json 名单语义），取最高档，任一标识拉黑即拉黑。"""
        lvls = [self.get_level(uid), self._static_level(open_id),
                self._static_level(sender)]
        if "BLOCKED" in lvls:
            return "BLOCKED"
        return max(lvls, key=lambda x: self._ORDER.get(x, 0))

    def is_superadmin(self, uid: str) -> bool:
        return str(uid) in self.superadmin

    def is_file_uploader(self, uid: str) -> bool:
        return self.get_level(uid) == "L3"

    def sender_data_dir(self, uid: str):
        """L3 隔离区 = 本人 sender 数据区（可跨多个日期层，由 file_inbox 枚举）。"""
        return self.st.data_dir(uid)

    # ── tier → 约束表（两后端共读） ──
    def _sandbox_spec(self, sec: dict) -> dict:
        """local 内核沙箱规格（systemd-run 瞬态 unit 的属性来源）。

        为什么 local 必须有内核层：relay-avatar.service 无任何沙箱指令且以 root
        运行，而 claude 的 Bash/路径权限规则是对命令字符串做**词法**匹配——实测
        路径 deny 规则可被 perl/node/dd/python3 绕过，且能写入"受保护"目录。
        黑名单要成立必须有「所有访问必经」的检查点，无中介环境下只有内核 mount
        namespace 能充当（syscall 必经）。同一份路径名单：应用层 6 拦 5 绕，
        内核层 7/7 全拦。

        enabled=False 或 rw/hide 为空时，exec_local 必须 **fail-closed 回落
        relay**，绝不回落无沙箱 local。
        """
        rw = [str(x).strip() for x in (sec.get("local_sandbox_rw") or [])
              if str(x).strip()]
        hide = [str(x).strip() for x in (sec.get("local_sandbox_hide") or [])
                if str(x).strip()]
        # 网络白名单（IP/CIDR）。非空即施加 IPAddressDeny=any + IPAddressAllow=列表。
        # 为什么必须收：沙箱只管「能读到什么」，不管「能不能发出去」。Bash 放开后
        # 二者叠加才成外泄通道——实测 python3 urllib 的外发尝试**完全不被权限层
        # 拦截**（只因目标端口无监听才失败）。必须同时放行 claude 代理与 PG，
        # 否则 claude 起不来、store 连不上库。
        net = [str(x).strip() for x in (sec.get("local_sandbox_net_allow") or [])
               if str(x).strip()]
        on = bool(sec.get("local_sandbox", False)) and bool(rw) and bool(hide)
        return {"enabled": on, "rw": rw, "hide": hide, "net_allow": net}

    def sandbox_spec(self) -> dict:
        """local 内核沙箱规格（公开入口，供 exec_local.available() 做 fail-closed
        门禁：配置要求沙箱而规格不完整/工具缺失时，local 整体不可用 → 路由改投
        relay，绝不降级成无沙箱 local）。"""
        return self._sandbox_spec(self.cfg.section("security"))

    def _local_orchestrate(self, c: dict, sec: dict, tier: str) -> dict:
        """local 后端「只读编排」约束集：可委派 subagent（Task 在白名单内），
        但禁执行禁联网，cwd 锁 sandbox、add_dirs 给共享区只读目录。

        两条安全底线：
        ① sandbox 为空则 backend 强制回落 relay —— cons["sandbox"] 此前是死配置
           （全仓无消费点），local 后端一旦没有 cwd 约束就等于放开整个文件系统；
        ② permission_mode 一律用 permission_mode_l2（default），**不用** super 的
           bypassPermissions —— 后者叠加 local 后端即无限制文件系统访问。
           super 要落地写操作请走 relay 门禁（升级不降级）。

        作用域写（security.local_write_dirs 非空时开启）：
        放开指定目录的 Edit/Write 写入。实测依据（2026-09-24，claude 本机验证）：
        · 文件写权限只认 `Edit(//绝对路径/**)` 作用域规则；裸 `Write`/`Edit`
          放进 --allowedTools **不生效**（claude 明示：只有 Edit(path) 规则被
          文件权限检查匹配，且 Edit 规则覆盖所有文件编辑工具）；
        · `--disallowedTools` **压过**作用域 allow，故开写时必须把
          Write/Edit/MultiEdit 从 disallowed 里摘掉（MultiEdit 已非有效工具名）；
        · 配 `--permission-prompts none` 时越界写**自动拒绝**（fail-closed）。

        阶段二（2026-09-25）起 Bash 在 local 沙箱内可用，于是本项与
        security.local_sandbox_rw 构成**两层作用域**：Edit/Write 受本项管辖，
        Bash 落盘受内核 ReadWritePaths 管辖。本项刻意保持窄（report/+work/），
        因为它**同时是 ```send_file``` 代投的作用域**（main.py 直接读这一项），
        放宽到 shared/data 会让代投能取任意 sender 目录的件外发、破坏 L3 隔离。
        要放宽内核可写范围请改 local_sandbox_rw，不要改这一项。
        """
        sandbox = str(sec.get("l2_sandbox_dir", "") or "").strip()
        write_dirs = [str(x).strip() for x in (sec.get("local_write_dirs") or [])
                      if str(x).strip()]
        # 可删作用域**独立配置且应更窄**：只给草稿区。正式交付区无 baseline 快照、
        # 删了不可追溯，不放进这里（要删走 relay 门禁）。
        delete_dirs = [str(x).strip() for x in (sec.get("local_delete_dirs") or [])
                       if str(x).strip()]
        dis = str(c.get("local_orchestrate_disallowed",
                        "Bash Write Edit MultiEdit WebFetch WebSearch"))
        if write_dirs:
            # 作用域写已开：Write/Edit 改由 Edit(//abs/**) allow 规则管辖，
            # 留在 disallowed 会把作用域授权整个压掉。Bash/WebFetch/WebSearch 保留。
            dis = " ".join(t for t in dis.split()
                           if t not in ("Write", "Edit", "MultiEdit"))
        return {"tier": tier,
                "backend": "local" if sandbox else "relay",
                "permission_mode": c.get("permission_mode_l2"),
                "sandbox": sandbox,
                "allowed_tools": list(c.get("local_orchestrate_tools", [])),
                "disallowed_tools": dis,
                "write_dirs": write_dirs,
                "delete_dirs": delete_dirs,
                "sbx": self._sandbox_spec(sec),
                "add_dirs": [str(x) for x in (sec.get("l2_add_dirs") or [])]}

    def constraints(self, uid: str) -> dict:
        """返回执行约束（exec_relay / exec_local 各自映射，语义等价）。

        backend 取值：relay=走 /tasks 门禁体系（默认）；local=本机 claude 只读
        编排（可委派 subagent，需 execution.local_enabled=true 且 sandbox 非空）。
        """
        c = self.cfg.section("claude")
        sec = self.cfg.section("security")
        lvl = self.get_level(uid)
        if lvl == "super":
            if str(self.cfg.get("execution", "super_backend", "relay")) == "local":
                return self._local_orchestrate(c, sec, "super")
            return {"tier": "super", "backend": "relay",
                    "permission_mode": c.get("permission_mode_super"),
                    "sandbox": "", "allowed_tools": [],
                    "disallowed_tools": "", "add_dirs": []}
        if lvl in ("L1", "L2"):
            if str(self.cfg.get("execution", "l2_backend", "relay")) == "local":
                return self._local_orchestrate(c, sec, "L2")
            lockdown = bool(sec.get("l2_lockdown", True))
            return {"tier": "L2",
                    "backend": "relay",   # L2 主执行走 relay 门禁；local 需显式开
                    "permission_mode": c.get("permission_mode_l2"),
                    "sandbox": str(sec.get("l2_sandbox_dir", "")) if lockdown else "",
                    "allowed_tools": list(c.get("l2_allowed_tools", [])),
                    "disallowed_tools": "Bash" if lockdown else "",
                    "add_dirs": []}
        if lvl == "L3":
            return {"tier": "L3", "backend": "local",   # L3 强制 local（硬隔离）
                    "permission_mode": c.get("permission_mode_l3"),
                    "permission_prompts": c.get("l3_permission_prompts"),
                    "sandbox": "", "allowed_tools": [],
                    "disallowed_tools": c.get("l3_disallowed_tools"),
                    "sbx": self._sandbox_spec(sec),
                    "add_dirs": ["<sender_data_dirs>"]}
        return {"tier": "guest", "backend": "none", "permission_mode": "",
                "sandbox": "", "allowed_tools": [], "disallowed_tools": "",
                "add_dirs": []}

    # ── 动态白名单 + 审批流 ──
    def load_dynamic_wl(self) -> dict:
        return self._load(self._dyn_file, {})

    def _dyn_valid(self, uid: str) -> dict:
        rec = self.load_dynamic_wl().get(str(uid))
        if not rec:
            return {}
        exp = rec.get("expires")
        if exp and time.time() > exp:
            return {}
        return rec

    def dyn_expired(self, uid: str) -> dict:
        rec = self.load_dynamic_wl().get(str(uid))
        if not rec:
            return {}
        exp = rec.get("expires")
        return rec if (exp and time.time() > exp) else {}

    def record_pending(self, uid: str, name: str, title: str = "",
                       openid: str = "", conv_id: str = ""):
        d = self._load(self._pend_file, {})
        d[str(uid)] = {"name": name, "title": title, "openid": openid,
                       "conv": conv_id, "ts": time.time()}
        self._save(self._pend_file, d)

    def find_pending_all(self, target_key: str) -> list:
        """按 userId 或姓名找待审申请（重名返回多条，由超管用工号消歧）。"""
        d = self._load(self._pend_file, {})
        if target_key in d:
            return [(target_key, d[target_key])]
        return [(uid, info) for uid, info in d.items()
                if info.get("name") == target_key]

    def grant(self, uid: str, name: str, tier: str = "L2",
              by_name: str = "", valid_days: int | None = None):
        wl = self.load_dynamic_wl()
        now = time.time()
        days = valid_days if valid_days is not None else self.grant_valid_days
        wl[str(uid)] = {"name": name, "tier": tier, "by": by_name, "ts": now,
                        "expires": now + days * 86400}
        self._save(self._dyn_file, wl)
        self.revoke_pending(uid)

    def revoke_pending(self, uid: str):
        d = self._load(self._pend_file, {})
        if str(uid) in d:
            del d[str(uid)]
            self._save(self._pend_file, d)

    def load_pending(self) -> dict:
        return self._load(self._pend_file, {})

    def revoke_dynamic(self, uid: str) -> bool:
        """撤销动态白名单（热读，立即生效）。返回是否确有该记录。"""
        wl = self.load_dynamic_wl()
        rec = wl.pop(str(uid), None)
        if rec is None:
            return False
        self._save(self._dyn_file, wl)
        return True

    def has_pending(self) -> bool:
        return bool(self._load(self._pend_file, {}))

    def parse_approval(self, text: str):
        """超管审批指令解析：('grant'|'grant_l3'|'deny', target) 或 None。"""
        t = (text or "").strip().lstrip("/／").strip()
        m = re.match(r"^(同意传|批准传|通过传)\s*(.+)$", t)
        if m:
            return "grant_l3", m.group(2).strip()
        m = re.match(r"^(同意|通过|批准|拒绝|驳回)\s*(.+)$", t)
        if m:
            kind = "grant" if m.group(1) in ("同意", "通过", "批准") else "deny"
            return kind, m.group(2).strip()
        return None

    # ── 限流（每用户日任务数） ──
    def rate_hit(self, uid: str) -> bool:
        """记一次任务并判断是否超限。"""
        if self.rate_limit <= 0:
            return False
        today = time.strftime("%Y%m%d")
        d = self._load(self._rate_file, {})
        if d.get("date") != today:
            d = {"date": today, "counts": {}}
        c = d.setdefault("counts", {})
        c[str(uid)] = int(c.get(str(uid), 0)) + 1
        self._save(self._rate_file, d)
        if c[str(uid)] > self.rate_limit:
            self.audit.log("rate_limited", uid,
                           f"日任务数超限 {c[str(uid)]}/{self.rate_limit}")
            return True
        return False
