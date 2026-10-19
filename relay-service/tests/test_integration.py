from __future__ import annotations

import json
import os
import re
import subprocess
import time

import httpx
import pytest

from conftest import EXEC_DONE, audit_lines, submit, wait_task


def _tok(member) -> str:
    return member.headers["Authorization"].split()[1]


def _wait_running(relay, member, tid, timeout=60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = member.get(f"/tasks/{tid}").json()
        if t["status"] == "running":
            return t
        if t["status"] in EXEC_DONE:
            raise AssertionError(f"task {tid} 提前终结: {t['status']} err={t['error']}")
        time.sleep(0.5)
    raise TimeoutError("task not running in time")


@pytest.mark.integration
def test_priority_urgent_jumps_queue(relay, member):
    """max_concurrent=2：前两个占坑后，urgent 必须排在普通任务前面。"""
    tok = _tok(member)
    a = submit(relay["base"], tok, "用 bash 工具执行 sleep 12，然后只回复 done。")["task_id"]
    b = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]
    time.sleep(3)  # 等 a、b 进入 running
    c = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。",
               priority="urgent")["task_id"]
    d = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]

    for tid in (a, b, c, d):
        wait_task(relay["base"], tok, tid, timeout=240)

    starts = [ln.split("\t")[1].split("=")[1]
              for ln in audit_lines(relay["cfg"].log_path) if "\tstart\t" in ln]
    starts = [s for s in starts if s in (str(a), str(b), str(c), str(d))]
    # a、b 先跑（先提交），b 结束后 c(urgent) 必须早于 d
    assert starts[0] in (str(a), str(b))
    assert starts.index(str(c)) < starts.index(str(d)), f"urgent 未插队: {starts}"


@pytest.mark.integration
def test_cancel_running_kills_process(relay, member):
    tok = _tok(member)
    a = submit(relay["base"], tok, "用 bash 工具执行 sleep 60，然后只回复 done。")["task_id"]
    _wait_running(relay, member, a)
    t0 = time.time()
    r = member.post(f"/tasks/{a}/cancel")
    assert r.status_code == 200
    t = wait_task(relay["base"], tok, a, timeout=30)
    elapsed = time.time() - t0
    assert t["status"] == "cancelled", t
    assert elapsed < 20, f"取消耗时过长: {elapsed:.1f}s"
    # 进程组确实被杀：不应残留 sleep 60
    out = subprocess.run(["pgrep", "-af", "sleep 60"], capture_output=True,
                         text=True).stdout
    assert "sleep 60" not in out, f"残留进程: {out}"


@pytest.mark.integration
def test_timeout_marks_failed(relay_factory):
    r2 = relay_factory(task_timeout=10)
    admin_c = httpx.Client(base_url=r2["base"],
                           headers={"Authorization": "Bearer admin-test"}, timeout=30)
    u = admin_c.post("/users", json={"name": "t2"}).json()["token"]
    base, tok = r2["base"], u
    tid = submit(base, tok, "用 bash 工具执行 sleep 120，然后只回复 done。")["task_id"]
    t0 = time.time()
    t = wait_task(base, tok, tid, timeout=60)
    elapsed = time.time() - t0
    assert t["status"] == "failed", t
    assert "timeout" in t["error"], t
    assert 8 < elapsed < 40, f"超时控制失准: {elapsed:.1f}s"
    out = subprocess.run(["pgrep", "-af", "sleep 120"], capture_output=True,
                         text=True).stdout
    assert "sleep 120" not in out, f"残留进程: {out}"


