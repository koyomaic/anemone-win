"""门禁三层分流（2026-09-29）：Tier 1 精度 / Tier 2 自动拒绝 / Tier 3 超时与分类。

背景：13 条历史被拦命令里 3 条是误报（自身 workdir 清理、/tmp 内相对路径），
其余 10 条拦得对。但**全部 13 条都去问了业务用户**——而业务用户无法判断
`ls -laR /opt/team/relay-avatar/` 安不安全，理性反应只有「点确认让它闭嘴」
（门禁形同虚设）或「点拒绝」（#299 卡 76 分钟后整个任务作废，而它的正事早已办完）。

三层分流后：误报自动放行（Tier 1）、侦察类自动拒绝并回注提示让 agent 改道
（Tier 2，客户完全无感）、真正需要人判断的仍走人工但改问超管且用业务语言
（Tier 3，见 relay-avatar 侧测试）。本文件覆盖 relay 侧的 Tier 2 与 Tier 3c。
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from conftest import audit_lines, submit, wait_task
from app.config import (AUTO_DENY_REASONS, RECON_BLOCKED_PATTERNS,
                        _SYSTEM_PROTECT_PATTERNS, block_category)
from app.db import DB
from app.main import sweep_stale


# ── 分类：不跑正则，只比对已算出的原因字符串 ──

def test_block_category_recon():
    for p in RECON_BLOCKED_PATTERNS:
        assert block_category(p) == "recon", p


def test_block_category_rm_and_dws():
    assert block_category("recursive rm outside safe dirs: /x") == "rm"
    assert block_category("dws chat message send 目标为群聊会话（cid…）") == "dws"


def test_block_category_system_and_other():
    for p in _SYSTEM_PROTECT_PATTERNS:
        assert block_category(p) == "system", p
    assert block_category("") == "other"
    assert block_category("某种没见过的原因") == "other"


def test_auto_deny_set_is_exactly_the_recon_group():
    """自动拒绝类＝侦察类，一条不多一条不少。

    多了＝把该给人看的（删共享区/对外发送）自动放过；少了＝继续拿侦察命令去
    骚扰业务用户。系统保护类（git/systemctl/mkfs…）**不得**进自动拒绝集合：
    运维任务可能合法需要它们。
    """
    assert AUTO_DENY_REASONS == frozenset(RECON_BLOCKED_PATTERNS)
    assert not (AUTO_DENY_REASONS & set(_SYSTEM_PROTECT_PATTERNS))


# ── Tier 3c：待审批超时 → failed（此前与 review 共用一条 UPDATE 被置成 done）──

class _NullBus:
    def __init__(self):
        self.seen = []

    def publish(self, tid, **kw):
        self.seen.append({"tid": tid, **kw})


class _NullAudit:
    def __init__(self):
        self.lines = []

    def line(self, *a, **k):
        self.lines.append(a)


class _NullWs:
    def cleanup_terminal(self, t):
        pass


def _force_created_at(db, tid, ts):
    db._run("UPDATE tasks SET created_at = ? WHERE id = ?", (ts, tid))


def _mk(db, status, age_sec, cfg, blocked_cmd="rm -rf /x"):
    tid = db.create_task(user="u", description="t", priority="normal",
                         read_only=False, project="", targets=[])
    db.set(tid, status=status, blocked_cmd=blocked_cmd)
    _force_created_at(db, tid, time.time() - age_sec)
    return tid


def test_sweep_approval_timeout_marks_failed_not_done(relay):
    """核心 bug 修复：待审批超时**不得**被标成 done。

    pending_approval 是"活干到一半被门禁拦住"，标 done 等于把没办完的任务
    报成办完了（#299 若无人手工拒批，24h 后就会静默变成 done）。
    """
    cfg = relay["cfg"]
    db = DB(cfg.db_path)
    tid = _mk(db, "pending_approval", cfg.approval_timeout_minutes * 60 + 120, cfg)
    bus = _NullBus()
    swept = sweep_stale(db, bus, _NullAudit(), cfg, _NullWs())
    assert tid in swept
    t = db.task(tid)
    assert t["status"] == "failed", t
    assert "待审批超过" in t["error"] and t["finished_at"]
    assert "rm -rf /x" in t["error"], "终止原因要带上被拦命令，否则无从排查"
    assert bus.seen and bus.seen[-1]["s"] == "failed"


def test_sweep_review_still_marks_done(relay):
    """review 是"活已干完、等人过目"，超时置 done 无损——这条行为不得被改坏。"""
    cfg = relay["cfg"]
    db = DB(cfg.db_path)
    tid = _mk(db, "review", cfg.stale_timeout_hours * 3600 + 120, cfg)
    sweep_stale(db, _NullBus(), _NullAudit(), cfg, _NullWs())
    t = db.task(tid)
    assert t["status"] == "done" and "超时自动done" in t["error"]


def test_sweep_fresh_approval_untouched(relay):
    """未超时的待审批不得被清扫——否则超管还没来得及看任务就没了。"""
    cfg = relay["cfg"]
    db = DB(cfg.db_path)
    tid = _mk(db, "pending_approval", 60, cfg)
    assert sweep_stale(db, _NullBus(), _NullAudit(), cfg, _NullWs()) == []
    assert db.task(tid)["status"] == "pending_approval"


def test_sweep_approval_uses_shorter_threshold_than_review(relay):
    """两个阈值必须独立：review 24h、待审批 60min。

    此前共用 stale_timeout_hours，于是被拦的任务要干等一整天。
    """
    cfg = relay["cfg"]
    assert cfg.approval_timeout_minutes * 60 < cfg.stale_timeout_hours * 3600
    db = DB(cfg.db_path)
    # 卡在两个阈值之间：待审批该清、review 不该清
    mid = cfg.approval_timeout_minutes * 60 + 60
    a = _mk(db, "pending_approval", mid, cfg)
    r = _mk(db, "review", mid, cfg)
    swept = sweep_stale(db, _NullBus(), _NullAudit(), cfg, _NullWs())
    assert a in swept and r not in swept
    assert db.task(a)["status"] == "failed"
    assert db.task(r)["status"] == "review"


# ── Tier 2：侦察类自动拒绝 + 回注提示续跑（客户完全无感）──

# 首次运行吐一条侦察命令（/proc/self/environ 属 RECON 组）；被自动拒绝续跑后
# （prompt 含"自动拒绝"）老实收尾。sleep 保活：执行器读到 tool_use 行即 kill，
# 但高负载下循环可能被饿到 agent 退出之后，留一点窗口消除竞态。
RECON_AGENT = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "自动拒绝"; then
  printf '%s\\n' '{"type":"text","sessionID":"ses_recon","part":{"type":"text","text":"改道后完成"}}'
  printf '%s\\n' '{"type":"step_finish","sessionID":"ses_recon","part":{"tokens":{"total":10},"cost":0}}'
  exit 0
fi
printf '%s\\n' '{"type":"tool_use","sessionID":"ses_recon","part":{"tool":"bash","title":"Bash","state":{"status":"completed","input":{"command":"cat /proc/self/environ"}}}}'
sleep 10
"""

