from __future__ import annotations

import json
import time
import types

import httpx
import pytest

import app.main as m
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def relay_dash(relay_factory) -> dict:
    return relay_factory(dashboard_password="dash-test-pw")


def test_dashboard_disabled_without_password(relay):
    r = httpx.get(relay["base"] + "/", timeout=5)
    assert r.status_code == 404
    r = httpx.get(relay["base"] + "/dashboard/data", timeout=5)
    assert r.status_code == 404
    r = httpx.get(relay["base"] + "/dashboard/tokens", timeout=5)
    assert r.status_code == 404


def test_dashboard_login_page_and_data(relay_dash):
    c = httpx.Client(base_url=relay_dash["base"], timeout=10)
    try:
        r = c.get("/")
        assert r.status_code == 200
        assert "访问口令" in r.text

        r = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert r.status_code == 303
        assert "/?e=1" in r.headers["location"]

        r = c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        assert r.status_code == 302
        assert "relay_dash" in c.cookies

        r = c.get("/")
        assert r.status_code == 200
        assert "调用情况" in r.text

        r = c.get("/dashboard/data")
        assert r.status_code == 200
        d = r.json()
        assert set(d) == {"metrics", "tasks", "report"}
        assert "active" in d["metrics"] and "by_user" in d["report"]

        r = httpx.get(relay_dash["base"] + "/dashboard/data", timeout=5)
        assert r.status_code == 401

        c.get("/logout", follow_redirects=False)
        assert c.get("/dashboard/data").status_code == 401
    finally:
        c.close()


def test_token_mgmt_crud(relay_dash):
    base = relay_dash["base"]
    c = httpx.Client(base_url=base, timeout=10)
    try:
        # 未登录 → 401
        assert c.get("/dashboard/tokens").status_code == 401
        r = c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        assert r.status_code == 302 and "relay_dash" in c.cookies

        # 查：初始为空
        assert c.get("/dashboard/tokens").json() == {"users": []}

        # 增
        r = c.post("/dashboard/tokens", json={"name": "测试员"})
        assert r.status_code == 201, r.text
        tok1 = r.json()["token"]
        assert tok1.startswith("ta_")

        assert c.post("/dashboard/tokens", json={"name": "测试员"}).status_code == 409
        assert c.post("/dashboard/tokens", json={"name": "  "}).status_code == 422

        # 新 token 立即可用
        h1 = {"Authorization": f"Bearer {tok1}"}
        assert httpx.get(base + "/tasks", headers=h1, timeout=10).status_code == 200

        # 查：列表展示完整 token
        d = c.get("/dashboard/tokens").json()
        assert len(d["users"]) == 1
        u = d["users"][0]
        assert u["name"] == "测试员"
        assert u["token"] == tok1
        assert u["tasks_7d"] == 0 and u["done_7d"] == 0
        assert u["created_at"] > 0
        # 核销状态：未激活 → consumed_at 为空、设备数 0
        assert u["consumed_at"] is None and u["devices"] == 0

        # 激活（核销 token 并绑定设备）→ 监控页体现「已核销」+ 设备数
        r = httpx.post(base + "/auth/activate",
                       json={"token": tok1, "device_id": "dev-dash-1",
                             "device_name": "pytest"}, timeout=10)
        assert r.status_code == 200, r.text
        u = c.get("/dashboard/tokens").json()["users"][0]
        assert u["consumed_at"] and u["consumed_at"] > 0
        assert u["devices"] == 1

        # 改（重置）：旧 token 立即失效，新 token 可用
        r = c.post("/dashboard/tokens/%E6%B5%8B%E8%AF%95%E5%91%98/regenerate")  # 测试员
        assert r.status_code == 200, r.text
        tok2 = r.json()["token"]
        assert tok2.startswith("ta_") and tok2 != tok1
        assert httpx.get(base + "/tasks", headers=h1, timeout=10).status_code == 401
        assert httpx.get(base + "/tasks",
                         headers={"Authorization": f"Bearer {tok2}"}, timeout=10).status_code == 200

        # 删
        assert c.delete("/dashboard/tokens/%E6%B5%8B%E8%AF%95%E5%91%98").json() == {"ok": True, "name": "测试员"}
        assert httpx.get(base + "/tasks",
                         headers={"Authorization": f"Bearer {tok2}"}, timeout=10).status_code == 401
        assert c.get("/dashboard/tokens").json() == {"users": []}
        assert c.delete("/dashboard/tokens/%E6%B5%8B%E8%AF%95%E5%91%98").status_code == 404
        assert c.post("/dashboard/tokens/%E6%B5%8B%E8%AF%95%E5%91%98/regenerate").status_code == 404
    finally:
        c.close()


