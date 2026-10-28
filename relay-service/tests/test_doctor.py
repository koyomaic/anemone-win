"""boot/doctor_checks.py 判级器密封单测——纯函数，无网络无子进程，进夜间门禁。

采集器（check_*）带副作用不进这里；判级器（eval_*/apply_acks/render_*）是
日报的唯一真相源，全部锁死行为。doctor_checks 模块加载只读 env 常量，安全。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "doctor_checks", _ROOT / "boot" / "doctor_checks.py")
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)  # 模块级仅解析 DOCTOR_* 环境变量，无副作用


# ── mk / overall ──

def test_mk_truncates_detail():
    c = D.mk("x", "biz", "ok", "长" * 500)
    assert len(c["detail"]) == 400


def test_overall_priority():
    ok = D.mk("a", "biz", "ok", "")
    warn = D.mk("b", "biz", "warn", "")
    crit = D.mk("c", "biz", "crit", "")
    info = D.mk("d", "biz", "info", "")
    assert D.overall([ok, info]) == "ok"
    assert D.overall([ok, warn]) == "warn"
    assert D.overall([warn, crit]) == "crit"


# ── eval_disk ──

def test_disk_ok_warn_crit():
    assert D.eval_disk([{"mount": "/", "free_pct": 50, "inode_free_pct": 50}])["status"] == "ok"
    assert D.eval_disk([{"mount": "/", "free_pct": 10, "inode_free_pct": 50}])["status"] == "warn"
    assert D.eval_disk([{"mount": "/", "free_pct": 5, "inode_free_pct": 50}])["status"] == "crit"
    assert D.eval_disk([])["status"] == "warn"


def test_disk_inode_zero_is_crit_not_ok():
    # 回归：inode_free_pct=0.0 是假值，曾被 `or 100` 兜成 100% 漏报
    c = D.eval_disk([{"mount": "/", "free_pct": 50, "inode_free_pct": 0.0}])
    assert c["status"] == "crit"
    assert "inode余0%" in c["detail"]


def test_disk_inode_none_treated_full():
    c = D.eval_disk([{"mount": "/", "free_pct": 50, "inode_free_pct": None}])
    assert c["status"] == "ok"


def test_disk_min_free_metric_reports_worst():
    c = D.eval_disk([{"mount": "/a", "free_pct": 40, "inode_free_pct": 90},
                    {"mount": "/b", "free_pct": 12, "inode_free_pct": 90}])
    assert c["status"] == "warn"
    assert c["metrics"]["min_free_pct"] == 12
    assert c["detail"].startswith("/b")  # 最紧的挂载点排前面


# ── eval_forecast ──

_NOW = 1_800_000_000.0
_D = 86400.0


def test_forecast_needs_three_samples():
    c = D.eval_forecast({"/": [[_NOW - _D, 70], [_NOW, 75]]}, now=_NOW)
    assert c["status"] == "info"


def test_forecast_warn_and_crit_bands():
    # 斜率≈1.4%/日、当前 80% → ≈10.7 天到 95% → WARN（<14d）
    warn = D.eval_forecast({"/": [[_NOW - 7 * _D, 70], [_NOW - 4 * _D, 76], [_NOW, 80]]},
                           now=_NOW)
    assert warn["status"] == "warn"
    assert 5 <= warn["metrics"]["days"] < 14
    # 斜率≈4.2%/日、当前 90% → ≈1.2 天到 95% → CRIT（<5d）
    crit = D.eval_forecast({"/": [[_NOW - 7 * _D, 60], [_NOW - 4 * _D, 80], [_NOW, 90]]},
                           now=_NOW)
    assert crit["status"] == "crit"


def test_forecast_flat_and_already_full():
    flat = D.eval_forecast({"/": [[_NOW - 7 * _D, 50], [_NOW - 4 * _D, 50], [_NOW, 50]]},
                           now=_NOW)
    assert flat["status"] == "ok" and flat["detail"] == "增长平稳"
    full = D.eval_forecast({"/": [[_NOW - 7 * _D, 80], [_NOW - 4 * _D, 90], [_NOW, 96]]},
                           now=_NOW)
    assert full["status"] == "ok"  # 已满由 disk_space 直接点名，外推不重复


def test_forecast_ignores_stale_samples():
    old = D.eval_forecast({"/": [[_NOW - 30 * _D, 10], [_NOW - 20 * _D, 40],
                                 [_NOW - 10 * _D, 70]]}, now=_NOW)
    assert old["status"] == "info"  # 全部超 8 天窗口 → 样本不足


# ── eval_tasks / queue / approval ──

def test_tasks_thresholds_and_timeout_count():
    assert D.eval_tasks([])["status"] == "ok"
    assert D.eval_tasks([{"id": 1, "user": "u", "err": "boom"}])["status"] == "warn"
    three = [{"id": i, "user": "u", "err": "x"} for i in (2, 3, 4)]
    c = D.eval_tasks(three)
    assert c["status"] == "crit" and c["metrics"]["failed"] == 3
    t = D.eval_tasks([{"id": 5, "user": "u", "err": "task timed out after 1800s"}])
    assert t["metrics"]["timeout"] == 1


def test_tasks_clusters_by_error_prefix():
    fails = [{"id": i, "user": "u", "err": "same error text"} for i in (1, 2, 3)]
    c = D.eval_tasks(fails)
    assert "same error text×3" in c["detail"]


def test_queue_backlog():
    assert D.eval_queue(0, 0, 0, 8)["status"] == "ok"
    assert D.eval_queue(6, 10, 0, 8)["status"] == "warn"        # 积压量
    assert D.eval_queue(1, 700, 0, 8)["status"] == "warn"       # 单条滞留
    assert D.eval_queue(1, 300, 0, 8)["status"] == "ok"


def test_approval_stuck():
    assert D.eval_approval([])["status"] == "ok"
    c = D.eval_approval([{"id": 7, "age_s": 1200}])
    assert c["status"] == "warn" and "7" in c["detail"] and "20min" in c["detail"]


# ── eval_backup ──

def test_backup_states():
    assert D.eval_backup(None, None, [], 0, False, False)["status"] == "info"
    assert D.eval_backup(None, None, [], 0, False, True)["status"] == "warn"
    assert D.eval_backup(None, None, [], 0, True, True)["status"] == "crit"
    ok = D.eval_backup(1.0, 4_000_000, [], 3, True, True)
    assert ok["status"] == "ok" and ok["metrics"]["age_h"] == 1.0
    assert D.eval_backup(30.0, 4_000_000, [], 3, True, True)["status"] == "crit"


def test_backup_size_anomaly_vs_median():
    med = [10_000_000] * 5
    assert D.eval_backup(2.0, 4_000_000, med, 3, True, True)["status"] == "warn"
    assert D.eval_backup(2.0, 40_000_000, med, 3, True, True)["status"] == "warn"
    assert D.eval_backup(2.0, 12_000_000, med, 3, True, True)["status"] == "ok"


# ── eval_versions / cert / debt / provision / self ──

def test_versions_flow():
    assert D.eval_versions({}, {"opencode": "v1"})["status"] == "info"
    assert D.eval_versions({"opencode": "v1"}, {"opencode": "v1"})["status"] == "ok"
    c = D.eval_versions({"opencode": "v1"}, {"opencode": "v2.2.0"})
    assert c["status"] == "warn" and "v1→v2.2.0" in c["detail"]


def test_cert_bands():
    assert D.eval_cert(None)["status"] == "warn"
    assert D.eval_cert(2)["status"] == "crit"
    assert D.eval_cert(10)["status"] == "warn"
    assert D.eval_cert(100)["status"] == "ok"


def test_debt_tracker():
    assert D.eval_debt({"auth_legacy": False, "max_concurrent": 8})["status"] == "ok"
    assert "auth_legacy" in D.eval_debt({"auth_legacy": True})["detail"]
    c = D.eval_debt({"auth_legacy": False, "max_concurrent": 3}, fleet_mc=8)
    assert c["status"] == "warn" and "3≠" in c["detail"]
    assert D.eval_debt({})["status"] == "ok"  # 字段缺失不误报


def test_provision_recap():
    assert D.eval_provision(50, 3, 1, 3)["status"] == "crit"
    assert D.eval_provision(50, 5, 0, 3)["status"] == "warn"   # WARN 高于基线
    assert D.eval_provision(50, 2, 0, 3)["status"] == "ok"     # 低于基线不报警
    assert D.eval_provision(50, 3, 0, None)["status"] == "ok"  # 无基线只报数


def test_eval_self():
    assert D.eval_self(None, _NOW)["status"] == "info"
    assert D.eval_self(_NOW - 30 * 3600, _NOW)["status"] == "warn"
    assert D.eval_self(_NOW - 2 * 3600, _NOW)["status"] == "ok"


# ── apply_acks ──

def test_ack_downgrades_within_window():
    checks = [D.mk("debt_tracker", "data", "warn", "技术债未清：x")]
    acks = {"debt_tracker": {"until": _NOW + 7 * _D, "reason": "等切并发"}}
    out, acked = D.apply_acks(checks, acks, now=_NOW)
    assert out[0]["status"] == "info" and out[0]["detail"].startswith("🔕")
    assert "原:技术债未清" in out[0]["detail"]
    assert acked == [("debt_tracker", 7)]


def test_ack_expired_or_mismatched_no_effect():
    checks = [D.mk("a", "biz", "warn", "x"), D.mk("b", "biz", "ok", "y")]
    acks = {"a": {"until": _NOW - 1}, "b": {"until": _NOW + _D}}
    out, acked = D.apply_acks(checks, acks, now=_NOW)
    assert out[0]["status"] == "warn" and out[1]["status"] == "ok"
    assert acked == []


# ── 渲染 ──

def _sample_checks():
    return [D.mk("relay_health", "biz", "ok", "healthy"),
            D.mk("task_failed_24h", "biz", "ok", "无失败", failed=0),
            D.mk("queue_backlog", "biz", "ok", "排队 0", queued=0),
            D.mk("disk_space", "res", "ok", "余量足", min_free_pct=68.1),
            D.mk("backup_fresh", "data", "ok", "1h 前", age_h=1.0),
            D.mk("service_recap", "res", "ok", "4/4", n_ok=4, n_total=4),
            D.mk("debt_tracker", "data", "warn", "auth_legacy=on")]


def test_digest_all_green_one_liner():
    checks = _sample_checks()
    for c in checks:
        if c["id"] == "debt_tracker":
            c["status"] = "ok"
    d = D.render_digest(checks, _NOW, 5.0, "/p/2026-10-28.md")
    assert "全绿" in d and "/p/2026-10-28.md" in d and "🟡" not in d


def test_digest_lists_warn_and_crit():
    checks = _sample_checks() + [D.mk("engine_smoke", "dep", "crit", "claude 失败:boom")]
    d = D.render_digest(checks, _NOW, 5.0, "/p/x.md")
    assert "🔴 engine_smoke" in d and "🟡 debt_tracker" in d
    assert "1 WARN / 1 CRIT" in d


def test_digest_missing_metrics_show_dash_not_zero():
    # relay 挂时 task_failed 是 skip：摘要行不能谎报「0 失败」
    checks = [D.mk("relay_health", "biz", "crit", "不可达"),
              D.mk("task_failed_24h", "biz", "skip", "relay 不可达")]
    d = D.render_digest(checks, _NOW, 1.0, "/p/x.md")
    assert "任务 — 失败" in d


def test_digest_capped_at_2200():
    checks = [D.mk(f"w{i}", "biz", "warn", "长" * 300) for i in range(20)]
    d = D.render_digest(checks, _NOW, 1.0, "/p/x.md")
    assert len(d) <= 2200


def test_report_groups_by_category():
    r = D.render_report(_sample_checks(), _NOW, 5.0)
    assert "任务与业务面" in r and "数据与安全" in r
    assert "**debt_tracker**" in r and "🟡" in r
