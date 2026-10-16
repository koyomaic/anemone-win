from __future__ import annotations

import os

import httpx

from app.config import DEFAULT_AUTO_DONE_PREFIXES, Settings
from app.main import _prefix_match, auto_done_reason
from conftest import submit, wait_task


def _cfg(**kw) -> Settings:
    kw.setdefault("auto_done_mode", "on")
    return Settings(
        db_path="/tmp/unused-review-policy.db",
        workspace_root="/tmp",
        shared_dir="/tmp/shared",
        task_root="/tmp/tasks",
        users_root="/tmp/users",
        admin_token="admin-test",
        **kw,
    )


def _scan(created=None, modified=None, deleted=None, foreign=None, volatile=None):
    created = created or []
    modified = modified or []
    deleted = deleted or []
    all_changed = sorted(set(created) | set(modified) | set(deleted))
    return {"created": created, "modified": modified, "deleted": deleted,
            "all": all_changed, "foreign": foreign or [], "volatile": volatile or []}


def test_prefix_match_normalizes_trailing_slash():
    assert _prefix_match("shared/report/a.md", ["shared/report"])
    assert _prefix_match("shared/report/a.md", ["shared/report/"])
    assert not _prefix_match("shared/reporting/a.md", ["shared/report/"])


def test_auto_done_reason_requires_only_new_low_risk_files():
    cfg = _cfg()
    row = {"id": 1, "read_only": False}
    assert auto_done_reason(cfg, row, _scan(created=["shared/report/a.md"]), "ok", [], [])
    assert not auto_done_reason(cfg, row, _scan(created=["shared/knowledge/a.md"]), "ok", [], [])
    assert not auto_done_reason(
        cfg, row,
        _scan(created=["shared/report/a.md"], modified=["shared/report/b.md"]),
        "ok", [], [])
    assert not auto_done_reason(
        cfg, row, _scan(created=["shared/report/a.md"], deleted=["shared/tmp/b.md"]),
        "ok", [], [])
    assert not auto_done_reason(
        cfg, row, _scan(created=["shared/report/a.md"],
                        foreign=[("shared/report/a.md", 999)]),
        "ok", [], [])
    assert not auto_done_reason(cfg, row, _scan(created=["shared/report/a.md"]), "", [], [])
    assert not auto_done_reason(
        cfg, row, _scan(created=["shared/report/a.md"]), "ok",
        [{"file": "shared/report/a.md"}], [])
    assert not auto_done_reason(
        cfg, row, _scan(created=["shared/report/a.md"]), "ok", [], ["shared/report/a.md"])


def test_auto_done_reason_mode_and_limit():
    row = {"id": 1, "read_only": False}
    scan = _scan(created=["shared/report/a.md", "shared/report/b.md"])
    assert not auto_done_reason(_cfg(auto_done_mode="off"), row, scan, "ok", [], [])
    assert not auto_done_reason(_cfg(auto_done_max_files=1), row, scan, "ok", [], [])
    assert auto_done_reason(_cfg(auto_done_max_files=2), row, scan, "ok", [], [])


def test_auto_done_reason_allows_read_only_new_low_risk_output():
    cfg = _cfg()
    row = {"id": 1, "read_only": True}
    reason = auto_done_reason(cfg, row, _scan(created=["shared/tmp/a.txt"]), "ok", [], [])
    assert reason and "只读任务新建低风险产出" in reason


def test_default_auto_done_prefixes_cover_report_tmp_and_system_dirs():
    for rel in ("shared/report/a.md", "shared/tmp/a.txt", "shared/data/a.csv",
                "shared/_sync/a.txt", "shared/_migrate/a.part",
                "shared/_inventory/a.md", "shared/_upgrade-backup/a.txt"):
        assert _prefix_match(rel, DEFAULT_AUTO_DONE_PREFIXES), rel
    for rel in ("shared/knowledge/a.md", "shared/memory/a.md", "shared/skills/a/SKILL.md",
                "shared/mcp/a.json", "shared/tools/a.py", "shared/secrets/a.txt"):
        assert not _prefix_match(rel, DEFAULT_AUTO_DONE_PREFIXES), rel


def _fake_relay(relay_factory, tmp_path, script: str, **overrides) -> dict:
    p = tmp_path / "agent.sh"
    p.write_text(script)
    p.chmod(0o755)
    return relay_factory(agent_bin=str(p), task_timeout=60, **overrides)


