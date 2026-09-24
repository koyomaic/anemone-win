"""tests/test_local_sandbox.py —— local 内核沙箱（systemd-run 瞬态 unit）。

为什么 local 必须有内核层：relay-avatar.service **无任何沙箱指令**且以 root 运行，
而 relay.service 有 ProtectSystem=strict + InaccessiblePaths。local 侧一旦没有内核
边界，就只剩 claude 的词法权限规则——实测 7 种删除手法放过 4 种（python3 -c
os.remove / find -delete / 写脚本再 bash / 重定向截断），路径 deny 规则也被
perl/node/dd 绕过且能**写入**"受保护"目录。黑名单要成立必须有「所有访问必经」的
检查点，无中介环境下只有内核 mount namespace 能充当。

覆盖：
① sandbox_spec 仅在 local_sandbox=true 且 rw/hide 均非空时 enabled；
② available() fail-closed：要求沙箱而 systemd-run/systemctl 缺失或规格不完整 →
   local 整体不可用（路由改投 relay），**绝不降级成无沙箱 local**；
③ _sandbox_wrap 的 argv 结构：路径列表必须在**单条** -p 内空格分隔且带 `-` 前缀
   （拆成多条 -p 实测只保留最后一条，隐藏会静默失效）；
④ _claude_env(sandboxed=True)：HOME→.cchome（/root/.claude.json 被遮成空文件会让
   claude 报 Unexpected EOF）、token 换占位符（本机代理不校验入站 token）；
⑤ _run 在「要求沙箱但规格不可用」时不 spawn、返回 sbx_unavailable；
⑥ run_task/run_self 对 sbx_unavailable 升级 relay；run_l3 **不升级**（硬隔离铁律）；
⑦ _kill_unit 用 systemctl kill --kill-who=all（只 proc.kill() 会留孤儿，孤儿还会
   持有输出管道把调用方阻塞到超时）；
⑧ constraints 各 local tier 都带 sbx。
"""
import subprocess

import pytest

from app.exec_local import ExecLocal


def setcfg(cfg, section, **kw):
    cfg._p.setdefault(section, {}).update(kw)


def make_ex(cfg, storage, audit, policy, sbx_bins=True):
    ex = ExecLocal(cfg, storage, audit, policy)
    ex.bin = "/bin/true"                       # 强制 available()，不真跑 claude
    ex.sbx_bin = "/usr/bin/systemd-run" if sbx_bins else ""
    ex.ctl_bin = "/usr/bin/systemctl" if sbx_bins else ""
    return ex


def sbx_ready(cfg, shared, **over):
    """打开 local + 完整沙箱配置，返回 sandbox cwd。"""
    sb = shared / "work"
    sb.mkdir(parents=True, exist_ok=True)
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local")
    kw = dict(local_sandbox=True,
              local_sandbox_rw=[str(sb), "/tmp"],
              local_sandbox_hide=[str(shared / "secrets"), "/root/.ssh"])
    kw.update(over)
    setcfg(cfg, "security", l2_sandbox_dir=str(sb), **kw)
    return sb


SPEC = {"enabled": True, "rw": ["/tmp", "/w"], "hide": ["/root/.ssh"]}


# ── ① sandbox_spec ──

def test_sandbox_spec_needs_flag_and_both_lists(cfg, storage, audit, policy,
                                                shared):
    sbx_ready(cfg, shared)
    spec = policy.sandbox_spec()
    assert spec["enabled"] is True and spec["rw"] and spec["hide"]

    setcfg(cfg, "security", local_sandbox=False)
    assert policy.sandbox_spec()["enabled"] is False

    sbx_ready(cfg, shared, local_sandbox_rw=[])       # 可写白名单空 → 不完整
    assert policy.sandbox_spec()["enabled"] is False

    sbx_ready(cfg, shared, local_sandbox_hide=[])     # 隐藏名单空 → 不完整
    assert policy.sandbox_spec()["enabled"] is False


def test_sandbox_spec_strips_blank_entries(cfg, storage, audit, policy, shared):
    sbx_ready(cfg, shared, local_sandbox_rw=["/tmp", "  ", ""],
              local_sandbox_hide=["/root/.ssh", " "])
    spec = policy.sandbox_spec()
    assert spec["rw"] == ["/tmp"] and spec["hide"] == ["/root/.ssh"]


