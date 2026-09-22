from __future__ import annotations

import os
import time

import httpx

from app.config import DEFAULT_VOLATILE_PATTERNS, Settings
from app.db import DB
from app.workspace import Workspace, _snap, is_runtime_dir, is_volatile
from conftest import submit, wait_task

DWS_TOKEN = "shared/secrets/dws-cli-seed/dws-cli/auth-token_test.enc"
TOKEN_CACHE = "shared/skills/jingbo-data-api/config/.token_cache"


def _cfg(tmp_path, **kw) -> Settings:
    return Settings(
        db_path=str(tmp_path / "relay.db"),
        workspace_root=str(tmp_path),
        shared_dir=str(tmp_path / "shared"),
        task_root=str(tmp_path / "tasks"),
        users_root=str(tmp_path / "users"),
        admin_token="admin-test",
        **kw,
    )


def _write(root, rel: str, content: str) -> None:
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def test_is_volatile_matches_tool_caches():
    pats = DEFAULT_VOLATILE_PATTERNS
    assert is_volatile(DWS_TOKEN, pats)
    assert is_volatile("shared/skills/jingbo-data-api/config/.token_cache", pats)
    assert is_volatile("shared/reports/a.md.bak-崔成东-1212", pats)
    assert not is_volatile("shared/knowledge/调价.md", pats)
    assert not is_volatile("shared/reports/日报.md", pats)


def test_snap_skips_runtime_dirs(tmp_path):
    """虚拟环境/模型缓存/归档/台账不进基线快照，否则每任务要哈希数万无关文件（实测 ~76s）。"""
    _write(tmp_path, "shared/knowledge/调价.md", "口径")
    _write(tmp_path, "shared/report/日报.md", "产出")
    _write(tmp_path, "shared/venv/bin/python", "#!/x")
    _write(tmp_path, "shared/venv_funasr/pyvenv.cfg", "home = /usr")
    _write(tmp_path, "shared/venv_funasr/lib/site.py", "y")
    _write(tmp_path, "shared/models/modelscope/m.bin", "weights")
    _write(tmp_path, "shared/.archive/bak/a.md.bak-x", "old")
    # index/ 由 store CLI 刷新，若进基线会把 manifest 更新误判成任务产出
    _write(tmp_path, "shared/index/manifest.jsonl", "{}")
    _write(tmp_path, "shared/work/T1/scratch.tmp", "x")
    snap = _snap(str(tmp_path / "shared"), str(tmp_path))
    assert sorted(snap) == ["shared/knowledge/调价.md", "shared/report/日报.md"]


def test_is_runtime_dir_detects_venv_by_marker(tmp_path):
    """按 pyvenv.cfg 泛化识别虚拟环境，不依赖站点特有的目录名。"""
    _write(tmp_path, "shared/asr_env_2026/pyvenv.cfg", "home = /usr")
    _write(tmp_path, "shared/asr_env_2026/bin/python", "#!/x")
    _write(tmp_path, "shared/venture-notes/a.md", "业务")
    root = str(tmp_path / "shared")
    assert is_runtime_dir(root, "asr_env_2026")
    assert is_runtime_dir(root, "venv")
    assert is_runtime_dir(root, "models")
    assert is_runtime_dir(root, ".archive")
    assert is_runtime_dir(root, "index")
    assert is_runtime_dir(root, "work")
    assert not is_runtime_dir(root, "venture-notes")
    assert not is_runtime_dir(root, "knowledge")
    assert not is_runtime_dir(root, "report")


