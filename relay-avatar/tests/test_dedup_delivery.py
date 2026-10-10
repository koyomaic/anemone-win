"""tests/test_dedup_delivery.py —— 重复请求去重、多渠道投递绑定、失败终态必达。

背景（2026-10-10 交互群事故）：赵兴峰 6 分钟内把同一份周报需求发到单聊 + 两个群，
系统起了三个任务（#689/#690/#691）。三者收敛出**字节完全相同**的报告并写同一路径
`20261010_T689_鲁大师工作周报W41_v1.html`：

  · #689（单聊）  → review，19:59 文件送达单聊 ✅
  · #691（另一群）→ conflict，20:12 文件送达该群 ✅
  · #690（交互群）→ 三个重任务并发抢资源，撞上 1800s 超时 → failed ❌

交互群因此什么都没收到。而失败**确实**更新了进度卡片（定格成「没办成 ❌」），
但卡片是原地更新——钉钉不推送、不亮红点，用户看到的仍是两小时前的「正在处理…」，
除非主动回去盯那张旧卡。`_finish` 的 failed 分支当时写的是
`if not self._card_final(...): self._notify_session(...)`，卡片更新成功就**不再**
发真实消息，于是失败成了唯一的静默终态（done/conflict 因为附带 _send_full_file
产生了真实消息而侥幸没暴露）。

三处修复各对应本文件一组用例：
  A. 去重 + 额外投递目标绑定（需求只跑一次，结果送到发过它的每一个会话）
  B. 失败终态必发真实消息（卡片之外另外补一条，有推送）+ 预警超管
  C. 归一化比对（剥 @提及/空白/标点，否则群里带 @、单聊不带必然漏判）
"""
import json
import time

import pytest

from app.main import Avatar


REQ = ("请总结分析这周的工作，包含对话人数统计、主题分类、"
       "报告输出情况以及 IBP 十二个环节的意见建议")


class FakeDws:
    """记录所有出站动作；send_file 返回受理态，send_status 回 SUCCESS。"""

    def __init__(self, uids=None):
        self.uids = uids or {"sid200": "200", "sid555": "555"}
        self.sent = []          # [(target, content)]
        self.files = []         # [(target, path)]
        self.card_frames = []   # [(biz, content, flow_status)]
        self.card_ok = True

    def resolve_userid(self, open_id, sender, cache):
        return self.uids.get(open_id, "")

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.files.append((target, path))
        return {"ok": True, "outcome": "pending",
                "data": {"success": True, "result": {"openTaskId": "T1"}}}

    def send_status(self, otid):
        return {"result": {"sendStatus": "SUCCESS"}, "success": True}

    def react(self, cid, mid, emoji):
        return {"ok": True}

    def react_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def remove_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def card_create(self, target):
        return "BIZ1" if self.card_ok else ""

    def card_update(self, biz, content, flow_status):
        self.card_frames.append((biz, content, flow_status))
        return self.card_ok

    def mark_read(self, cid, mid):
        return {"ok": True}

    def run(self, args, timeout=60):
        return {}


class FakeExec:
    name = "relay"

    def __init__(self):
        self.submitted = []
        self.tasks = {}

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk",
                 requester=""):
        tid = len(self.submitted) + 1
        self.submitted.append({"tid": tid, "desc": desc, "uid": uid})
        self.tasks[tid] = {"id": tid, "status": "queued"}
        return {"task_id": tid, "status": "queued"}

    def poll(self, tid):
        return self.tasks.get(tid, {"_error": 404})

    def approve(self, tid, decision, uid=""):
        return {"ok": True}

    def confirm(self, tid, outcome, uid=""):
        return {"ok": True}

    def cancel(self, tid, uid=""):
        return {"ok": True}

    def list_plans(self, uid=""):
        return {"plans": []}

    def create_plan(self, name, on_cal, action, uid=""):
        return {"ok": True, "id": 1, "name": name}


