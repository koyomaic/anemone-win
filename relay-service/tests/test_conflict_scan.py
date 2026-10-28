from __future__ import annotations

import os
import time

import httpx
import pytest

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


# 增量任务水位文件（2026-10-23）。「财务智脑攻坚组群消息即时提炼」计划每 15 分钟
# 触发一次，累计 32 单里 27 单（84%）判 conflict，冲突记录里该文件被命中 1140 次，
# 而每单 changed_files **只有这一个水位文件**——纯伪冲突：白干 1.2h 机时、堆了 68MB
# 冲突备份，还占并发槽跟真实用户任务抢。
GONGJIANZU_WM = "shared/report/dingtalk-digest/state/gongjianzu.watermark.txt"


def test_is_volatile_matches_watermark_state():
    pats = DEFAULT_VOLATILE_PATTERNS
    assert is_volatile(GONGJIANZU_WM, pats)                    # 线上真实路径
    assert is_volatile("shared/report/x/state/y.watermark.txt", pats)
    assert is_volatile("shared/report/x/state/y.watermark.json", pats)
    assert is_volatile("shared/report/x/state/y.watermark", pats)
    assert is_volatile("shared/report/dingtalk-digest/gongjianzu.watermark.txt", pats)
    # 不能顺手把真实产出也豁免掉
    for keep in ("shared/report/dingtalk-digest/20261023_攻坚组纪要.md",
                 "shared/report/dingtalk-digest/state/digest.md",
                 "shared/knowledge/ai-daily-sources/ledger.csv",
                 "shared/knowledge/调价.md"):
        assert not is_volatile(keep, pats), keep


def test_watermark_only_task_produces_no_changed_files(tmp_path):
    """无新消息的一轮：只重写水位 → scan["all"] 为空，不进冲突判定。"""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    ws = Workspace(cfg, db)
    tid = db.create_task(user="im-bridge", description="群消息增量提炼")
    workdir = ws.prepare(db.task(tid))
    _write(tmp_path, GONGJIANZU_WM, "2026-10-23T11:00:00+08:00")

    scan = ws.scan(db.task(tid))
    assert scan["all"] == []
    assert scan["volatile"] == [GONGJIANZU_WM]
    assert db.task(tid)["changed_files"] == []
    # 水位不进 .result，也就不会被当冲突备份源（68MB 备份堆积就是这么来的）
    assert not os.path.exists(os.path.join(workdir, ".result", GONGJIANZU_WM))


def test_watermark_exempt_but_real_output_still_counted(tmp_path):
    """有新消息的一轮：水位豁免，但真实纪要照常计入产出、照常参与冲突判定。"""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    ws = Workspace(cfg, db)
    tid = db.create_task(user="im-bridge", description="群消息增量提炼")
    ws.prepare(db.task(tid))
    _write(tmp_path, GONGJIANZU_WM, "2026-10-23T11:15:00+08:00")
    _write(tmp_path, "shared/report/dingtalk-digest/20261023_攻坚组纪要.md", "纪要")

    scan = ws.scan(db.task(tid))
    assert scan["all"] == ["shared/report/dingtalk-digest/20261023_攻坚组纪要.md"]
    assert scan["volatile"] == [GONGJIANZU_WM]


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

    # 1) 小文件 + 业务目录 → 备份进归档区，不污染正式目录
    _write(tmp_path, "tasks/9/.result/shared/report/a.md", "v1")
    p, why = ws.backup_version(other, "shared/report/a.md")
    assert p and why == "" and os.path.isfile(p)
    assert "/shared/.archive/bak/conflict/T9/report/a.md.bak-崔成东-" in p
    prod_dir = tmp_path / "shared" / "report"
    assert not prod_dir.exists() or not any(f.startswith("a.md.bak-") for f in os.listdir(prod_dir))

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
    # shared/reports/ 属低风险新建产出，首单自动 done；冲突守卫看的是 changed_files，
    # 不因自动 done 而失效。
    assert t1["status"] == "done", t1
    assert "低风险新建产出" in (t1.get("error") or ""), t1
    t2 = wait_task(base, tok, submit(base, tok, "写报告 v2")["task_id"], timeout=60)
    assert t2["status"] == "conflict", t2
    assert any(c["with_task"] == t1["id"] and c["backup"] for c in t2["conflicts"]), t2

    r["cfg"].conflict_window_hours = 0  # 时间窗收到 0：此前任务全部落在窗外
    t3 = wait_task(base, tok, submit(base, tok, "写报告 v3")["task_id"], timeout=60)
    assert t3["status"] == "review", t3
    assert t3["conflicts"] == [], t3
    admin_c.close()