@pytest.mark.integration
def test_conflict_keeps_both_versions(relay, member):
    """两个任务先后改同一共享文件：后完成的标记 conflict，先完成的版本留备份。"""
    shared = relay["cfg"].shared_dir
    fpath = os.path.join(shared, "conflict.txt")
    target = os.path.relpath(fpath, relay["cfg"].workspace_root)
    tok = _tok(member)

    t1_id = submit(relay["base"], tok,
                   f"用 write 工具覆盖写入 {fpath}，最终文件内容只有 version-one 一行。完成后只回复 done。",
                   targets=[target])["task_id"]
    t1 = wait_task(relay["base"], tok, t1_id, timeout=240)
    assert t1["status"] == "review", t1
    assert open(fpath).read().strip() == "version-one"

    t2_id = submit(relay["base"], tok,
                   f"用 write 工具覆盖写入 {fpath}，最终文件内容只有 version-two 一行（不要追加，完整覆盖）。完成后只回复 done。",
                   targets=[target])["task_id"]
    t2 = wait_task(relay["base"], tok, t2_id, timeout=240)
    assert t2["status"] == "conflict", t2
    disk = open(fpath).read()
    assert "version-two" in disk, "后写入版本必须在磁盘上"

    # 先完成者的版本必须有备份（若 agent 恰好追加而非覆盖，磁盘上也可能同时保留，均可接受）
    bak_root = os.path.join(shared, ".archive", "bak", "conflict")
    baks = []
    for dp, _, fns in os.walk(bak_root):
        for f in fns:
            if re.match(r"conflict\.txt\.bak-tester-\d{4}", f):
                baks.append(os.path.join(dp, f))
    v1_in_backup = baks and any("version-one" in open(b).read() for b in baks)
    assert v1_in_backup or "version-one" in disk, "version-one 不能丢（磁盘或备份至少一处）"
    if "version-one" not in disk:
        assert v1_in_backup, f"覆盖写入场景下必须有备份: {os.listdir(shared)}"

    # 冲突详情
    assert any(c["file"] == target and c["with_task"] == t1["id"] and c["backup"]
               for c in t2["conflicts"]), t2["conflicts"]
    # 早先任务也被标注
    t1b = member.get(f"/tasks/{t1['id']}").json()
    assert any(c["with_task"] == t2["id"] for c in t1b["conflicts"]), t1b["conflicts"]


@pytest.mark.integration
def test_resume_continues_session(relay, member):
    shared = relay["cfg"].shared_dir
    fpath = os.path.join(shared, "resume.txt")
    tok = _tok(member)

    t1 = submit(relay["base"], tok,
                f"用 write 工具在 {fpath} 写入内容 line-alpha。完成后只回复 done。")["task_id"]
    t1 = wait_task(relay["base"], tok, t1, timeout=240)
    assert t1["status"] == "review", t1
    assert t1["session_id"]

    t2 = submit(relay["base"], tok,
                f"在上一个任务创建的文件 {fpath} 末尾追加一行 line-beta（保留原内容）。完成后只回复 done。",
                resume_from=t1["id"])["task_id"]
    t2 = wait_task(relay["base"], tok, t2, timeout=240)
    assert t2["status"] in ("review", "conflict"), t2
    # 复用原会话与原工作目录
    assert t2["session_id"] == t1["session_id"]
    assert t2["workdir"] == t1["workdir"]
    content = open(fpath).read()
    assert "line-alpha" in content and "line-beta" in content, content


@pytest.mark.integration
def test_sse_stream_events_and_history(relay, member):
    shared = relay["cfg"].shared_dir
    fpath = os.path.join(shared, "sse.txt")
    tok = _tok(member)
    tid = submit(relay["base"], tok,
                 f"用 write 工具在 {fpath} 写入内容 sse-ok。完成后只回复 done。")["task_id"]

    # 任务进行中订阅：应收到 agent 事件 + 终态 status
    events = []
    with member.stream("GET", f"/tasks/{tid}/stream", timeout=240) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:]))
            if events[-1].get("t") == "status" and events[-1].get("s") in EXEC_DONE:
                break
    kinds = [e.get("t") for e in events]
    assert "agent" in kinds, f"缺少 agent 事件: {events}"
    assert any(e.get("t") == "status" and e.get("s") == "review" for e in events)

    # 完成后再次订阅：历史重放，立刻拿到全部事件
    events2 = []
    with member.stream("GET", f"/tasks/{tid}/stream", timeout=30) as r:
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            events2.append(json.loads(line[5:]))
            if events2[-1].get("t") == "status" and events2[-1].get("s") in EXEC_DONE:
                break
    assert any(e.get("t") == "status" and e.get("s") in EXEC_DONE for e in events2)


