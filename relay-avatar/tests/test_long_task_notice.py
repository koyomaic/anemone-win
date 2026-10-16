"""长任务提示（分身侧）：判据自算、模式门控、分段边界、只发一次。

分身**不依赖 relay 新增字段**：elapsed 由 row.started_at 自算，段号从 resume_hint 的
[seg:N] 读回——poll 本来就返回整行，故零新增 HTTP 请求、零 API 变更。
"""
from __future__ import annotations

import time

import pytest

from app.main import Avatar, _seg_of


class MiniDws:
    def __init__(self):
        self.sent = []

    def card_update(self, biz, content, flow_status):
        self.sent.append((biz, content))
        return True


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


@pytest.fixture
def av(cfg):
    a = Avatar(cfg, dry=True)
    a.dws = MiniDws()
    a.notified = []
    a._notify_session = lambda s, text: a.notified.append(text)
    return a


def sess(**kw):
    s = {"card_biz": "BIZ1", "card_start": time.time(), "card_upd": 0.0,
         "card_hint": "做个门店日销报告", "state": "RUNNING", "task_id": 7}
    s.update(kw)
    return s


def row(elapsed=0.0, hint=""):
    return {"status": "running", "started_at": time.time() - elapsed,
            "resume_hint": hint}


# ── 段号解析 ──────────────────────────────────────────────────────────

def test_seg_of():
    assert _seg_of("") == 0
    assert _seg_of("普通提示") == 0
    assert _seg_of("[seg:2]\n继续未完成的工作") == 2
    assert _seg_of("[seg:10] x") == 10


def test_seg_of_malformed():
    assert _seg_of("[seg:abc] x") == 0
    assert _seg_of("[seg:3 未闭合") == 0
    assert _seg_of(None) == 0


# ── 模式门控：shadow/off 必须完全静默 ─────────────────────────────────

@pytest.mark.parametrize("mode", ["shadow", "off", "", "notifyx"])
def test_silent_unless_notify_or_on(av, cfg, mode):
    """shadow/off 必须完全静默（默认档已是 on，但显式设 shadow 仍可退回观察态）。"""
    setcfg(cfg, "ingress", long_task_mode=mode, long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=900), "running") is False
    assert av.dws.sent == []
    assert not s.get("long_notified")


def test_threshold_zero_disables(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=0)
    assert av._long_task_notice(sess(), 7, row(elapsed=9999), "running") is False
    assert av.dws.sent == []


def test_no_started_at_is_safe(av, cfg):
    """queued 尚未开跑时 started_at 为空：不能崩，也不能误报。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    assert av._long_task_notice(sess(), 7, {"status": "queued"}, "queued") is False


# ── 阈值触发 ──────────────────────────────────────────────────────────

def test_below_threshold_silent(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    assert av._long_task_notice(sess(), 7, row(elapsed=599), "running") is False
    assert av.dws.sent == []


def test_fires_at_threshold_on_mode(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=601), "running") is True
    assert len(av.dws.sent) == 1
    body = av.dws.sent[0][1]
    assert "后台长任务" in body and "分段接续" in body   # on 才敢承诺不掐断
    assert "取消" in body and "进度" in body             # 必须给出可控出口
    assert s["long_notified"] is True


def test_notify_mode_does_not_overpromise(av, cfg):
    """notify 阶段 relay 仍会在单段上限处停，不能许下做不到的话。"""
    setcfg(cfg, "ingress", long_task_mode="notify", long_task_threshold_sec=600)
    assert av._long_task_notice(sess(), 7, row(elapsed=700), "running") is True
    body = av.dws.sent[0][1]
    assert "分段接续" not in body and "后台长任务" not in body
    assert "还在继续跑" in body


def test_fires_only_once(av, cfg):
    """每任务一次：反复 poll 不能把用户刷屏。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=700), "running") is True
    for _ in range(5):
        assert av._long_task_notice(s, 7, row(elapsed=900), "running") is False
    assert len(av.dws.sent) == 1


def test_elapsed_text_is_human_readable(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(card_start=time.time() - 754)
    av._long_task_notice(s, 7, row(elapsed=754), "running")
    assert "12 分 34 秒" in av.dws.sent[0][1]


# ── 分段边界 ──────────────────────────────────────────────────────────

def test_segment_boundary_notice(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    r = row(elapsed=5, hint="[seg:2]\n上一段跑满 30 分钟被系统中断")
    assert av._long_task_notice(s, 7, r, "queued") is True
    body = av.dws.sent[0][1]
    assert "第 2 段" in body and "不会重做" in body
    assert s["seg_seen"] == 2


def test_segment_notice_once_per_segment(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    r2 = row(elapsed=5, hint="[seg:2] x")
    assert av._long_task_notice(s, 7, r2, "queued") is True
    assert av._long_task_notice(s, 7, r2, "running") is False   # 同段不重复
    r3 = row(elapsed=5, hint="[seg:3] y")
    assert av._long_task_notice(s, 7, r3, "queued") is True     # 新段再报一次
    assert len(av.dws.sent) == 2
    assert "第 3 段" in av.dws.sent[1][1]


def test_segment_notice_precedes_threshold(av, cfg):
    """刚进新段时 elapsed 很小（started_at 每段重置），仍应先报分段而不是等满 T。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=3, hint="[seg:2] x"), "running") is True
    assert "第 2 段" in av.dws.sent[0][1]


# ── 无卡片时退回会话消息 ──────────────────────────────────────────────

def test_falls_back_to_session_message(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(card_biz="")
    assert av._long_task_notice(s, 7, row(elapsed=900), "running") is True
    assert av.dws.sent == []
    assert len(av.notified) == 1 and "后台长任务" in av.notified[0]


def test_card_failure_does_not_raise(av, cfg):
    """钉钉抽风不能把 poll 循环带崩。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)

    def boom(*a):
        raise RuntimeError("dingtalk down")

    av.dws.card_update = boom
    assert av._long_task_notice(sess(), 7, row(elapsed=900), "running") is False


def test_notice_refreshes_throttle_stamp(av, cfg):
    """发完重要提示要刷新节流时间戳，否则紧接着的心跳帧会把它冲掉。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=30)
    s = sess(card_upd=0.0)
    av._long_task_notice(s, 7, row(elapsed=900), "running")
    assert time.time() - s["card_upd"] < 5
