from __future__ import annotations

import time

import httpx
import pytest

from conftest import EXEC_DONE, audit_lines, submit, wait_task


def _tok(member) -> str:
    return member.headers["Authorization"].split()[1]


def _wait_pending(base: str, tok: str, tid: int, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = httpx.get(f"{base}/tasks/{tid}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()
        if last["status"] in {"pending_approval"} | EXEC_DONE:
            return last
        time.sleep(0.6)
    raise TimeoutError(f"task {tid} 未进入 pending_approval: last={last}")


def _wait_exec_done(member, tid: int, timeout: float = 240) -> dict:
    """等待执行结束；若续跑再次被拦截则代为批准（最多 2 次）。"""
    deadline = time.time() + timeout
    reapproved = 0
    last = None
    while time.time() < deadline:
        last = member.get(f"/tasks/{tid}").json()
        if last["status"] == "pending_approval" and reapproved < 2:
            member.post(f"/tasks/{tid}/approve", json={"decision": "approve"})
            reapproved += 1
            continue
        if last["status"] in EXEC_DONE:
            return last
        time.sleep(0.8)
    raise TimeoutError(f"task {tid} 未在 {timeout}s 内执行结束: last={last}")


def _submit_until_blocked(base: str, tok: str, description: str,
                          attempts: int = 3, timeout: float = 60) -> tuple[int, dict]:
    """提交直到任务进入 pending_approval。

    真实模型有时不执行高危命令就直接完成（review/failed），或未在超时内触发；
    这类情况重发重试，消除模型不确定性带来的偶发失败。返回 (task_id, task)。
    """
    last = None
    for _ in range(attempts):
        tid = submit(base, tok, description)["task_id"]
        try:
            t = _wait_pending(base, tok, tid, timeout=timeout)
        except TimeoutError as e:
            last = str(e)
            continue
        last = t
        if t["status"] == "pending_approval":
            return tid, t
    raise AssertionError(f"{attempts} 次提交后仍未进入 pending_approval: last={last}")


# 假 agent：首次运行发出一条命中高危模式的 bash 事件后被杀；
# 续跑（prompt 含“人工已批准”）时直接回复 done，便于不依赖真实模型验证全流程。
# 注意：命令须用非 /tmp 路径（递归 rm 现在对 /tmp 下放行）。
FAKE_AGENT = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "人工已批准"; then
  printf '%s\\n' '{"type":"text","sessionID":"ses_fake","part":{"type":"text","text":"done"}}'
  printf '%s\\n' '{"type":"step_finish","sessionID":"ses_fake","part":{"tokens":{"total":10},"cost":0}}'
  exit 0
fi
printf '%s\\n' '{"type":"tool_use","sessionID":"ses_fake","part":{"tool":"bash","title":"Bash","state":{"status":"completed","input":{"command":"rm -rf /root"}}}}'
sleep 20
"""


def _fake_relay(relay_factory, tmp_path) -> dict:
    script = tmp_path / "fake_agent.sh"
    script.write_text(FAKE_AGENT)
    script.chmod(0o755)
    return relay_factory(agent_bin=str(script), task_timeout=60)


def test_blocked_command_full_flow_fake_agent(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "faker"}).json()["token"]
    member_c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {tok}"},
                            timeout=30)
    tid = submit(base, tok, "删除某个临时目录")["task_id"]

    # 窗口 55s；fake agent 保活 20s——即使高负载下执行器循环被饿到 agent 退出后，
    # 管道缓冲的 tool_use 行仍会被读到并转 pending_approval，消除窗口边界竞态
    t = _wait_pending(base, tok, tid, timeout=55)
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"] == "rm -rf /root", t
    assert t["error"].startswith("blocked:"), t
    r2 = member_c.post(f"/tasks/{tid}/approve", json={"decision": "approve"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "queued"

    t = wait_task(base, tok, tid, timeout=60)
    assert t["status"] in ("review", "done"), t
    assert t["session_id"] == "ses_fake", "续跑应复用原会话"
    assert t["resume_hint"] == "", "续跑完成后应清空 resume_hint"
    assert t["blocked_cmd"], "blocked_cmd 保留作记录"

    lines = audit_lines(r["cfg"].log_path)
    assert any(f"task={tid}\tuser=faker\tblock" in ln for ln in lines)
    assert any(f"task={tid}\tuser=faker\tapprove" in ln for ln in lines)
    member_c.close()
    admin_c.close()


def test_blocked_command_deny_fake_agent(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "faker2"}).json()["token"]
    member_c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {tok}"},
                            timeout=30)
    tid = submit(base, tok, "删除某个临时目录")["task_id"]

    t = _wait_pending(base, tok, tid, timeout=55)  # 高负载余量，同 approve 用例
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"]
    r2 = member_c.post(f"/tasks/{tid}/approve", json={"decision": "deny"})
    assert r2.status_code == 200, r2.text
    t = member_c.get(f"/tasks/{tid}").json()
    assert t["status"] == "failed", t
    assert "denied" in t["error"], t
    lines = audit_lines(r["cfg"].log_path)
    assert any(f"task={tid}\tuser=faker2\tblock" in ln for ln in lines)
    assert any(f"task={tid}\tuser=faker2\tdeny" in ln for ln in lines)
    member_c.close()
    admin_c.close()


# 假 agent：prompt 含「实际写入」时在共享区写一个文件（模拟违反只读声明），否则纯读回 done
# 内容带纳秒时间戳，确保重复写入能被哈希比对识别为改动
FAKE_WRITER = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "实际写入"; then
  mkdir -p ../../shared/knowledge
  printf 'dirty-%s\\n' "$(date +%s%N)" > ../../shared/knowledge/dirty.txt
fi
printf '%s\\n' '{"type":"text","sessionID":"ses_w","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_w","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_read_only_violation_and_clean(relay_factory, tmp_path):
    script = tmp_path / "writer_agent.sh"
    script.write_text(FAKE_WRITER)
    script.chmod(0o755)
    r = relay_factory(agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "reader"}).json()["token"]

    # 描述带「只读」标记 → read_only 落库；仅新建**非低风险目录**文件 → review + 违规说明
    tid = submit(base, tok, "只读核查（实际写入验证）：回复 done",
                 targets=["shared/knowledge"])["task_id"]
    t = wait_task(base, tok, tid, timeout=60)
    assert t["read_only"] is True, t
    assert t["status"] == "review", t
    assert any("只读" in c["note"] and "新建" in c["note"] for c in t["conflicts"]), t
    assert "shared/knowledge/dirty.txt" in t["changed_files"], t

    # 真纯读无改动 → 自动 done（基线已含 dirty.txt，无新增变更）
    tid2 = submit(base, tok, "只读查询：回复 done", targets=["shared/knowledge"])["task_id"]
    t2 = wait_task(base, tok, tid2, timeout=60)
    assert t2["status"] == "done", t2
    assert t2["read_only"] is True, t2

    # 只读声明却覆盖既有文件 → conflict（有数据损失风险）；时间窗收 0 以隔离跨任务判定
    r["cfg"].conflict_window_hours = 0
    tid3 = submit(base, tok, "只读核查（实际写入验证）第二轮：回复 done",
                  targets=["shared/knowledge"])["task_id"]
    t3 = wait_task(base, tok, tid3, timeout=60)
    assert t3["status"] == "conflict", t3
    assert any("覆盖" in c["note"] for c in t3["conflicts"]), t3
    admin_c.close()


@pytest.mark.integration
def test_blocked_command_approve_flow(relay, member):
    tok = _tok(member)
    tid, t = _submit_until_blocked(relay["base"], tok,
                  "必须先用 bash 工具执行命令 rm -rf /root/relay-block-test，执行完再回复 done")
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"], t
    assert "/root/relay-block-test" in t["blocked_cmd"], t

    r = member.post(f"/tasks/{tid}/approve", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    t = _wait_exec_done(member, tid, timeout=240)
    assert t["status"] in ("review", "conflict", "done"), t

    lines = audit_lines(relay["cfg"].log_path)
    assert any(f"task={tid}\tuser=tester\tblock" in ln for ln in lines)
    assert any(f"task={tid}\tuser=tester\tapprove" in ln for ln in lines)


@pytest.mark.integration
def test_blocked_command_deny_flow(relay, member):
    tok = _tok(member)
    tid, t = _submit_until_blocked(relay["base"], tok,
                  "必须先用 bash 工具执行命令 rm -rf /root/relay-block-test-deny，执行完再回复 done")
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"], t

    r = member.post(f"/tasks/{tid}/approve", json={"decision": "deny"})
    assert r.status_code == 200, r.text
    t = member.get(f"/tasks/{tid}").json()
    assert t["status"] == "failed", t
    assert "denied" in t["error"], t
    lines = audit_lines(relay["cfg"].log_path)
    assert any(f"task={tid}\tuser=tester\tblock" in ln for ln in lines)
    assert any(f"task={tid}\tuser=tester\tdeny" in ln for ln in lines)


@pytest.mark.integration
def test_approve_only_from_pending(relay, member):
    tok = _tok(member)
    tid = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]
    wait_task(relay["base"], tok, tid, timeout=240)
    r = member.post(f"/tasks/{tid}/approve", json={"decision": "approve"})
    assert r.status_code == 409
    r = member.post("/tasks/99999/approve", json={"decision": "approve"})
    assert r.status_code == 404


@pytest.mark.integration
def test_admin_report_fields_and_auth(relay, admin, member):
    tok = _tok(member)
    submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")
    r = admin.get("/admin/report", params={"days": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    for k in ("by_day", "by_user", "conflicts", "blocked", "audit_tail"):
        assert k in body, body
    assert body["by_day"], body
    for k in ("day", "submitted", "done", "failed", "conflict", "cancelled",
              "pending_approval"):
        assert k in body["by_day"][0], body["by_day"][0]
    assert body["by_user"], body
    for k in ("user", "tasks", "done", "done_rate", "tokens"):
        assert k in body["by_user"][0], body["by_user"][0]
    assert body["by_user"][0]["user"] == "tester"
    assert any("submit" in ln for ln in body["audit_tail"])
    r = member.get("/admin/report")
    assert r.status_code == 403


def test_metrics_open_no_auth(relay, member):
    tok = _tok(member)
    tid = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]
    r = httpx.get(relay["base"] + "/metrics", timeout=5)
    assert r.status_code == 200
    body = r.json()
    for k in ("uptime_seconds", "engine", "max_concurrent", "active", "queued",
              "tasks", "today", "last_7d", "recent_task_ids"):
        assert k in body, body
    assert body["engine"] == "opencode"
    assert body["max_concurrent"] == 2
    assert isinstance(body["tasks"], dict)
    assert body["today"]["submitted"] >= 1
    assert "done_rate" in body["today"] and "done_rate" in body["last_7d"]
    assert tid in body["recent_task_ids"]