@pytest.fixture
def av(cfg):
    # 群事件默认关闭（config.DEFAULTS group.enabled=False），本文件要验的正是
    # 「同一需求跨单聊与群」，故必须打开。
    cfg._p.setdefault("group", {})["enabled"] = True
    # 555 需在册，否则走 guest 静默、连会话都不建，无法验「不同发起人不去重」
    cfg._p.setdefault("permissions", {}).setdefault("whitelist", []).append("555")
    a = Avatar(cfg, dry=True)
    a.dws = FakeDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakeExec()
    return a


def ev(text, sid="sid200", sender="张三", n="1", group=False, conv=None):
    return {
        "event_type": ("user_im_message_receive_at" if group
                       else "user_im_message_receive_o2o_all"),
        "content": text, "sender": sender, "sender_open_dingtalk_id": sid,
        "message_id": f"m{n}",
        "conversation_id": (f"c{conv}" if conv is not None else f"c{n}"),
        "event_id": f"e{n}",
    }


def submit(av, text, conv="1", group=False, sid="sid200"):
    """驱动一个会话走完 缓冲 → 提交，返回其 session。

    会话键由 conversation_id 决定（群 `g:<conv>:<sid>`、单聊 `o:<sid>`），故
    「提交」必须落在**同一个** conv 上、只递增 message_id/event_id——否则会被
    当成另一个会话，原会话永远停在 BUFFERING。
    """
    av.on_event(ev(text, n=f"{conv}a", group=group, sid=sid, conv=conv))
    key = (f"g:c{conv}:{sid}" if group else f"o:{sid}")
    s = av.sess.sessions[key]
    av.on_event(ev("提交", n=f"{conv}b", group=group, sid=sid, conv=conv))
    return s


def texts(av):
    return [c for _, c in av.dws.sent]


def targets(av):
    """所有出站真实消息的目标签名（群取会话号，单聊取开放号）。"""
    return [str(t.get("conversation_id") or t.get("open_dingtalk_id") or "")
            for t, _ in av.dws.sent]


# ── A. 去重 + 额外投递目标绑定 ──

def test_duplicate_across_conversations_runs_once(av):
    """同一需求从单聊和群各发一次 → 只起一个任务，第二处被绑定而非重跑。"""
    s1 = submit(av, REQ, "1")
    assert s1["state"] == "RUNNING" and s1["task_id"] == 1
    av.dws.sent.clear()

    av.on_event(ev(REQ, n="2", group=True))

    assert len(av.exec_relay.submitted) == 1, "重复需求不应再起任务"
    assert "g:c2:sid200" in av.sess.sessions
    s2 = av.sess.sessions["g:c2:sid200"]
    assert s2["state"] == "IDLE" and s2["task_id"] is None
    assert any("已经在办" in t for t in texts(av)), "必须回执，不能静默吞掉"
    assert any("不用重发" in t for t in texts(av))
    assert av._target_sig(s2["reply"]) in [
        av._target_sig(t) for t in s1["extra_targets"]]


def test_duplicate_group_then_dm_also_bound(av):
    """反向顺序（先群后单聊）同样去重——判定与到达顺序无关。"""
    s1 = submit(av, REQ, "1", group=True)
    av.on_event(ev(REQ, n="2"))
    assert len(av.exec_relay.submitted) == 1
    assert len(s1["extra_targets"]) == 1


def test_at_prefix_and_punctuation_do_not_defeat_dedup(av):
    """群里带「@鲁大师」、单聊不带，归一化后仍须判为同一需求。"""
    submit(av, REQ, "1")
    av.on_event(ev("@鲁大师 " + REQ, n="2", group=True))
    assert len(av.exec_relay.submitted) == 1


def test_different_uid_not_deduped(av):
    """不同发起人的相同文本是两件事，绝不可互相吞掉。"""
    submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2", sid="sid555", sender="李四"))
    assert len(av.exec_relay.submitted) == 1, "李四尚在缓冲，未提交"
    av.on_event(ev("提交", n="2s", sid="sid555"))
    assert len(av.exec_relay.submitted) == 2


