"""tests/test_recall.py —— 出站回执落档 + 撤回链路。"""
import json

from app import recall
from app.storage import Storage


class FakeDws:
    """query-send-status / recall 的假 dws。"""

    def __init__(self, resolvable=True):
        self.resolvable = resolvable
        self.calls = []

    def run(self, args, timeout=60):
        self.calls.append(args)
        if "query-send-status" in args:
            if not self.resolvable:
                return {"error": "expired"}
            return {"result": {"openConversationId": "cidX",
                               "openMessageId": "msgBASE64==",
                               "sendStatus": "SUCCESS"}}
        if "recall" in args:
            return {"success": True}
        return {}


def test_open_task_id_layers():
    assert recall.open_task_id(
        {"data": {"result": {"openTaskId": "T1"}}}) == "T1"
    assert recall.open_task_id({"result": {"openTaskId": "T2"}}) == "T2"
    assert recall.open_task_id({"openTaskId": "T3"}) == "T3"
    assert recall.open_task_id({"dry": True}) == ""
    assert recall.open_task_id(None) == ""


def test_record_and_load(cfg, storage):
    recall.record(storage, "text", "18434",
                  {"data": {"result": {"openTaskId": "TA"}}},
                  task_id=8, snippet="结果A")
    recall.record(storage, "card", "sidX",
                  {"data": {"result": {"openTaskId": "TB"}}},
                  task_id=9, snippet="card:BIZ")
    recall.record(storage, "text", "x", {"dry": True})   # 无回执不落档
    lines = (storage.run / "outbox.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2
    recent = recall.load_outbox(storage, n=1)
    assert recent[0]["open_task_id"] == "TB"            # 新→旧
    by_task = recall.load_outbox(storage, task_id=8, n=5)
    assert len(by_task) == 1 and by_task[0]["snippet"] == "结果A"


def test_resolve_and_recall(cfg, storage):
    dws = FakeDws()
    cid, mid = recall.resolve(dws, "TA")
    assert cid == "cidX" and mid == "msgBASE64=="
    r = recall.recall_message(dws, cid, mid)
    assert r["ok"]


def test_recall_recent_happy(cfg, storage):
    recall.record(storage, "text", "18434",
                  {"data": {"result": {"openTaskId": "TA"}}}, snippet="A")
    results = recall.recall_recent(storage, FakeDws(), n=1)
    assert results[0]["recalled"] is True
    assert results[0]["snippet"] == "A"


def test_recall_recent_unresolvable(cfg, storage):
    recall.record(storage, "text", "18434",
                  {"data": {"result": {"openTaskId": "TA"}}}, snippet="A")
    results = recall.recall_recent(storage, FakeDws(resolvable=False), n=1)
    assert results[0]["recalled"] is False
    assert "未解析" in results[0]["reason"]


def test_gc_outbox(cfg, storage):
    for i in range(600):
        recall.record(storage, "text", "u",
                      {"openTaskId": f"T{i}"}, snippet=str(i))
    recall.gc_outbox(storage, keep=500)
    lines = (storage.run / "outbox.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 500
    assert json.loads(lines[-1])["snippet"] == "599"    # 留最新


def test_notify_records_receipt(cfg):
    """守护进程发送路径自动落回执（_notify → outbox.jsonl）。"""
    from app.main import Avatar
    av = Avatar(cfg, dry=True)

    class DwsWithTaskId:
        def send_text(self, target, content):
            return {"ok": True, "data": {"result": {"openTaskId": "OT9"}}}

    av.dws = DwsWithTaskId()
    av._notify({"user_id": "18434"}, "测试消息", task_id=9)
    recs = recall.load_outbox(av.st, n=5)
    assert recs and recs[0]["open_task_id"] == "OT9"
    assert recs[0]["task_id"] == 9 and recs[0]["to"] == "18434"
