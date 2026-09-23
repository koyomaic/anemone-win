"""tests/test_integrity.py —— 基线 manifest + 漂移检测 + 固化快照。"""
from pathlib import Path

from app import integrity


def make_tree(base: Path):
    (base / "app").mkdir(parents=True, exist_ok=True)
    (base / "app" / "main.py").write_text("print(1)")
    (base / "app" / "policy.py").write_text("x = 1")
    (base / "avatar-cli").write_text("#!/bin/sh")


def test_no_manifest_returns_none(tmp_path, state):
    make_tree(tmp_path / "code")
    state.mkdir(parents=True, exist_ok=True)
    assert integrity.check_drift(tmp_path / "code", state) is None


def test_clean_and_drift(tmp_path, state):
    base = tmp_path / "code"
    make_tree(base)
    state.mkdir(parents=True, exist_ok=True)
    integrity.write_manifest(base, state)
    assert integrity.check_drift(base, state) == {}
    # 手改 → 漂移
    (base / "app" / "main.py").write_text("print(2)")
    drift = integrity.check_drift(base, state)
    assert "app/main.py" in drift
    # 新增文件 → 漂移
    (base / "app" / "extra.py").write_text("y")
    drift = integrity.check_drift(base, state)
    assert "app/extra.py" in drift
    # 删除文件 → 漂移
    (base / "app" / "policy.py").unlink()
    drift = integrity.check_drift(base, state)
    assert drift["app/policy.py"][1] == "(deleted)"[:12]
    # 固化快照 + 签名/摘要
    snap = integrity.save_snapshot(base, state, drift)
    assert snap.is_dir() and (snap / "app" / "main.py").exists()
    assert len(integrity.drift_signature(drift)) == 16
    assert "main.py" in integrity.drift_summary(drift)
