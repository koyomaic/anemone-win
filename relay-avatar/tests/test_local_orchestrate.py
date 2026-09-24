"""tests/test_local_orchestrate.py —— local「只读编排」通路（tier→backend 可配）。

覆盖四条安全底线：
① 默认配置行为完全不变（relay 后端、白名单无 Task）——回归保护；
② backend=local 时工具白名单含 Task（能委派 subagent）且禁写禁执行禁联网齐全；
③ sandbox 为空/不存在一律回落或拒绝，绝不让 local 后端裸奔；
④ super 走 local 时不得用 bypassPermissions。
另覆盖：升级不降级（越权→relay 且不死循环）、LOCAL 态不被 _poll_sessions 误重置、
LOCAL 态收到「取消」不得重置会话态（否则卡片永远停在处理中）。
"""
import os
import re
import time

import pytest

from app.exec_local import ExecLocal
from app.main import Avatar


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


def local_ready(cfg, shared, **kw):
    """打开 local 只读编排所需的最小配置，返回 sandbox 目录。"""
    sb = shared / "work"
    sb.mkdir(parents=True, exist_ok=True)
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local", **kw)
    setcfg(cfg, "security", l2_sandbox_dir=str(sb),
           l2_add_dirs=[str(shared / "knowledge")])
    return sb


# ── A. policy.constraints：tier → backend 与工具约束 ──

def test_l2_default_backend_relay(cfg, storage, audit, policy):
    """默认（未显式开 local）行为完全不变。"""
    cons = policy.constraints("200")
    assert cons["backend"] == "relay" and cons["tier"] == "L2"
    assert "Task" not in cons["allowed_tools"]
    assert cons["disallowed_tools"] == "Bash"      # l2_lockdown 默认开
    assert cons["add_dirs"] == []


def test_super_default_relay_bypass_unchanged(cfg, storage, audit, policy):
    cons = policy.constraints("18434")
    assert cons["backend"] == "relay" and cons["tier"] == "super"
    assert cons["permission_mode"] == "bypassPermissions"


def test_l2_local_backend_enables_task(cfg, storage, audit, policy, shared):
    sb = local_ready(cfg, shared)
    cons = policy.constraints("200")
    assert cons["backend"] == "local"
    assert "Task" in cons["allowed_tools"]         # 能委派 subagent
    assert "Read" in cons["allowed_tools"]
    dis = cons["disallowed_tools"]
    for t in ("Bash", "Write", "Edit", "MultiEdit", "WebFetch", "WebSearch"):
        assert t in dis                            # 只读：禁写禁执行禁联网
    assert cons["sandbox"] == str(sb)
    assert str(shared / "knowledge") in cons["add_dirs"]


def test_l1_also_gets_local(cfg, storage, audit, policy, shared):
    """L1（admins）与 L2 共用约束表，同样可走 local 编排。"""
    local_ready(cfg, shared)
    assert policy.constraints("100")["backend"] == "local"


def test_local_falls_back_without_sandbox(cfg, storage, audit, policy, shared):
    """安全底线③：sandbox 为空则强制回落 relay。"""
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local")
    setcfg(cfg, "security", l2_sandbox_dir="")
    assert policy.constraints("200")["backend"] == "relay"


def test_super_local_never_bypass(cfg, storage, audit, policy, shared):
    """安全底线④：super 走 local 也不得 bypassPermissions。"""
    local_ready(cfg, shared, super_backend="local")
    cons = policy.constraints("18434")
    assert cons["backend"] == "local" and cons["tier"] == "super"
    assert cons["permission_mode"] != "bypassPermissions"
    assert "Task" in cons["allowed_tools"]
    assert "Write" in cons["disallowed_tools"]


def test_l3_unaffected_by_local_backend(cfg, storage, audit, policy, shared):
    """L3 仍强制 local 硬隔离，不受 l2_backend 影响。"""
    local_ready(cfg, shared)
    cons = policy.constraints("300")
    assert cons["tier"] == "L3" and cons["backend"] == "local"
    assert cons["permission_mode"] == "manual"


def test_guest_never_local(cfg, storage, audit, policy, shared):
    local_ready(cfg, shared)
    cons = policy.constraints("555")               # 名单外
    assert cons["tier"] == "guest" and cons["backend"] == "none"


# ── B. exec_local.run_task ──

def make_ex(cfg, storage, audit, policy):
    ex = ExecLocal(cfg, storage, audit, policy)
    ex.bin = "/bin/true"                           # 强制 available()，不真跑 claude
    return ex


def test_run_task_escalates_when_disabled(cfg, storage, audit, policy):
    ex = make_ex(cfg, storage, audit, policy)
    assert not ex.available()
    assert ex.run_task("200", "干活")["escalate"] is True


def test_run_task_escalates_when_backend_relay(cfg, storage, audit, policy):
    """local 总开关开了但 l2_backend 仍是 relay → 不接管，升级 relay。"""
    setcfg(cfg, "execution", local_enabled=True)
    ex = make_ex(cfg, storage, audit, policy)
    assert ex.available()
    assert ex.run_task("200", "干活")["escalate"] is True


def test_run_task_refuses_missing_sandbox(cfg, storage, audit, policy, shared):
    """安全底线③第二道：sandbox 配了但目录不存在 → 拒绝并升级。"""
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local")
    setcfg(cfg, "security", l2_sandbox_dir=str(shared / "不存在的目录"))
    ex = make_ex(cfg, storage, audit, policy)
    assert ex.run_task("200", "干活")["escalate"] is True


def test_run_task_uses_sandbox_cwd_and_task_flag(cfg, storage, audit, policy,
                                                 shared, monkeypatch):
    sb = local_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    seen = {}

    def fake_run(prompt, conv_key=None, cwd=None, flags=None, uid="",
                 on_step=None, sbx=None):
        seen.update(prompt=prompt, conv_key=conv_key, cwd=cwd, flags=flags,
                    sbx=sbx)
        return {"text": "本机结论", "denied": False}

    monkeypatch.setattr(ex, "_run", fake_run)
    r = ex.run_task("200", "统计门店日销")
    assert r == {"text": "本机结论", "escalate": False}
    assert seen["cwd"] == str(sb)                  # cwd 锁 sandbox
    assert seen["conv_key"] == "task:200"
    f = seen["flags"]
    assert "Task" in f[f.index("--allowedTools") + 1]
    assert "--add-dir" in f and str(shared / "knowledge") in f
    assert "Write" in f[f.index("--disallowedTools") + 1]
    assert "统计门店日销" in seen["prompt"]
    assert "只读" in seen["prompt"] and "Task" in seen["prompt"]