# ── ② available() fail-closed ──

def test_available_fail_closed_without_systemd_run(cfg, storage, audit, policy,
                                                   shared):
    """要求沙箱而 systemd-run 缺失 → local 不可用（不得裸奔）。"""
    sbx_ready(cfg, shared)
    assert make_ex(cfg, storage, audit, policy, sbx_bins=False).available() is False


def test_available_fail_closed_on_incomplete_spec(cfg, storage, audit, policy,
                                                  shared):
    """要求沙箱而 rw/hide 为空 → 规格不完整 → 不可用。"""
    sbx_ready(cfg, shared, local_sandbox_hide=[])
    ex = make_ex(cfg, storage, audit, policy)
    assert ex.available() is False


def test_available_unchanged_when_sandbox_off(cfg, storage, audit, policy,
                                              shared):
    """未开沙箱时行为完全不变（回归保护）——即使 systemd-run 缺失。"""
    sb = shared / "work"
    sb.mkdir(parents=True, exist_ok=True)
    setcfg(cfg, "execution", local_enabled=True, l2_backend="local")
    setcfg(cfg, "security", l2_sandbox_dir=str(sb), local_sandbox=False)
    assert make_ex(cfg, storage, audit, policy, sbx_bins=False).available() is True


def test_available_true_with_full_sandbox(cfg, storage, audit, policy, shared):
    sbx_ready(cfg, shared)
    assert make_ex(cfg, storage, audit, policy).available() is True


# ── ③ _sandbox_wrap argv 结构 ──

def test_sandbox_wrap_argv_structure(cfg, storage, audit, policy, shared):
    ex = make_ex(cfg, storage, audit, policy)
    argv, unit = ex._sandbox_wrap(["claude", "-p"], "/w",
                                  {"HOME": "/root", "PATH": "/usr/bin"}, SPEC)
    assert argv[0] == "/usr/bin/systemd-run"
    # 必须瞬态 service + --pipe（--scope 不支持沙箱属性）
    assert "--pipe" in argv and "--collect" in argv
    assert "--scope" not in argv
    assert unit.startswith("avatar-local-") and f"--unit={unit}" in argv
    for p in ("ProtectSystem=strict", "NoNewPrivileges=true",
              "CapabilityBoundingSet=", "ProtectKernelTunables=true",
              "ProtectControlGroups=true", "RestrictSUIDSGID=true"):
        assert p in argv, p
    assert "WorkingDirectory=/w" in argv


def test_sandbox_wrap_path_lists_single_p_with_dash(cfg, storage, audit,
                                                    policy, shared):
    """路径列表必须在**单条** -p 内、空格分隔、每条带 `-` 前缀。

    拆成多条 -p 实测只保留最后一条 → 隐藏静默失效（比不隐藏更危险，因为看起来配了）。
    """
    ex = make_ex(cfg, storage, audit, policy)
    argv, _ = ex._sandbox_wrap(["c"], "/w", {}, SPEC)
    rw = [a for a in argv if a.startswith("ReadWritePaths=")]
    hide = [a for a in argv if a.startswith("InaccessiblePaths=")]
    assert len(rw) == 1 and len(hide) == 1
    assert rw[0].split("=", 1)[1].split() == ["-/tmp", "-/w"]
    assert hide[0].split("=", 1)[1].split() == ["-/root/.ssh"]


def test_sandbox_wrap_forwards_only_whitelisted_env(cfg, storage, audit,
                                                    policy, shared):
    """只转发白名单键——全量转发会把 avatar 进程环境写进 unit 属性（systemctl show 可见）。"""
    ex = make_ex(cfg, storage, audit, policy)
    env = {"HOME": "/root", "PATH": "/usr/bin", "RELAY_ADMIN_TOKEN": "secret",
           "AVATAR_INTERNAL": "x"}
    argv, _ = ex._sandbox_wrap(["c"], "/w", env, SPEC)
    assert "--setenv=HOME=/root" in argv
    assert "--setenv=PATH=/usr/bin" in argv
    assert not any("RELAY_ADMIN_TOKEN" in a for a in argv)
    assert not any("AVATAR_INTERNAL" in a for a in argv)


