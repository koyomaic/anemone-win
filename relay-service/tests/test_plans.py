from __future__ import annotations

import time

import httpx
import pytest

from app import plans as plan_util
from app.db import DB
from conftest import audit_lines, submit, wait_task

# 假 agent：立即成功结束（无文件改动 → 任务自动 done），让 fire 测试无需真实 opencode。
FAKE_AGENT_OK = """#!/bin/sh
printf '%s\\n' '{"type":"text","sessionID":"ses_plan","part":{"type":"text","text":"ok"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_plan","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


@pytest.fixture()
def fake_cal(monkeypatch):
    """把 next_elapse 换成确定性假实现：合法表达式→1h 后；'BAD'→失败。

    服务器线程与测试同进程、同 app.plans 模块对象，monkeypatch 对端点生效，
    从而 API 测试不依赖宿主机是否有 systemd-analyze。
    """
    def fake(expr: str):
        if (expr or "").strip() == "BAD":
            return None, "invalid calendar"
        return time.time() + 3600, None
    monkeypatch.setattr(plan_util, "next_elapse", fake)
    return fake


# ---- 纯单元：日历解析 / 动作白名单 / 名称正则 ----

def test_parse_utc_elapse():
    out = ("  Original form: *-*-* 08:00:00\n"
           "Normalized form: *-*-* 08:00:00\n"
           "    Next elapse: Fri 2026-09-25 08:00:00 CST\n"
           "       (in UTC): Fri 2026-09-25 00:00:00 UTC\n"
           "       From now: 23h left\n")
    assert plan_util.parse_utc_elapse(out) == 1790294400.0  # 2026-09-25 00:00:00 UTC


def test_parse_utc_elapse_bad():
    assert plan_util.parse_utc_elapse("nope") is None
    assert plan_util.parse_utc_elapse("") is None
    assert plan_util.parse_utc_elapse("Next elapse: Fri 2026-09-25 08:00:00 CST") is None


def test_validate_action():
    assert plan_util.validate_action({"type": "task", "description": "hi"}) is None
    assert plan_util.validate_action({"type": "task", "description": "hi", "priority": "urgent",
                                      "targets": ["a"], "read_only": True, "project": "p"}) is None
    assert plan_util.validate_action({"type": "shell", "description": "x"}) is not None
    assert plan_util.validate_action({"type": "task"}) is not None
    assert plan_util.validate_action({"type": "task", "description": ""}) is not None
    assert plan_util.validate_action({"type": "task", "description": "x", "priority": "low"}) is not None
    assert plan_util.validate_action({"type": "task", "description": "x", "targets": "a"}) is not None
    assert plan_util.validate_action({"type": "task", "description": "x", "read_only": "yes"}) is not None
    assert plan_util.validate_action("nope") is not None


def test_name_re():
    assert plan_util.NAME_RE.match("daily-brief")
    assert plan_util.NAME_RE.match("日报_2026.09")
    assert not plan_util.NAME_RE.match("bad name")   # 空格
    assert not plan_util.NAME_RE.match("a" * 65)     # 超长
    assert not plan_util.NAME_RE.match("")           # 空


# ---- DB：CRUD + due + claim 原子性 ----

def test_db_plan_crud_and_claim(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    pid = db.create_plan(name="p1", owner_name="u", on_calendar="*-*-* 08:00:00",
                         action={"type": "task", "description": "d"},
                         enabled=True, next_run=1000.0)
    p = db.plan(pid)
    assert p["name"] == "p1" and p["action"]["description"] == "d" and p["enabled"] is True
    assert db.plan_by_name("p1")["id"] == pid
    assert db.plan_by_name("nope") is None

    # due：next_run=1000 <= now(2000) → 到点；now(500) → 未到点
    assert [x["id"] for x in db.due_plans(2000.0)] == [pid]
    assert db.due_plans(500.0) == []

    # claim 原子：首次成功推进 next_run/last_run；同 now 二次失败（防双发）
    assert db.claim_plan(pid, 2000.0, 3000.0) is True
    assert db.plan(pid)["next_run"] == 3000.0
    assert db.plan(pid)["last_run"] == 2000.0
    assert db.claim_plan(pid, 2000.0, 9999.0) is False

    # 停用后不 due、不可 claim
    db.update_plan(pid, enabled=False, next_run=None)
    assert db.due_plans(99999.0) == []
    assert db.claim_plan(pid, 99999.0, 1.0) is False

    # list / update / delete
    assert len(db.list_plans()) == 1
    db.update_plan(pid, name="p2")
    assert db.plan(pid)["name"] == "p2"
    assert db.delete_plan(pid) is True
    assert db.plan(pid) is None
    assert db.delete_plan(pid) is False
    db.close()


# ---- API：CRUD / 校验 / due 鉴权 / 属主越权 ----

def _client(relay, token) -> httpx.Client:
    return httpx.Client(base_url=relay["base"],
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)


def test_api_plan_crud_and_validation(relay, admin, member, fake_cal):
    r = member.post("/plans", json={"name": "daily", "on_calendar": "*-*-* 08:00:00",
                                    "action": {"type": "task", "description": "跑日报"}})
    assert r.status_code == 201, r.text
    p = r.json()
    pid = p["id"]
    assert p["owner_name"] == "tester" and p["enabled"] is True
    assert p["next_run"] > time.time()

    # 重名 409
    assert member.post("/plans", json={"name": "daily", "on_calendar": "*-*-* 08:00:00",
                                       "action": {"type": "task", "description": "x"}}).status_code == 409
    # 非法名 / 非法日历 / 非法动作 → 422
    assert member.post("/plans", json={"name": "bad name", "on_calendar": "*-*-* 08:00:00",
                                       "action": {"type": "task", "description": "x"}}).status_code == 422
    assert member.post("/plans", json={"name": "c", "on_calendar": "BAD",
                                       "action": {"type": "task", "description": "x"}}).status_code == 422
    assert member.post("/plans", json={"name": "a", "on_calendar": "*-*-* 08:00:00",
                                       "action": {"type": "shell", "description": "x"}}).status_code == 422

    # list / get
    assert any(x["id"] == pid for x in member.get("/plans").json()["plans"])
    assert member.get(f"/plans/{pid}").json()["name"] == "daily"

    # due 仅 admin
    assert member.get("/plans/due").status_code == 403
    assert admin.get("/plans/due").status_code == 200

    # 属主越权：另一成员不可改/删；admin 可以
    other = _client(relay, admin.post("/users", json={"name": "other"}).json()["token"])
    assert other.patch(f"/plans/{pid}", json={"enabled": False}).status_code == 403
    assert other.delete(f"/plans/{pid}").status_code == 403
    assert admin.patch(f"/plans/{pid}", json={"enabled": False}).status_code == 200
    other.close()

    # 停用 → next_run 清空；启用 → 重算
    r = member.patch(f"/plans/{pid}", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["next_run"] is None
    r = member.patch(f"/plans/{pid}", json={"enabled": True})
    assert r.json()["enabled"] is True and r.json()["next_run"] > time.time()

    # 改日历 → 重算 next_run
    r = member.patch(f"/plans/{pid}", json={"on_calendar": "Mon *-*-* 09:30:00"})
    assert r.json()["on_calendar"] == "Mon *-*-* 09:30:00" and r.json()["next_run"] > time.time()

    # 删除
    assert member.delete(f"/plans/{pid}").json()["ok"] is True
    assert member.get(f"/plans/{pid}").status_code == 404


# ---- fire：原子认领 + 提交任务（假 agent，非 integration） ----

def test_api_plan_fire(relay_factory, tmp_path, fake_cal):
    script = tmp_path / "fake_agent.sh"
    script.write_text(FAKE_AGENT_OK)
    script.chmod(0o755)
    r = relay_factory(agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"}, timeout=30)
    tok = admin_c.post("/users", json={"name": "planner"}).json()["token"]
    member_c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {tok}"}, timeout=30)

    pid = member_c.post("/plans", json={"name": "fire", "on_calendar": "*-*-* 08:00:00",
                                        "action": {"type": "task", "description": "只回复 ok"}}).json()["id"]

    # 回拨 next_run 到过去（直开第二个 DB 连接，模拟到点）
    d = DB(r["cfg"].db_path)
    d.update_plan(pid, next_run=time.time() - 1)
    d.close()

    assert any(p["id"] == pid for p in admin_c.get("/plans/due").json()["plans"])

    res = admin_c.post(f"/plans/{pid}/fire").json()
    assert res.get("task_id", 0) > 0, res
    tid = res["task_id"]

    # 计划已推进、记录 last_task_id；再次 fire（未到点）→ skipped，不双发
    p = admin_c.get(f"/plans/{pid}").json()
    assert p["last_task_id"] == tid and p["next_run"] > time.time()
    assert admin_c.post(f"/plans/{pid}/fire").json().get("skipped") is True

    # 任务确实创建并跑完（假 agent → done）
    t = wait_task(base, tok, tid, timeout=60)
    assert t["status"] in ("done", "review"), t
    assert t["user"] == "planner"

    # 审计留痕
    lines = audit_lines(r["cfg"].log_path)
    assert any(f"task={tid}\tuser=planner\tplan_fire" in ln for ln in lines)

    member_c.close()
    admin_c.close()