def test_run_task_denied_escalates(cfg, storage, audit, policy, shared,
                                   monkeypatch):
    """安全底线：越权被拒 → 升级 relay 门禁（升级不降级）。"""
    local_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run",
                        lambda prompt, **kw: {"text": "", "denied": True})
    assert ex.run_task("200", "越权请求")["escalate"] is True


def test_run_task_rejects_l3(cfg, storage, audit, policy, shared, monkeypatch):
    """L3 有自己的硬隔离通路，不得走团队任务编排。"""
    local_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run",
                        lambda prompt, **kw: {"text": "不该发生", "denied": False})
    assert ex.run_task("300", "干活")["escalate"] is True


# ── C. main 路由与会话态 ──

class MiniDws:
    def __init__(self):
        self.sent = []
        self.reacts = []
        self.cards = []

    def resolve_userid(self, open_id, sender, cache):
        return {"sid200": "200", "sid18434": "18434",
                "sid300": "300", "sid555": "555"}.get(open_id, "")

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.sent.append((target, f"[file]{path}"))
        return {"ok": True}

    def react(self, cid, mid, emoji):
        self.reacts.append(emoji)
        return {"ok": True}

    def react_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def remove_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def card_create(self, target):
        self.cards.append(target)
        return "BIZ1"

    def card_update(self, biz, content, flow_status):
        self.sent.append((biz, content))
        return True

    def mark_read(self, *a, **k):
        return {"ok": True}


class FakeExecLocal:
    name = "local"

    def __init__(self, result=None, available=True):
        self.calls = []
        self.result = ({"text": "本机只读结论", "escalate": False}
                       if result is None else result)
        self._av = available

    def available(self):
        return self._av

    def run_task(self, uid, text, kb="", on_step=None):
        self.calls.append({"uid": uid, "text": text})
        return dict(self.result)


class FakeExec:
    name = "relay"

    def __init__(self):
        self.submitted = []
        self.shadows = []          # local 影子记录
        self.local_status = []     # 影子记录终态回报

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk"):
        tid = len(self.submitted) + 1
        self.submitted.append({"tid": tid, "desc": desc, "uid": uid})
        return {"task_id": tid, "status": "queued"}

    def create_local_record(self, desc, uid=""):
        tid = 900 + len(self.shadows) + 1
        self.shadows.append({"tid": tid, "desc": desc, "uid": uid})
        return {"task_id": tid, "status": "running", "backend": "local"}

    def report_local_status(self, tid, status, uid="", result="", error=""):
        self.local_status.append({"tid": tid, "status": status, "uid": uid,
                                  "result": result, "error": error})
        return {"task_id": tid, "status": status, "already_terminal": False}

    def poll(self, tid):
        return {"id": tid, "status": "queued"}

    def approve(self, *a, **k):
        return {"ok": True}

    def confirm(self, *a, **k):
        return {"ok": True}

    def cancel(self, *a, **k):
        return {"ok": True}


@pytest.fixture
def av(cfg):
    a = Avatar(cfg, dry=True)
    a.dws = MiniDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakeExec()
    return a


def sess_of(a, sid="sid200", text="统计门店日销", n="1"):
    a.on_event({"event_type": "user_im_message_receive_o2o_all",
                "content": text, "sender": "张三",
                "sender_open_dingtalk_id": sid, "message_id": f"m{n}",
                "conversation_id": f"c{n}", "event_id": f"e{n}"})
    return a.sess.sessions["o:" + sid]


def wait_local(s, timeout=4.0):
    """等后台 worker 写出结果（fake 立即返回，正常几毫秒）。"""
    end = time.time() + timeout
    while time.time() < end and not s.get("local_result"):
        time.sleep(0.01)
    return s.get("local_result")


def test_flush_routes_to_local(av, cfg, shared):
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    # task_id 现在是**影子记录**号（只登记不派发），不再是 None；
    # 但仍未走 relay 执行通路（submitted 为空）。
    assert s["state"] == "LOCAL" and s["task_id"] == 901
    assert av.exec_relay.submitted == []           # 没走 relay 执行
    assert len(av.exec_relay.shadows) == 1         # 建了影子记录
    assert wait_local(s) is not None
    av._drain_local()
    assert s["state"] == "IDLE"
    assert av.exec_local.calls[0]["uid"] == "200"


def test_local_escalate_falls_back_to_relay(av, cfg, shared):
    """升级不降级：local 被拒 → relay 门禁，且不再二次进 local（无死循环）。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "", "escalate": True})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert len(av.exec_local.calls) == 1
    assert len(av.exec_relay.submitted) == 1
    assert s["state"] == "RUNNING" and s["task_id"] == 1


def test_local_not_used_when_backend_relay(av, cfg, shared):
    """默认配置下 local 通路完全不介入（回归保护）。"""
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    assert av.exec_local.calls == []
    assert len(av.exec_relay.submitted) == 1
    assert s["state"] == "RUNNING"


def test_local_not_used_when_unavailable(av, cfg, shared):
    """l2_backend=local 但 local_enabled=false → 直接走 relay，不报错。"""
    sb = shared / "work"
    sb.mkdir(parents=True, exist_ok=True)
    setcfg(cfg, "execution", local_enabled=False, l2_backend="local")
    setcfg(cfg, "security", l2_sandbox_dir=str(sb))
    av.exec_local = FakeExecLocal(available=False)
    s = sess_of(av)
    av._flush(s, uid="200")
    assert av.exec_local.calls == []
    assert len(av.exec_relay.submitted) == 1


def test_drain_local_replies_text(av, cfg, shared):
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "结论正文", "escalate": False})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert any("结论正文" in c for _, c in av.dws.sent)


def test_drain_local_empty_text_escalates(av, cfg, shared):
    """空结果视同失败 → 升级 relay，不回一句空话。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "  ", "escalate": False})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert len(av.exec_relay.submitted) == 1