# ── identical_version：内容一致即无覆盖损失，不构成真冲突 ──
# 2026-10-10 事故：同一份周报需求 6 分钟内从单聊 + 两个群各触发一个任务
# （#689/#690/#691），三者收敛出**字节完全相同**的 HTML（三份 MD5 一致）并写同一
# 路径，却因"都改动了同一文件"被判 conflict/review，全部卡在人工核查；其中交互群
# 那个还因三个重任务并发抢资源撞上 1800s 超时。冲突的本质是内容分歧导致覆盖损失，
# 不是碰过同一个路径。

def _two_tasks(tmp_path, rel: str, a: str, b: str):
    ta = {"id": 1, "workdir": str(tmp_path / "tasks" / "1"),
          "user": "赵兴峰", "finished_at": time.time()}
    tb = {"id": 2, "workdir": str(tmp_path / "tasks" / "2"),
          "user": "赵兴峰", "finished_at": time.time()}
    _write(tmp_path, f"tasks/1/.result/{rel}", a)
    _write(tmp_path, f"tasks/2/.result/{rel}", b)
    return ta, tb


def test_identical_version_true_when_bytes_equal(tmp_path):
    cfg = _cfg(tmp_path)
    ws = Workspace(cfg, DB(cfg.db_path))
    rel = "shared/report/周报/20261010_T689_周报W41_v1.html"
    body = "<html>鲁大师工作周报 · 2026-W41</html>"
    ta, tb = _two_tasks(tmp_path, rel, body, body)
    assert ws.identical_version(ta, tb, rel) is True


def test_identical_version_false_when_content_differs(tmp_path):
    """内容真有分歧时必须判为冲突——这是守卫存在的理由，不能一并放掉。"""
    cfg = _cfg(tmp_path)
    ws = Workspace(cfg, DB(cfg.db_path))
    rel = "shared/report/a.md"
    ta, tb = _two_tasks(tmp_path, rel, "v1 原始结论", "v2 被改写的结论")
    assert ws.identical_version(ta, tb, rel) is False


def test_identical_version_false_when_size_differs(tmp_path):
    """尺寸不同直接短路（省两次全量哈希），且必须判 False。"""
    cfg = _cfg(tmp_path)
    ws = Workspace(cfg, DB(cfg.db_path))
    rel = "shared/report/a.md"
    ta, tb = _two_tasks(tmp_path, rel, "short", "much longer content")
    assert ws.identical_version(ta, tb, rel) is False


def test_identical_version_false_when_either_side_missing(tmp_path):
    """拿不到证据就保守按冲突处理，绝不因为缺版本而放行。

    .result/ 可能已被 cleanup 或从未落盘（任务在写文件前就失败）。
    """
    cfg = _cfg(tmp_path)
    ws = Workspace(cfg, DB(cfg.db_path))
    rel = "shared/report/a.md"
    ta = {"id": 1, "workdir": str(tmp_path / "tasks" / "1"),
          "user": "x", "finished_at": time.time()}
    tb = {"id": 2, "workdir": str(tmp_path / "tasks" / "2"),
          "user": "x", "finished_at": time.time()}
    _write(tmp_path, f"tasks/1/.result/{rel}", "只有单边有")
    assert ws.identical_version(ta, tb, rel) is False
    assert ws.identical_version(tb, ta, rel) is False      # 反向同样保守
    assert ws.identical_version(ta, tb, "shared/report/不存在.md") is False


