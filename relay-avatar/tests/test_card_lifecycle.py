import json
import time

import pytest

from app.main import Avatar


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


def local_ready(cfg, shared, **kw):
    sb = shared / "work"
    sb.mkdir(parents=True, exist_ok=True)
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local", **kw)
    setcfg(cfg, "security", l2_sandbox_dir=str(sb),
           l2_add_dirs=[str(shared / "knowledge")])
    return sb


class CardDws:
    def __init__(self):
        self.created = []
        self.frames = []
        self.sent = []
        self.files = []
        self.update_ok = True
        self.create_ok = True

    def resolve_userid(self, open_id, sender, cache):
        return {"sid200": "200"}.get(open_id, "")

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.files.append((target, path))
        return {"ok": True}

    def react(self, cid, mid, emoji):
        return {"ok": True}

    def react_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def remove_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def card_create(self, target):
        if not self.create_ok:
            return {}
        self.created.append(target)
        return {"cardRef": {"bizId": f"BIZ{len(self.created)}"}}

    def card_update(self, biz, content, flow_status):
        self.frames.append((biz, content, flow_status))
        return self.update_ok

    def mark_read(self, *a, **k):
        return {"ok": True}

    def run(self, args, timeout=60):
        return {}


class FakeExecRelay:
    name = "relay"

    def __init__(self):
        self.submitted = []
        self.shadows = []
        self.local_status = []
        self.tasks = {}

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk",
               requester=""):
        tid = len(self.submitted) + 1
        self.submitted.append({"tid": tid, "desc": desc})
        self.tasks[tid] = {"id": tid, "status": "queued"}
        return {"task_id": tid, "status": "queued"}

    def create_local_record(self, desc, uid="", requester=""):
        tid = 900 + len(self.shadows) + 1
        self.shadows.append({"tid": tid})
        return {"task_id": tid, "status": "running", "backend": "local"}

    def report_local_status(self, tid, status, uid="", result="", error=""):
        self.local_status.append({"tid": tid, "status": status})
        return {"task_id": tid, "status": status}

    def poll(self, tid):
        return self.tasks.get(tid, {"_error": 404})

    def approve(self, *a, **k):
        return {"ok": True}

    def confirm(self, *a, **k):
        return {"ok": True}

    def cancel(self, *a, **k):
        return {"ok": True}


class FakeExecLocal:
    name = "local"

    def __init__(self, result=None):
        self.result = result or {"text": "本机结论", "escalate": False}
        self.calls = []

    def available(self):
        return True

    def local_timeout(self, text):
        return 300

    def run_task(self, uid, text, kb="", on_step=None):
        self.calls.append(text)
        if on_step:
            on_step("📖 读取文件 · data.xlsx", ["📖 读取文件 · data.xlsx"])
        return dict(self.result)

    def run_l3(self, uid, text, kb="", on_step=None):
        raise RuntimeError("boom")


@pytest.fixture
def av(cfg, shared):
    cfg._p.setdefault("ingress", {})["use_card"] = True
    local_ready(cfg, shared)
    a = Avatar(cfg, dry=True)
    a.dws = CardDws()
    a.inbox.dws = a.dws
    a.exec_relay = FakeExecRelay()
    a.exec_local = FakeExecLocal()
    a._notify_session = lambda s, text: a.dws.sent.append((s.get("reply"), text))
    return a


def sess_of(a, text="生成报告", sid="sid200", n="1"):
    a.on_event({"event_type": "user_im_message_receive_o2o_all",
                "content": text, "sender": "张三",
                "sender_open_dingtalk_id": sid, "message_id": f"m{n}",
                "conversation_id": f"c{n}", "event_id": f"e{n}",
                "_uid": "200"})
    return a.sess.sessions["o:" + sid]


def wait_local(s, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and not s.get("local_result"):
        time.sleep(0.01)
    return s.get("local_result")


def test_local_escalate_reuses_single_card(av):
    av.exec_local = FakeExecLocal({"text": "", "escalate": True})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    assert len(av.dws.created) == 1
    assert s.get("card_biz") == "BIZ1"
    assert s.get("card_backend") == "relay"
    assert any(b == "BIZ1" and f == 1 and "已转后台持久通道" in c
               for b, c, f in av.dws.frames)
    assert av.cards.active().get("BIZ1") is not None


def test_local_done_finalizes_card(av):
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    assert any("本机预算" in c and "读取文件" in c
               for _, c, f in av.dws.frames if f == 1)
    av._drain_local()
    assert not s.get("card_biz")
    assert av.dws.frames[-1][2] == 3
    assert "本机结论" in av.dws.frames[-1][1]
    assert av.cards.active() == {}


def test_final_failure_registers_orphan_and_falls_back(av):
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av.dws.update_ok = False
    av.dws.sent.clear()
    av._drain_local()
    assert not s.get("card_biz")
    assert any("本机结论" in c for _, c in av.dws.sent)
    orphans = (av.st.run / "card_orphans.jsonl").read_text(encoding="utf-8")
    assert "BIZ1" in orphans and "final_failed" in orphans


def test_relay_finish_finalizes_reused_card(av):
    av.exec_local = FakeExecLocal({"text": "", "escalate": True})
    s = sess_of(av)
    av._flush(s, uid="200")
    wait_local(s)
    av._drain_local()
    tid = s["task_id"]
    av._finish(s, {"id": tid, "status": "done", "result": "后台结果"})
    assert len(av.dws.created) == 1
    assert av.dws.frames[-1] == ("BIZ1", av.dws.frames[-1][1], 3)
    assert "后台结果" in av.dws.frames[-1][1]
    assert not s.get("card_biz")


def test_stale_card_is_orphaned_before_new_card(av):
    s = sess_of(av)
    s["state"] = "IDLE"
    s["card_biz"] = "OLD"
    s["card_backend"] = "relay"
    av._card_open(s, 1, "新任务", backend="relay", reuse=False)
    assert s.get("card_biz") == "BIZ1"
    assert len(av.dws.created) == 1
    orphans = (av.st.run / "card_orphans.jsonl").read_text(encoding="utf-8")
    assert "OLD" in orphans


def test_sweep_finalizes_inactive_card(av):
    av.cards.open("STALE", "sig", 999, "relay", "旧任务")
    av.cards._active["STALE"]["last_update"] = time.time() - 7200
    av.cards.save()
    av._sweep_cards(force=True)
    assert any(b == "STALE" and f == 3 for b, c, f in av.dws.frames)
    assert av.cards.active() == {}


def test_l3_exception_finalizes_card(av):
    s = sess_of(av, text="文件问答")
    s["state"] = "IDLE"
    av._l3_qa(s, "200", "总结这个文件")
    assert av.dws.frames[-1][2] == 5
    assert not s.get("card_biz")
    assert any("文件问答处理失败" in c for _, c in av.dws.sent)


def test_user_cancel_finalizes_card(av):
    s = sess_of(av)
    av._flush(s, uid="200", force_relay=True)
    assert s.get("card_biz") == "BIZ1"
    av._on_message("o:sid200", s, "取消", "200")
    assert av.dws.frames[-1][2] == 3
    assert "已取消" in av.dws.frames[-1][1]
    assert not s.get("card_biz")


def test_gate_deny_finalizes_card(av):
    s = sess_of(av)
    av._flush(s, uid="200", force_relay=True)
    s["state"] = "WAIT_APPROVAL"
    av._on_message("o:sid200", s, "拒绝", "200")
    assert av.dws.frames[-1][2] == 3
    assert "已拒绝" in av.dws.frames[-1][1]
    assert not s.get("card_biz")
    assert s["state"] == "IDLE"
