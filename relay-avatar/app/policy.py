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
    def _local_orchestrate(self, c: dict, sec: dict, tier: str) -> dict:
        """local 后端「只读编排」约束集：可委派 subagent（Task 在白名单内），
        但禁写禁执行禁联网，cwd 锁 sandbox、add_dirs 给共享区只读目录。

        两条安全底线：
        ① sandbox 为空则 backend 强制回落 relay —— cons["sandbox"] 此前是死配置
           （全仓无消费点），local 后端一旦没有 cwd 约束就等于放开整个文件系统；
        ② permission_mode 一律用 permission_mode_l2（default），**不用** super 的
           bypassPermissions —— 后者叠加 local 后端即无限制文件系统访问。
           super 要落地写操作请走 relay 门禁（升级不降级）。
        """
        sandbox = str(sec.get("l2_sandbox_dir", "") or "").strip()
        return {"tier": tier,
                "backend": "local" if sandbox else "relay",
                "permission_mode": c.get("permission_mode_l2"),
                "sandbox": sandbox,
                "allowed_tools": list(c.get("local_orchestrate_tools", [])),
                "disallowed_tools": str(c.get(
                    "local_orchestrate_disallowed",
                    "Bash Write Edit MultiEdit WebFetch WebSearch")),
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
