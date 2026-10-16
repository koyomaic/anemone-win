from __future__ import annotations

import json
import os
import time

import pytest

from app.main import Avatar


class FakeDws:
    def __init__(self):
        self.sent = []
        self.files = []

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.files.append((target, path))
        self.sent.append((target, f"[file]{path}"))
        return {"ok": True}

    def react(self, *a, **k):
        return {"ok": True}

    def react_text_emotion(self, *a, **k):
        return {"ok": True}

    def remove_text_emotion(self, *a, **k):
        return {"ok": True}

    def card_create(self, *a, **k):
        return {}

    def card_update(self, *a, **k):
        return False

    def mark_read(self, *a, **k):
        return {"ok": True}


class FakePlanRelay:
    name = "relay"

    def __init__(self, plans=None, tasks=None):
        self.plans = plans or []
        self.tasks = tasks or {}
        self.created_plans = []
        self.polled = []

    def create_plan(self, name, on_calendar, action, uid=""):
        pid = len(self.plans) + 1
        p = {"id": pid, "name": name, "on_calendar": on_calendar,
             "action": action, "enabled": True, "next_run": time.time() + 3600,
             "last_task_id": None}
        self.plans.append(p)
        self.created_plans.append(p)
        return p

    def list_plans(self, uid=""):
        return {"plans": self.plans}

    def poll(self, tid):
        self.polled.append(tid)
        return self.tasks.get(tid, {"_error": 404})


@pytest.fixture
def av(cfg, tmp_path):
    a = Avatar(cfg, dry=True)
    a.dws = FakeDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakePlanRelay()
    return a


GROUP_REPLY = {"is_group": True, "conversation_id": "cidGROUP==",
               "open_dingtalk_id": "openSender", "sender": "闫路英"}
GROUP_DELIVERY = {"is_group": True, "conversation_id": "cidGROUP==",
                  "sender": "闫路英"}


def _setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


def _make_task_file(tmp_path, tid, name="a.txt", content="file"):
    d = tmp_path / "tasks" / str(tid) / "output"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_plan_delivery_target_group_and_single(av):
    assert av._plan_delivery_target(GROUP_REPLY) == GROUP_DELIVERY
    single = {"is_group": False, "conversation_id": "cidS==",
              "open_dingtalk_id": "openX", "sender": "张三"}
    out = av._plan_delivery_target(single)
    assert out["is_group"] is False and out["open_dingtalk_id"] == "openX"
    assert av._plan_delivery_target({"is_group": True}) is None
    assert av._plan_delivery_target({"is_group": False}) is None
    assert av._plan_delivery_target(None) is None


def test_plan_create_injects_current_reply_delivery(av):
    note = av._plan_create_note(
        {"name": "每日0755首页", "on_calendar": "*-*-* 07:55:00",
         "description": "取当日首页并投递"}, "34718", reply=GROUP_REPLY)
    assert len(av.exec_relay.created_plans) == 1
    action = av.exec_relay.created_plans[0]["action"]
    assert action["delivery"] == GROUP_DELIVERY
    assert "自动发回本群" in note


def test_plan_create_without_reply_has_no_delivery(av):
    av._plan_create_note(
        {"name": "无会话计划", "on_calendar": "*-*-* 08:00:00",
         "description": "x"}, "34718")
    assert "delivery" not in av.exec_relay.created_plans[0]["action"]


def test_plan_create_ignores_agent_supplied_delivery(av):
    av._plan_create_note(
        {"name": "注入计划", "on_calendar": "*-*-* 08:00:00",
         "description": "x",
         "delivery": {"is_group": True, "conversation_id": "cidATTACKER=="}},
        "34718", reply=GROUP_REPLY)
    action = av.exec_relay.created_plans[0]["action"]
    assert action["delivery"] == GROUP_DELIVERY
    assert "cidATTACKER==" not in json.dumps(action, ensure_ascii=False)


def test_poll_plan_delivery_baselines_existing_task(av, tmp_path):
    av.exec_relay = FakePlanRelay(plans=[
        {"id": 7, "name": "old", "action": {"delivery": GROUP_DELIVERY},
         "last_task_id": 70},
    ])
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert av.exec_relay.polled == []
    state = av._plan_delivery_state()
    assert state["7"]["delivered_task_id"] == 70
    assert av.dws.sent == []


