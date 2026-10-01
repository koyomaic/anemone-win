"""tests/test_migrate.py —— im-bridge → relay-avatar 迁移（幂等）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import migrate as M


def make_old_bridge(ws: Path):
    old = ws / "im-bridge"
    (old / "rounds").mkdir(parents=True)
    (old / "bridge.env").write_text(
        "RELAY_BASE=http://127.0.0.1:8787\n"
        "RELAY_DEVICE_ID=dev-1\nRELAY_DEVICE_SECRET=sec-1\n", encoding="utf-8")
    (old / "config.json").write_text(json.dumps({
        "personal": {"mode": "whitelist", "whitelist": ["200", "300"],
                     "blacklist": ["999"]},
        "self_open_id": "OLDSELFID",
        "debounce_sec": 25}), encoding="utf-8")
    (old / "state.json").write_text(json.dumps(
        {"sessions": {"o:x": {"state": "IDLE"}}, "seen": ["e1"]}),
        encoding="utf-8")
    (old / "rounds" / "7.jsonl").write_text('{"kind":"submit"}\n',
                                            encoding="utf-8")
    (old / "events.ndjson").write_text('{"a":1}\n', encoding="utf-8")
    return old


def test_migrate_full(tmp_path):
    ws = tmp_path / "ws"
    old = make_old_bridge(ws)
    r = M.migrate(ws)
    state = ws / "avatar"
    env = ws / "shared" / "secrets" / "relay-avatar.env"
    # env：凭据迁移 + 看板 token 生成 + 600
    text = env.read_text(encoding="utf-8")
    assert "RELAY_DEVICE_ID=dev-1" in text
    assert "AVATAR_DASHBOARD_TOKEN=" in text
    assert len(text.split("AVATAR_DASHBOARD_TOKEN=")[1].splitlines()[0]) == 32
    assert (env.stat().st_mode & 0o777) == 0o600
    # policy：名单与 self_open_id 注入
    pol = (state / "policy.toml").read_text(encoding="utf-8")
    assert 'whitelist = ["200", "300"]' in pol
    assert 'blacklist = ["999"]' in pol
    assert 'self_open_id = "OLDSELFID"' in pol
    # 运行态
    assert json.loads((state / "state.json").read_text())["seen"] == ["e1"]
    assert (state / "rounds" / "7.jsonl").exists()
    assert (state / "events.ndjson").exists()
    assert (state / "run" / "integrity-manifest.json").exists()
    # 旧目录只读不删
    assert (old / "bridge.env").exists()
    assert r["steps"]


def test_migrate_idempotent(tmp_path):
    ws = tmp_path / "ws"
    make_old_bridge(ws)
    M.migrate(ws)
    env = ws / "shared" / "secrets" / "relay-avatar.env"
    tok1 = env.read_text(encoding="utf-8")
    (ws / "avatar" / "policy.toml").write_text("# 本机演进\n", encoding="utf-8")
    M.migrate(ws)
    assert env.read_text(encoding="utf-8") == tok1        # env 不覆盖
    assert (ws / "avatar" / "policy.toml").read_text(
        encoding="utf-8") == "# 本机演进\n"                # policy 不覆盖


def test_migrate_fresh_machine(tmp_path):
    """无 im-bridge 历史的新机器：也能生成 env+policy 骨架。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    M.migrate(ws)
    assert (ws / "shared" / "secrets" / "relay-avatar.env").exists()
    assert (ws / "avatar" / "policy.toml").exists()


# ── 2b：沙箱出网白名单置空（2026-10-01 用户指示：出网不限制，机队统一）──

_OLD_NET = """[security]
local_sandbox = true
local_sandbox_rw = ["/w"]
local_sandbox_hide = ["/root"]
# 沙箱网络白名单（IP/CIDR）。非空即施加 IPAddressDeny=any + IPAddressAllow=本列表。
# 刻意**不给**公网：给公网等于把隐藏名单的残余风险变成可外泄。留空＝不施加 IP 规则
# （**不要**留空，那等于放开全部外发）。
local_sandbox_net_allow = [
  "127.0.0.1",
  "10.189.21.235",
]
profile_enforce = "strict"
"""


def _ws_with_policy(tmp_path, text):
    ws = tmp_path / "ws"
    (ws / "avatar").mkdir(parents=True)
    (ws / "avatar" / "policy.toml").write_text(text, encoding="utf-8")
    return ws