def test_sandbox_wrap_cmd_after_separator(cfg, storage, audit, policy, shared):
    ex = make_ex(cfg, storage, audit, policy)
    argv, _ = ex._sandbox_wrap(["claude", "-p", "--verbose"], "/w", {}, SPEC)
    assert argv[argv.index("--") + 1:] == ["claude", "-p", "--verbose"]


def test_sandbox_wrap_passthrough_when_disabled(cfg, storage, audit, policy,
                                                shared):
    """未启用时原样返回、unit 为空（调用方据此决定 fail-closed）。"""
    ex = make_ex(cfg, storage, audit, policy)
    for bad in (None, {}, {"enabled": False, "rw": ["/tmp"], "hide": ["/x"]}):
        argv, unit = ex._sandbox_wrap(["claude"], "/w", {}, bad)
        assert argv == ["claude"] and unit == ""


def test_sandbox_wrap_unit_names_unique(cfg, storage, audit, policy, shared):
    ex = make_ex(cfg, storage, audit, policy)
    us = {ex._sandbox_wrap(["c"], "/w", {}, SPEC)[1] for _ in range(20)}
    assert len(us) == 20                       # 重名会撞 unit，kill 会误杀


# ── ④ _claude_env ──

def test_claude_env_sandboxed_home_and_placeholder(cfg, storage, audit,
                                                   policy, shared, monkeypatch):
    """HOME 必须指向 .cchome：/root/.claude.json 被遮成**空文件**（非 ENOENT），
    claude 读它会报 JSON Parse error: Unexpected EOF 而拒绝启动。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-upstream-token")
    ex = make_ex(cfg, storage, audit, policy)
    env = ex._claude_env(sandboxed=True)
    assert env["HOME"] == str(ex.cchome)
    assert env["CLAUDE_CONFIG_DIR"] == str(ex.cchome)
    assert env["ANTHROPIC_AUTH_TOKEN"] == ex._SBX_TOKEN_PLACEHOLDER
    assert "real-upstream-token" not in env.values()


def test_claude_env_unsandboxed_keeps_real_token(cfg, storage, audit, policy,
                                                 shared, monkeypatch):
    """未沙箱时行为不变（回归保护）：真 token、不改 HOME。"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-upstream-token")
    monkeypatch.setenv("HOME", "/root")
    ex = make_ex(cfg, storage, audit, policy)
    env = ex._claude_env(sandboxed=False)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "real-upstream-token"
    assert env["HOME"] == "/root"
    assert env["CLAUDE_CONFIG_DIR"] == str(ex.cchome)


# ── ⑤⑥ _run / 各场景入口的 fail-closed ──

def test_run_does_not_spawn_when_sandbox_required_but_missing(
        cfg, storage, audit, policy, shared, monkeypatch):
    sbx_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or pytest.fail("不该 spawn"))
    r = ex._run("p", conv_key="k", cwd="/tmp", flags=[], uid="200", sbx=None)
    assert r["sbx_unavailable"] is True and r["text"] == ""
    assert spawned == []


def test_run_task_escalates_on_sandbox_unavailable(cfg, storage, audit, policy,
                                                   shared, monkeypatch):
    sbx_ready(cfg, shared)
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run", lambda prompt, **kw: {
        "text": "", "denied": False, "steps": [], "sbx_unavailable": True})
    assert ex.run_task("200", "干活")["escalate"] is True


def test_run_self_escalates_on_sandbox_unavailable(cfg, storage, audit, policy,
                                                   shared, monkeypatch):
    sbx_ready(cfg, shared)
    setcfg(cfg, "execution", super_backend="local")
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run", lambda prompt, **kw: {
        "text": "", "denied": False, "steps": [], "sbx_unavailable": True})
    assert ex.run_self("18434", "干活")["escalate"] is True