def test_poll_plan_delivers_done_task_once_and_strips_plan_marker(av, tmp_path):
    path = _make_task_file(tmp_path, 11)
    result = (
        "当日首页已准备。\n"
        f"```send_file\n{{\"path\": {json.dumps(path, ensure_ascii=False)}}}\n```\n"
        "```plan\n{\"op\": \"create\", \"name\": \"递归计划\", "
        "\"on_calendar\": \"*-*-* 09:00:00\", \"description\": \"不应执行\"}\n```"
    )
    plan = {"id": 1, "name": "每日0755首页", "action": {"delivery": GROUP_DELIVERY},
            "last_task_id": 10}
    av.exec_relay = FakePlanRelay(plans=[plan], tasks={
        11: {"id": 11, "status": "done", "result": result, "error": ""},
    })
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert av.exec_relay.polled == []          # 首次只打基线，不追溯旧任务
    assert av.dws.sent == []

    plan["last_task_id"] = 11
    av._plan_delivery_next = 0
    av.exec_relay.tasks[11] = {"id": 11, "status": "done", "result": result,
                               "error": ""}
    av._poll_plan_deliveries()

    assert av.exec_relay.polled == [11]
    assert any(t.get("conversation_id") == "cidGROUP==" and "当日首页" in c
               for t, c in av.dws.sent)
    assert any(t.get("conversation_id") == "cidGROUP==" and p == path
               for t, p in av.dws.files)
    assert av.exec_relay.created_plans == []   # 计划任务里的 plan 标记不得递归执行
    state = av._plan_delivery_state()
    assert state["1"]["delivered_task_id"] == 11

    sent_before = len(av.dws.sent)
    polled_before = len(av.exec_relay.polled)
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert len(av.dws.sent) == sent_before
    assert len(av.exec_relay.polled) == polled_before


def test_poll_plan_delivers_review_and_failed(av, tmp_path):
    plan = {"id": 2, "name": "review计划", "action": {"delivery": GROUP_DELIVERY},
            "last_task_id": 0}
    av.exec_relay = FakePlanRelay(plans=[plan], tasks={
        21: {"id": 21, "status": "review", "result": "待过目结果", "error": ""},
        22: {"id": 22, "status": "failed", "result": "", "error": "boom"},
    })
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()

    plan["last_task_id"] = 21
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert any("待人工过目" in c and "待过目结果" in c for _, c in av.dws.sent)

    plan["last_task_id"] = 22
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert any("没办成" in c and "boom" in c for _, c in av.dws.sent)
    assert av._plan_delivery_state()["2"]["delivered_task_id"] == 22


def test_poll_plan_ignores_plans_without_delivery(av):
    av.exec_relay = FakePlanRelay(plans=[
        {"id": 3, "name": "无绑定", "action": {}, "last_task_id": 31},
    ], tasks={31: {"id": 31, "status": "done", "result": "x"}})
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert av.exec_relay.polled == []
    assert av.dws.sent == []
    assert av._plan_delivery_state() == {}


def test_poll_plan_delivery_disabled(av):
    _setcfg(av.cfg, "execution", plan_delivery_enabled=False)
    av.exec_relay = FakePlanRelay(plans=[
        {"id": 4, "name": "p", "action": {"delivery": GROUP_DELIVERY},
         "last_task_id": 41},
    ], tasks={41: {"id": 41, "status": "done", "result": "x"}})
    av._plan_delivery_next = 0
    av._poll_plan_deliveries()
    assert av.exec_relay.polled == []
    assert av.dws.sent == []


def test_plan_list_note_shows_delivery(av):
    av.exec_relay = FakePlanRelay(plans=[
        {"id": 5, "name": "群投递", "on_calendar": "*-*-* 07:55:00",
         "enabled": True, "next_run": time.time() + 3600,
         "action": {"delivery": GROUP_DELIVERY}},
        {"id": 6, "name": "无投递", "on_calendar": "*-*-* 08:00:00",
         "enabled": True, "next_run": time.time() + 3600, "action": {}},
    ])
    note = av._plan_list_note("34718")
    assert "群投递" in note and "投递:群" in note
    assert "无投递" in note and note.count("投递:群") == 1