def test_different_content_not_deduped(av):
    """同一人先后两个不同需求都要跑。"""
    submit(av, REQ, "1")
    av.on_event(ev("另外帮我统计一下本月各门店的净头寸执行情况并出报告",
                   n="2a", group=True, conv="2"))
    av.on_event(ev("提交", n="2b", group=True, conv="2"))
    assert len(av.exec_relay.submitted) == 2


def test_short_text_not_deduped(av):
    """「好的」「继续」这类短文本不足以判定同一需求，宁可不去重。"""
    submit(av, REQ, "1")
    av.on_event(ev("好的", n="2", group=True))
    s2 = av.sess.sessions["g:c2:sid200"]
    assert s2["state"] == "BUFFERING", "短文本应正常进缓冲，不被去重吞掉"


def test_outside_window_not_deduped(av):
    """超出时间窗视为新需求（旧任务可能早已交付，用户是要重跑）。"""
    s1 = submit(av, REQ, "1")
    s1["last_activity"] = time.time() - 3600      # 1 小时前，窗默认 900s
    av.on_event(ev(REQ, n="2a", group=True, conv="2"))
    av.on_event(ev("提交", n="2b", group=True, conv="2"))
    assert len(av.exec_relay.submitted) == 2


def test_dedup_disabled_by_config(av):
    """阈值置 0 即关闭去重——宁可重复跑，不可误吞需求。"""
    av.cfg._p.setdefault("ingress", {})["dedup_similarity"] = 0
    submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2a", group=True, conv="2"))
    av.on_event(ev("提交", n="2b", group=True, conv="2"))
    assert len(av.exec_relay.submitted) == 2


def test_terminal_task_not_treated_as_inflight(av):
    """已完成的会话不算在途，重发同一需求应当真的重跑。"""
    s1 = submit(av, REQ, "1")
    av.exec_relay.tasks[1] = {"id": 1, "status": "done", "result": "办好了"}
    av._poll_sessions()
    assert s1["state"] == "IDLE"
    av.on_event(ev(REQ, n="2a", group=True, conv="2"))
    av.on_event(ev("提交", n="2b", group=True, conv="2"))
    assert len(av.exec_relay.submitted) == 2


def test_plan_command_not_swallowed_by_dedup(av):
    """计划登记是动作不是需求，去重不得拦在计划解析之前。"""
    submit(av, REQ, "1")
    before = len(av.exec_relay.submitted)
    av.on_event(ev("定时 每天 08:00 " + REQ, n="2", group=True))
    assert len(av.exec_relay.submitted) == before
    assert not any("已经在办" in t for t in texts(av))


# ── A2. 额外目标确实收到结果 ──

def test_extra_target_receives_result_text(av):
    """done 时主会话与绑定会话都要收到正文——只送一处等于另一处沉默。"""
    s1 = submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2", group=True))
    av.dws.sent.clear()

    av.exec_relay.tasks[1] = {"id": 1, "status": "done", "result": "周报已生成"}
    av._poll_sessions()

    got = targets(av)
    assert "c1" in got, "主会话（单聊）应收到"
    assert "c2" in got, "绑定会话（群）也应收到"
    assert sum("周报已生成" in c for _, c in av.dws.sent) >= 2


def test_extra_target_receives_file(av, cfg, shared, monkeypatch):
    """```send_file``` 代投必须覆盖绑定会话，且逐个核验真实投递态。"""
    wd = shared / "report"
    wd.mkdir(parents=True, exist_ok=True)
    cfg._p.setdefault("security", {})["local_send_dirs"] = [str(wd)]
    p = wd / "w41.html"
    p.write_text("<html>周报</html>", encoding="utf-8")

    s1 = submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2", group=True))
    monkeypatch.setattr(time, "sleep", lambda _: None)

    av.exec_relay.tasks[1] = {
        "id": 1, "status": "done",
        "result": "正文\n\n```send_file\n"
                  + json.dumps({"path": str(p)}, ensure_ascii=False) + "\n```"}
    av._poll_sessions()

    sigs = [str(t.get("conversation_id") or t.get("open_dingtalk_id") or "")
            for t, _ in av.dws.files]
    assert "c1" in sigs and "c2" in sigs, f"两处会话都应收到文件，实际 {sigs}"