def test_local_state_not_reset_by_poll(av, cfg, shared):
    """LOCAL 态无 task_id，但不得被 _poll_sessions 当 RUNNING 重置。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    av._poll_sessions()
    assert s["state"] == "LOCAL"


def test_cancel_during_local_keeps_state(av, cfg, shared):
    """LOCAL 态收到「取消」不得重置会话态，否则卡片永远停在处理中。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    av._on_message("o:sid200", s, "取消", "200")
    assert s["state"] == "LOCAL"
    assert any("不支持中途取消" in c for _, c in av.dws.sent)


def test_no_concurrent_local_task(av, cfg, shared):
    """同一会话已有本机任务在跑时不得并发第二个。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL"
    s["buffer"] = ["再来一个"]
    av._flush(s, uid="200")                      # 应被 _try_local_task 拒接
    assert len(av.exec_local.calls) <= 1


def test_super_can_use_local(av, cfg, shared):
    local_ready(cfg, shared, super_backend="local")
    av.exec_local = FakeExecLocal()
    s = sess_of(av, sid="sid18434")
    av._flush(s, uid="18434")
    assert s["state"] == "LOCAL"
    assert wait_local(s) is not None               # 后台线程是异步的，必须等
    assert av.exec_local.calls[0]["uid"] == "18434"


def test_defaults_not_aliased_by_config(cfg):
    """回归：cfg._p 的嵌套 dict 不得别名模块级 DEFAULTS。

    别名会让任何就地修改污染全进程默认值，使测试顺序相关（本文件其余用例
    曾因此在整轮运行时集体失败、单独运行却通过）。
    """
    from app import config as C
    cfg._p.setdefault("execution", {})["l2_backend"] = "local"
    cfg._p.setdefault("claude", {})["permission_mode_l2"] = "HACKED"
    assert C.DEFAULTS["execution"]["l2_backend"] == "relay"
    assert C.DEFAULTS["claude"]["permission_mode_l2"] == "default"
    c2 = C.Config(C._deep_merge(C.DEFAULTS, {}), {}, cfg.state)
    assert c2.get("execution", "l2_backend") == "relay"
    assert c2.get("claude", "permission_mode_l2") == "default"


# ── D. LOCAL 态重启/超时清理（2026-09-24 事故回归） ──
# 事故：11:03 服务重启打断 10:58 起跑的 local 任务。local 通路无 relay task_id，
# daemon 线程随进程消失，而 state="LOCAL" 已落盘 → _drain_local 永远等不到
# local_result，_try_local_task 又因 state=="LOCAL" 拒接新任务：会话永久砖化，
# 用户既拿不到结果、也发不出下一条，且收不到任何失败回执。

def stale_local(av, sid="sid200", text="统计门店日销", uid="200", desc=None,
                deadline=None, reap=0):
    """构造上一进程遗留的 LOCAL 态会话（模拟重启后自 state.json 恢复）。"""
    s = sess_of(av, sid=sid, text=text)
    s.update(state="LOCAL", task_id=None, local_result=None, uid=uid,
             local_desc=text if desc is None else desc,
             local_started=time.time() - 300,
             local_deadline=(time.time() + 600 if deadline is None
                             else deadline),
             buffer=[], followups=[], deadline=0)
    if reap:
        s["local_reap_count"] = reap
    av.dws.sent.clear()
    return s


def test_try_local_task_persists_recovery_metadata(av, cfg, shared):
    """起跑即落恢复元数据（desc/started/deadline）——重启才有救。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL"
    assert s["local_desc"] == "统计门店日销"
    assert s["local_started"] > 0
    assert s["local_deadline"] > s["local_started"]


def test_stale_local_reaped_and_resubmitted_on_restart(av, cfg, shared):
    """重启遗留的 LOCAL 态必须被清理并改投 relay（持久、可轮询、跨重启存活）。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av)
    av._reap_stale_local()
    assert s["state"] == "RUNNING"
    assert len(av.exec_relay.submitted) == 1
    d = av.exec_relay.submitted[0]["desc"]
    assert d.startswith("统计门店日销")                 # 用户原文在最前
    assert "投递铁律" in d and "send_file" in d          # 附了代投口径（防自发 dws）
    assert s["task_id"] == 1
    assert s["local_reap_count"] == 1
    assert any("中断" in c for _, c in av.dws.sent)      # 如实回执，不静默


def test_stale_local_without_desc_asks_resend(av, cfg, shared):
    """升级兼容：旧版落盘无 local_desc 时不瞎猜内容，请用户重发。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av, desc="")
    av._reap_stale_local()
    assert s["state"] == "IDLE"
    assert av.exec_relay.submitted == []
    assert any("再发一次" in c for _, c in av.dws.sent)


def test_reap_guard_stops_after_two_attempts(av, cfg, shared):
    """反复中断（崩溃循环）不得无限自动重投，避免任务风暴。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av, reap=2)
    av._reap_stale_local()
    assert s["state"] == "IDLE"
    assert av.exec_relay.submitted == []
    assert any("不再自动重试" in c for _, c in av.dws.sent)


def test_local_deadline_timeout_reaps(av, cfg, shared):
    """同进程内 worker 卡死/异常退出：超硬截止即判失败并改投 relay。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av, deadline=time.time() - 1)
    av._drain_local()
    assert s["state"] == "RUNNING"
    assert len(av.exec_relay.submitted) == 1


def test_local_within_deadline_keeps_waiting(av, cfg, shared):
    """回归保护：未超截止的在跑 local 任务不得被误清理。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av, deadline=time.time() + 600)
    av._drain_local()
    assert s["state"] == "LOCAL"
    assert av.exec_relay.submitted == []


def test_session_not_bricked_after_reap(av, cfg, shared):
    """清理后会话必须能重新接活（原缺陷：LOCAL 态永久拒接新任务）。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av)
    av._reap_stale_local()
    assert s["state"] == "RUNNING"
    s.update(state="IDLE", task_id=None, buffer=["再来一个"])   # 模拟 relay 任务收尾
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL"
    assert wait_local(s) is not None               # 后台线程异步，必须等
    assert len(av.exec_local.calls) == 1


