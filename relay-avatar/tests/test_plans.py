"""tests/test_plans.py —— 大脑 ```plan 标记 → relay-avatar 带凭据执行（创建/列表/删除/启停）。"""
import time

import pytest

from app.main import Avatar


class FakeDws:
    def __init__(self, uids=None):
        self.uids = uids or {}
        self.sent = []
        self.reacts = []
        self.text_emotions = []

    def resolve_userid(self, open_id, sender, cache):
        return self.uids.get(open_id, "")

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
        self.text_emotions.append((mid, te))
        return {"ok": True}

    def remove_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def card_create(self, target):
        return ""

    def card_update(self, biz, content, flow_status):
        return False

    def mark_read(self, cid, mid):
        return {"ok": True}

    def run(self, args, timeout=60):
        return {}


class PlanExec:
    name = "relay"

    def __init__(self):
        self.submitted = []
        self.tasks = {}
        self.plans = []
        self.created = []
        self.deleted = []
        self.patched = []

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk"):
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

    def create_plan(self, name, on_calendar, action, uid=""):
        self.created.append((name, on_calendar, action))
        if any(p["name"] == name for p in self.plans):
            return {"_error": 409, "_detail": "plan name exists"}
        pid = len(self.plans) + 1
        plan = {"id": pid, "name": name, "on_calendar": on_calendar,
                "action": action, "enabled": True,
                "next_run": time.time() + 3600, "last_run": None}
        self.plans.append(plan)
        return plan

    def list_plans(self, uid=""):
        return {"plans": list(self.plans)}

    def patch_plan(self, pid, fields, uid=""):
        self.patched.append((pid, fields))
        for p in self.plans:
            if p["id"] == pid:
                p.update(fields)
        return {"ok": True}

    def delete_plan(self, pid, uid=""):
        self.deleted.append(pid)
        self.plans = [p for p in self.plans if p["id"] != pid]
        return {"ok": True}


def ev(text, sid="sid200", sender="张三", n="1"):
    return {"event_type": "user_im_message_receive_o2o_all",
            "content": text, "sender": sender,
            "sender_open_dingtalk_id": sid, "message_id": f"m{n}",
            "conversation_id": f"c{n}", "event_id": f"e{n}"}


@pytest.fixture
def avatar(cfg):
    av = Avatar(cfg, dry=True)
    av.dws = FakeDws({"sid200": "200", "sid18434": "18434"})
    av.inbox.dws = av.dws
    av.exec_relay = PlanExec()
    return av


def texts(avatar):
    return [c for _, c in avatar.dws.sent]


def _run_with_result(avatar, user_msg, result):
    """走完整链路：用户消息→缓冲→提交→done(带 result)→轮询回流。返回会话。"""
    avatar.on_event(ev(user_msg, n="1"))
    s = avatar.sess.sessions["o:sid200"]
    avatar._flush(s, uid="200")
    tid = s["task_id"]
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "done", "result": result}
    avatar.dws.sent.clear()
    avatar._poll_sessions()
    return s


def test_create_marker_executed_and_stripped(avatar):
    result = ("好的，已安排每天 8 点跑经营日报。\n\n"
              "```plan\n"
              '{"op":"create","name":"经营日报","on_calendar":"*-*-* 08:00:00",'
              '"description":"生成昨日经营日报并发送给王洪彬","priority":"normal","read_only":false}\n'
              "```")
    _run_with_result(avatar, "每天8点跑经营日报", result)
    # 执行：create_plan 被调用，参数正确
    assert len(avatar.exec_relay.created) == 1
    name, on_cal, action = avatar.exec_relay.created[0]
    assert name == "经营日报" and on_cal == "*-*-* 08:00:00"
    assert action["type"] == "task" and "经营日报" in action["description"]
    # 正文：标记块被剔除，权威确认被追加
    joined = "\n".join(texts(avatar))
    assert "```plan" not in joined and "on_calendar" not in joined
    assert "已安排每天 8 点" in joined          # 大脑原话保留
    assert "已创建定时计划「经营日报」" in joined  # 系统权威确认
    assert "下次" in joined