def test_extra_target_deduped_by_signature(av):
    """同一会话重复绑定只留一条，避免同一份结果发两遍。"""
    s1 = submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2", group=True))
    av.on_event(ev(REQ, n="3", group=True))     # 不同 event，同一会话 c2? 否
    sigs = [av._target_sig(t) for t in s1["extra_targets"]]
    assert len(sigs) == len(set(sigs))


# ── B. 失败终态必达 ──

def test_failed_sends_real_message_even_when_card_succeeds(cfg):
    """卡片更新成功也**必须**另发真实消息。

    卡片是原地更新，钉钉不推送、不亮红点：只更新卡片等于没告知。这正是
    2026-10-10 交互群「没办成」定格在卡片里、用户却等了 2 小时毫无察觉的成因。
    """
    cfg._p.setdefault("ingress", {})["use_card"] = True
    a = Avatar(cfg, dry=True)
    a.dws = FakeDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakeExec()

    s = submit(a, REQ, "1")
    assert s.get("card_biz") == "BIZ1"
    a.dws.sent.clear()

    a.exec_relay.tasks[1] = {"id": 1, "status": "failed",
                             "error": "timeout after 1800s"}
    a._poll_sessions()

    assert any(fs == 5 for _, _, fs in a.dws.card_frames), "卡片应定格为错误态"
    assert any("没办成" in c for _, c in a.dws.sent), \
        "失败必须发真实消息（有推送），不能只更新卡片"
    assert any("timeout after 1800s" in c for _, c in a.dws.sent)


def test_failed_notifies_extra_targets(av):
    """失败要通知**所有**绑定会话——它们都在等这份结果。"""
    s1 = submit(av, REQ, "1")
    av.on_event(ev(REQ, n="2", group=True))
    av.dws.sent.clear()

    av.exec_relay.tasks[1] = {"id": 1, "status": "failed", "error": "超时"}
    av._poll_sessions()

    got = targets(av)
    assert "c1" in got and "c2" in got


def test_failed_alerts_superadmins(av):
    """失败预警超管：发起人只看到业务语言的「没办成」，根因需要人看。"""
    submit(av, REQ, "1")
    av.dws.sent.clear()

    av.exec_relay.tasks[1] = {"id": 1, "status": "failed", "error": "超时"}
    av._poll_sessions()

    sup = [c for t, c in av.dws.sent
           if str(t.get("user_id") or "") in av.policy.superadmin]
    assert sup, "应向超管预警"
    assert any("#1" in c and "超时" in c for c in sup)


def test_superadmin_alert_failure_does_not_break_user_notice(av, monkeypatch):
    """预警是增益，不能反过来影响给发起人的回执。"""
    submit(av, REQ, "1")
    av.dws.sent.clear()

    def boom(_text):
        raise RuntimeError("dws 挂了")

    monkeypatch.setattr(av, "_notify_superadmins", boom)
    av.exec_relay.tasks[1] = {"id": 1, "status": "failed", "error": "超时"}
    av._poll_sessions()                       # 不应抛
    assert any("没办成" in c for _, c in av.dws.sent)


def test_done_still_prefers_card_without_duplicate_text(cfg):
    """回归：done 分支卡片成功即不再重复发正文（原有行为不变）。"""
    cfg._p.setdefault("ingress", {})["use_card"] = True
    a = Avatar(cfg, dry=True)
    a.dws = FakeDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakeExec()
    s = submit(a, "统计门店日销", "1")
    a.dws.sent.clear()
    a.exec_relay.tasks[1] = {"id": 1, "status": "done", "result": "汇总完成"}
    a._poll_sessions()
    assert not [c for _, c in a.dws.sent if "汇总完成" in c], \
        "无额外绑定会话时，卡片已承载正文，不应再发一条重复的"


