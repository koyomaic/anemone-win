#!/usr/bin/env python3
"""日度使用分析群播确定性快路径。

只复用已由完整日报流程生成并补齐的完整件，生成推送摘要与 ```send_file``` 标记。
不采集、不渲染新报告、不编造建议、不直连钉钉。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_report import render_summary  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FACTS = 2
EXIT_REPORT = 3
EXIT_INCOMPLETE = 4
EXIT_COPY = 5


def default_workspace() -> Path:
    for key in ("RELAY_WORKSPACE", "AVATAR_WORKSPACE"):
        v = os.environ.get(key, "").strip()
        if v:
            return Path(v)
    here = Path(__file__).resolve()
    for anc in here.parents:
        if (anc / "shared" / "tmp" / "usage").is_dir():
            return anc
    return Path("/mnt/vol-eltaah12/workspace")


def script_path(workspace: Path) -> Path:
    return (workspace / "shared" / "skills" / "ludashi-usage-daily-report"
            / "scripts" / "group_broadcast.py")


def infer_task_id(out_dir: Path) -> str:
    parts = out_dir.resolve().parts
    if "tasks" in parts:
        i = parts.index("tasks")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return parts[i + 1]
    return ""


def find_report(report_dir: Path, date: str) -> Path | None:
    if not report_dir.is_dir():
        return None
    cands = [p for p in report_dir.glob(f"*_{date}_v*.md") if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: (p.stat().st_mtime, p.name))


def plan_description(workspace: Path, display_name: str) -> str:
    sp = script_path(workspace)
    return (
        f"【每日·{display_name}用户日度使用分析群播（确定性快路径）】"
        "只执行以下步骤，禁止探索代码/计划/定时器，禁止跑 usage_collect.py，"
        "禁止自行 dws 发送。\n"
        f"1. 运行：python3 {sp} --out-dir \"$PWD/output\"\n"
        "2. 退出码 0：把 stdout 原样作为最终结果输出（已含摘要与 send_file 围栏块），"
        "不要改写数字、不要追加探索。\n"
        "3. 退出码 2：输出「预采集缺失或非 pg_authoritative，当日日报不可用，"
        "查 /var/log/ludashi-usage-collect.log」后结束。\n"
        "4. 退出码 3：输出「当日完整件尚未生成；请检查日报渲染计划/链路，"
        "不要自行重采集或编造建议」后结束。\n"
        "5. 退出码 4：输出「当日完整件仍有 {{待填}} 叙述位，不可群播」后结束。\n"
        "6. 其他非零：输出退出码与 stderr 摘要后结束。"
    )


def emit_plan(workspace: Path, display_name: str, plan_name: str,
              plan_time: str) -> int:
    spec = {
        "op": "create",
        "name": plan_name,
        "on_calendar": f"*-*-* {plan_time}",
        "description": plan_description(workspace, display_name),
    }
    print("```plan")
    print(json.dumps(spec, ensure_ascii=False))
    print("```")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="日度使用分析群播确定性快路径")
    ap.add_argument("--date", help="报告日 YYYY-MM-DD，默认昨天")
    ap.add_argument("--workspace", help="工作区根目录")
    ap.add_argument("--facts", help="facts.json 路径")
    ap.add_argument("--report-dir", help="完整件所在目录")
    ap.add_argument("--out-dir", help="任务 output/ 或 work/ 目录，用于 send_file 代投")
    ap.add_argument("--task-id", help="仅用于日志/命名；缺省从 out-dir 推断")
    ap.add_argument("--plan-name")
    ap.add_argument("--plan-time", default="09:00:00")
    ap.add_argument("--emit-plan", action="store_true",
                    help="只输出创建每日群播计划的 ```plan``` 标记")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    a = ap.parse_args()

    ws = Path(a.workspace or default_workspace()).resolve()
    try:
        date = (datetime.date.fromisoformat(a.date) if a.date
                else datetime.date.today() - datetime.timedelta(days=1))
    except ValueError:
        print(f"--date 非法：{a.date}", file=sys.stderr)
        return EXIT_USAGE
    ds = date.isoformat()
    facts_path = Path(a.facts or ws / "shared" / "tmp" / "usage" / f"facts-{ds}.json")
    report_dir = Path(a.report_dir or ws / "shared" / "report" / "自检报告"
                      / ds[:7])

    if not facts_path.is_file():
        msg = (f"预采集缺失：{facts_path} 不存在；当日日报不可用，"
               "查 /var/log/ludashi-usage-collect.log")
        print(msg, file=sys.stderr)
        if a.json:
            print(json.dumps({"ok": False, "reason": "facts_missing",
                              "date": ds, "facts": str(facts_path)},
                             ensure_ascii=False))
        return EXIT_FACTS

    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"facts 解析失败：{e}", file=sys.stderr)
        return EXIT_FACTS

    if facts.get("report_date") != ds:
        print(f"facts 报告日不匹配：期望 {ds}，实际 {facts.get('report_date')}",
              file=sys.stderr)
        return EXIT_FACTS
    if facts.get("source") != "pg_authoritative" or not (facts.get("pg") or {}).get("ok"):
        print(f"facts 非 pg_authoritative 或 PG 不可用：source={facts.get('source')}",
              file=sys.stderr)
        return EXIT_FACTS

    display_name = str(facts.get("display_name") or "鲁大师")
    plan_name = a.plan_name or f"{display_name}用户日度使用分析-每日群播"
    if a.emit_plan:
        return emit_plan(ws, display_name, plan_name, a.plan_time)

    report = find_report(report_dir, ds)
    if not report:
        print(f"当日完整件尚未生成：{report_dir}/*_{ds}_v*.md 不存在；"
              "请检查日报渲染计划/链路，不要自行重采集或编造建议", file=sys.stderr)
        return EXIT_REPORT

    body = report.read_text(encoding="utf-8")
    if "{{待填" in body:
        print(f"当日完整件仍有待填叙述位：{report}；不可群播", file=sys.stderr)
        return EXIT_INCOMPLETE

    summary = render_summary(facts)
    out_path = report
    if a.out_dir:
        out_dir = Path(a.out_dir).expanduser().resolve()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / report.name
            if out_path != report:
                shutil.copy2(report, out_path)
        except Exception as e:
            print(f"复制到任务交付目录失败：{e}", file=sys.stderr)
            return EXIT_COPY

    task_id = a.task_id or (infer_task_id(Path(a.out_dir)) if a.out_dir else "")
    if a.json:
        print(json.dumps({
            "ok": True, "date": ds, "task_id": task_id,
            "report": str(report), "send_path": str(out_path),
            "summary_chars": len(summary),
        }, ensure_ascii=False))
        return EXIT_OK

    print(summary)
    print()
    print("```send_file")
    print(json.dumps({"path": str(out_path)}, ensure_ascii=False))
    print("```")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