def test_migrate_empties_net_allow(tmp_path):
    """非空白名单被置空，且旧注释块一并换掉（否则注释与 [] 自相矛盾）。"""
    ws = _ws_with_policy(tmp_path, _OLD_NET)
    pol = ws / "avatar" / "policy.toml"
    r = M.migrate(ws)
    text = pol.read_text(encoding="utf-8")
    assert "local_sandbox_net_allow = []" in text
    # 原白名单项不再作为**列表值**存在（注释里"如需收紧：填回 10.189.21.235"是
    # 合法指引，故断言带引号的 TOML 字符串形态，而非裸 IP 全文不出现）
    assert '"10.189.21.235"' not in text
    assert '"127.0.0.1"' not in text
    assert "不要" not in text                    # 旧的矛盾注释已替换
    assert "2026-10-01" in text                 # 留下决策日期与依据
    assert any("置空" in s for s in r["steps"])
    # 首次改动前必须留原件备份
    assert (ws / "avatar" / "policy.toml.bak-netopen").exists()
    assert "10.189.21.235" in (ws / "avatar" / "policy.toml.bak-netopen"
                               ).read_text(encoding="utf-8")


def test_migrate_net_allow_preserves_other_keys(tmp_path):
    """只动 net_allow 一个键：TOML 仍可解析，沙箱开关与隐藏名单不得被波及。"""
    import tomllib
    ws = _ws_with_policy(tmp_path, _OLD_NET)
    pol = ws / "avatar" / "policy.toml"
    before = tomllib.loads(_OLD_NET)
    M.migrate(ws)
    after = tomllib.loads(pol.read_text(encoding="utf-8"))
    assert after["security"]["local_sandbox_net_allow"] == []
    assert after["security"]["local_sandbox"] is True
    assert after["security"]["local_sandbox_hide"] == ["/root"]
    assert after["security"]["local_sandbox_rw"] == ["/w"]
    assert after["security"]["profile_enforce"] == "strict"
    # 全文差异面必须恰好只有这一个键
    diff = [k for k in before["security"]
            if before["security"][k] != after["security"].get(k)]
    assert diff == ["local_sandbox_net_allow"]


def test_migrate_net_allow_idempotent(tmp_path):
    """重复执行不再写盘：内容不变、备份不被覆盖成已置空的版本。"""
    ws = _ws_with_policy(tmp_path, _OLD_NET)
    pol = ws / "avatar" / "policy.toml"
    M.migrate(ws)
    first = pol.read_text(encoding="utf-8")
    mtime = pol.stat().st_mtime_ns
    r2 = M.migrate(ws)
    assert pol.read_text(encoding="utf-8") == first
    assert pol.stat().st_mtime_ns == mtime          # 未写盘
    assert any("已为空" in s for s in r2["steps"])
    # 备份仍是最初那份（含原白名单），没被第二次执行覆盖
    assert "10.189.21.235" in (ws / "avatar" / "policy.toml.bak-netopen"
                               ).read_text(encoding="utf-8")


def test_migrate_net_allow_absent_key_untouched(tmp_path):
    """没有该键的 policy.toml（如各机自行演进过的）不得被改写。"""
    body = "[security]\nlocal_sandbox = true\n# 本机自有说明\n"
    ws = _ws_with_policy(tmp_path, body)
    pol = ws / "avatar" / "policy.toml"
    r = M.migrate(ws)
    assert pol.read_text(encoding="utf-8") == body
    assert not (ws / "avatar" / "policy.toml.bak-netopen").exists()
    assert any("无 local_sandbox_net_allow 键" in s for s in r["steps"])


def test_migrate_net_allow_inline_form(tmp_path):
    """单行内联写法 local_sandbox_net_allow = ["a"] 同样能被置空。"""
    body = ('[security]\nlocal_sandbox_net_allow = ["127.0.0.1", "1.2.3.4"]\n'
            "other = 1\n")
    ws = _ws_with_policy(tmp_path, body)
    pol = ws / "avatar" / "policy.toml"
    M.migrate(ws)
    text = pol.read_text(encoding="utf-8")
    assert "local_sandbox_net_allow = []" in text
    assert "1.2.3.4" not in text
    assert "other = 1" in text                      # 后续键未受损


def test_migrate_net_allow_dry_run_no_write(tmp_path):
    """--dry 只报告不落盘。"""
    ws = _ws_with_policy(tmp_path, _OLD_NET)
    pol = ws / "avatar" / "policy.toml"
    r = M.migrate(ws, dry=True)
    assert pol.read_text(encoding="utf-8") == _OLD_NET
    assert not (ws / "avatar" / "policy.toml.bak-netopen").exists()
    assert any("置空" in s for s in r["steps"])      # 但仍如实报告将做什么