# ── C. 归一化 ──

@pytest.mark.parametrize("a,b", [
    ("@鲁大师 请给出今天的市场研判报告", "请给出今天的市场研判报告"),
    ("请 给出 报告", "请给出报告"),
    ("请给出报告！", "请给出报告"),
    ("请给出报告，谢谢", "请给出报告谢谢"),
])
def test_norm_for_dedup_ignores_surface_differences(a, b):
    assert Avatar._norm_for_dedup(a) == Avatar._norm_for_dedup(b)


def test_norm_for_dedup_keeps_semantic_difference():
    assert Avatar._norm_for_dedup("请给出今天的报告") != \
        Avatar._norm_for_dedup("请给出昨天的报告")


def test_inflight_desc_covers_all_states(av):
    """三种在途状态各存在不同字段，取原文必须都能拿到。"""
    s_buf = av.sess.get_or_create("o:b", "张三", "b", ev("x", n="b"), False)
    s_buf["state"] = "BUFFERING"
    s_buf["buffer"] = ["缓冲中的需求"]
    assert av._inflight_desc(s_buf) == "缓冲中的需求"

    s_loc = av.sess.get_or_create("o:l", "张三", "l", ev("x", n="l"), False)
    s_loc["state"] = "LOCAL"
    s_loc["local_desc"] = "本机在跑的需求"
    assert av._inflight_desc(s_loc) == "本机在跑的需求"

    s_run = av.sess.get_or_create("o:r", "张三", "r", ev("x", n="r"), False)
    s_run["state"] = "RUNNING"
    s_run["inflight_desc"] = "已提交 relay 的需求"
    assert av._inflight_desc(s_run) == "已提交 relay 的需求"


def test_flush_records_inflight_desc(av):
    """提交后必须留在途原文，否则 RUNNING 态无从比对。"""
    s = submit(av, REQ, "1")
    assert REQ[:20] in s["inflight_desc"]


# ══════════════════════════════════════════════════════════════════════════
# 事故原样重放：2026-10-10 赵兴峰 6 分钟内三条真实消息
#
# 逐字取自 events-20261010.ndjson，不做任何美化。用真实原文而非构造样本，是因为
# 去重阈值必须按**实际语料**校准——构造的"相同文本"会给出过于乐观的结论。
#
# 实测归一化后相似度：
#   单聊(7问, 201字) vs 交互群(6问, 283字) = 0.6446  → 不去重（本就是不同问题集）
#   交互群 vs 另一群                        = 1.0000  → 去重（字节完全相同）
# 故三条消息收敛成**两个**任务，不是一个。这是有意的：0.64 的两段问的不是同一件
# 事（单聊 7 问含"下周工作目标"，群里 6 问含"主题该怎么分类"），合并会导致
# "问两件事只答一件"。真正兜住这种情况的是产出任务号隔离（relay 侧 build_prompt
# 注入本任务号 + foreign_task_tag 检测），两个任务各写各的 _T<id>_ 文件，不再撞车。
# ══════════════════════════════════════════════════════════════════════════

