"""tests/test_monitor.py —— 心跳/快照结构/告警跳变逻辑（dws mock）。

hermetic：屏蔽真机 systemctl/relay 探活（机群门禁不受宿主机服务状态抖动影响），
快照里 relay/timer 恒 ok，变量只剩心跳/入站/资源。
"""
import json
import time

import pytest

import app.monitor as M
from app.monitor import Monitor


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(M, "_http_version",
                        lambda url, timeout=5: (True, "testver"))
    monkeypatch.setattr(M, "_run", lambda cmd, timeout=10: (0, "active"))
    # 资源恒定安全区，避免宿主磁盘/内存水位引入额外 issue
    monkeypatch.setattr(M, "_mem_pct", lambda: 42)
    import shutil as _sh
    monkeypatch.setattr(_sh, "disk_usage",
                        lambda p: _sh._ntuple_diskusage(100, 42, 58))


class FakeDws:
    def __init__(self):
        self.sent = []

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}


def make_monitor(cfg, storage, audit):
    dws = FakeDws()
    return Monitor(cfg, storage, dws, audit), dws


def test_heartbeat_file(cfg, storage, audit):
    mon, _ = make_monitor(cfg, storage, audit)
    hb = storage.run / "heartbeat"
    hb.write_text(str(int(time.time())))
    snap = mon.build_snapshot()
    svc = {s["name"]: s for s in snap["services"]}
    assert svc["avatar-heartbeat"]["status"] == "ok"


def test_snapshot_structure(cfg, storage, audit):
    mon, _ = make_monitor(cfg, storage, audit)
    snap = mon.build_snapshot()
    assert snap["host"] and snap["overall"] in ("ok", "warn", "critical")
    names = {s["name"] for s in snap["services"]}
    assert {"relay", "avatar-heartbeat", "inbound",
            "relay-boot.timer"} <= names
    assert "disk_used_pct" in snap["resources"]
    p = mon.persist(snap)
    assert p.exists()
    assert json.loads(p.read_text("utf-8"))["host"] == snap["host"]


def test_inbound_bad_credentials_is_critical(cfg, storage, audit):
    mon, _ = make_monitor(cfg, storage, audit)
    (storage.run / "inbound_health.json").write_text(json.dumps(
        {"ts": int(time.time()), "state": "bad_credentials",
         "detail": "load access token failed"}), encoding="utf-8")
    snap = mon.build_snapshot()
    assert snap["overall"] == "critical"
    assert any(i["target"] == "inbound" for i in snap["issues"])


def test_alarm_only_on_transition(cfg, storage, audit):
    mon, dws = make_monitor(cfg, storage, audit)
    (storage.run / "heartbeat").write_text(str(int(time.time() - 9999)))
    mon.alarm()   # 心跳过期 → critical → 首报
    n1 = len(dws.sent)
    assert n1 >= 1 and "🔴" in dws.sent[0][1]
    mon.alarm()   # 持续异常不刷屏
    assert len(dws.sent) == n1
    # 恢复 → 报绿
    (storage.run / "heartbeat").write_text(str(int(time.time())))
    mon.alarm()
    assert any("🟢" in c for _, c in dws.sent[n1:])


def test_alarm_repeat_after_hours(cfg, storage, audit):
    mon, dws = make_monitor(cfg, storage, audit)
    (storage.run / "heartbeat").write_text(str(int(time.time() - 9999)))
    mon.alarm()
    n1 = len(dws.sent)
    sf = mon.mon_dir / "notify_state.json"
    st = json.loads(sf.read_text("utf-8"))
    for k in st["active_issues"]:
        st["active_issues"][k] = time.time() - 7 * 3600   # 拨回 7h 前
    sf.write_text(json.dumps(st), encoding="utf-8")
    mon.alarm()
    assert len(dws.sent) > n1   # 超过 6h 重提
