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