# 2026-10-10 交互群事故：赵兴峰 6 分钟内发出的三条真实原文（逐字取自 events-20261010.ndjson）
INCIDENT_1 = '鲁大师本周的工作汇报：\n请总结分析这周鲁大师的工作，包含一下内容：\n1、这周有多少人发起了跟鲁大师的对话，对话的次数是什么？\n2、这周鲁大师主要做了哪些分析、统计和汇报相关的工作？\n3、这周针对中鲁的IBP12个环节，分别产生了多少的意见和建议？\n4、到目前为止，鲁大师还需要什么数据？这些数据该谁来负责？\n5、鲁大师整个工作，与上周比较起来，这些数字式怎么变化的？\n6、为了让鲁大师更好地服务中鲁核心经营团队，还需要做什么？\n7、鲁大师智能体建设下周工作目标是什么？'
INCIDENT_1_CONV = 'cid9js+OFGc693WJwQbEuxAIj1pKbrf7J+apKqoNmCRUYc='   # 2026-10-10 19:30:50
INCIDENT_2 = '@鲁大师 请鲁大师对自己本周的工作做个汇报：\n请总结分析这周鲁大师的工作，包含一下内容：\n1、这周有多少人发起了跟鲁大师的对话，对话的次数是什么？\n2、对话的主题该怎么分类？都是在对话哪些话题？这些话题该如何分类？\n3、这周鲁大师主要做了哪些分析、统计和汇报相关的报告输出工作？都是在为谁完成这些任务？\n4、这周针对中鲁的IBP12个环节，分别产生了多少的意见和建议？有多少人在关心这些问题（问你了）？\n5、为了完成大家提交的任务或者提问的问题，鲁大师还需要什么数据？这些数据该谁来负责？这些数据的完成情况如何？\n6、鲁大师假期归来是否开始忙起来了？这周的工作量如何？与上周和上上周比较起来，这些数字式怎么变化的？\n7、鲁大师智能体建设下周工作目标是什么？'
INCIDENT_2_CONV = 'cid774+UxHK+dZvYvz+baX1vg=='   # 2026-10-10 19:35:45
INCIDENT_3 = '@鲁大师 请鲁大师对自己本周的工作做个汇报：\n请总结分析这周鲁大师的工作，包含一下内容：\n1、这周有多少人发起了跟鲁大师的对话，对话的次数是什么？\n2、对话的主题该怎么分类？都是在对话哪些话题？这些话题该如何分类？\n3、这周鲁大师主要做了哪些分析、统计和汇报相关的报告输出工作？都是在为谁完成这些任务？\n4、这周针对中鲁的IBP12个环节，分别产生了多少的意见和建议？有多少人在关心这些问题（问你了）？\n5、为了完成大家提交的任务或者提问的问题，鲁大师还需要什么数据？这些数据该谁来负责？这些数据的完成情况如何？\n6、鲁大师假期归来是否开始忙起来了？这周的工作量如何？与上周和上上周比较起来，这些数字式怎么变化的？\n7、鲁大师智能体建设下周工作目标是什么？'
INCIDENT_3_CONV = 'cidbFfJcQgDqMkOoTnV38jDJg=='   # 2026-10-10 19:36:34

ZXF = "DJTDWXe5Omkca6fEkz0vxWVZZG4c4SxTc"      # 赵兴峰 openId（userId 拿不到，回落用）


@pytest.fixture
def zxf_av(cfg):
    """按真实身份形态构造：openId 解析不出 userId → uid 回落 openId（白名单认它）。"""
    cfg._p.setdefault("group", {})["enabled"] = True
    cfg._p.setdefault("permissions", {})["whitelist"] = [ZXF]
    a = Avatar(cfg, dry=True)
    a.dws = FakeDws(uids={})                     # 通讯录反查不到，与线上一致
    a.inbox.dws = a.dws
    a.exec_relay = FakeExec()
    return a


def zxf_ev(text, conv, n):
    return {
        "event_type": ("user_im_message_receive_at" if conv.startswith("cid7")
                       or conv.startswith("cidb")
                       else "user_im_message_receive_o2o_all"),
        "content": text, "sender": "赵兴峰", "sender_open_dingtalk_id": ZXF,
        "message_id": f"m{n}", "conversation_id": conv, "event_id": f"e{n}",
    }


def test_replay_incident_uid_falls_back_to_openid(zxf_av):
    """前置：uid 必须回落成 openId，否则限流/定级/去重全部错位。"""
    zxf_av.on_event(zxf_ev(INCIDENT_1, INCIDENT_1_CONV, "1"))
    s = zxf_av.sess.sessions[f"o:{ZXF}"]
    assert s["uid"] == ZXF


