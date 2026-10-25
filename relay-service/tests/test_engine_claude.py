"""claude 引擎支持：命令构建、环境隔离、共享知识注入、会话守卫与 e2e 假 agent 全流程。

opencode 既有行为由原有测试文件守护（本文件不重复）；此处仅断言 opencode 分支
不受 claude 改动污染的关键点。
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from app.config import Settings
from app.db import DB
from app.executor import (Executor, _find_transcript, _parse_claude,
                          _state_dirname, strip_transcript_images)
from app.main import _session_if_same_engine
from app.shared_knowledge import _claude_mcp_entry, prepare_run

from conftest import submit, wait_task


def _cfg(tmp_path, engine: str = "claude", model: str = "") -> Settings:
    return Settings(
        db_path=str(tmp_path / "relay.db"),
        workspace_root=str(tmp_path),
        shared_dir=str(tmp_path / "shared"),
        task_root=str(tmp_path / "tasks"),
        users_root=str(tmp_path / "users"),
        engine=engine,
        model=model,
    )


# ---- build_cmd ----

def test_build_cmd_claude_flags(tmp_path):
    ex = Executor(_cfg(tmp_path, model="Qwen3.8-27B-FP8-LCBD"))
    cmd = ex.build_cmd(str(tmp_path), "do it", None)
    assert cmd[1] == "-p" and cmd[2] == "do it"
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--model") + 1] == "Qwen3.8-27B-FP8-LCBD"
    assert "--resume" not in cmd


def test_build_cmd_claude_resume_and_mcp(tmp_path):
    ex = Executor(_cfg(tmp_path))
    xdg = tmp_path / ".xdg"
    xdg.mkdir()
    (xdg / "claude-mcp.json").write_text("{}")
    cmd = ex.build_cmd(str(tmp_path), "p", "ses_1")
    assert cmd[cmd.index("--resume") + 1] == "ses_1"
    assert cmd[cmd.index("--mcp-config") + 1] == str(xdg / "claude-mcp.json")
    assert "--strict-mcp-config" in cmd
    assert "--model" not in cmd  # cfg.model 为空则不传，走 ANTHROPIC_MODEL


def test_build_cmd_opencode_untouched(tmp_path):
    ex = Executor(_cfg(tmp_path, engine="opencode", model="m1"))
    cmd = ex.build_cmd(str(tmp_path), "p", "ses_x")
    assert cmd[1] == "run" and "--auto" in cmd
    assert cmd[cmd.index("-m") + 1] == "m1"
    assert cmd[cmd.index("-s") + 1] == "ses_x"
    for flag in ("--dangerously-skip-permissions", "--output-format", "--mcp-config",
                 "--disallowedTools"):
        assert flag not in cmd


# ---- 内置工具裁剪（2026-10-23）----
#
# claude-code 默认挂 26 个工具，光工具定义就占 19788 token——实测固定上下文
# 21539 token 里 89.8% 是它（CLAUDE.md 只 1567、CEO_PROMPT 486、19 个技能合计
# 仅 185）。这 21.5k 每轮 LLM 往返都要重新 prefill，按 ~3000 tok/s 就是每轮
# ~7s 纯固定开销。禁用清单按 314 会话 / 6779 次调用的真实使用统计挑，只砍
# 全量历史里调用次数为 0 的 7 个：26→19 工具、21539→15166 token。

_A_GROUP = {"NotebookEdit", "DesignSync", "EnterWorktree", "ExitWorktree",
            "ReportFindings", "ScheduleWakeup", "CronDelete"}

# 有实测调用、砍了会降质的工具，必须永远留在白名单外
_MUST_KEEP = {"Bash", "Read", "Write", "Edit", "Skill", "Task", "SendMessage",
              "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput",
              "TaskStop", "WebFetch", "WebSearch"}


def test_default_disallowed_tools_is_exactly_a_group():
    from app.config import DEFAULT_CLAUDE_DISALLOWED_TOOLS
    assert set(DEFAULT_CLAUDE_DISALLOWED_TOOLS) == _A_GROUP


def test_default_disallowed_tools_never_touches_used_ones():
    """回归保护：TaskCreate(166次)/TaskUpdate(250次)/Skill(99次)/WebFetch(8次)
    等都有实测调用，绝不能进禁用清单。第一版清单曾误砍 TaskCreate/TaskUpdate。"""
    from app.config import DEFAULT_CLAUDE_DISALLOWED_TOOLS
    assert not (set(DEFAULT_CLAUDE_DISALLOWED_TOOLS) & _MUST_KEEP)


def test_build_cmd_claude_disallowed_tools(tmp_path):
    ex = Executor(_cfg(tmp_path))
    cmd = ex.build_cmd(str(tmp_path), "p", None)
    assert "--disallowedTools" in cmd
    val = cmd[cmd.index("--disallowedTools") + 1]
    # 逗号连成单个 argv（与 avatar exec_local 既有写法一致），实测 claude 认这种形式
    assert set(val.split(",")) == _A_GROUP
    assert not (_MUST_KEEP & set(val.split(",")))


def test_build_cmd_claude_disallowed_tools_custom(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.claude_disallowed_tools = ["NotebookEdit", "DesignSync"]
    cmd = Executor(cfg).build_cmd(str(tmp_path), "p", None)
    assert cmd[cmd.index("--disallowedTools") + 1] == "NotebookEdit,DesignSync"


def test_build_cmd_claude_disallowed_tools_empty(tmp_path):
    """空列表＝一个都不禁，回到 claude-code 原生 26 工具。"""
    cfg = _cfg(tmp_path)
    cfg.claude_disallowed_tools = []
    assert "--disallowedTools" not in Executor(cfg).build_cmd(str(tmp_path), "p", None)
    cfg.claude_disallowed_tools = ["  ", ""]
    assert "--disallowedTools" not in Executor(cfg).build_cmd(str(tmp_path), "p", None)


def test_settings_claude_disallowed_tools_from_env(monkeypatch, tmp_path):
    from app.config import Settings
    monkeypatch.setenv("RELAY_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RELAY_CLAUDE_DISALLOWED_TOOLS", raising=False)
    assert set(Settings.from_env().claude_disallowed_tools) == _A_GROUP

    monkeypatch.setenv("RELAY_CLAUDE_DISALLOWED_TOOLS", "NotebookEdit, DesignSync")
    assert Settings.from_env().claude_disallowed_tools == ["NotebookEdit", "DesignSync"]

    # 空格分隔也认
    monkeypatch.setenv("RELAY_CLAUDE_DISALLOWED_TOOLS", "NotebookEdit DesignSync")
    assert Settings.from_env().claude_disallowed_tools == ["NotebookEdit", "DesignSync"]

    # 显式关闭：排查「某工具被误禁」时不用改代码
    for off in ("none", "NONE", "-", "off"):
        monkeypatch.setenv("RELAY_CLAUDE_DISALLOWED_TOOLS", off)
        assert Settings.from_env().claude_disallowed_tools == []


# ---- build_env ----

def test_build_env_claude_sandbox_and_state(tmp_path):
    cfg = _cfg(tmp_path)
    ex = Executor(cfg)
    workdir = str(tmp_path / "wd")
    env = ex.build_env(workdir, None, user="王洪彬")
    assert env["IS_SANDBOX"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == os.path.join(cfg.users_root, "王洪彬", ".claude")
    assert os.path.isdir(env["CLAUDE_CONFIG_DIR"])
    assert env["HOME"] == os.path.join(workdir, "home")


def test_build_env_claude_user_sanitized(tmp_path):
    cfg = _cfg(tmp_path)
    ex = Executor(cfg)
    env = ex.build_env(str(tmp_path / "wd"), None, user="../../etc")
    state = os.path.normpath(env["CLAUDE_CONFIG_DIR"])
    assert state.startswith(os.path.normpath(cfg.users_root) + os.sep), \
        "恶意用户名不得逃逸 users_root"


def test_build_env_opencode_no_claude_vars(tmp_path):
    ex = Executor(_cfg(tmp_path, engine="opencode"))
    env = ex.build_env(str(tmp_path / "wd"), None, user="u")
    assert "IS_SANDBOX" not in env and "CLAUDE_CONFIG_DIR" not in env
    # 单轮保护是 claude-code 专有环境变量，opencode 分支不得注入
    for k in ("API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS",
              "CLAUDE_CODE_MAX_RETRIES"):
        assert k not in env


# ---- 单轮 LLM 调用保护（停顿轮治理）----
#
# 背景：task_timeout 只管整个任务，管不到单轮。实测 20/145 轮是停顿轮
# （输出 <2000 字符却 >60s），吃掉 LLM 总时间 35%，最坏一轮单次 API 调用
# 挂 269s。claude-code 的 API_TIMEOUT_MS 默认 600s，停顿完全在阈值内。

def test_build_env_claude_api_guards_default(tmp_path):
    env = Executor(_cfg(tmp_path)).build_env(str(tmp_path / "wd"), None, user="u")
    assert env["API_TIMEOUT_MS"] == "180000"
    assert env["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"] == "60000"
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "3"


def test_build_env_claude_retry_var_name(tmp_path):
    """名字必须是 CLAUDE_CODE_MAX_RETRIES：MAX_RETRIES 在 claude 二进制里 0 命中
    （只作为前者后缀存在），传了等于没传——这条防的是「静默无效」回归。"""
    env = Executor(_cfg(tmp_path)).build_env(str(tmp_path / "wd"), None, user="u")
    assert "MAX_RETRIES" not in env, "裸 MAX_RETRIES 不是 claude-code 的环境变量名"
    assert "CLAUDE_CODE_MAX_RETRIES" in env


def test_build_env_claude_api_guards_from_settings(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.api_timeout_ms = 240000.0
    cfg.api_first_byte_timeout_ms = 90000.0
    cfg.api_max_retries = 5
    env = Executor(cfg).build_env(str(tmp_path / "wd"), None, user="u")
    assert env["API_TIMEOUT_MS"] == "240000"
    assert env["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"] == "90000"
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "5"


def test_build_env_claude_api_guards_disabled(tmp_path):
    """0/负值＝不设限，沿用 claude-code 默认（逃生舱：阈值误杀合法轮时可关）。"""
    cfg = _cfg(tmp_path)
    cfg.api_timeout_ms = 0.0
    cfg.api_first_byte_timeout_ms = -1.0
    env = Executor(cfg).build_env(str(tmp_path / "wd"), None, user="u")
    assert "API_TIMEOUT_MS" not in env
    assert "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS" not in env
    assert "CLAUDE_CODE_MAX_RETRIES" in env, "重试次数独立于两个超时，不受其开关影响"


def test_build_env_claude_max_retries_clamped(tmp_path):
    """claude-code 内部钳位上限 15；超了会被静默 clamp 并打 warn，不如自己先夹。"""
    cfg = _cfg(tmp_path)
    cfg.api_max_retries = 99
    env = Executor(cfg).build_env(str(tmp_path / "wd"), None, user="u")
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "15"


def test_settings_api_guards_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_API_TIMEOUT_MS", "150000")
    monkeypatch.setenv("RELAY_API_FIRST_BYTE_TIMEOUT_MS", "45000")
    monkeypatch.setenv("RELAY_API_MAX_RETRIES", "2")
    monkeypatch.setenv("RELAY_WORKSPACE", str(tmp_path))
    from app.config import Settings
    s = Settings.from_env()
    assert s.api_timeout_ms == 150000.0
    assert s.api_first_byte_timeout_ms == 45000.0
    assert s.api_max_retries == 2


def test_state_dirname():
    assert _state_dirname("") == "default"
    assert _state_dirname("..") == "default"
    assert _state_dirname("a/b") == "a_b"
    assert _state_dirname("王洪彬") == "王洪彬"


# ---- _parse_claude ----

def test_parse_claude_events():
    sys_ev = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    assert _parse_claude(sys_ev) == {"session_id": "s1"}

    asst = json.dumps({"type": "assistant", "session_id": "s1", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf /root"}},
    ]}})
    out = _parse_claude(asst)
    assert out["text"] == "hi"
    assert out["agent_event"]["kind"] == "tool"

    tool_only = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls /"}},
    ]}})
    assert _parse_claude(tool_only)["tool_cmd"] == "ls /"

    res = json.dumps({"type": "result", "subtype": "success", "session_id": "s2",
                      "result": "final answer", "total_cost_usd": 0.002,
                      "usage": {"input_tokens": 10, "output_tokens": 5}})
    out = _parse_claude(res)
    assert out["text_final"] == "final answer"
    assert "text" not in out  # result 不追加，避免与流式 text 重复
    assert out["tokens"] == 15 and out["cost"] == 0.002 and out["session_id"] == "s2"

    assert _parse_claude("not json") is None


# ---- MCP 条目转换 ----

def test_claude_mcp_entry_local():
    e = _claude_mcp_entry({"type": "local", "command": ["python3", "a.py"],
                           "environment": {"K": "V"}})
    assert e == {"command": "python3", "args": ["a.py"], "env": {"K": "V"}}


def test_claude_mcp_entry_local_str_command():
    assert _claude_mcp_entry({"type": "local", "command": "node"}) == \
        {"command": "node", "args": []}


def test_claude_mcp_entry_remote():
    assert _claude_mcp_entry({"type": "remote", "url": "http://b"}) == \
        {"type": "http", "url": "http://b"}


def test_claude_mcp_entry_invalid():
    assert _claude_mcp_entry({"type": "local"}) == {}
    assert _claude_mcp_entry("x") == {}


# ---- prepare_run claude 分支 ----

class _Cfg:
    def __init__(self, shared_dir: str, engine: str = "claude",
                 workspace_root: str = ""):
        self.shared_dir = shared_dir
        self.engine = engine
        self.xdg_config_home = ""
        self.relay_exclude_providers = []
        self.workspace_root = workspace_root


def test_prepare_run_claude_injects(tmp_path):
    sh = tmp_path / "shared"
    (sh / "skills" / "good").mkdir(parents=True)
    (sh / "skills" / "good" / "SKILL.md").write_text("# good")
    (sh / "skills" / "noskill").mkdir()          # 无 SKILL.md → 不注入
    (sh / "mcp").mkdir()
    (sh / "mcp" / "alpha.json").write_text(
        json.dumps({"type": "local", "command": ["python3", "a.py"]}))
    (sh / "memory").mkdir()
    (sh / "memory" / "m1.md").write_text("# 记忆内容")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# 团队约定\n- dws 用法")
    workdir = tmp_path / "wd"
    workdir.mkdir()

    xdg = prepare_run(str(workdir), _Cfg(str(sh), workspace_root=str(ws)))
    assert xdg == str(workdir / ".xdg")

    link = workdir / ".claude" / "skills" / "good"
    assert link.is_symlink() and os.path.isfile(link / "SKILL.md")
    assert not (workdir / ".claude" / "skills" / "noskill").exists()

    mcp = json.loads((workdir / ".xdg" / "claude-mcp.json").read_text())
    assert mcp == {"mcpServers": {"alpha": {"command": "python3", "args": ["a.py"]}}}

    claude_md = (workdir / "CLAUDE.md").read_text()
    assert "记忆内容" in claude_md
    assert "团队约定" in claude_md  # workspace AGENTS.md 并入
    assert not (workdir / ".xdg" / "opencode").exists()  # 不写 opencode 配置
    assert not (workdir / "AGENTS.md").exists()
    assert (workdir / "home").is_dir()


def test_prepare_run_claude_empty_shared(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    prepare_run(str(workdir), _Cfg(str(sh)))
    assert not (workdir / ".xdg" / "claude-mcp.json").exists()
    assert not (workdir / "CLAUDE.md").exists()


def test_prepare_run_opencode_stub_untouched(tmp_path):
    """无 engine 属性的旧 stub（默认 opencode）仍走 opencode 分支。"""
    class _Old:
        def __init__(self, shared_dir):
            self.shared_dir = shared_dir
            self.xdg_config_home = ""
            self.relay_exclude_providers = []

    sh = tmp_path / "shared"
    (sh / "memory").mkdir(parents=True)
    (sh / "memory" / "m.md").write_text("mem")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    xdg = prepare_run(str(workdir), _Old(str(sh)))
    assert os.path.isfile(os.path.join(xdg, "opencode", "opencode.json"))
    assert (workdir / "AGENTS.md").exists()


# ---- 会话同引擎守卫 ----

def test_session_if_same_engine():
    row = {"session_id": "s1", "engine": "claude"}
    assert _session_if_same_engine(row, "claude") == "s1"
    assert _session_if_same_engine(row, "opencode") is None
    legacy = {"session_id": "s2", "engine": ""}
    assert _session_if_same_engine(legacy, "opencode") == "s2"
    assert _session_if_same_engine(legacy, "claude") is None
    assert _session_if_same_engine({"session_id": "", "engine": "claude"}, "claude") is None
    assert _session_if_same_engine(None, "claude") is None


# ---- db engine 列 ----

def test_db_engine_column(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    tid = db.create_task(user="u", description="d")
    assert db.task(tid)["engine"] == ""
    db.set(tid, engine="claude", session_id="s1")
    row = db.task(tid)
    assert row["engine"] == "claude" and row["session_id"] == "s1"


# ---- e2e：假 claude agent 全流程（拦截→审批→续跑） ----

FAKE_CLAUDE = """#!/bin/sh
# 假 claude：首跑发出命中高危模式的 tool_use 后被杀；续跑（prompt 含“人工已批准”）直接 done。
if printf '%s' "$*" | grep -q "人工已批准"; then
  printf '%s\\n' '{"type":"system","subtype":"init","session_id":"ses_claude_fake"}'
  printf '%s\\n' '{"type":"assistant","session_id":"ses_claude_fake","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}'
  printf '%s\\n' '{"type":"result","subtype":"success","session_id":"ses_claude_fake","result":"done","total_cost_usd":0.002,"usage":{"input_tokens":10,"output_tokens":5}}'
  exit 0