def test_identical_version_not_fooled_by_same_size(tmp_path):
    """同长度不同内容（哈希才分得出来）必须判 False。"""
    cfg = _cfg(tmp_path)
    ws = Workspace(cfg, DB(cfg.db_path))
    rel = "shared/report/a.md"
    ta, tb = _two_tasks(tmp_path, rel, "AAAA1234", "AAAA1235")
    assert ws.identical_version(ta, tb, rel) is False


# 假 agent：写**确定性**内容（不带时间戳），用于复现"多任务收敛出同一份产出"
FAKE_AGENT_SAME = """#!/bin/sh
prompt="$*"
if printf '%s' "$prompt" | grep -q "写同一份报告"; then
  mkdir -p ../../shared/reports
  printf '<html>鲁大师工作周报 2026-W41 七节齐全</html>\\n' > ../../shared/reports/same.html
fi
printf '%s\\n' '{"type":"text","sessionID":"ses_s","part":{"type":"text","text":"done"}}'
printf '%s\\n' '{"type":"step_finish","sessionID":"ses_s","part":{"tokens":{"total":5},"cost":0}}'
exit 0
"""


def test_identical_output_from_concurrent_tasks_is_not_conflict(
        relay_factory, tmp_path):
    """端到端回归：两个任务写同一路径但**内容字节相同** → 不判冲突，正常自动 done。

    复现 2026-10-10 交互群事故的核心误判：同一需求经三个会话各触发一个任务，
    三者收敛出完全一致的周报（MD5 相同），却因"都改动了同一文件"被判
    conflict/review 卡在人工核查，且核查提示不外发，发起人只看到沉默。
    对照 test_conflict_only_within_window（内容每次不同 → 仍判 conflict），
    可见判据是**内容分歧**而非路径重合。shared/reports/ 纳入低风险前缀后，
    首单也直接 done。
    """
    script = tmp_path / "fake_agent_same.sh"
    script.write_text(FAKE_AGENT_SAME)
    script.chmod(0o755)
    r = relay_factory(agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base,
                           headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "赵兴峰"}).json()["token"]

    t1 = wait_task(base, tok, submit(base, tok, "写同一份报告 第一遍")["task_id"],
                   timeout=60)
    assert t1["status"] == "done", t1
    assert "低风险新建产出" in (t1.get("error") or ""), t1
    assert t1["changed_files"] == ["shared/reports/same.html"], t1

    t2 = wait_task(base, tok, submit(base, tok, "写同一份报告 第二遍")["task_id"],
                   timeout=60)
    # 关键断言是**不判 conflict**。状态为 done 而非 review 也是对的：第二个任务
    # 写出的字节与基线（第一个任务的产出）完全相同，scan 判定无净变更，故走
    # "自动done（只读，无文件改动）"分支——既无覆盖损失，也就无需人工过目。
    assert t2["status"] != "conflict", \
        f"内容一致不应判冲突，实际 {t2['status']} conflicts={t2['conflicts']}"
    assert t2["conflicts"] == [], t2

    # 先完成的那个也不应被回写"有其它任务也在改动该文件"
    t1b = admin_c.get(f"/tasks/{t1['id']}").json()
    t1b = t1b.get("task", t1b)
    assert t1b["conflicts"] == [], t1b
    admin_c.close()


# ── 产出路径隔离：文件名里的任务号只能是本任务 ──
# prompt 此前从不告知 agent 自己的任务号，它看到磁盘上已有 `_T689_` 的报告就接着写。
# 2026-10-10 事故里 #690/#691 都写进了 `_T689_` 那个路径。

from app.workspace import foreign_task_tag          # noqa: E402
from app.main import build_prompt                   # noqa: E402