# ---- 登录防爆破：窗口内同 IP 失败达上限 → 锁定；窗口过后自动解锁 ----

def test_login_rate_limit(relay_dash, monkeypatch):
    monkeypatch.setattr(m, "LOGIN_LOCK_SEC", 1.0)  # 测试用短窗口
    c = httpx.Client(base_url=relay_dash["base"], timeout=10)
    try:
        for _ in range(m.LOGIN_MAX_FAILS):
            r = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
            assert r.status_code == 303 and "/?e=1" in r.headers["location"]
        # 锁定后即使口令正确也拒绝：429 + Retry-After，且不发会话 cookie
        r = c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        assert "尝试次数过多" in r.text
        assert "relay_dash" not in c.cookies
        # 窗口过后自动解锁
        time.sleep(1.1)
        r = c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        assert r.status_code == 302 and "relay_dash" in c.cookies
        # 失败与锁定均留审计痕迹（ip + 累计次数）
        log = (relay_dash["ws"] / "relay.log").read_text(encoding="utf-8")
        assert "login_fail" in log and "ip=127.0.0.1" in log
        assert "login_locked" in log
    finally:
        c.close()


def test_login_success_clears_fails(relay_dash):
    c = httpx.Client(base_url=relay_dash["base"], timeout=10)
    try:
        for _ in range(m.LOGIN_MAX_FAILS - 1):  # 差一次到上限
            c.post("/login", data={"password": "wrong"}, follow_redirects=False)
        r = c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        assert r.status_code == 302 and "relay_dash" in c.cookies
        c.get("/logout", follow_redirects=False)
        # 成功已清零：再失败 MAX-1 次仍不锁
        for _ in range(m.LOGIN_MAX_FAILS - 1):
            r = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
            assert "/?e=1" in r.headers["location"]
    finally:
        c.close()


# ---- 启动自检：admin_token 缺失/默认值拒绝启动（/admin/update 可触发部署） ----

def test_create_app_rejects_default_admin_token(tmp_path):
    for bad in ("change-me", ""):
        cfg = Settings(db_path=str(tmp_path / "relay.db"), workspace_root=str(tmp_path),
                       shared_dir=str(tmp_path / "shared"), task_root=str(tmp_path / "tasks"),
                       users_root=str(tmp_path / "users"), admin_token=bad)
        with pytest.raises(RuntimeError):
            create_app(cfg)


# ---- 系统定时器只读列表（dashboard「计划」tab 下方） ----

def test_list_system_timers_parses_and_sorts(monkeypatch):
    raw = json.dumps([
        {"unit": "stopped.timer", "activates": "stopped.service",
         "next": None, "left": None, "last": 0, "passed": 0},
        {"unit": "later.timer", "activates": "later.service",
         "next": 2_000_000_000_000, "left": 0, "last": 1_000_000_000_000, "passed": 0},
        {"unit": "soon.timer", "activates": "soon.service",
         "next": 1_500_000_000_000, "left": 0, "last": 0, "passed": 0},
    ])
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=raw))
    out = m._list_system_timers()
    # 运行中按 next 升序在前，未排程（next=null）沉底
    assert [t["unit"] for t in out] == ["soon.timer", "later.timer", "stopped.timer"]
    assert out[0]["next"] == 1_500_000.0 and out[0]["running"] is True
    assert out[0]["last"] is None          # last=0 → 从未运行 → None
    assert out[1]["last"] == 1_000_000.0   # 微秒 → 秒
    assert out[2]["next"] is None and out[2]["running"] is False