fi
printf '%s\\n' '{"type":"system","subtype":"init","session_id":"ses_claude_fake"}'
printf '%s\\n' '{"type":"assistant","session_id":"ses_claude_fake","message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"rm -rf /root"}}]}}'
sleep 20
"""


def _wait_pending(base: str, tok: str, tid: int, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{base}/tasks/{tid}", headers={"Authorization": f"Bearer {tok}"},
                      timeout=10)
        t = r.json()
        if t["status"] == "pending_approval":
            return t
        if t["status"] in ("done", "review", "conflict", "failed", "cancelled"):
            return t
        time.sleep(0.5)
    raise TimeoutError(f"task {tid} 未进入 pending_approval")


def test_claude_engine_e2e_blocked_approve_resume(relay_factory, tmp_path):
    script = tmp_path / "fake_claude.sh"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    r = relay_factory(engine="claude", agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "claudeuser"}).json()["token"]

    assert admin_c.get("/health").json()["engine"] == "claude"

    tid = submit(base, tok, "删除某个临时目录")["task_id"]
    t = _wait_pending(base, tok, tid, timeout=55)
    assert t["status"] == "pending_approval", t
    assert t["blocked_cmd"] == "rm -rf /root", t
    # 首跑（被杀前）也应已落 session 与 engine
    assert t["session_id"] == "ses_claude_fake", t
    assert t["engine"] == "claude", t

    r2 = admin_c.post(f"/tasks/{tid}/approve", json={"decision": "approve"})
    assert r2.status_code == 200, r2.text
    t = wait_task(base, tok, tid, timeout=60)
    assert t["status"] in ("review", "done"), t
    assert t["session_id"] == "ses_claude_fake"
    assert t["engine"] == "claude"
    assert t["tokens"] == 15, "result.usage 应计入 tokens"
    assert t["result"] == "done", "result 事件应替换而非追加流式 text"
    assert t["resume_hint"] == ""

    # claude 状态目录持久化在 users/<user>/.claude（不随任务 workdir 清理）
    state = os.path.join(r["cfg"].users_root, "claudeuser", ".claude")
    assert os.path.isdir(state)


def test_claude_engine_e2e_plain_task(relay_factory, tmp_path):
    """无拦截路径：直接跑完，engine 落库。"""
    script = tmp_path / "fake_claude2.sh"
    script.write_text("""#!/bin/sh
