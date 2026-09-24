from __future__ import annotations

"""计划（定时任务）的纯逻辑：OnCalendar 下次触发计算 + 动作白名单校验。

设计要点（稳定性优先）：
- next_elapse 复用 systemd 自身的 `systemd-analyze calendar`，与 .timer 单元语义
  完全一致，零第三方依赖；解析其 "(in UTC)" 行得 epoch，避免本机时区歧义。
- 计划只允许一种动作：提交一个 relay 任务（type=task）。动作字段白名单校验，
  杜绝任意 shell / 任意路径，特权面收敛在 root 侧 tick + relay 任务沙箱内。
"""

import calendar
import re
import subprocess
import time

# 计划名：字母/数字/中文/下划线/点/连字符，1-64。仅用于展示与唯一性，不参与 shell。
NAME_RE = re.compile(r"^[A-Za-z0-9_.\u4e00-\u9fff-]{1,64}$")

# systemd-analyze calendar 输出里的 UTC 行：
#   "       (in UTC): Fri 2026-09-25 00:00:00 UTC"
_UTC_RE = re.compile(
    r"\(in UTC\):\s+\w+\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+UTC")

_MAX_DESC = 4000


def parse_utc_elapse(stdout: str) -> float | None:
    """从 systemd-analyze calendar 输出解析下次触发的 epoch（UTC）。纯函数，可单测。"""
    m = _UTC_RE.search(stdout or "")
    if not m:
        return None
    try:
        st = time.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return float(calendar.timegm(st))


def next_elapse(on_calendar: str) -> tuple[float | None, str | None]:
    """计算 OnCalendar 表达式的下次触发时刻（epoch UTC）。

    返回 (epoch, None) 成功；(None, error) 失败（表达式非法 / systemd-analyze 不可用）。
    """
    expr = (on_calendar or "").strip()
    if not expr:
        return None, "on_calendar required"
    try:
        p = subprocess.run(["systemd-analyze", "calendar", expr],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"systemd-analyze unavailable: {e!r}"
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip()[:200] or "invalid calendar"
    epoch = parse_utc_elapse(p.stdout)
    if epoch is None:
        return None, "cannot parse next elapse from systemd-analyze output"
    return epoch, None


def validate_action(action: dict) -> str | None:
    """动作白名单校验。返回错误信息字符串，或 None 表示通过。"""
    if not isinstance(action, dict):
        return "action must be an object"
    if action.get("type") != "task":
        return "action.type must be 'task'"
    desc = action.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return "action.description required (non-empty string)"
    if len(desc) > _MAX_DESC:
        return f"action.description too long (<= {_MAX_DESC})"
    if action.get("priority", "normal") not in ("normal", "urgent"):
        return "action.priority must be normal|urgent"
    targets = action.get("targets", [])
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        return "action.targets must be a list of strings"
    if not isinstance(action.get("read_only", False), bool):
        return "action.read_only must be a boolean"
    if not isinstance(action.get("project", ""), str):
        return "action.project must be a string"
    return None