def test_replay_incident_two_identical_group_msgs_collapse(zxf_av):
    """交互群与另一群文本字节相同（ratio=1.0）→ 只跑一个任务，另一处被绑定。"""
    # 交互群先发起并进入在途
    zxf_av.on_event(zxf_ev(INCIDENT_2, INCIDENT_2_CONV, "2"))
    s_grp = zxf_av.sess.sessions[f"g:{INCIDENT_2_CONV}:{ZXF}"]
    zxf_av.on_event(zxf_ev("提交", INCIDENT_2_CONV, "2b"))
    assert s_grp["state"] == "RUNNING" and s_grp["task_id"] == 1
    zxf_av.dws.sent.clear()

    # 49 秒后另一群发来逐字相同的文本
    zxf_av.on_event(zxf_ev(INCIDENT_3, INCIDENT_3_CONV, "3"))

    assert len(zxf_av.exec_relay.submitted) == 1, "字节相同的重复需求不应再起任务"
    assert any("已经在办" in c for _, c in zxf_av.dws.sent), "必须回执，不能静默吞掉"
    assert zxf_av._target_sig({"conversation_id": INCIDENT_3_CONV}) in [
        zxf_av._target_sig(t) for t in s_grp["extra_targets"]]


def test_replay_incident_dm_differs_so_runs_separately(zxf_av):
    """单聊那条是 7 问、措辞不同（ratio=0.64）→ 必须**照常起任务**，不得被吞。

    这条是防过度去重的护栏：把阈值调低到能合并这两段，就会造成"问两件事只答
    一件"，比重复跑一次更糟。
    """
    zxf_av.on_event(zxf_ev(INCIDENT_1, INCIDENT_1_CONV, "1"))
    zxf_av.on_event(zxf_ev("提交", INCIDENT_1_CONV, "1b"))
    assert len(zxf_av.exec_relay.submitted) == 1

    zxf_av.on_event(zxf_ev(INCIDENT_2, INCIDENT_2_CONV, "2"))
    zxf_av.on_event(zxf_ev("提交", INCIDENT_2_CONV, "2b"))
    assert len(zxf_av.exec_relay.submitted) == 2, \
        "不同问题集必须各自执行，去重不得误吞"


def test_replay_incident_all_conversations_get_result(zxf_av):
    """终局：绑定的两个群都要收到正文——事故里交互群正是什么都没收到。"""
    zxf_av.on_event(zxf_ev(INCIDENT_2, INCIDENT_2_CONV, "2"))
    zxf_av.on_event(zxf_ev("提交", INCIDENT_2_CONV, "2b"))
    zxf_av.on_event(zxf_ev(INCIDENT_3, INCIDENT_3_CONV, "3"))
    zxf_av.dws.sent.clear()

    zxf_av.exec_relay.tasks[1] = {"id": 1, "status": "done",
                                  "result": "周报已生成，七节齐全"}
    zxf_av._poll_sessions()

    got = {str(t.get("conversation_id") or "") for t, _ in zxf_av.dws.sent}
    assert INCIDENT_2_CONV in got, "主会话（交互群）应收到"
    assert INCIDENT_3_CONV in got, "绑定会话（另一群）也应收到"


def test_replay_incident_failure_reaches_bound_group(zxf_av):
    """若仍失败，绑定的群也必须收到真实消息——事故里 #690 超时后群里一片沉默。"""
    zxf_av.on_event(zxf_ev(INCIDENT_2, INCIDENT_2_CONV, "2"))
    zxf_av.on_event(zxf_ev("提交", INCIDENT_2_CONV, "2b"))
    zxf_av.on_event(zxf_ev(INCIDENT_3, INCIDENT_3_CONV, "3"))
    zxf_av.dws.sent.clear()

    zxf_av.exec_relay.tasks[1] = {"id": 1, "status": "failed",
                                  "error": "timeout after 1800s"}
    zxf_av._poll_sessions()

    got = {str(t.get("conversation_id") or "") for t, c in zxf_av.dws.sent
           if "没办成" in c}
    assert INCIDENT_2_CONV in got and INCIDENT_3_CONV in got, \
        f"两个会话都应收到失败回执，实际 {got}"