@pytest.mark.integration
def test_same_content_rewrite_not_conflict(relay, member):
    """同内容重写（仅 mtime 变化）不算变更，不应误报冲突。"""
    shared = relay["cfg"].shared_dir
    fpath = os.path.join(shared, "noop.txt")
    tok = _tok(member)
    t1_id = submit(relay["base"], tok,
                   f"用 write 工具覆盖写入 {fpath}，最终文件内容只有 same-content 一行。完成后只回复 done。",
                   targets=["shared/noop.txt"])["task_id"]
    t1 = wait_task(relay["base"], tok, t1_id, timeout=240)
    assert t1["status"] == "review", t1

    t2_id = submit(relay["base"], tok,
                   f"读取 {fpath}，然后用 write 工具以完全相同的内容重写该文件（不要改动任何字符）。完成后只回复 done。",
                   targets=["shared/noop.txt"])["task_id"]
    t2 = wait_task(relay["base"], tok, t2_id, timeout=240)
    assert t2["status"] in ("review", "done"), t2
    assert t2["changed_files"] == [], f"同内容重写不应计入变更: {t2['changed_files']}"
    assert t2["conflicts"] == []


@pytest.mark.integration
def test_task_list_and_audit(relay, member):
    tok = _tok(member)
    n0 = len(member.get("/tasks", params={"limit": 100}).json()["tasks"])
    tid = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]
    t = wait_task(relay["base"], tok, tid, timeout=240)
    assert t["status"] in ("review", "done", "failed")
    tasks = member.get("/tasks", params={"limit": 100}).json()["tasks"]
    assert len(tasks) == n0 + 1
    assert any(x["id"] == tid for x in tasks)
    # 审计日志有 submit/start/stop
    lines = audit_lines(relay["cfg"].log_path)
    assert any(f"task={tid}\tuser=tester\tsubmit" in ln for ln in lines)
    assert any(f"task={tid}\tuser=tester\tstart" in ln for ln in lines)
    assert any(f"task={tid}\tuser=tester\tstop" in ln for ln in lines)


@pytest.mark.integration
def test_readonly_auto_done(relay, member):
    """纯只读（无文件改动）任务 → 自动 done，不落 review。"""
    tok = _tok(member)
    tid = submit(relay["base"], tok, "只回复 ok，不要使用任何工具。")["task_id"]
    t = wait_task(relay["base"], tok, tid, timeout=240)
    assert t["status"] == "done", t
    assert t["changed_files"] == [], t
    assert "只读" in t["error"], t


@pytest.mark.integration
def test_stale_timeout_auto_done(relay_factory):
    """review 停留超时时限 → 后台清扫自动 done 并标记“超时自动done”。"""
    r = relay_factory(stale_timeout_hours=0, sweep_interval_sec=1)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "stale"}).json()["token"]
    admin_c.close()
    shared = r["cfg"].shared_dir
    fpath = os.path.join(shared, "stale.txt")
    tid = submit(base, tok,
                 f"用 write 工具在 {fpath} 写入内容 stale-ok。完成后只回复 done。")["task_id"]
    deadline = time.time() + 180
    t = None
    while time.time() < deadline:
        t = httpx.get(f"{base}/tasks/{tid}",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()
        if t["status"] == "done":
            break
        time.sleep(1)
    assert t["status"] == "done", t
    assert "超时自动done" in t["error"], t