def test_reap_count_cleared_on_success(av, cfg, shared):
    """成功收尾必须清零 local_reap_count，否则累计 2 次后永久禁用自动重投。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "结论正文", "escalate": False})
    s = sess_of(av)
    s["local_reap_count"] = 1                      # 曾被清理过一次
    av._flush(s, uid="200")
    assert wait_local(s) is not None
    av._drain_local()
    assert s["state"] == "IDLE"
    assert s["local_reap_count"] == 0
    assert s["local_desc"] == "" and s["local_deadline"] == 0


# ── E. 作用域写（local 出件）+ needs_relay_backend 路由前置判定 ──
# 实测依据（2026-09-24，claude 本机验证）：
#   · 文件写权限只认 Edit(//绝对路径/**)；裸 Write/Edit 放进 --allowedTools 不生效
#   · --disallowedTools 压过作用域 allow（Write 会被整个禁用）
#   · --permission-prompts none 时越界写自动拒绝（fail-closed）
#   · Bash 在 disallowed 内即整体不可用（含子代理）
# 2026-09-25 阶段二语义反转：local 沙箱内已放开 Bash，故「出 pptx/docx/xlsx、跑
# python 脚本」**不再**是升级理由；只判 relay 独有能力（外发凭据/任意 SQL/系统运维/
# 业务动作/联网检索）。

from app.policy import needs_relay_backend            # noqa: E402


def write_ready(cfg, shared, dirs=("report", "work")):
    """开 local + 作用域写，返回可写目录绝对路径列表。"""
    sb = local_ready(cfg, shared)
    wd = []
    for d in dirs:
        p = shared / d
        p.mkdir(parents=True, exist_ok=True)
        wd.append(str(p))
    setcfg(cfg, "security", local_write_dirs=wd)
    return sb, wd


def test_write_dirs_default_empty_keeps_readonly(cfg, storage, audit, policy,
                                                 shared):
    """回归：未配 local_write_dirs 时行为完全不变（仍禁 Write/Edit）。"""
    local_ready(cfg, shared)
    cons = policy.constraints("200")
    assert cons["write_dirs"] == []
    for t in ("Write", "Edit"):
        assert t in cons["disallowed_tools"]


def test_write_dirs_drops_write_edit_from_disallowed(cfg, storage, audit,
                                                     policy, shared):
    """开作用域写后必须把 Write/Edit 从 disallowed 摘掉，否则压掉作用域授权。"""
    _, wd = write_ready(cfg, shared)
    cons = policy.constraints("200")
    assert cons["write_dirs"] == wd
    for t in ("Write", "Edit", "MultiEdit"):
        assert t not in cons["disallowed_tools"]
    assert "Bash" in cons["disallowed_tools"]          # Bash 始终禁
    assert "WebFetch" in cons["disallowed_tools"]
    assert "Task" in cons["allowed_tools"]             # 仍能委派 subagent


def test_super_write_dirs_also_scoped(cfg, storage, audit, policy, shared):
    """super 走 local 同样只拿作用域写，且仍不得 bypassPermissions。"""
    setcfg(cfg, "execution", super_backend="local")
    _, wd = write_ready(cfg, shared)
    cons = policy.constraints("18434")
    assert cons["tier"] == "super" and cons["backend"] == "local"
    assert cons["write_dirs"] == wd
    assert cons["permission_mode"] != "bypassPermissions"


def test_flags_emit_scoped_edit_rules(cfg, storage, audit, policy, shared):
    """_flags 必须发 Edit(//abs/**) 双斜杠绝对路径形式（单斜杠实测无效）。"""
    _, wd = write_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    cons = policy.constraints("200")
    f = ex._flags(cons, list(cons["add_dirs"]))
    allowed = f[f.index("--allowedTools") + 1]
    for d in wd:
        want = "Edit(//" + d.lstrip("/") + "/**)"
        assert want in allowed, f"缺 {want}，实得 {allowed}"
    assert "Edit(//" in allowed
    # 不得出现单斜杠绝对路径形式（实测无效，会静默不授权）
    assert not re.search(r"Edit\(/[^/]", allowed), allowed
    dis = f[f.index("--disallowedTools") + 1]
    assert "Bash" in dis and "Write" not in dis.split(",")


def test_try_local_skipped_when_needs_relay(av, cfg, shared):
    """需 relay 独有能力（此处：dws 外发凭据）的任务不得进 local，
    否则空转数分钟才 escalate。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av, text="把净头寸三层决策方案发送到钉钉群里")
    av._flush(s, uid="200")
    assert av.exec_local.calls == []                 # 没进 local
    assert len(av.exec_relay.submitted) == 1         # 直接走 relay
    assert s["state"] == "RUNNING"


def test_ppt_task_now_uses_local(av, cfg, shared):
    """**阶段二语义反转**：出 PPT 不再是升级理由。local 沙箱内已有 Bash，且
    shared/venv/bin/python 带 python-pptx（实测沙箱内只读但可执行）。旧口径把这类
    任务全推给 relay，是 local 禁 Bash 时代的产物。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av, text="生成净头寸三层决策方案，做成 PPT 报告")
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL"
    assert av.exec_relay.submitted == []