# 无论续跑多少次都继续侦察：用于验证次数上限后转人工，防死循环烧配额。
STUBBORN_AGENT = """#!/bin/sh
printf '%s\\n' '{"type":"tool_use","sessionID":"ses_bad","part":{"tool":"bash","title":"Bash","state":{"status":"completed","input":{"command":"cat /proc/self/environ"}}}}'
sleep 10
"""


def _fake(relay_factory, tmp_path, script, **kw):
    p = tmp_path / "agent.sh"
    p.write_text(script)
    p.chmod(0o755)
    return relay_factory(agent_bin=str(p), task_timeout=60, **kw)


def test_recon_auto_denied_and_resumed_without_human(relay_factory, tmp_path):
    """侦察类不进 pending_approval：自动拒绝 + 回注提示续跑，任务自己走完。

    这是 Tier 2 的目的——客户侧零审批弹窗，且 agent 拿到"此路不通"的明确反馈后
    能改道完成正事（#299 当年是卡 76 分钟后被拒、正事白做）。
    """
    r = _fake(relay_factory, tmp_path, RECON_AGENT)
    base = r["base"]
    tok = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                       timeout=30).post("/users", json={"name": "faker"}).json()["token"]
    tid = submit(base, tok, "把 Excel 放到 master 目录")["task_id"]
    t = wait_task(base, tok, tid, timeout=90)
    assert t["status"] in ("done", "review"), t
    assert "改道后完成" in (t.get("result") or ""), "续跑未把 agent 的收尾带回来"
    lines = audit_lines(r["cfg"].log_path)
    assert any(f"task={tid}\tuser=faker\tauto_deny" in ln for ln in lines), lines[-6:]
    assert not any(f"task={tid}\tuser=faker\tblock\t" in ln for ln in lines), \
        "侦察类不该走人工审批（block 审计）"