@pytest.mark.parametrize("rel,own,expect", [
    # 越界：文件名带的是别的任务号
    ("shared/report/周报/20261010_T689_周报W41_v1.html", 690, 689),
    ("shared/report/20261010_T689_x_v1.md", 691, 689),
    # 合规：自己的任务号
    ("shared/report/20261010_T690_周报W41_v1.html", 690, None),
    ("shared/report/20261010_T689_周报W41_v1.html", 689, None),
    # T0 = 无关联任务，约定内合法值
    ("shared/report/20261010_T0_会议纪要_v1.md", 690, None),
    # 没有任务号段 / 不是该命名体系 → 不判越界
    ("shared/report/周报.md", 690, None),
    ("shared/knowledge/工具运维/dws.md", 690, None),
    # 任务号在中间段才算，纯数字文件名不误判
    ("shared/report/20261010_689_x_v1.md", 690, None),
])
def test_foreign_task_tag(rel, own, expect):
    assert foreign_task_tag(rel, own) == expect


def test_foreign_task_tag_handles_str_and_none_id():
    """任务号可能是 str（DB 驱动差异）或 None，都不能抛。

    own_id 未知时**不指控**：这是检测性启发式，宁可漏报也不制造无法核实的
    审计噪音；真正的覆盖损失由冲突守卫（内容哈希 + 备份）兜住。
    """
    assert foreign_task_tag("shared/report/x_T689_y_v1.md", "689") is None
    assert foreign_task_tag("shared/report/x_T689_y_v1.md", "690") == 689
    assert foreign_task_tag("shared/report/x_T689_y_v1.md", None) is None
    assert foreign_task_tag("shared/report/x_T689_y_v1.md", "abc") is None


def test_scan_reports_foreign_task_files(tmp_path):
    """scan 必须把越界写入单独列出来，供审计归因。"""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    tid = db.create_task(user="赵兴峰", description="写周报")
    db.set(tid, workdir=str(tmp_path / "tasks" / str(tid)))
    ws = Workspace(cfg, db)
    ws.prepare(db.task(tid))
    # 本任务写进了带 #689 任务号的路径
    _write(tmp_path, "shared/report/20261010_T689_周报W41_v1.html", "<html>x</html>")
    _write(tmp_path, f"tasks/{tid}/.baseline.json", "{}")
    scan = ws.scan(db.task(tid))
    assert scan["foreign"] == [("shared/report/20261010_T689_周报W41_v1.html", 689)]


def test_scan_foreign_empty_for_own_task_id(tmp_path):
    """写自己的任务号 → foreign 为空（不能把合规产出误报成越界）。"""
    cfg = _cfg(tmp_path)
    db = DB(cfg.db_path)
    tid = db.create_task(user="赵兴峰", description="写周报")
    db.set(tid, workdir=str(tmp_path / "tasks" / str(tid)))
    ws = Workspace(cfg, db)
    ws.prepare(db.task(tid))
    _write(tmp_path, f"shared/report/20261010_T{tid}_周报W41_v1.html", "<html>x</html>")
    scan = ws.scan(db.task(tid))
    assert scan["foreign"] == []


def test_build_prompt_tells_agent_its_own_task_id(tmp_path):
    """prompt 必须带上本任务号与命名铁律——否则 agent 无从遵守 AGENTS.md 的约定。"""
    cfg = _cfg(tmp_path)
    row = {"id": 690, "description": "写周报", "targets": [], "resume_from": None}
    p = build_prompt(row, cfg)
    assert "#690" in p
    assert "T690" in p, "必须给出可直接照抄的任务号段"
    assert "禁止创建或覆盖带其它任务号的文件" in p


def test_build_prompt_keeps_resume_prefix(tmp_path):
    """回归：resume 前缀仍在最前，任务号注入不得打乱原有结构。"""
    cfg = _cfg(tmp_path)
    row = {"id": 691, "description": "继续", "targets": [], "resume_from": 689}
    p = build_prompt(row, cfg)
    assert p.startswith("接续任务 #689")
    assert "#691" in p and "T691" in p


def test_build_prompt_includes_machine_scoped_wiki_rule(tmp_path):
    cfg = _cfg(tmp_path)
    row = {"id": 692, "description": "归档知识", "targets": [], "resume_from": None}
    p = build_prompt(row, cfg)
    assert "store add --kind knowledge" in p
    assert "store sync push" in p
    assert "`report/data` 不自动同步" in p
    assert "本机 AGENTS" in p
