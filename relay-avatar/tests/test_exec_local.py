"""tests/test_exec_local.py —— local 后端约束映射/隔离区/默认关（不真跑 claude）。"""
import datetime as dt

from app.exec_local import ExecLocal


def make(cfg, storage, audit, policy):
    return ExecLocal(cfg, storage, audit, policy)


def test_disabled_by_default(cfg, storage, audit, policy):
    ex = make(cfg, storage, audit, policy)
    assert not ex.available()
    assert "未启用" in ex.run_l3("300", "问题")


def test_sender_data_dirs(cfg, storage, shared, audit, policy):
    d = shared / "data" / "2026" / "2026-09" / "2026-09-23" / "300"
    d.mkdir(parents=True)
    d2 = shared / "data" / "2026" / "2026-09" / "2026-09-22" / "300"
    d2.mkdir(parents=True)
    (shared / "data" / "2026" / "2026-09" / "2026-09-23" / "400").mkdir(
        parents=True)
    ex = make(cfg, storage, audit, policy)
    dirs = ex.sender_data_dirs("300")
    assert len(dirs) == 2 and all(d.endswith("/300") for d in dirs)
    assert ex.sender_data_dirs("../../etc") == []   # 路径穿越清洗后无匹配
    assert len(ex.sender_data_dirs("400")) == 1     # 他人数据区互不可见


def test_flags_l3_hard_isolation(cfg, storage, audit, policy):
    ex = make(cfg, storage, audit, policy)
    cons = policy.constraints("300")
    flags = ex._flags(cons, ["/data/300"])
    assert "--permission-mode" in flags and "manual" in flags
    assert "--permission-prompts" in flags and "none" in flags
    assert "--add-dir" in flags and "/data/300" in flags
    dis = flags[flags.index("--disallowedTools") + 1]
    for t in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
        assert t in dis


def test_flags_l2_no_bypass(cfg, storage, audit, policy):
    ex = make(cfg, storage, audit, policy)
    cons = policy.constraints("200")
    flags = ex._flags(cons, [])
    assert flags[flags.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in flags
    allowed = flags[flags.index("--allowedTools") + 1]
    assert "Bash" not in allowed


def test_session_ttl(cfg, storage, audit, policy):
    import time
    ex = make(cfg, storage, audit, policy)
    ex.save_session("k1", "sid-1")
    assert ex.get_session("k1") == "sid-1"
    d = ex._sessions()
    d["k1"]["ts"] = time.time() - 99999
    storage.save_json(ex.sessions_file, d)
    assert ex.get_session("k1") == ""