printf '%s\\n' '{"type":"system","subtype":"init","session_id":"ses_c2"}'
printf '%s\\n' '{"type":"result","subtype":"success","session_id":"ses_c2","result":"all good","total_cost_usd":0,"usage":{"input_tokens":1,"output_tokens":2}}'
exit 0
""")
    script.chmod(0o755)
    r = relay_factory(engine="claude", agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "c2"}).json()["token"]
    tid = submit(base, tok, "随便干点什么")["task_id"]
    t = wait_task(base, tok, tid, timeout=60)
    assert t["status"] in ("review", "done"), t
    assert t["result"] == "all good" and t["engine"] == "claude" and t["tokens"] == 3


def test_claude_engine_e2e_oversized_stream_line(relay_factory, tmp_path):
    """单行 stream-json 远超 asyncio 默认 64KB 缓冲时不得崩任务。

    回归守护：executor 未给 create_subprocess_exec 传 limit 时，readline() 抛
    ValueError('Separator is found, but chunk is longer than limit')，任务以
    internal error 失败（生产实测任务 #195）。大工具结果/长文本块/内联图片都会触发。
    """
    script = tmp_path / "fake_claude_big.sh"
    script.write_text("""#!/bin/sh
printf '%s\\n' '{"type":"system","subtype":"init","session_id":"ses_big"}'
BIG=$(head -c 200000 /dev/zero | tr '\\0' 'x')
printf '{"type":"result","subtype":"success","session_id":"ses_big","result":"%s","total_cost_usd":0,"usage":{"input_tokens":1,"output_tokens":2}}\\n' "$BIG"
exit 0
""")
    script.chmod(0o755)
    r = relay_factory(engine="claude", agent_bin=str(script), task_timeout=60)
    base = r["base"]
    admin_c = httpx.Client(base_url=base, headers={"Authorization": "Bearer admin-test"},
                           timeout=30)
    tok = admin_c.post("/users", json={"name": "big"}).json()["token"]
    tid = submit(base, tok, "读一个很大的东西")["task_id"]
    t = wait_task(base, tok, tid, timeout=60)
    assert "Separator" not in str(t.get("error") or ""), t
    assert t["status"] in ("review", "done"), t
    assert str(t.get("result") or "").startswith("x" * 1000)


# ---- resume 前剥离历史图片（2026-10-23）----
#
# --resume 把整段历史重放给模型，图片块由 claude-proxy 转成
# image_url(data:<mime>;base64,...) 原样上送。实测宋连杰一个 181s 录屏经
# 378→380→382→384→386 五任务共用一个 session，累积 15 个图片块 / 6.11MB
# base64、转写 12.66MB，每回合整体重算 prefill，一句话追问（#382）跑满 134s。

_B64 = "A" * 5000


def _img_line(sid: str = "ses1", b64: str = _B64) -> str:
    """照生产转写的真实结构造一行含图 tool_result（message.content + toolUseResult 双份）。"""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{
            "tool_use_id": "chatcmpl-tool-1", "type": "tool_result",
            "content": [{"type": "image", "source": {
                "type": "base64", "data": b64, "media_type": "image/jpeg"}}]}]},
        "toolUseResult": {"type": "image", "file": {
            "base64": b64, "type": "image/jpeg", "originalSize": 724106,
            "dimensions": {"originalWidth": 2100, "originalHeight": 2349}}},
        "sessionId": sid,
    }, ensure_ascii=False)


def _write_transcript(tmp_path, sid: str, lines: list[str], user: str = "u1"):
    d = tmp_path / "users" / user / ".claude" / "projects" / "-mnt-vol-x-workspace-tasks-378"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    return str(p)


def test_strip_transcript_images_replaces_payload(tmp_path):
    plain = json.dumps({"type": "assistant", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "看到了"}]}})
    p = _write_transcript(tmp_path, "ses1", [plain, _img_line(), plain])
    before = os.path.getsize(p)
    blocks, saved = strip_transcript_images(p)
    assert blocks == 2 and saved > 0                   # message.content + toolUseResult
    body = open(p, encoding="utf-8").read()
    assert _B64 not in body                            # base64 载荷已清干净
    assert len(body.splitlines()) == 3                 # 行数不变
    assert os.path.getsize(p) < before
    objs = [json.loads(x) for x in body.splitlines()]
    blk = objs[1]["message"]["content"][0]["content"][0]
    assert blk["type"] == "text"                       # 换成文本占位，结构仍合法
    assert "2100x2349" in blk["text"] and "image/jpeg" in blk["text"]
    assert "724KB" in blk["text"]                      # 元信息保留，模型知道曾有图
    assert objs[1]["toolUseResult"]["file"]["base64"] == "<stripped>"
    assert objs[1]["toolUseResult"]["file"]["originalSize"] == 724106
    assert objs[0] == json.loads(plain)                # 无图行原样保留，不重排 JSON


def test_strip_transcript_images_noop_without_images(tmp_path):
    plain = json.dumps({"type": "assistant", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "hi"}]}})
    p = _write_transcript(tmp_path, "ses1", [plain, plain])
    raw = open(p, encoding="utf-8").read()
    assert strip_transcript_images(p) == (0, 0)
    assert open(p, encoding="utf-8").read() == raw     # 零改动
    assert not os.path.exists(p + ".bak-imgstrip")     # 无图不产生备份


def test_strip_transcript_images_tolerates_bad_line(tmp_path):
    bad = '{"type":"user","message":<<< 不是合法 JSON'
    p = _write_transcript(tmp_path, "ses1", [bad, _img_line()])
    blocks, _ = strip_transcript_images(p)
    assert blocks == 2
    lines = open(p, encoding="utf-8").read().splitlines()
    assert lines[0] == bad                             # 坏行原样保留，绝不弄坏转写
    assert _B64 not in lines[1]


def test_strip_transcript_images_backup_written_once(tmp_path):
    """滚动备份只留一份：首次剥离时落盘，之后同名覆盖，不随轮次无限堆积。"""
    p = _write_transcript(tmp_path, "ses1", [_img_line()])
    strip_transcript_images(p)
    bak = p + ".bak-imgstrip"
    assert os.path.exists(bak) and _B64 in open(bak, encoding="utf-8").read()
    first = os.path.getsize(bak)
    strip_transcript_images(p)                         # 已无图 → 不再改写
    assert os.path.getsize(bak) == first


def test_strip_transcript_images_missing_file(tmp_path):
    assert strip_transcript_images(str(tmp_path / "nope.jsonl")) == (0, 0)


def test_find_transcript_globs_by_session_id(tmp_path):
    """按 session_id glob，不自己拼 projects 子目录名。

    那个目录名是 claude-code 按它看到的 cwd 编码的，可能是符号链接解析后的真实
    路径（本机 /mnt/vol-eltaah12 → /mnt/vol-71srnryd），拼错就静默失效。
    """
    p = _write_transcript(tmp_path, "ses-abc", [_img_line("ses-abc")])
    state = str(tmp_path / "users" / "u1" / ".claude")
    assert _find_transcript(state, "ses-abc") == p
    assert _find_transcript(state, "ses-missing") is None
    assert _find_transcript(state, "") is None
    assert _find_transcript(str(tmp_path / "no-such-dir"), "ses-abc") is None
    # 防路径穿越：session_id 会拼进 glob 模式，含分隔符/上跳的一律不认
    assert _find_transcript(state, "../../etc/passwd") is None
    assert _find_transcript(state, "../ses-abc") is None


def test_executor_strip_resume_images_end_to_end(tmp_path):
    """Executor._strip_resume_images：按 users_root + user 定位并剥离，事件上报体量。"""
    p = _write_transcript(tmp_path, "ses-e2e", [_img_line("ses-e2e")], user="宋连杰")
    ex = Executor(_cfg(tmp_path))
    ev = []
    ex._strip_resume_images("ses-e2e", "宋连杰", 384, lambda tid, **kw: ev.append((tid, kw)))
    assert _B64 not in open(p, encoding="utf-8").read()
    assert ev and ev[0][0] == 384
    assert ev[0][1]["t"] == "imgstrip" and ev[0][1]["blocks"] == 2
    assert ev[0][1]["saved_kb"] > 0


def test_executor_strip_resume_images_never_raises(tmp_path):
    """剥离只是优化：定位不到 / 权限炸了都必须静默放过，绝不能让任务跑不起来。"""
    ex = Executor(_cfg(tmp_path))
    ev = []
    ex._strip_resume_images("ses-gone", "u1", 1, lambda tid, **kw: ev.append(tid))
    assert ev == []
    ex._strip_resume_images("../../etc/passwd", "u1", 1, lambda tid, **kw: ev.append(tid))
    assert ev == []


# ---- 管道切碎护栏（2026-10-25；事故见知识库 #8105 补记）----

def _assistant_ev(*blocks):
    return json.dumps({"type": "assistant",
                       "message": {"content": list(blocks)}})


def test_parse_claude_counts_unparsed_tool_input():
    """__unparsedToolInput 签名（管道切碎的铁证）必须被计数。"""
    out = _parse_claude(_assistant_ev(
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"__unparsedToolInput": {"raw": '{"command": "sto', "len": 27}}},
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "ls"}},
    ))
    assert out["tool_use_n"] == 2
    assert out["tool_verr_n"] == 1


def test_parse_claude_clean_stream_has_no_verr():
    out = _parse_claude(_assistant_ev(
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}},
    ))
    assert out["tool_use_n"] == 1
    assert "tool_verr_n" not in out


def test_pipeline_shred_suspect_thresholds(tmp_path):
    from app.executor import AgentResult, pipeline_shred_suspect
    cfg = _cfg(tmp_path)
    # 事故形态：占比高、次数多 → 触发
    note = pipeline_shred_suspect(AgentResult(tool_calls=10, tool_verrs=8), cfg)
    assert "平台管道缺陷" in note and "8/10" in note
    # 小样本不触发（min_count=5：4 次全拒也不定罪管道）
    assert pipeline_shred_suspect(AgentResult(tool_calls=4, tool_verrs=4), cfg) == ""
    # 占比不足不触发（100 次里 6 次异常：记审计即可，不够定罪）
    assert pipeline_shred_suspect(AgentResult(tool_calls=100, tool_verrs=6), cfg) == ""
    # 基线：零签名
    assert pipeline_shred_suspect(AgentResult(tool_calls=50, tool_verrs=0), cfg) == ""


def test_pipeline_shred_suspect_custom_thresholds(tmp_path):
    from app.executor import AgentResult, pipeline_shred_suspect
    cfg = _cfg(tmp_path)
    cfg.tool_verr_min_count = 2
    cfg.tool_verr_min_ratio = 0.5
    assert pipeline_shred_suspect(AgentResult(tool_calls=4, tool_verrs=2), cfg) != ""
    assert pipeline_shred_suspect(AgentResult(tool_calls=5, tool_verrs=2), cfg) == ""