def test_scan_excludes_volatile_from_changed_files(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    ws = Workspace(cfg, db)
    tid = db.create_task(user="tester", description="用 dws 发消息并产出报告")
    workdir = ws.prepare(db.task(tid))
    _write(tmp_path, "shared/reports/out.md", "real")
    _write(tmp_path, DWS_TOKEN, "tok")
    _write(tmp_path, TOKEN_CACHE, "cache")

    scan = ws.scan(db.task(tid))
    assert scan["all"] == ["shared/reports/out.md"]
    assert scan["volatile"] == sorted([DWS_TOKEN, TOKEN_CACHE])
    assert db.task(tid)["changed_files"] == ["shared/reports/out.md"]
    # 易变文件不进 .result，也就不会被当作冲突备份源
    for rel in (DWS_TOKEN, TOKEN_CACHE):
        assert not os.path.exists(os.path.join(workdir, ".result", rel))
    assert os.path.exists(os.path.join(workdir, ".result", "shared/reports/out.md"))


def test_read_only_violation_ignores_volatile(tmp_path):
    """只读任务仅触发工具缓存重写 → 不算违规（线上 #43 误报形态）。"""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    ws = Workspace(cfg, db)
    tid = db.create_task(user="tester", description="只读核查", read_only=True)
    ws.prepare(db.task(tid))
    _write(tmp_path, DWS_TOKEN, "tok")
    _write(tmp_path, TOKEN_CACHE, "cache")
    scan = ws.scan(db.task(tid))
    assert scan["all"] == []
    assert scan["volatile"] == sorted([DWS_TOKEN, TOKEN_CACHE])


def test_tasks_with_file_time_window(tmp_path):
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    now = time.time()
    old = db.create_task(user="a", description="old")
    db.set(old, changed_files=["shared/x.md"], status="done",
           started_at=now - 72 * 3600, finished_at=now - 72 * 3600)
    recent = db.create_task(user="b", description="recent")
    db.set(recent, changed_files=["shared/x.md"], status="review",
           started_at=now - 60, finished_at=now - 30)
    cur = db.create_task(user="c", description="cur")

    assert {t["id"] for t in db.tasks_with_file("shared/x.md", exclude=cur)} == {old, recent}
    got = db.tasks_with_file("shared/x.md", exclude=cur, since=now - 24 * 3600)
    assert [t["id"] for t in got] == [recent]


# 假 agent：按 prompt 关键字写「凭据缓存」或「真实报告」，内容每次不同以触发哈希变更
FAKE_AGENT = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "写缓存"; then
  mkdir -p ../../shared/secrets/dws-cli-seed/dws-cli
  printf 'tok-%s\\n' "$(date +%s%N)" > ../../shared/secrets/dws-cli-seed/dws-cli/auth-token_test.enc
fi
if printf '%s' "$prompt" | grep -q "写报告"; then
  mkdir -p ../../shared/reports
  printf 'v-%s\\n' "$(date +%s%N)" > ../../shared/reports/r.md
fi
printf '%s\\n' '{"type":"text","sessionID":"ses_c","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_c","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def _fake_relay(relay_factory, tmp_path, **overrides) -> dict:
    script = tmp_path / "fake_agent.sh"
    script.write_text(FAKE_AGENT)
    script.chmod(0o755)
    return relay_factory(agent_bin=str(script), task_timeout=60, **overrides)


def test_backup_version_skips_oversize_and_runtime(tmp_path):
    """护栏：超大文件与运行时目录不备份。

    历史事故——backup_version 无上限 copy2，一个声明宽 target 的任务把 venv/models
    整棵树备份成 18972 个 .bak、4.92G（单个 model.bin.bak 1.5G 还拷了两份）。
    """
    cfg = _cfg(tmp_path, backup_max_mb=1)
    ws = Workspace(cfg, DB(cfg.db_path))
    other = {"id": 9, "workdir": str(tmp_path / "tasks" / "9"),
             "user": "崔成东", "finished_at": time.time()}

    # 1) 小文件 + 业务目录 → 正常备份
    _write(tmp_path, "tasks/9/.result/shared/report/a.md", "v1")
    p, why = ws.backup_version(other, "shared/report/a.md")
    assert p and why == "" and os.path.isfile(p)

    # 2) 超过 backup_max_mb → 跳过并给出原因
    big = tmp_path / "tasks/9/.result/shared/report/big.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    p, why = ws.backup_version(other, "shared/report/big.bin")
    assert p == "" and "超过备份上限" in why

    # 3) 运行时目录（SKIP_DIRS 名单）→ 跳过
    for rel in ("shared/venv/lib/x.py", "shared/models/m.bin", "shared/.archive/bak/o.md"):
        _write(tmp_path, f"tasks/9/.result/{rel}", "y")
        p, why = ws.backup_version(other, rel)
        assert p == "" and "运行时目录" in why, rel

    # 4) 任意名字的虚拟环境（靠 pyvenv.cfg 标记识别）→ 跳过
    _write(tmp_path, "shared/asr_env/pyvenv.cfg", "home = /usr")
    _write(tmp_path, "tasks/9/.result/shared/asr_env/lib/x.py", "y")
    p, why = ws.backup_version(other, "shared/asr_env/lib/x.py")
    assert p == "" and "运行时目录" in why

    # 5) 源不存在 → 空路径空原因（调用方据此写"源版本已不在 .result 中"）
    assert ws.backup_version(other, "shared/report/nope.md") == ("", "")


def test_volatile_rewrite_never_conflicts(relay_factory, tmp_path):
    """线上误报复现：连续多个任务调用 dws，凭据缓存每次被重写 → 全部 done，不判冲突。"""
    r = _fake_relay(relay_factory, tmp_path)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "中鲁运营"}).json()["token"]
    for i in range(3):
        tid = submit(base, tok, f"用 dws 给王洪彬发消息（写缓存）第 {i + 1} 次")["task_id"]
        t = wait_task(base, tok, tid, timeout=60)
        assert t["status"] == "done", t
        assert t["conflicts"] == [], t
        assert t["changed_files"] == [], t
        assert "缓存" in t["error"], t
    admin_c.close()


def test_conflict_only_within_window(relay_factory, tmp_path):
    """同一真实文件：窗内先后写入仍判冲突并备份；窗外属正常演进，不判冲突。"""
    r = _fake_relay(relay_factory, tmp_path)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "tester"}).json()["token"]

    t1 = wait_task(base, tok, submit(base, tok, "写报告 v1")["task_id"], timeout=60)
    assert t1["status"] == "review", t1
    t2 = wait_task(base, tok, submit(base, tok, "写报告 v2")["task_id"], timeout=60)
    assert t2["status"] == "conflict", t2
    assert any(c["with_task"] == t1["id"] and c["backup"] for c in t2["conflicts"]), t2

    r["cfg"].conflict_window_hours = 0  # 时间窗收到 0：此前任务全部落在窗外
    t3 = wait_task(base, tok, submit(base, tok, "写报告 v3")["task_id"], timeout=60)
    assert t3["status"] == "review", t3
    assert t3["conflicts"] == [], t3
    admin_c.close()
