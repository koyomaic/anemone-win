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
    # [seg:2] 意为"已完成 2 段"，正在跑的是第 3 段（人读序号，不是标记值）
    assert "第 3 段" in body and "不会重做" in body
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
    assert "第 4 段" in av.dws.sent[1][1]   # [seg:3] → 正在跑第 4 段


def test_segment_notice_precedes_threshold(av, cfg):
    """刚进新段时 elapsed 很小（started_at 每段重置），仍应先报分段而不是等满 T。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=3, hint="[seg:2] x"), "running") is True
    assert "第 3 段" in av.dws.sent[0][1]


# ── 无卡片时退回会话消息 ──────────────────────────────────────────────

def test_falls_back_to_session_message(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess(card_biz="")
    assert av._long_task_notice(s, 7, row(elapsed=900), "running") is True
    assert av.dws.sent == []
    assert len(av.notified) == 1 and "后台长任务" in av.notified[0]


def test_card_failure_does_not_raise(av, cfg):
    """钉钉抽风不能把 poll 循环带崩；卡片写不进去时仍要退回独立消息。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)

    def boom(*a):
        raise RuntimeError("dingtalk down")

    av.dws.card_update = boom
    assert av._long_task_notice(sess(), 7, row(elapsed=900), "running") is True
    assert len(av.notified) == 1 and "后台长任务" in av.notified[0]


# ── 回归 #147：只改卡片等于没提示，必须另发一条独立消息 ────────────────
# 实测：card_update 返回 success=true、long_notified=True、心跳按 30s 正常刷新，
# 但用户两次反馈「看不到提示」——卡片是原地更新的，不产生新消息也不触发提醒，
# 外观几乎没变（只有 ⏳ 那一行）。重要状态变化必须落成一条真正的消息。

def test_escalation_sends_distinct_message(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    assert av._long_task_notice(s, 7, row(elapsed=900), "running") is True
    assert len(av.dws.sent) == 1, "卡片帧照旧要写（承载持续状态）"
    assert len(av.notified) == 1, "必须另发一条独立消息，否则用户看不见"
    assert "后台长任务" in av.notified[0]


def test_segment_boundary_sends_distinct_message(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600)
    s = sess()
    av._long_task_notice(s, 7, row(elapsed=5, hint="[seg:1] x"), "queued")
    assert len(av.notified) == 1 and "第 2 段" in av.notified[0]
    assert len(av.dws.sent) == 1


def test_heartbeat_does_not_spam_messages(av, cfg):
    """心跳只改卡片、**不**发独立消息，否则 30s 一条会把用户刷屏。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=0)
    s = sess(card_upd=0.0, long_notified=True)
    for _ in range(4):
        s["card_upd"] = 0.0
        av._card_beat(s, 7, "running")
    assert len(av.dws.sent) == 4
    assert av.notified == []


def test_card_beat_logs_unaccepted_once(av, cfg):
    """card_update 返回 False 要留痕（每会话一次）：此前 except:pass 把失败全吞了，
    排查「用户看不到进度」时无从判断是没发、发失败、还是发了不显眼。"""
    setcfg(cfg, "ingress", card_progress_sec=0)
    av.dws.card_update = lambda *a: False
    s = sess(card_upd=0.0)
    av._card_beat(s, 7, "running")
    av._card_beat(s, 7, "running")
    assert s.get("card_fail_logged") is True


def test_notice_refreshes_throttle_stamp(av, cfg):
    """发完重要提示要刷新节流时间戳，否则紧接着的心跳帧会把它冲掉。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=30)
    s = sess(card_upd=0.0)
    av._long_task_notice(s, 7, row(elapsed=900), "running")
    assert time.time() - s["card_upd"] < 5


# ── 回归 #146：长任务态必须**持续**写在心跳帧里 ──────────────────────
# 实测事故：2026-10-16 #146 于 14:03:44 触发升级、long_notified=True、提示帧确实发出，
# 但 _card_beat 每 30s 用「正在为你办…（已用 Xs）」把整张卡片覆盖掉——提示只活了 30 秒，
# 用户 20 分钟后看卡片只见心跳文案，等于没提示。一次性帧对抗不了持续心跳。

def test_heartbeat_keeps_long_task_state(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=0)
    s = sess(card_upd=0.0, long_notified=True)
    av._card_beat(s, 7, "running")
    body = av.dws.sent[-1][1]
    assert "长任务后台执行中" in body
    assert "正在为你办" not in body          # 不能再被通用心跳文案覆盖
    assert "分段接续" in body and "取消" in body


def test_heartbeat_shows_segment_number(av, cfg):
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=0)
    s = sess(card_upd=0.0, long_notified=True, seg_seen=2)
    av._card_beat(s, 7, "running")
    assert "第 3 段" in av.dws.sent[-1][1]   # seg_seen=2 → 正在跑第 3 段


