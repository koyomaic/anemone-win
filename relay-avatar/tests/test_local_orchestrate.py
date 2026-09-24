"""tests/test_local_orchestrate.py —— local「只读编排」通路（tier→backend 可配）。

覆盖四条安全底线：
① 默认配置行为完全不变（relay 后端、白名单无 Task）——回归保护；
② backend=local 时工具白名单含 Task（能委派 subagent）且禁写禁执行禁联网齐全；
③ sandbox 为空/不存在一律回落或拒绝，绝不让 local 后端裸奔；
④ super 走 local 时不得用 bypassPermissions。
另覆盖：升级不降级（越权→relay 且不死循环）、LOCAL 态不被 _poll_sessions 误重置、
LOCAL 态收到「取消」不得重置会话态（否则卡片永远停在处理中）。
"""
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
                 on_step=None):
        seen.update(prompt=prompt, conv_key=conv_key, cwd=cwd, flags=flags)
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

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk"):
        tid = len(self.submitted) + 1
        self.submitted.append({"tid": tid, "desc": desc, "uid": uid})
        return {"task_id": tid, "status": "queued"}

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
    assert s["state"] == "LOCAL" and s["task_id"] is None
    assert av.exec_relay.submitted == []           # 没走 relay
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
    assert av.exec_relay.submitted[0]["desc"] == "统计门店日销"
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
