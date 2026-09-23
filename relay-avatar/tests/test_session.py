"""tests/test_session.py —— 会话状态机 + debounce + resume_from + 去重。"""
import time

from app.session import SessionManager


def make_ev(**kw):
    ev = {"conversation_id": "cid1", "message_id": "m1", "sender": "张三",
          "_uid": "200"}
    ev.update(kw)
    return ev


def test_new_session_and_persist(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:sid1", "张三", "sid1", make_ev(), False)
    assert s["state"] == "IDLE" and s["uid"] == "200"
    sm.start_buffering(s, "任务内容")
    assert s["state"] == "BUFFERING" and s["buffer"] == ["任务内容"]
    sm.save()
    sm2 = SessionManager(cfg, storage)
    assert sm2.sessions["o:sid1"]["state"] == "BUFFERING"
    assert sm2.sessions["o:sid1"]["buffer"] == ["任务内容"]


def test_dedup(cfg, storage):
    sm = SessionManager(cfg, storage)
    assert not sm.already_seen("e1")
    assert sm.already_seen("e1")
    assert not sm.already_seen("")


def test_due_flush(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    sm.start_buffering(s, "x")
    assert sm.due() == []
    s["deadline"] = time.time() - 1
    due = sm.due()
    assert len(due) == 1 and due[0][2] == "buffer"
    s["state"] = "FOLLOWUP"
    assert sm.due()[0][2] == "followup"


def test_resume_candidate(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    assert sm.resume_candidate(s) is None
    s["last_task_id"] = 42
    s["last_activity"] = time.time()
    assert sm.resume_candidate(s) == 42
    s["last_activity"] = time.time() - 99999
    assert sm.resume_candidate(s) is None


def test_cleanup_expired(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    s["last_activity"] = time.time() - 999999
    sm.cleanup_expired()
    assert "o:a" not in sm.sessions
    s2 = sm.get_or_create("o:b", "李四", "b", make_ev(), False)
    s2["buffer"] = ["x"]
    s2["last_activity"] = time.time() - 999999
    sm.cleanup_expired()
    assert "o:b" in sm.sessions   # 有缓冲不清