def _token(r: dict, name: str = "tester") -> str:
    admin_c = httpx.Client(base_url=r["base"],
                           headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    try:
        return admin_c.post("/users", json={"name": name}).json()["token"]
    finally:
        admin_c.close()


SAFE_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'safe-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_safe.md
printf '%s\\n' '{"type":"text","sessionID":"ses_safe","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_safe","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_safe_new_report_auto_done(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, SAFE_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "生成报告")["task_id"], timeout=60)
    assert t["status"] == "done", t
    assert t["changed_files"] == ["shared/report/auto_safe.md"], t
    assert t["conflicts"] == [], t
    assert "低风险新建产出" in t["error"], t


UNSAFE_AGENT = """#!/bin/sh
mkdir -p ../../shared/knowledge
printf 'unsafe-%s\\n' "$(date +%s%N)" > ../../shared/knowledge/auto_unsafe.md
printf '%s\\n' '{"type":"text","sessionID":"ses_unsafe","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_unsafe","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_unsafe_new_file_still_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, UNSAFE_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "写知识")["task_id"], timeout=60)
    assert t["status"] == "review", t
    assert t["changed_files"] == ["shared/knowledge/auto_unsafe.md"], t


MODIFY_SAFE_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'modified-%s\\n' "$(date +%s%N)" > ../../shared/report/existing.md
printf '%s\\n' '{"type":"text","sessionID":"ses_mod","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_mod","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_modified_safe_file_still_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, MODIFY_SAFE_AGENT)
    existing = r["ws"] / "shared" / "report" / "existing.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("original\n", encoding="utf-8")
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "改报告")["task_id"], timeout=60)
    assert t["status"] == "review", t
    assert t["changed_files"] == ["shared/report/existing.md"], t


MIXED_AGENT = """#!/bin/sh
mkdir -p ../../shared/report ../../shared/knowledge
printf 'safe-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_mixed.md
printf 'unsafe-%s\\n' "$(date +%s%N)" > ../../shared/knowledge/auto_mixed.md
printf '%s\\n' '{"type":"text","sessionID":"ses_mixed","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_mixed","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_mixed_safe_and_unsafe_still_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, MIXED_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "混合产出")["task_id"], timeout=60)
    assert t["status"] == "review", t
    assert set(t["changed_files"]) == {
        "shared/report/auto_mixed.md", "shared/knowledge/auto_mixed.md"}, t


FOREIGN_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'foreign-%s\\n' "$(date +%s%N)" > ../../shared/report/20261016_T999999_auto_v1.md
printf '%s\\n' '{"type":"text","sessionID":"ses_foreign","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_foreign","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_foreign_task_tag_still_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, FOREIGN_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "越界任务号")["task_id"], timeout=60)
    assert t["status"] == "review", t
    assert t["changed_files"] == ["shared/report/20261016_T999999_auto_v1.md"], t


NO_TEXT_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'no-text-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_no_text.md
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_no_text","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_no_result_text_still_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, NO_TEXT_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "无文本产出")["task_id"], timeout=60)
    assert t["status"] == "review", t


READ_ONLY_SAFE_AGENT = """#!/bin/sh
mkdir -p ../../shared/tmp
printf 'ro-%s\\n' "$(date +%s%N)" > ../../shared/tmp/auto_ro.txt
printf '%s\\n' '{"type":"text","sessionID":"ses_ro","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_ro","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_read_only_new_safe_file_auto_done_with_note(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, READ_ONLY_SAFE_AGENT)
    tok = _token(r)
    t = wait_task(r["base"], tok,
                  submit(r["base"], tok, "只读核查但写临时文件")["task_id"], timeout=60)
    assert t["status"] == "done", t
    assert t["read_only"] is True, t
    assert t["changed_files"] == ["shared/tmp/auto_ro.txt"], t
    assert any("只读" in c["note"] and "新建" in c["note"] for c in t["conflicts"]), t


CONFLICT_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'conflict-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_conflict.md
printf '%s\\n' '{"type":"text","sessionID":"ses_conf","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_conf","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_safe_prefix_does_not_bypass_real_conflict(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, CONFLICT_AGENT)
    tok = _token(r)
    t1 = wait_task(r["base"], tok, submit(r["base"], tok, "写冲突报告 v1")["task_id"], timeout=60)
    assert t1["status"] == "done", t1
    t2 = wait_task(r["base"], tok, submit(r["base"], tok, "写冲突报告 v2")["task_id"], timeout=60)
    assert t2["status"] == "conflict", t2
    assert any(c["with_task"] == t1["id"] for c in t2["conflicts"]), t2


def test_auto_done_mode_off_restores_review(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, SAFE_AGENT, auto_done_mode="off")
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "生成报告")["task_id"], timeout=60)
    assert t["status"] == "review", t


TWO_SAFE_FILES_AGENT = """#!/bin/sh
mkdir -p ../../shared/report
printf 'a-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_two_a.md
printf 'b-%s\\n' "$(date +%s%N)" > ../../shared/report/auto_two_b.md
printf '%s\\n' '{"type":"text","sessionID":"ses_two","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_two","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_auto_done_max_files(relay_factory, tmp_path):
    r = _fake_relay(relay_factory, tmp_path, TWO_SAFE_FILES_AGENT, auto_done_max_files=1)
    tok = _token(r)
    t = wait_task(r["base"], tok, submit(r["base"], tok, "两个报告")["task_id"], timeout=60)
    assert t["status"] == "review", t
    assert len(t["changed_files"]) == 2, t