def test_list_system_timers_degrades_to_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("no systemctl")
    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m._list_system_timers() == []


def test_dashboard_timers_endpoint_auth_and_shape(relay_dash, monkeypatch):
    fixed = [{"unit": "x.timer", "activates": "x.service",
              "next": 1.0, "last": None, "running": True}]
    monkeypatch.setattr(m, "_list_system_timers", lambda: fixed)
    c = httpx.Client(base_url=relay_dash["base"], timeout=10)
    try:
        assert c.get("/dashboard/timers").status_code == 401  # 未登录
        c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        r = c.get("/dashboard/timers")
        assert r.status_code == 200
        assert r.json() == {"timers": fixed}
    finally:
        c.close()


def test_dashboard_page_has_timers_section(relay_dash):
    c = httpx.Client(base_url=relay_dash["base"], timeout=10)
    try:
        c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
        html = c.get("/").text
        assert 'id="timers"' in html and "系统定时器" in html
    finally:
        c.close()


# ---- P1 运营视角：days 档位 / 用户 last_active / 任务详情端点 ----

def _dash_login(base: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=10)
    c.post("/login", data={"password": "dash-test-pw"}, follow_redirects=False)
    return c


def test_dashboard_data_days_param(relay_dash):
    c = _dash_login(relay_dash["base"])
    try:
        assert c.get("/dashboard/data").json()["report"]["days"] == 7
        assert c.get("/dashboard/data?days=30").json()["report"]["days"] == 30
        # 任意值收敛到两档：<30 → 7，>=30 → 30（防任意区间扫库）
        assert c.get("/dashboard/data?days=999").json()["report"]["days"] == 30
        assert c.get("/dashboard/data?days=3").json()["report"]["days"] == 7
    finally:
        c.close()


def test_report_by_user_last_active_and_failed(tmp_path):
    from app.db import DB
    db = DB(str(tmp_path / "relay.db"))
    t1 = db.create_task(user="甲", description="x")
    db.set(t1, status="failed")
    t2 = db.create_task(user="甲", description="y")
    db.set(t2, status="done", tokens=3)
    u = db.report(7)["by_user"][0]
    assert u["tasks"] == 2 and u["done"] == 1 and u["failed"] == 1
    assert u["last_active"] > 0 and u["tokens"] == 3
    db.close()


def test_dashboard_task_detail(relay_dash):
    from app.db import DB
    db = DB(str(relay_dash["ws"] / "relay.db"))
    tid = db.create_task(user="tester", description="详情测试")
    db.set(tid, status="done", tokens=5, result="ok", finished_at=time.time())
    db.close()
    base = relay_dash["base"]
    anon = httpx.Client(base_url=base, timeout=10)
    try:
        assert anon.get(f"/dashboard/tasks/{tid}").status_code == 401  # 未登录
    finally:
        anon.close()
    c = _dash_login(base)
    try:
        r = c.get(f"/dashboard/tasks/{tid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["description"] == "详情测试"
        assert body["status"] == "done" and body["tokens"] == 5
        assert body["result"] == "ok"
        # 内部实现字段不回显
        assert "workdir" not in body and "session_id" not in body
        assert c.get("/dashboard/tasks/999999").status_code == 404
    finally:
        c.close()


def test_dashboard_page_has_ops_elements(relay_dash):
    c = _dash_login(relay_dash["base"])
    try:
        html = c.get("/").text
        for marker in ("任务趋势", 'id="trend"', 'id="fstatus"', 'id="fuser"',
                       'id="modal"', "用户运营", "rng30", "openDetail"):
            assert marker in html, marker
    finally:
        c.close()