def test_html_task_still_uses_local(av, cfg, shared):
    """HTML 文本件 local 能出，不该被误判去 relay。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av, text="生成一周市场研判报告，html形式")
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL"
    assert av.exec_relay.submitted == []


@pytest.mark.parametrize("text,want", [
    # ── 阶段二反转：出二进制件 / 跑脚本 → local 沙箱内能做了 ──
    ("请给我净头寸三层决策方案，生成PPT报告，我要开会讲解", False),
    ("生成一份 docx 版周报", False),
    ("导出 xlsx 台账", False),
    ("把报告转成 pdf", False),
    ("以 pptx 形式给我", False),
    ("跑一下 scripts/build.py 脚本", False),
    ("执行这段 python 统计一下", False),
    # ── 仍需 relay：① 外发（local 无 dws 凭据）──
    ("把结果发送到钉钉群里", True),
    ("发邮件给王洪彬", True),
    ("推送到 webhook", True),
    # ── ② 系统运维（沙箱可写范围外 + 需门禁留痕）──
    ("在服务器上安装 python-pptx", True),
    ("部署新版本并重启服务", True),
    ("systemctl restart relay", True),
    # ── ③ 任意 SQL / 直接改库（store_task 授权不够）──
    ("跑一下这段 sql", True),
    ("把结果写入数据库台账", True),
    ("修改数据表字段", True),
    # ── ④ 业务动作（需门禁审批）──
    ("审批通过这单", True),
    ("下单采购 500 吨", True),
    # ── ⑤ 联网检索（WebFetch/WebSearch 仍禁 + 公网不可达）──
    ("联网搜一下今天的国际油价", True),
    ("抓取这个网页的内容 https://example.com", True),
    # ── 纯读/研判/文本件 → local ──
    ("生成一周市场研判报告，html形式", False),
    ("统计门店日销", False),
    ("查一下今天的裂解价差", False),
    ("帮我看看这个报告说了什么", False),
    ("生成一份摘要给我看", False),
    ("调用舒小瀚做本周市场研判，出 md 结论", False),
    ("净头寸现在是什么水位", False),
])
def test_needs_relay_backend(text, want):
    assert needs_relay_backend(text) is want


# ── F. local 影子记录（任务列表可见性）──
# 缺陷：local 任务无 relay task_id → 任务列表/看板/`/tasks` 全查不到。
# 2026-09-24 毕倩那条就是靠翻 journal + audit.log + state.json 三处对账才拼出真相。
# 影子记录只登记不派发：relay 不执行它、不占 max_concurrent（/health 的 active
# 取 sched.active，是调度器进程内计数），终态由 avatar 回报，avatar 死了由
# relay 的 sweep_stale 兜底判 failed。

def test_shadow_record_created_on_local_start(av, cfg, shared):
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    assert len(av.exec_relay.shadows) == 1
    assert av.exec_relay.shadows[0]["desc"] == "统计门店日销"
    assert s["task_id"] == 901


def test_shadow_reported_done_with_result(av, cfg, shared):
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "结论正文", "escalate": False})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    rep = av.exec_relay.local_status
    assert len(rep) == 1
    assert rep[0]["tid"] == 901 and rep[0]["status"] == "done"
    assert rep[0]["result"] == "结论正文"
    assert s["task_id"] is None                    # 回报后摘掉，避免被误轮询


def test_shadow_reported_cancelled_before_escalate(av, cfg, shared):
    """升级 relay 时必须**先**报影子记录终态——_flush 会覆盖 task_id，晚了就丢。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "", "escalate": True})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    rep = av.exec_relay.local_status
    assert len(rep) == 1
    assert rep[0]["tid"] == 901 and rep[0]["status"] == "cancelled"
    assert "升级 relay" in rep[0]["error"]
    # 影子记录已摘掉，s["task_id"] 现在是真 relay 任务号
    assert s["state"] == "RUNNING" and s["task_id"] == 1
    assert len(av.exec_relay.submitted) == 1


def test_shadow_reported_failed_on_reap(av, cfg, shared):
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = stale_local(av)
    s["task_id"] = 901                             # 模拟已有影子记录
    av._reap_stale_local()
    rep = av.exec_relay.local_status
    assert len(rep) == 1
    assert rep[0]["tid"] == 901 and rep[0]["status"] == "failed"
    assert "中断" in rep[0]["error"]
    # 改投 relay 后 task_id 换成真任务号
    assert s["state"] == "RUNNING" and s["task_id"] == 1


def test_shadow_failure_does_not_block_local(av, cfg, shared):
    """建影子记录失败（relay 不可达）不得阻断 local 执行——可见性是增益不是前提。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal(result={"text": "结论", "escalate": False})

    def boom(desc, uid=""):
        raise OSError("relay unreachable")
    av.exec_relay.create_local_record = boom
    s = sess_of(av)
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL" and s["task_id"] is None
    assert wait_local(s) is not None               # 照常执行
    av._drain_local()
    assert any("结论" in c for _, c in av.dws.sent)   # 照常回话
    assert av.exec_relay.local_status == []        # 无影子记录则不回报


def test_poll_sessions_skips_local_shadow(av, cfg, shared):
    """_poll_sessions 只管 RUNNING/WAIT_*，不得去轮询 LOCAL 的影子记录
    （relay 不执行它，轮询只会永远看到 running）。"""
    local_ready(cfg, shared)
    av.exec_local = FakeExecLocal()
    s = sess_of(av)
    av._flush(s, uid="200")
    assert s["state"] == "LOCAL" and s["task_id"] == 901
    polled = []
    av.exec_relay.poll = lambda tid: polled.append(tid) or {"id": tid,
                                                            "status": "running"}
    av._poll_sessions()
    assert polled == []                            # 没去轮询
    assert s["state"] == "LOCAL"                   # 态未被改


# ── G. send_file 标记代投（local 无 Bash，投递由 avatar 代做）──
# 缺口：放开作用域写后 local 能出 HTML，但**发不出去**——dws 要 Bash，而 Bash 仍禁
# （avatar 服务无沙箱，开 Bash 等于无限制 root）。故给一个标记出口由 avatar 代投。
# 标记内容来自模型输出、可被用户输入间接注入，故五道护栏逐条锁死。

import json as _json                      # noqa: E402
import os as _os                          # noqa: E402


def _mk(path, content="x", size=None):
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if size is None else "a" * size)
    return path


def _send_block(path, extra=None):
    obj = {"path": path}
    if extra:
        obj.update(extra)
    return "结论正文\n\n```send_file\n" + _json.dumps(obj, ensure_ascii=False) + "\n```"


def test_send_marker_delivers_and_strips(av, cfg, shared):
    """护栏内正常路径：文件发出、标记块从正文剔除、附注告知用户。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/x.html", "<html>报告</html>")
    s = sess_of(av)
    s["reply"] = {"is_group": False, "open_dingtalk_id": "sid200",
                  "conversation_id": "c1", "sender": "张三"}
    clean, notes = av._consume_send_markers(_send_block(p), s, 1, "200")
    assert "send_file" not in clean and "结论正文" in clean   # 标记已剔除
    assert len(notes) == 1 and "已发文件" in notes[0]
    assert any("[file]" in c and p in c for _, c in av.dws.sent)