def test_list_marker(avatar):
    avatar.exec_relay.plans.append({"id": 1, "name": "日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "我有哪些计划", '查一下～\n```plan\n{"op":"list"}\n```')
    joined = "\n".join(texts(avatar))
    assert "定时计划" in joined and "日报" in joined
    assert "```plan" not in joined


def test_delete_marker_by_name(avatar):
    avatar.exec_relay.plans.append({"id": 7, "name": "晨会提醒", "on_calendar": "Mon..Fri *-*-* 09:30:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "删掉晨会提醒",
                     '好\n```plan\n{"op":"delete","name":"晨会"}\n```')
    assert avatar.exec_relay.deleted == [7]
    assert "已删除计划「晨会提醒」" in "\n".join(texts(avatar))


def test_disable_marker(avatar):
    avatar.exec_relay.plans.append({"id": 3, "name": "巡检", "on_calendar": "*-*-* *:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "停用巡检", '```plan\n{"op":"disable","name":"巡检"}\n```')
    assert avatar.exec_relay.patched == [(3, {"enabled": False})]
    assert "已停用计划「巡检」" in "\n".join(texts(avatar))


def test_name_dedupe(avatar):
    avatar.exec_relay.plans.append({"id": 1, "name": "经营日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "再来个经营日报",
                     '```plan\n{"op":"create","name":"经营日报","on_calendar":"*-*-* 09:00:00","description":"日报"}\n```')
    # 重名 → 自动加后缀 -2
    assert avatar.exec_relay.created[-1][0] == "经营日报-2"


def test_malformed_marker_passthrough(avatar):
    result = "正文\n```plan\n{not valid json}\n```"
    _run_with_result(avatar, "随便", result)
    assert not avatar.exec_relay.created
    joined = "\n".join(texts(avatar))
    assert "{not valid json}" in joined   # 解析失败原样保留，不吞


def test_no_marker_normal_flow(avatar):
    _run_with_result(avatar, "查个东西", "普通结果，没有计划")
    assert not avatar.exec_relay.created
    assert "普通结果" in "\n".join(texts(avatar))


def test_marker_executed_once_per_task(avatar):
    """review→done 二次回流同一 result：只执行一次（plan_applied 守卫）。"""
    avatar.on_event(ev("每天8点跑日报", n="1"))
    s = avatar.sess.sessions["o:sid200"]
    avatar._flush(s, uid="200")
    tid = s["task_id"]
    result = ('```plan\n{"op":"create","name":"日报","on_calendar":"*-*-* 08:00:00","description":"日报"}\n```')
    # 第一次：review
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "review", "result": result}
    avatar._poll_sessions()
    assert len(avatar.exec_relay.created) == 1
    # 第二次：done（同一 result 再回流）
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "done", "result": result}
    avatar._poll_sessions()
    assert len(avatar.exec_relay.created) == 1   # 不重复创建


# ── 主路径：定时/计划 前缀在进大脑前就地拦截，绝不提交任务 ──

def test_direct_create_does_not_reach_brain(avatar):
    avatar.on_event(ev("定时 每天8点 跑经营日报", n="1"))
    # 建了计划
    assert len(avatar.exec_relay.created) == 1
    name, on_cal, action = avatar.exec_relay.created[0]
    assert name == "跑经营日报" and on_cal == "*-*-* 08:00:00"
    assert action["type"] == "task" and "经营日报" in action["description"]
    # 关键：没有提交任何大脑任务（不会被"当场执行"）
    assert avatar.exec_relay.submitted == []
    # 会话仍 IDLE（计划命令不进缓冲流）
    assert avatar.sess.sessions["o:sid200"]["state"] == "IDLE"
    joined = "\n".join(texts(avatar))
    assert "已创建定时计划「跑经营日报」" in joined and "下次" in joined


def test_direct_list(avatar):
    avatar.exec_relay.plans.append({"id": 1, "name": "日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    avatar.on_event(ev("计划列表", n="2"))
    assert avatar.exec_relay.submitted == []
    joined = "\n".join(texts(avatar))
    assert "定时计划" in joined and "日报" in joined


def test_direct_delete(avatar):
    avatar.exec_relay.plans.append({"id": 9, "name": "晨会提醒", "on_calendar": "Mon..Fri *-*-* 09:30:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    avatar.on_event(ev("计划删除 晨会", n="3"))
    assert avatar.exec_relay.deleted == [9]
    assert avatar.exec_relay.submitted == []
    assert "已删除计划「晨会提醒」" in "\n".join(texts(avatar))


def test_direct_create_fail_guides(avatar):
    avatar.on_event(ev("定时 每天 跑日报", n="4"))   # 缺时间 → create_fail
    assert not avatar.exec_relay.created
    assert avatar.exec_relay.submitted == []
    assert "没太看懂" in "\n".join(texts(avatar))


def test_normal_task_still_reaches_brain(avatar):
    """非计划消息照常走会话流（解析器不误拦）。"""
    avatar.on_event(ev("统计每天的销量", n="5"))
    avatar._flush(avatar.sess.sessions["o:sid200"], uid="200")
    assert len(avatar.exec_relay.submitted) == 1
    assert not avatar.exec_relay.created


def test_plan_permission_gate(avatar):
    """_handle_plan 纵深防御：非 super/L1/L2 拒绝（正常路由里 L3/NONE 到不了 IDLE）。"""
    s = {"reply": {}, "uid": "999"}
    avatar._handle_plan({"cmd": "create", "name": "x", "on_calendar": "*-*-* 08:00:00",
                         "description": "y"}, s, uid="999")
    assert not avatar.exec_relay.created
    assert "权限" in "\n".join(texts(avatar))
