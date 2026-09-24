"""tests/test_local_shadow.py —— local 影子记录（只登记不派发）。

背景：avatar 的 local 通路在本机跑 claude，此前**没有 relay task_id**，于是
任务列表、看板、`/tasks` 全都查不到它。2026-09-24 毕倩那条任务被服务重启打断后，
只能靠翻 journal + audit.log + state.json 三处对账才拼出真相。

影子记录的设计约束（逐条在此锁死）：
① 只登记不派发——**绝不进调度器**，故不占 max_concurrent 额度；
② 直接以 status='running'、engine='local' 落库，永不经过 'queued'
   （否则调度器可能捞起来执行一个没有 workdir/baseline 的任务）；
③ 终态由 avatar 经 POST /tasks/{id}/local_status 回报，**幂等**且拒绝从终态再转
   （avatar 重试与 sweep 兜底可能并发到达，谁先落谁算）；
④ 该端点只接受 engine='local' 的记录——普通 relay 任务状态由 executor 自己写，
   外部不得改；
⑤ avatar 崩溃来不及回报时，sweep_stale 超 local_stale_minutes 判 failed，
   不留永久 running 的僵尸行；
⑥ 不参与冲突检测（active_tasks 排除 engine='local'）——local 只写作用域内文本件、
   不做 baseline 快照，其 changed_files 只能由 agent 自报，不可信。
"""
from __future__ import annotations

import time

import httpx

from app.db import DB
from app.main import sweep_stale


def _mk_local(member, desc="统计门店日销"):
    r = member.post("/tasks", json={"description": desc, "backend": "local"})
    assert r.status_code == 202, r.text
    return r.json()


def test_local_record_created_running_not_queued(relay, admin, member):
    """约束①②：直接 running 落库、engine=local、read_only、无 workdir。"""
    body = _mk_local(member)
    assert body["status"] == "running" and body["backend"] == "local"
    tid = body["task_id"]
    t = member.get(f"/tasks/{tid}").json()
    assert t["status"] == "running"
    assert t["engine"] == "local"
    assert t["read_only"] is True
    assert t["workdir"] == ""            # relay 不执行它，不该有工作目录
    assert t["started_at"]               # 起跑时刻已记


def test_local_record_visible_in_task_list(relay, admin, member):
    """这就是本次改动的目的：local 任务在任务列表里查得到。"""
    tid = _mk_local(member, "生成一周市场研判报告")["task_id"]
    rows = member.get("/tasks?limit=50").json()["tasks"]
    assert tid in [r["id"] for r in rows]
    hit = [r for r in rows if r["id"] == tid][0]
    assert hit["engine"] == "local" and hit["status"] == "running"


def test_local_record_does_not_consume_concurrency(relay, admin, member):
    """约束①：/health 的 active 取 sched.active（调度器进程内计数），
    影子记录从不进调度器，故一个额度都不占。"""
    before = httpx.get(relay["base"] + "/health", timeout=5).json()["active"]
    for i in range(5):
        _mk_local(member, f"只读任务 {i}")
    after = httpx.get(relay["base"] + "/health", timeout=5).json()["active"]
    assert after == before


def test_local_status_report_done(relay, admin, member):
    """约束③：回报终态，result/tokens 落库。"""
    tid = _mk_local(member)["task_id"]
    r = member.post(f"/tasks/{tid}/local_status",
                    json={"status": "done", "result": "结论正文",
                          "tokens": 1234, "cost": 0.5,
                          "changed_files": ["shared/report/x.html"]})
    assert r.status_code == 200
    assert r.json() == {"task_id": tid, "status": "done",
                        "already_terminal": False}
    t = member.get(f"/tasks/{tid}").json()
    assert t["status"] == "done" and t["result"] == "结论正文"
    assert t["tokens"] == 1234 and t["finished_at"]
    assert t["changed_files"] == ["shared/report/x.html"]


def test_local_status_idempotent_and_terminal_frozen(relay, admin, member):
    """约束③：已终态再报 → already_terminal=True，且**不得覆盖**先到的结局。"""
    tid = _mk_local(member)["task_id"]
    member.post(f"/tasks/{tid}/local_status",
                json={"status": "done", "result": "第一次"})
    r = member.post(f"/tasks/{tid}/local_status",
                    json={"status": "failed", "error": "第二次想改判"})
    assert r.status_code == 200
    assert r.json()["already_terminal"] is True
    t = member.get(f"/tasks/{tid}").json()
    assert t["status"] == "done" and t["result"] == "第一次"   # 未被覆盖
    assert t["error"] == ""


