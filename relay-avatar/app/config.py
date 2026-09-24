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
                "idle_watchdog_sec": 270, "debounce_sec": 20,
                "followup_debounce_sec": 30, "session_ttl_sec": 1800,
                "poll_sec": 3, "result_max_chars": 3000,
                "ack_emoji": "奋斗", "submit_emoji": "",
                "noise_emoji": "打招呼", "followup_emoji": "OK",
                "ack_text_emotion": {}, "done_text_emotion": {},
                "fail_text_emotion": {}, "use_card": True,
                "card_progress_sec": 30,
                "noise_ignore": [], "self_poll": False,
                "self_poll_interval_sec": 5},
    "keywords": {"submit": ["提交", "开始执行"], "cancel": ["取消"],
                 "confirm": ["确认", "同意"], "deny": ["拒绝", "不同意"],
                 "conflict": ["冲突"]},
    "group": {"enabled": False},
    "permissions": {"default_mode": "whitelist", "guest_mode": "silent",
                    "superadmin": [],
                    "admins": [], "whitelist": [], "blacklist": [],
                    "file_uploaders": [], "grant_valid_days": 90},
    "execution": {"default_backend": "relay", "local_enabled": False,
                  "local_allowed_for": ["self", "l3"],
                  "l2_backend": "relay", "super_backend": "relay"},
    "claude": {"permission_mode_super": "bypassPermissions",
               "permission_mode_l2": "default",
               "l2_allowed_tools": ["Read", "Glob", "Grep", "LS"],
               "local_orchestrate_tools": ["Read", "Glob", "Grep", "LS", "Task"],
               "local_orchestrate_disallowed":
                   "Bash Write Edit MultiEdit WebFetch WebSearch",
               "permission_mode_l3": "manual",
               "l3_permission_prompts": "none",
               "l3_disallowed_tools": "Bash Write Edit MultiEdit WebFetch WebSearch",
               "session_ttl_sec": 3600, "reply_timeout_sec": 600},
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
                 "profile_enforce": "strict", "sanitize": "on",
                 "rate_limit_daily_tasks": 100, "reprompt_cooldown_sec": 60,
                 "bot_blocklist": [], "media_max_size_mb": 20,
                 "media_allowed_ext": []},
    "store": {"root": f"{DEFAULT_WORKSPACE}/shared",
              "cli": "/usr/local/bin/store", "least_privilege": True,
              "staging_fallback": True},
    "knowledge": {"local_dir": f"{DEFAULT_WORKSPACE}/shared/knowledge",
                  "wiki_enabled": False, "recall_max_files": 6,
                  "recall_char_budget": 3000,
                  "wiki_env": f"{DEFAULT_WORKSPACE}/shared/secrets/jingbowiki.env",
                  "wiki_kb_id": ""},
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
