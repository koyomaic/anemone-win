"""长任务完成态通知：卡片定格之外必须补一条真实消息。

2026-10-21 舒瀚反馈 #1173→#1175：local 预处理 10 分钟后升级 relay，relay 又跑
24 分钟。最终结果虽然更新了流式卡片，但卡片原地更新不产生新推送；用户长时间
看不到明确完成反馈。短任务维持卡片静默完成，长任务必须补发真实消息。
"""
from __future__ import annotations

import time

import pytest

from app.main import Avatar


class MiniDws:
    def __init__(self):
        self.sent = []
        self.cards = []

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.sent.append((target, f"[file]{path}"))
        return {"ok": True}

    def card_update(self, biz, content, flow_status):
        self.cards.append((biz, content, flow_status))
        return True


@pytest.fixture
def av(cfg):
    a = Avatar(cfg, dry=True)
    a.dws = MiniDws()
    a._consume_plan_markers = lambda result, s, tid: (result, [])
    a._consume_send_markers = lambda result, s, tid, uid, extra_roots=None: (result, [])
    a._task_artifact_roots = lambda row: []
    a._extra_targets = lambda s: []
    return a


def sess(**kw):
    s = {
        "card_biz": "BIZ1",
        "reply": {"open_dingtalk_id": "sid200", "conversation_id": "c1",
                  "sender": "张三", "is_group": False},
        "uid": "200",
        "task_id": 7,
        "long_notified": False,
        "user_started_at": 0.0,
    }
    s.update(kw)
    return s


def row(elapsed=100, result="中文结果", **kw):
    now = time.time()
    r = {
        "id": 7,
        "status": "done",
        "result": result,
        "created_at": now - elapsed,
        "started_at": now - elapsed,
        "finished_at": now,
    }
    r.update(kw)
    return r


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


def test_short_carded_done_does_not_double_notify(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    av._send_result(s, row(elapsed=100))
    assert len(av.dws.cards) == 1
    assert av.dws.sent == []


def test_long_carded_done_sends_real_notice(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(long_notified=True, user_started_at=time.time() - 700)
    av._send_result(s, row(elapsed=100))
    assert len(av.dws.cards) == 1
    assert len(av.dws.sent) == 1
    body = av.dws.sent[0][1]
    assert "已办完" in body
    assert "总用时" in body


def test_long_by_elapsed_without_flag_sends_real_notice(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(user_started_at=time.time() - 700)
    av._send_result(s, row(elapsed=100))
    assert len(av.dws.sent) == 1
    assert "11 分" in av.dws.sent[0][1]


def test_long_review_notice_mentions_confirm(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(long_notified=True)
    av._send_result(s, row(elapsed=100), prefix="👀 办完了，等你过目（回复「确认」完成）")
    assert "待你确认" in av.dws.sent[0][1]


def test_long_without_card_still_sends_body_once(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(card_biz="", long_notified=True)
    av._send_result(s, row(elapsed=100))
    assert av.dws.cards == []
    assert len(av.dws.sent) == 1
    assert "中文结果" in av.dws.sent[0][1]