def test_heartbeat_first_segment_no_number(av, cfg):
    """seg_seen=0（还没被拆过）不显示段号，避免用户以为任务被拆过；
    seg_seen=1 起显示"第 2 段"。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=0)
    av._card_beat(sess(card_upd=0.0, long_notified=True, seg_seen=0), 7, "running")
    assert "段）" not in av.dws.sent[-1][1]
    av.dws.sent.clear()
    av._card_beat(sess(card_upd=0.0, long_notified=True, seg_seen=1), 7, "running")
    assert "第 2 段" in av.dws.sent[-1][1]


def test_heartbeat_normal_task_unchanged(av, cfg):
    """未升级任务的心跳文案一字不变（既有测试依赖「正在为你办…（已用 31s）」）。"""
    setcfg(cfg, "ingress", card_progress_sec=0)
    s = sess(card_upd=0.0, card_start=time.time() - 31)
    av._card_beat(s, 7, "running")
    assert "正在为你办…（已用 31s）" in av.dws.sent[-1][1]
    assert "长任务" not in av.dws.sent[-1][1]


def test_notice_then_heartbeat_both_carry_state(av, cfg):
    """端到端：升级帧发出后，后续每一帧心跳都仍带长任务态。"""
    setcfg(cfg, "ingress", long_task_mode="on", long_task_threshold_sec=600,
           card_progress_sec=0)
    s = sess(card_upd=0.0)
    assert av._long_task_notice(s, 7, row(elapsed=700), "running") is True
    for _ in range(3):
        s["card_upd"] = 0.0
        av._card_beat(s, 7, "running")
    assert all("长任务" in c for _, c in av.dws.sent), av.dws.sent
    # 首帧是升级告知，其后每帧心跳都持续带长任务态
    assert "比一般任务大" in av.dws.sent[0][1]
    assert all("长任务后台执行中" in c for _, c in av.dws.sent[1:]), av.dws.sent[1:]


# ── 「进度」查询指令 ──────────────────────────────────────────────
# 长任务提示承诺了「发进度看状态」，故 progress 必须是真命令：RUNNING 时不得被
# 当追问吞进 followups，IDLE 时不得被当暂存笔记存起来。#149 实测复现过两者。
class MiniRelay:
    def __init__(self, row):
        self._row = row
        self.calls = 0

    def poll(self, tid):
        self.calls += 1
        return dict(self._row)


@pytest.fixture
def pav(av, cfg):
    """带 poll 桩的 avatar（默认：#7 执行中、已跑 700s）。"""
    av.exec_relay = MiniRelay(row(elapsed=700))
    return av


def test_progress_keywords_configured(av, cfg):
    for w in ("进度", "进展", "状态"):
        assert av._kw(w, "progress"), w


def test_progress_reply_running(pav):
    s = sess()
    pav._progress_reply(s, "u1")
    out = pav.notified[-1]
    assert "#7 · 执行中" in out
    assert "11 分" in out          # 700s → 11 分 40 秒
    assert "取消" in out


def test_progress_reply_segment(pav):
    """段号口径与提示一致：[seg:N] 是已完成段数，正在跑第 N+1 段。"""
    pav.exec_relay = MiniRelay(row(elapsed=300, hint="xxx [seg:2] yyy"))
    s = sess()
    pav._progress_reply(s, "u1")
    assert "第 3 段" in pav.notified[-1]
    assert "不重做" in pav.notified[-1]


def test_progress_reply_followups_counted(pav):
    s = sess(followups=["再加个口径", "顺便看下上周"])
    pav._progress_reply(s, "u1")
    assert "2 条已并入本轮" in pav.notified[-1]


def test_progress_reply_idle_no_task(pav):
    s = sess(state="IDLE", task_id=None, last_task_id=147)
    pav._progress_reply(s, "u1")
    assert "没有进行中的任务" in pav.notified[-1]
    assert "#147" in pav.notified[-1]


def test_progress_reply_poll_error_no_crash(pav):
    pav.exec_relay = MiniRelay({"_error": "timeout"})
    pav._progress_reply(sess(), "u1")
    assert "查 #7 状态失败" in pav.notified[-1]


def test_progress_reply_terminal_no_cancel_hint(pav):
    pav.exec_relay = MiniRelay({"status": "done", "started_at": time.time()})
    pav._progress_reply(sess(state="IDLE", task_id=7), "u1")
    out = pav.notified[-1]
    assert "已完成" in out
    assert "取消" not in out       # 已结束的任务不该再教用户取消


def test_on_message_progress_not_swallowed_by_followups(pav):
    """RUNNING 时问进度：必须答，且不得进聚合缓冲（否则等于没问）。"""
    s = sess(followups=[])
    pav._on_message("", s, "进度", "u1")
    assert pav.notified and "#7 · 执行中" in pav.notified[-1]
    assert s["followups"] == []


def test_handle_self_progress_idle_not_saved_as_note(pav, cfg):
    """IDLE 时问进度：必须答，且不得被当暂存笔记存起来。"""
    notes = []
    pav.know.save_note = lambda t: notes.append(t)
    s = sess(state="IDLE", task_id=None, last_task_id=None)
    pav._handle_self(s, "进度", "u1")
    assert notes == []
    assert pav.notified and "没有进行中的任务" in pav.notified[-1]
