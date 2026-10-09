"""tests/test_exec_local.py —— local 后端约束映射/隔离区/默认关（不真跑 claude）。"""
import datetime as dt

from app.exec_local import ExecLocal


def make(cfg, storage, audit, policy):
    return ExecLocal(cfg, storage, audit, policy)


def test_disabled_by_default(cfg, storage, audit, policy):
    ex = make(cfg, storage, audit, policy)
    assert not ex.available()
    assert "未启用" in ex.run_l3("300", "问题")["text"]


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


def test_parse_line_stream(cfg, storage, audit, policy):
    import json as _j
    ex = make(cfg, storage, audit, policy)
    steps = []
    fired = []
    # tool_use 事件 → 步骤回调
    line = _j.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read"}]}})
    assert ex._parse_line(line, steps, lambda s, a: fired.append(s)) is None
    assert steps == ["📖 读取文件"] and fired == ["📖 读取文件"]
    # 首段文本 → 组织回复步骤（只触发一次）
    line = _j.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "开始作答"}]}})
    ex._parse_line(line, steps, lambda s, a: fired.append(s))
    ex._parse_line(line, steps, lambda s, a: fired.append(s))
    assert steps.count("✍️ 正在组织回复…") == 1
    # result 事件 → 返回 dict
    line = _j.dumps({"type": "result", "result": "最终答复",
                     "session_id": "sid-9", "is_error": False})
    d = ex._parse_line(line, steps, None)
    assert d["result"] == "最终答复" and d["session_id"] == "sid-9"
    # 非 JSON / 非对象行忽略
    assert ex._parse_line("noise", steps, None) is None


def test_is_deny():
    from app.exec_local import ExecLocal
    assert ExecLocal._is_deny("The tool use was rejected (permission denied)")
    assert ExecLocal._is_deny("此操作需要批准，已被拒绝")
    assert not ExecLocal._is_deny("正常完成的答复")


def test_claude_env_merges_settings(cfg, storage, audit, policy,
                                    tmp_path, monkeypatch):
    """认证 env 从 ~/.claude/settings.json 注入；CLAUDE_CONFIG_DIR 隔离。"""
    import json as _j
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(_j.dumps(
        {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8790",
                 "ANTHROPIC_AUTH_TOKEN": "tok"}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # 机队 shell（/etc/profile.d/claude.sh）会导出 ANTHROPIC_*；_claude_env 用
    # setdefault（进程环境优先），不清掉就无法确定注入源是 settings.json
    for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
              "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_CUSTOM_MODEL_OPTION"):
        monkeypatch.delenv(k, raising=False)
    ex = make(cfg, storage, audit, policy)
    env = ex._claude_env()
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8790"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"
    assert env["CLAUDE_CONFIG_DIR"] == str(ex.cchome)
    assert "127.0.0.1" in env["NO_PROXY"]


def test_run_self_escalates_when_unavailable(cfg, storage, audit, policy):
    """local 未启用时 self 任务返回 escalate=True（升级不降级 → relay 门禁）。"""
    ex = make(cfg, storage, audit, policy)
    r = ex.run_self("18434", "干活")
    assert r["escalate"] is True
