from __future__ import annotations

import json

from app import progress
from app.executor import _parse_claude, _parse_opencode


def test_safe_text_redacts_sensitive():
    assert progress.safe_text("RELAY_ADMIN_TOKEN=abc") == ""
    assert progress.safe_text("/mnt/vol-eltaah12/workspace/report.xlsx") == "report.xlsx"


def test_tool_label_bash_safe():
    label, detail = progress.tool_label("Bash", {"command": "python render_report.py"})
    assert label == "📑 渲染报告" and detail == "render_report.py"
    label, detail = progress.tool_label("Bash", {"command": "cat /etc/passwd"})
    assert label == "🔧 执行系统命令" and detail == ""
    label, detail = progress.tool_label("Bash", {"command": "grep secret token.txt"})
    assert detail == ""


def test_tool_label_file_basename_only():
    label, detail = progress.tool_label(
        "Read", {"file_path": "/task/work/shared/report/a.html"})
    assert label == "📖 读取文件" and detail == "a.html"


def test_summarize_events_keeps_latest_and_hides_text():
    events = [
        {"id": 1, "t": "status", "s": "running"},
        {"id": 2, "t": "agent", "kind": "tool", "tool": "Read",
         "label": "📖 读取文件", "detail": "a.xlsx"},
        {"id": 3, "t": "agent", "kind": "text", "text": "internal reasoning"},
        {"id": 4, "t": "agent", "kind": "tool", "tool": "Bash",
         "label": "📑 渲染报告", "detail": "render_report.py"},
    ]
    out = progress.summarize_events(events, 2)
    assert [e["id"] for e in out] == [3, 4]
    assert out[0] == {"id": 3, "kind": "text", "label": "✍️ 组织结论"}
    assert "internal reasoning" not in json.dumps(out, ensure_ascii=False)


def test_infer_stage():
    events = [{"id": 1, "t": "agent", "kind": "tool", "tool": "Bash",
               "label": "📑 渲染报告"}]
    assert progress.infer_stage(events, "running") == "报告装配"
    assert progress.infer_stage([], "queued") == "排队等待后台执行槽位"
    assert progress.infer_stage([], "running") == "后台执行中"


def test_parse_claude_publishes_safe_label():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "python render_report.py"}}]}})
    ev = _parse_claude(line)
    assert ev["agent_event"]["label"] == "📑 渲染报告"
    assert ev["agent_event"]["detail"] == "render_report.py"
    assert ev["tool_cmd"] == "python render_report.py"


def test_parse_opencode_publishes_safe_label():
    line = json.dumps({"type": "tool_use", "part": {
        "tool": "read", "title": "", "input": {"file_path": "/tmp/a.xlsx"}}})
    ev = _parse_opencode(line)
    assert ev["agent_event"]["label"] == "📖 读取文件"
    assert ev["agent_event"]["detail"] == "a.xlsx"


def test_progress_endpoint_local_shadow(relay, admin, member):
    r = member.post("/tasks", json={"description": "只读任务", "backend": "local"})
    assert r.status_code == 202
    tid = r.json()["task_id"]
    r = member.get(f"/tasks/{tid}/progress")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid
    assert body["status"] == "running"
    assert body["budget_sec"] == relay["cfg"].task_timeout
    assert body["max_segments"] == relay["cfg"].long_task_max_segments
    assert body["recent"] == []
    assert "pending" in body["queue"]


def test_progress_endpoint_requires_auth(relay):
    import httpx
    r = httpx.get(relay["base"] + "/tasks/1/progress", timeout=5)
    assert r.status_code == 401


def test_progress_endpoint_404(member):
    assert member.get("/tasks/999999/progress").status_code == 404