def test_run_l3_never_escalates_on_sandbox_unavailable(cfg, storage, audit,
                                                       policy, shared,
                                                       monkeypatch):
    """L3 硬隔离铁律：越权只拒不升级。沙箱不可用同样不升级——relay 侧没有 L3 的
    sender 数据区隔离语义，升级等于把隔离问题扩散到门禁通路。"""
    sbx_ready(cfg, shared)
    (shared / "data" / "2026" / "2026-09" / "2026-09-25" / "300").mkdir(
        parents=True, exist_ok=True)
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run", lambda prompt, **kw: {
        "text": "", "denied": False, "steps": [], "sbx_unavailable": True})
    r = ex.run_l3("300", "我的文件里写了什么")
    assert r["escalate"] is False
    assert "未就绪" in r["text"]


def test_run_l3_sandbox_unavailable_not_confused_with_denial(
        cfg, storage, audit, policy, shared, monkeypatch):
    """沙箱不可用的文案不得复用「涉及数据区以外」的越权拒答（会误导用户）。"""
    sbx_ready(cfg, shared)
    (shared / "data" / "2026" / "2026-09" / "2026-09-25" / "300").mkdir(
        parents=True, exist_ok=True)
    ex = make_ex(cfg, storage, audit, policy)
    monkeypatch.setattr(ex, "_run", lambda prompt, **kw: {
        "text": "", "denied": False, "steps": [], "sbx_unavailable": True})
    assert "数据区以外" not in ex.run_l3("300", "问")["text"]


# ── ⑦ _kill_unit ──

def test_kill_unit_kills_whole_unit(cfg, storage, audit, policy, shared,
                                    monkeypatch):
    """只 proc.kill() 杀的是 systemd-run **客户端**，unit 内进程会变孤儿（实测
    MainPID 存活、unit 仍 active），孤儿还会持有输出管道把调用方阻塞到超时。"""
    ex = make_ex(cfg, storage, audit, policy)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    ex._kill_unit("avatar-local-1-2")
    assert calls and list(calls[0][0]) == [
        "/usr/bin/systemctl", "kill", "--kill-who=all", "--signal=SIGKILL",
        "avatar-local-1-2.service"]


def test_kill_unit_noop_without_unit_or_ctl(cfg, storage, audit, policy,
                                            shared, monkeypatch):
    ex = make_ex(cfg, storage, audit, policy)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    ex._kill_unit("")                          # 未沙箱：不该调 systemctl
    assert calls == []
    ex2 = make_ex(cfg, storage, audit, policy, sbx_bins=False)
    ex2.ctl_bin = ""
    ex2._kill_unit("avatar-local-1-1")
    assert calls == []


def test_kill_unit_swallows_errors(cfg, storage, audit, policy, shared,
                                   monkeypatch):
    """清理路径不得抛异常（finally 里调用，抛出会盖掉真实执行结果）。"""
    ex = make_ex(cfg, storage, audit, policy)

    def boom(*a, **k):
        raise OSError("systemctl 炸了")

    monkeypatch.setattr(subprocess, "run", boom)
    ex._kill_unit("avatar-local-1-1")          # 不抛即通过


# ── ⑧ constraints 各 tier 都带 sbx ──

def test_constraints_carry_sbx_for_local_tiers(cfg, storage, audit, policy,
                                               shared):
    sbx_ready(cfg, shared)
    setcfg(cfg, "execution", super_backend="local")
    for uid, tier in (("200", "L2"), ("18434", "super"), ("300", "L3")):
        cons = policy.constraints(uid)
        assert cons["backend"] == "local", uid
        assert cons["sbx"]["enabled"] is True, f"{uid}({tier}) 缺沙箱规格"
        assert cons["sbx"]["hide"], uid


def test_constraints_relay_tiers_have_no_sbx(cfg, storage, audit, policy,
                                             shared):
    """relay 后端不需要 local 沙箱规格（它有自己的 systemd 沙箱）。"""
    sbx_ready(cfg, shared)
    setcfg(cfg, "execution", l2_backend="relay")
    cons = policy.constraints("200")
    assert cons["backend"] == "relay" and "sbx" not in cons


def test_guest_gets_no_sbx(cfg, storage, audit, policy, shared):
    sbx_ready(cfg, shared)
    cons = policy.constraints("555")
    assert cons["backend"] == "none" and "sbx" not in cons
