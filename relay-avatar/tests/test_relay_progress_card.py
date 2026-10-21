import time

import pytest

from app.main import Avatar


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


class CardDws:
    def __init__(self):
        self.frames = []
        self.sent = []

    def card_update(self, biz, content, flow_status):
        self.frames.append((biz, content, flow_status))
        return True

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}


class ProgressRelay:
    name = "relay"

    def __init__(self, resp=None):
        self.resp = resp or {
            "status": "running",
            "segment": 1,
            "max_segments": 3,
            "budget_sec": 1800,
            "elapsed_sec": 750,
            "segment_elapsed_sec": 150,
            "last_event_id": 42,
            "tool_calls": 18,
            "stage": "报告装配",
            "recent": [
                {"id": 41, "kind": "tool", "tool": "Bash",
                 "label": "📑 渲染报告", "detail": "render_report.py"},
                {"id": 42, "kind": "tool", "tool": "Read",
                 "label": "📖 读取文件", "detail": "data.json"},
            ],
            "queue": {"pending": 1, "active": 2},
        }
        self.calls = []

    def available(self):
        return True

    def poll(self, tid):
        return {"id": tid, "status": "running",
                "started_at": time.time() - 150,
                "resume_hint": "[seg:1] x"}

    def progress(self, tid, after_id=0, limit=5):
        self.calls.append({"tid": tid, "after_id": after_id, "limit": limit})
        return dict(self.resp)


@pytest.fixture
def av(cfg):
    a = Avatar(cfg, dry=True)
    a.dws = CardDws()
    a._notify_session = lambda s, text: a.dws.sent.append((s.get("reply"), text))
    return a


def sess(**kw):
    s = {"key": "o:sid", "card_biz": "BIZ1", "card_hint": "生成报告",
         "card_start": time.time() - 750, "card_upd": 0.0,
         "user_started_at": time.time() - 750, "state": "RUNNING",
         "task_id": 7, "reply": {"is_group": False}, "card_progress": {}}
    s.update(kw)
    return s


def test_card_beat_fetches_and_renders_progress(av, cfg):
    setcfg(cfg, "ingress", card_progress_sec=0, relay_progress_enabled=True)
    av.exec_relay = ProgressRelay()
    s = sess()
    av._card_beat(s, 7, "running", row=av.exec_relay.poll(7))
    prog = s["card_progress"]
    assert prog["cursor"] == 42
    assert prog["stage"] == "报告装配"
    assert len(prog["recent"]) == 2
    body = av.dws.frames[-1][1]
    assert "报告装配" in body
    assert "渲染报告 · render_report.py" in body
    assert "第 2/3 段" in body


def test_progress_fetch_throttled(av, cfg):
    setcfg(cfg, "ingress", card_progress_sec=30, relay_progress_enabled=True)
    av.exec_relay = ProgressRelay()
    s = sess(card_upd=0.0)
    av._card_beat(s, 7, "running", row=av.exec_relay.poll(7))
    first = len(av.exec_relay.calls)
    av._card_beat(s, 7, "running", row=av.exec_relay.poll(7))
    assert len(av.exec_relay.calls) == first
    s["card_upd"] = 0.0
    s["card_progress"]["fetched"] = 0.0
    av._card_beat(s, 7, "running", row=av.exec_relay.poll(7))
    assert len(av.exec_relay.calls) == first + 1
    assert av.exec_relay.calls[-1]["after_id"] == 42


def test_progress_404_disables_until_retry(av, cfg):
    setcfg(cfg, "ingress", card_progress_sec=0, relay_progress_enabled=True,
           relay_progress_retry_sec=1800)
    relay = ProgressRelay({"_error": 404})
    av.exec_relay = relay
    s = sess()
    av._card_beat(s, 7, "running", row={"status": "running"})
    av._card_beat(s, 7, "running", row={"status": "running"})
    assert len(relay.calls) == 1
    assert av._relay_progress_enabled is False


def test_progress_reply_includes_actions(av, cfg):
    setcfg(cfg, "ingress", relay_progress_enabled=True)
    av.exec_relay = ProgressRelay()
    s = sess()
    av._progress_reply(s, "200")
    out = av.dws.sent[-1][1]
    assert "#7 · 执行中" in out
    assert "当前阶段：报告装配" in out
    assert "渲染报告 · render_report.py" in out
    assert "发「取消」可随时中止" in out
