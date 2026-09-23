"""tests/test_storage.py —— 统一存储规则 + store 台账集成 + 暂存补登记。"""
import datetime as dt
import json

from app.storage import Storage, sanitize_topic, safe_sender


def test_sanitize():
    assert sanitize_topic("门店 日销#1") == "门店-日销1"
    assert sanitize_topic("  ") == "未命名"
    assert sanitize_topic("a%b&c d") == "abc-d"
    assert safe_sender("../../etc") == "etc"
    assert safe_sender("20102") == "20102"
    assert safe_sender("") == "unknown"


def test_data_dir_layout(storage, shared):
    d = storage.data_dir("20102", when=dt.date(2026, 9, 23))
    assert d == shared / "data" / "2026" / "2026-09" / "2026-09-23" / "20102"


def test_data_stem_naming(storage):
    stem = storage.data_stem("20102", "能源局 座谈会", task_id=0,
                             when=dt.date(2026, 9, 21))
    assert stem == "20260921_20102_T0_能源局-座谈会"
    stem = storage.data_stem("20102", "x", task_id=67,
                             when=dt.date(2026, 9, 21))
    assert "_T67_" in stem


def test_next_version_no_overwrite(storage, tmp_path):
    d = tmp_path / "v"
    p1 = storage.next_version(d, "20260921_20102_T0_x", ".md")
    assert p1.name == "20260921_20102_T0_x_v1.md"
    p1.write_text("a")
    p2 = storage.next_version(d, "20260921_20102_T0_x", ".md")
    assert p2.name == "20260921_20102_T0_x_v2.md"
    p2.write_text("b")
    p3 = storage.next_version(d, "20260921_20102_T0_x", ".md")
    assert p3.name == "20260921_20102_T0_x_v3.md"


def test_report_dir(storage, shared):
    d = storage.report_dir("经营汇报", when=dt.date(2026, 9, 23))
    assert d == shared / "report" / "经营汇报" / "2026-09"


def test_atomic_write(storage, tmp_path):
    p = tmp_path / "a" / "b.json"
    storage.save_json(p, {"k": "值"})
    assert json.loads(p.read_text("utf-8")) == {"k": "值"}
    assert not (tmp_path / "a" / "b.json.tmp").exists()


def test_events_rotation_and_gc(storage):
    storage.append_event('{"a":1}')
    f = storage.events_file()
    assert f.name.startswith("events-") and f.name.endswith(".ndjson")
    assert f.exists()
    import os
    import time
    old = storage.state / "events-20200101.ndjson"
    old.write_text("x")
    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    storage.gc_events(30)
    assert not old.exists()
    assert f.exists()


def test_round_archive(storage):
    storage.archive_round(42, "submit", {"desc": "x"})
    lines = (storage.rounds / "42.jsonl").read_text("utf-8").splitlines()
    rec = json.loads(lines[0])
    assert rec["kind"] == "submit" and rec["data"]["desc"] == "x"


def test_store_unavailable_staging(storage, tmp_path):
    """store CLI 不存在 → ping 失败 → 暂存 staging + pending 队列。"""
    assert not storage.store_ping(use_cache=False)
    src = tmp_path / "up.docx"
    src.write_bytes(b"x" * 10)
    dst = storage.stage_file(src, "20102", "测试 文件", task=0)
    assert dst is not None and dst.exists()
    pend = json.loads(
        (storage.run / "pending_store.json").read_text("utf-8"))
    assert pend[0]["sender"] == "20102"
    assert pend[0]["topic"] == "测试-文件"


def test_retry_pending_registers(storage, tmp_path, monkeypatch):
    """store 恢复后：staging 文件正式落盘 shared/data + store add。"""
    src = tmp_path / "up.md"
    src.write_text("内容")
    storage.stage_file(src, "20102", "会议纪要")
    calls = []
    monkeypatch.setattr(storage, "_store",
                        lambda args, timeout=60: (calls.append(args) or (0, "")))
    storage._store_ok = None
    n = storage.retry_pending()
    assert n == 1
    assert json.loads(
        (storage.run / "pending_store.json").read_text("utf-8")) == []
    added = [c for c in calls if c[0] == "add"]
    assert added and added[0][1].endswith(".md")
    assert "--sender" in added[0] and "20102" in added[0]
    # 正式落盘在 shared/data 下且命名合规
    p = added[0][1]
    assert "/data/" in p and "_20102_T0_会议纪要_v1.md" in p


def test_least_privilege_env(storage, monkeypatch):
    """store 子进程环境：屏蔽 RELAY_*/STORE_DB，STORE_ENV 指向不存在路径。"""
    monkeypatch.setenv("RELAY_DB", "postgresql://x")
    monkeypatch.setenv("STORE_DB", "postgresql://y")
    e = storage._store_env()
    assert "RELAY_DB" not in e and "STORE_DB" not in e
    assert e["STORE_ENV"].endswith(".no-relay-env")
    assert e["STORE_ROOT"] == str(storage.store_root)