def test_send_marker_confirms_delivery_via_status(av, cfg, shared, monkeypatch):
    """**受理 ≠ 到达**：响应带 openTaskId 时必须查 sendStatus 确认；确认成功则
    附注不带「未确认」后缀。dws 文件发送是异步的（outcome=pending），只看受理态
    就是 2026-09-25「审计记成功、用户没收到」的成因之一。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/y.html", "<html>x</html>")
    s = sess_of(av)
    monkeypatch.setattr(av.dws, "send_file", lambda t, path: {
        "ok": True, "outcome": "pending",
        "data": {"success": True, "result": {"openTaskId": "T1"}}})
    calls = []

    def fake_status(tid):
        calls.append(tid)
        return {"result": {"sendStatus": "SUCCESS"}, "success": True}

    monkeypatch.setattr(av.dws, "send_status", fake_status, raising=False)
    monkeypatch.setattr(time, "sleep", lambda _: None)      # 别真等
    _, notes = av._consume_send_markers(_send_block(p), s, 11, "200")
    assert calls == ["T1"]                                  # 确实去查了投递状态
    assert any("已发文件" in n and "未确认" not in n for n in notes)


def test_send_marker_reports_delivery_failure(av, cfg, shared, monkeypatch):
    """sendStatus 非 SUCCESS → 必须如实报失败，不得因为「受理成功」就报已发。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/z.html", "<html>x</html>")
    s = sess_of(av)
    monkeypatch.setattr(av.dws, "send_file", lambda t, path: {
        "ok": True, "outcome": "pending",
        "data": {"success": True, "result": {"openTaskId": "T2"}}})
    monkeypatch.setattr(av.dws, "send_status", lambda tid: {
        "result": {"sendStatus": "FAILED"}, "success": True}, raising=False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    _, notes = av._consume_send_markers(_send_block(p), s, 12, "200")
    assert any("发送失败" in n for n in notes)
    assert any("sendStatus=FAILED" in n for n in notes)
    assert not any("已发文件" in n for n in notes)


def test_send_marker_unconfirmed_delivery_says_so(av, cfg, shared, monkeypatch):
    """查不到投递状态时如实标「已受理，投递状态未确认」，不谎报成功。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/w.html", "<html>x</html>")
    s = sess_of(av)
    monkeypatch.setattr(av.dws, "send_file", lambda t, path: {
        "ok": True, "data": {"success": True, "result": {"openTaskId": "T3"}}})
    monkeypatch.setattr(av.dws, "send_status", lambda tid: {"result": {}},
                        raising=False)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    _, notes = av._consume_send_markers(_send_block(p), s, 13, "200")
    assert any("已发文件" in n and "未确认" in n for n in notes)


def test_send_marker_refuses_out_of_scope(av, cfg, shared):
    """护栏①：作用域外路径一律拒（哪怕文件真实存在）。"""
    _, wd = write_ready(cfg, shared)
    outside = _mk(str(shared / "knowledge" / "secret.md"), "内部知识")
    s = sess_of(av)
    clean, notes = av._consume_send_markers(_send_block(outside), s, 1, "200")
    assert any("拒绝代投" in n for n in notes)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_send_marker_refuses_dotdot_escape(av, cfg, shared):
    """护栏①：`../` 穿越必须被 realpath 归一后拦下。"""
    _, wd = write_ready(cfg, shared)
    _mk(str(shared / "knowledge" / "secret.md"), "内部知识")
    esc = wd[0] + "/../knowledge/secret.md"
    s = sess_of(av)
    _, notes = av._consume_send_markers(_send_block(esc), s, 1, "200")
    assert any("拒绝代投" in n for n in notes)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_send_marker_refuses_symlink_escape(av, cfg, shared):
    """护栏①：符号链接指向作用域外同样拦下（realpath 解析后才比对）。"""
    _, wd = write_ready(cfg, shared)
    target = _mk(str(shared / "knowledge" / "secret.md"), "内部知识")
    link = wd[0] + "/innocent.html"
    try:
        _os.symlink(target, link)
    except OSError:
        pytest.skip("无 symlink 权限")
    s = sess_of(av)
    _, notes = av._consume_send_markers(_send_block(link), s, 1, "200")
    assert any("拒绝代投" in n for n in notes)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_send_marker_ignores_recipient_override(av, cfg, shared):
    """护栏②：标记里写 to/收件人一律无效，只能发给发起会话。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/y.html", "x")
    s = sess_of(av)
    s["reply"] = {"is_group": False, "open_dingtalk_id": "sid200",
                  "conversation_id": "c1", "sender": "张三"}
    av._consume_send_markers(
        _send_block(p, {"to": "马韵升", "open_dingtalk_id": "EVIL",
                        "conversation_id": "EVIL"}), s, 1, "200")
    tgt = [t for t, c in av.dws.sent if "[file]" in c]
    assert tgt and tgt[0]["open_dingtalk_id"] == "sid200"    # 仍是发起会话


def test_send_marker_missing_and_oversize(av, cfg, shared):
    """护栏③：不存在的文件、超 media_max_size_mb 都要如实告知且不崩。"""
    _, wd = write_ready(cfg, shared)
    s = sess_of(av)
    _, n1 = av._consume_send_markers(
        _send_block(wd[0] + "/不存在.html"), s, 1, "200")
    assert any("不是已存在的文件" in n for n in n1)
    setcfg(cfg, "security", media_max_size_mb=0)      # 0MB → 任何件都超
    big = _mk(wd[0] + "/big.html", size=10)
    _, n2 = av._consume_send_markers(_send_block(big), s, 2, "200")
    assert any("超过" in n for n in n2)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_send_marker_caps_file_count(av, cfg, shared):
    """护栏④：单次最多 SEND_MAX_FILES 件，防刷。"""
    from app.main import SEND_MAX_FILES
    _, wd = write_ready(cfg, shared)
    blocks = []
    for i in range(SEND_MAX_FILES + 3):
        blocks.append("```send_file\n" + _json.dumps(
            {"path": _mk(f"{wd[0]}/f{i}.html")}) + "\n```")
    s = sess_of(av)
    _, notes = av._consume_send_markers("正文\n" + "\n".join(blocks), s, 1, "200")
    files = [c for _, c in av.dws.sent if "[file]" in c]
    assert len(files) == SEND_MAX_FILES
    assert any("上限" in n for n in notes)


def test_send_marker_dedupes_by_tid(av, cfg, shared):
    """护栏⑤：同一 tid 二次回流不重复发。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/z.html", "x")
    s = sess_of(av)
    av._consume_send_markers(_send_block(p), s, 7, "200")
    av._consume_send_markers(_send_block(p), s, 7, "200")
    assert len([c for _, c in av.dws.sent if "[file]" in c]) == 1


def test_send_marker_malformed_json_kept_in_text(av, cfg, shared):
    """解析失败不得吞正文：原样保留，让用户看见模型到底写了什么。"""
    _, wd = write_ready(cfg, shared)
    s = sess_of(av)
    bad = "正文\n\n```send_file\n{不是 JSON}\n```"
    clean, notes = av._consume_send_markers(bad, s, 1, "200")
    assert "{不是 JSON}" in clean and notes == []
    assert av.dws.sent == []


def test_drain_local_sends_file_end_to_end(av, cfg, shared):
    """全链路：local 出件 → 标记 → avatar 代投 → 正文回话（标记已剔除）。"""
    _, wd = write_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/报告.html", "<html>市场研判</html>")
    av.exec_local = FakeExecLocal(
        result={"text": _send_block(p), "escalate": False})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert any("[file]" in c and p in c for _, c in av.dws.sent)
    txt = [c for _, c in av.dws.sent if "结论正文" in c or "已发文件" in c]
    assert txt and "send_file" not in "".join(txt)
    assert s["state"] == "IDLE"


# ── H. delete_file 标记代删（作用域比可写更窄：只给草稿区）──
# 缺口：local 无 Bash、Write/Edit 也只能建/改不能删，所以"应用户请求清理草稿"
# agent 自己根本做不到（2026-09-24 王洪彬「删除这个helloworld」被挡，
# 表面是铁律禁止，实质是能力缺失）。

def del_ready(cfg, shared, dirs=("work",)):
    """开 local + 作用域写 + 指定可删目录，返回 (write_dirs, delete_dirs)。"""
    _, wd = write_ready(cfg, shared)
    dd = []
    for d in dirs:
        p = shared / d
        p.mkdir(parents=True, exist_ok=True)
        dd.append(str(p))
    setcfg(cfg, "security", local_delete_dirs=dd)
    return wd, dd


def _del_block(path):
    return "已处理\n\n```delete_file\n" + _json.dumps({"path": path}) + "\n```"


def test_delete_marker_removes_file_in_scope(av, cfg, shared):
    wd, dd = del_ready(cfg, shared)
    p = _mk(dd[0] + "/draft.html", "草稿")
    s = sess_of(av)
    clean, notes = av._consume_delete_markers(_del_block(p), s, 1, "200")
    assert not _os.path.exists(p)                    # 真删了
    assert any("已删除" in n for n in notes)
    assert "delete_file" not in clean and "已处理" in clean


def test_delete_marker_refuses_report_dir(av, cfg, shared):
    """核心边界：正式交付区不可删（无 baseline 快照，删了不可追溯）。"""
    wd, dd = del_ready(cfg, shared)
    p = _mk(wd[0] + "/2026-09/正式报告.html", "交付件")   # report/ 可写但不可删
    s = sess_of(av)
    _, notes = av._consume_delete_markers(_del_block(p), s, 1, "200")
    assert _os.path.isfile(p)                        # 没被删
    assert any("拒绝删除" in n for n in notes)


def test_delete_marker_refuses_dotdot_and_symlink(av, cfg, shared):
    wd, dd = del_ready(cfg, shared)
    secret = _mk(str(shared / "knowledge" / "k.md"), "内部知识")
    s = sess_of(av)
    _, n1 = av._consume_delete_markers(
        _del_block(dd[0] + "/../knowledge/k.md"), s, 1, "200")
    assert _os.path.isfile(secret) and any("拒绝删除" in n for n in n1)
    link = dd[0] + "/innocent.html"
    try:
        _os.symlink(secret, link)
    except OSError:
        pytest.skip("无 symlink 权限")
    _, n2 = av._consume_delete_markers(_del_block(link), s, 2, "200")
    assert _os.path.isfile(secret) and any("拒绝删除" in n for n in n2)


def test_delete_marker_refuses_directory(av, cfg, shared):
    """只删单个文件、不递归——避免一次误删整棵子树。"""
    wd, dd = del_ready(cfg, shared)
    sub = dd[0] + "/子目录"
    _mk(sub + "/inner.html", "x")
    s = sess_of(av)
    _, notes = av._consume_delete_markers(_del_block(sub), s, 1, "200")
    assert _os.path.isdir(sub) and _os.path.isfile(sub + "/inner.html")
    assert any("是目录" in n for n in notes)


def test_delete_marker_missing_file_and_cap(av, cfg, shared):
    from app.main import DELETE_MAX_FILES
    wd, dd = del_ready(cfg, shared)
    s = sess_of(av)
    _, n1 = av._consume_delete_markers(
        _del_block(dd[0] + "/不存在.html"), s, 1, "200")
    assert any("不存在" in n for n in n1)
    blocks = []
    for i in range(DELETE_MAX_FILES + 3):
        blocks.append("```delete_file\n" + _json.dumps(
            {"path": _mk(f"{dd[0]}/d{i}.html")}) + "\n```")
    _, n2 = av._consume_delete_markers("正文\n" + "\n".join(blocks), s, 2, "200")
    left = [i for i in range(DELETE_MAX_FILES + 3)
            if _os.path.exists(f"{dd[0]}/d{i}.html")]
    assert len(left) == 3                            # 只删了上限内的
    assert any("上限" in n for n in n2)


def test_delete_marker_dedupe_and_disabled(av, cfg, shared):
    wd, dd = del_ready(cfg, shared)
    p = _mk(dd[0] + "/dup.html", "x")
    s = sess_of(av)
    av._consume_delete_markers(_del_block(p), s, 9, "200")
    p2 = _mk(dd[0] + "/dup2.html", "x")
    _, notes = av._consume_delete_markers(_del_block(p2), s, 9, "200")
    assert _os.path.isfile(p2) and notes == []       # 同 tid 不重复处理
    # 未配 local_delete_dirs → 不开放代删
    setcfg(cfg, "security", local_delete_dirs=[])
    s2 = sess_of(av, sid="sid300", n="9")
    p3 = _mk(dd[0] + "/dup3.html", "x")
    _, n3 = av._consume_delete_markers(_del_block(p3), s2, 10, "300")
    assert _os.path.isfile(p3) and any("未开放代删" in n for n in n3)


def test_delete_marker_malformed_json_kept(av, cfg, shared):
    wd, dd = del_ready(cfg, shared)
    s = sess_of(av)
    clean, notes = av._consume_delete_markers(
        "正文\n\n```delete_file\n{坏 JSON}\n```", s, 1, "200")
    assert "{坏 JSON}" in clean and notes == []


def test_drain_local_deletes_end_to_end(av, cfg, shared):
    wd, dd = del_ready(cfg, shared)
    p = _mk(dd[0] + "/要删的.html", "x")
    av.exec_local = FakeExecLocal(
        result={"text": _del_block(p), "escalate": False})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert not _os.path.exists(p)
    assert any("已删除" in c for _, c in av.dws.sent)
    assert s["state"] == "IDLE"


def test_delete_dirs_narrower_than_write_dirs(cfg, storage, audit, policy,
                                              shared):
    """配置约束：可删作用域必须是可写作用域的子集（否则语义混乱）。"""
    wd, dd = del_ready(cfg, shared, dirs=("work",))
    cons = policy.constraints("200")
    assert set(cons["delete_dirs"]) <= set(cons["write_dirs"])
    assert cons["delete_dirs"] == dd


# ── H. relay 通路的 send_file 代投（2026-09-25 #242/#243 事故修复）──
# 事故：relay 任务没有标记代投这条路，agent 只能自己跑 dws；而本机 dws 登录账号
# 就是机器人自己，dingtalk-send 技能又明写「"我/本人"一律指当前登录 dws 的账号」
# 且「免确认直发」，于是「发我钉钉」被如实执行成发给机器人自己——sendStatus 真
# SUCCESS、发起人收不到，agent 还反过来猜「客户端未刷新」。

def _mk_task_dir(shared, tid=901):
    """造一个 relay 任务工作区：output/ work/ + 不该被代投的 .claude/ CLAUDE.md。"""
    w = shared.parent / "tasks" / str(tid)
    (w / "output").mkdir(parents=True, exist_ok=True)
    (w / "work").mkdir(parents=True, exist_ok=True)
    (w / ".claude").mkdir(parents=True, exist_ok=True)
    (w / "CLAUDE.md").write_text("注入的上下文", encoding="utf-8")
    (w / ".baseline.json").write_text("{}", encoding="utf-8")
    return w


def test_task_artifact_roots_only_output_and_work(av, cfg, shared):
    """只给 output/ 与 work/，不给整个 workdir——workdir 里还有 .claude/（会话数据）、
    CLAUDE.md（注入上下文）、.baseline.json，都不该被代投出去。"""
    w = _mk_task_dir(shared)
    roots = av._task_artifact_roots({"workdir": str(w)})
    assert sorted(os.path.basename(r) for r in roots) == ["output", "work"]
    assert not any(r == os.path.realpath(str(w)) for r in roots)


def test_task_artifact_roots_empty_without_workdir(av, cfg, shared):
    assert av._task_artifact_roots({}) == []
    assert av._task_artifact_roots({"workdir": "/nonexistent/xyz"}) == []


def test_send_result_delivers_relay_task_output(av, cfg, shared):
    """relay 任务的结果正文里带 send_file 标记 → 由 avatar 代投给发起会话。"""
    write_ready(cfg, shared)
    w = _mk_task_dir(shared, 902)
    p = w / "output" / "我行.pptx"
    p.write_bytes(b"fake-pptx-bytes")
    s = sess_of(av)
    row = {"id": 902, "workdir": str(w),
           "result": '做好了\n\n```send_file\n{"path": "%s"}\n```' % p}
    av._send_result(s, row)
    assert any("[file]" in c and "我行.pptx" in c for _, c in av.dws.sent), \
        "relay 出件应被代投"


def test_send_result_refuses_paths_outside_task_dirs(av, cfg, shared):
    """护栏①对 relay 同样生效：任务工作区与 local_write_dirs 之外一律拒。"""
    write_ready(cfg, shared)
    w = _mk_task_dir(shared, 903)
    outside = _mk(str(shared / "knowledge" / "secret.md"), "内部知识")
    s = sess_of(av)
    row = {"id": 903, "workdir": str(w),
           "result": '```send_file\n{"path": "%s"}\n```' % outside}
    av._send_result(s, row)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_send_result_refuses_task_context_files(av, cfg, shared):
    """.claude/ 与 CLAUDE.md 在 workdir 内但不在 output//work/，不得被代投。"""
    write_ready(cfg, shared)
    w = _mk_task_dir(shared, 904)
    s = sess_of(av)
    row = {"id": 904, "workdir": str(w),
           "result": '```send_file\n{"path": "%s"}\n```' % (w / "CLAUDE.md")}
    av._send_result(s, row)
    assert not any("[file]" in c for _, c in av.dws.sent)


def test_relay_delivery_rule_names_requester_and_forbids_dws(av, cfg, shared):
    """提交 relay 时必须附上发起人身份 + 禁止自行 dws 发送 + 标记代投口径。
    缺了它，agent 只能按 dingtalk-send 技能把「本人」理解成机器人自己。"""
    s = sess_of(av)
    s["reply"] = {"is_group": False, "open_dingtalk_id": "sid200",
                  "conversation_id": "c1", "sender": "王洪彬"}
    rule = av._relay_delivery_rule(s)
    assert "王洪彬" in rule                      # 告知发起人是谁
    assert "不要自己跑 dws" in rule               # 明确禁止自发
    assert "send_file" in rule                   # 给出替代路径
    assert "SUCCESS" in rule                     # 点明「成功也没送到」这个坑
    assert av.cfg.avatar_name in rule            # 点明 dws 账号是机器人自己


def test_relay_delivery_rule_group_vs_o2o(av, cfg, shared):
    s = sess_of(av)
    s["reply"] = {"is_group": True, "conversation_id": "cg", "sender": "李四"}
    assert "群聊" in av._relay_delivery_rule(s)
    s["reply"] = {"is_group": False, "open_dingtalk_id": "x", "sender": ""}
    r = av._relay_delivery_rule(s)
    assert "单聊" in r and "发起人" in r          # 无 sender 名时回落通称