def test_auto_deny_hint_does_not_exempt_the_command(relay_factory, tmp_path):
    """自动拒绝续跑时**必须清空 blocked_cmd**。

    self_resume 分支会把 blocked_cmd 当 exempt_cmd（人工批准后的通行证）。不清空
    等于给这条侦察命令发免死金牌——续跑时直接放行，Tier 2 反而成了绕过门禁的后门。
    """
    r = _fake(relay_factory, tmp_path, STUBBORN_AGENT, auto_deny_max=1)
    base = r["base"]
    tok = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                       timeout=30).post("/users", json={"name": "faker"}).json()["token"]
    tid = submit(base, tok, "探测环境")["task_id"]
    # 上限 1 次：第 1 次自动拒绝续跑（此时 blocked_cmd 被清空），第 2 次转人工
    deadline = time.time() + 90
    t = {}
    while time.time() < deadline:
        t = httpx.get(f"{base}/tasks/{tid}",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
        if t["status"] == "pending_approval":
            break
        time.sleep(0.5)
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"] == "cat /proc/self/environ", "转人工时要留回被拦命令"
    assert t["error"].startswith("blocked:recon:"), t["error"]


def test_non_recon_still_goes_to_human(relay_factory, tmp_path):
    """非侦察类（删共享区等）不得自动拒绝——那正是需要人判断的部分。"""
    script = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "人工已批准"; then
  printf '%s\\n' '{"type":"text","sessionID":"s","part":{"type":"text","text":"ok"}}'
  printf '%s\\n' '{"type":"step_finish","sessionID":"s","part":{"tokens":{"total":1},"cost":0}}'
  exit 0
fi
printf '%s\\n' '{"type":"tool_use","sessionID":"s","part":{"tool":"bash","title":"Bash","state":{"status":"completed","input":{"command":"rm -rf /root"}}}}'
sleep 10
"""
    r = _fake(relay_factory, tmp_path, script)
    base = r["base"]
    tok = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                       timeout=30).post("/users", json={"name": "faker"}).json()["token"]
    tid = submit(base, tok, "删点东西")["task_id"]
    deadline = time.time() + 90
    t = {}
    while time.time() < deadline:
        t = httpx.get(f"{base}/tasks/{tid}",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
        if t["status"] == "pending_approval":
            break
        time.sleep(0.5)
    assert t["status"] == "pending_approval", t
    assert t["error"].startswith("blocked:rm:"), t["error"]
    lines = audit_lines(r["cfg"].log_path)
    assert any(f"task={tid}\tuser=faker\tblock\t" in ln for ln in lines)
    assert not any(f"task={tid}\tuser=faker\tauto_deny" in ln for ln in lines)


# ── 配置卫生：dataclass 默认值 vs from_env 兜底字符串不得漂移 ──
#
# 2026-09-29 实测踩到：approval_timeout_minutes 的 dataclass 默认改成 60.0，但
# from_env 里的 os.environ.get(..., "30") 兜底字符串忘了同步。生产走 from_env()，
# 于是实际生效的是 30 而不是 60——单测全绿（单测直接构造 Settings，走 dataclass
# 默认），只有核验生产配置时才发现。这类重复字面量在 config.py 里有十几处，
# 靠人眼同步必然再犯，故加此测试自动兜住。

# from_env 里由 RELAY_WORKSPACE 派生的路径类字段：干净环境下必然与 dataclass
# 默认不同（后者无默认、必须显式传），不参与比对。
_PATH_DERIVED = {"db_path", "workspace_root", "shared_dir", "task_root",
                 "users_root", "log_path"}


def test_from_env_fallbacks_match_dataclass_defaults(monkeypatch):
    """清空所有 RELAY_* 后，from_env() 必须与 dataclass 默认值逐字段一致。"""
    import dataclasses
    from app.config import Settings

    for k in [k for k in os.environ if k.startswith("RELAY_")]:
        monkeypatch.delenv(k, raising=False)

    via_env = Settings.from_env()
    via_ctor = Settings(
        db_path=via_env.db_path, workspace_root=via_env.workspace_root,
        shared_dir=via_env.shared_dir, task_root=via_env.task_root,
        users_root=via_env.users_root, log_path=via_env.log_path)

    drift = {}
    for f in dataclasses.fields(Settings):
        if f.name in _PATH_DERIVED:
            continue
        a, b = getattr(via_env, f.name), getattr(via_ctor, f.name)
        if a != b:
            drift[f.name] = (a, b)
    assert not drift, (
        "from_env 兜底字符串与 dataclass 默认值漂移（左=from_env，右=dataclass）："
        f"{drift}")


def test_approval_timeout_default_is_60(monkeypatch):
    """显式锁死本次改动涉及的阈值，防再次被兜底字符串悄悄改掉。"""
    for k in [k for k in os.environ if k.startswith("RELAY_")]:
        monkeypatch.delenv(k, raising=False)
    from app.config import Settings
    assert Settings.from_env().approval_timeout_minutes == 60.0
    assert Settings.from_env().auto_deny_max == 2