def test_local_status_rejects_relay_task(relay, admin, member):
    """约束④：普通 relay 任务的状态由 executor 写，外部不得经此端点改。"""
    r = member.post("/tasks", json={"description": "只读任务，不要改任何文件"})
    assert r.status_code == 202
    tid = r.json()["task_id"]
    rr = member.post(f"/tasks/{tid}/local_status", json={"status": "done"})
    assert rr.status_code == 409
    assert "not a local shadow record" in rr.json()["detail"]


def test_local_status_validates_input(relay, admin, member):
    tid = _mk_local(member)["task_id"]
    assert member.post(f"/tasks/{tid}/local_status",
                       json={"status": "running"}).status_code == 422
    assert member.post(f"/tasks/{tid}/local_status",
                       json={"status": "review"}).status_code == 422
    assert member.post("/tasks/999999/local_status",
                       json={"status": "done"}).status_code == 404


def test_backend_field_validated(relay, admin, member):
    r = member.post("/tasks", json={"description": "x", "backend": "bogus"})
    assert r.status_code == 422
    assert "backend must be relay|local" in r.json()["detail"]


def test_backend_defaults_to_relay(relay, admin, member):
    """回归：不传 backend 的老客户端行为完全不变（正常派发执行）。"""
    r = member.post("/tasks", json={"description": "只读任务，不要改任何文件"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and "backend" not in body
    t = member.get(f"/tasks/{body['task_id']}").json()
    assert t["engine"] != "local"


def test_local_excluded_from_conflict_detection(relay, admin, member):
    """约束⑥：影子记录不参与 targets 重叠判定。"""
    _mk_local(member)
    db = DB(relay["cfg"].db_path)
    act = db.active_tasks()
    assert all(t["engine"] != "local" for t in act)
    # 带 targets 的正常任务不因影子记录而被判冲突
    r = member.post("/tasks", json={"description": "改文件",
                                    "targets": ["shared/report/a.html"]})
    assert r.status_code == 202, r.text


def test_sweep_marks_orphan_local_failed(relay, admin, member):
    """约束⑤：avatar 崩溃来不及回报 → sweep 判 failed，不留僵尸行。"""
    tid = _mk_local(member, "会被孤儿掉的 local 任务")["task_id"]
    db = DB(relay["cfg"].db_path)
    cfg = relay["cfg"]
    bus = _NullBus()
    audit = _NullAudit()
    # 未超期：不动
    assert sweep_stale(db, bus, audit, cfg, _NullWs()) == []
    assert db.task(tid)["status"] == "running"
    # 把 created_at 推到超期之外（created_at 不在 TASK_FIELDS 白名单，直接改库）
    old = time.time() - (cfg.local_stale_minutes * 60 + 60)
    _force_created_at(db, tid, old)
    swept = sweep_stale(db, bus, audit, cfg, _NullWs())
    assert tid in swept
    t = db.task(tid)
    assert t["status"] == "failed" and t["finished_at"]
    assert "avatar 未回报" in t["error"]
    assert bus.seen and bus.seen[-1]["s"] == "failed"


def test_sweep_leaves_fresh_local_alone(relay, admin, member):
    """未超期的在跑 local 记录不得被误清扫。"""
    tid = _mk_local(member)["task_id"]
    db = DB(relay["cfg"].db_path)
    sweep_stale(db, _NullBus(), _NullAudit(), relay["cfg"], _NullWs())
    assert db.task(tid)["status"] == "running"


# ---- 测试替身 ----

def _force_created_at(db, tid, ts):
    """created_at 不在 TASK_FIELDS 白名单内，直接改库（仅测试用）。"""
    db._run("UPDATE tasks SET created_at = ? WHERE id = ?", (ts, tid))


class _NullBus:
    def __init__(self):
        self.seen = []

    def publish(self, tid, **kw):
        self.seen.append({"tid": tid, **kw})


class _NullAudit:
    def line(self, *a, **k):
        pass


class _NullWs:
    def cleanup_terminal(self, t):
        pass
