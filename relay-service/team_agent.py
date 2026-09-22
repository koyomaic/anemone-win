#!/usr/bin/env python3
"""team-agent：千问/成员与团队中转服务交互的 CLI。

用法示例：
  team-agent --server http://10.0.0.5:8787 --token ta_xxx run "写一份9月经营月报" \
      --targets shared/report/市场分析/2026-09 --priority normal --wait
  team-agent task 12
  team-agent stream 12
  team-agent cancel 12
  team-agent diff 12
  team-agent confirm 12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

# 执行完成即停（review 待人工 confirm）
TERMINAL = {"done", "review", "conflict", "failed", "cancelled"}


def client(args) -> httpx.Client:
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    return httpx.Client(base_url=args.server, headers=headers, timeout=30)


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def print_task(t: dict):
    print(f"#{t['id']} [{t['status']}] user={t['user']} priority={t['priority']}")
    print(f"  desc: {t['description']}")
    if t.get("targets"):
        print(f"  targets: {', '.join(t['targets'])}")
    if t.get("result"):
        print(f"  result: {t['result']}")
    if t.get("error"):
        print(f"  error: {t['error']}")
    if t.get("changed_files"):
        print(f"  changed: {len(t['changed_files'])} files")
        for f in t["changed_files"][:20]:
            print(f"    - {f}")
    if t.get("conflicts"):
        print("  CONFLICTS:")
        for c in t["conflicts"]:
            print(f"    - {c['file']} (task #{c['with_task']} / {c['with_user']}) {c.get('note','')}")
            if c.get("backup"):
                print(f"      backup: {c['backup']}")


def cmd_run(args, c):
    body = {
        "description": args.description,
        "project": args.project or "",
        "targets": [t for t in (args.targets or "").split(",") if t.strip()],
        "priority": args.priority,
        "force": args.force,
    }
    if args.resume:
        body["resume_from"] = int(args.resume)
    r = c.post("/tasks", json=body)
    if r.status_code == 409:
        d = r.json().get("detail", {})
        print("⚠️ 目标与在途任务重叠：", file=sys.stderr)
        for o in d.get("overlaps", []):
            print(f"  任务 #{o['task_id']}（{o['user']}）正在动: {', '.join(o['targets'])}",
                  file=sys.stderr)
        print("如确认继续，请加 --force 重新提交。", file=sys.stderr)
        sys.exit(2)
    r.raise_for_status()
    data = r.json()
    task_id = data["task_id"]
    print(f"已提交任务 #{task_id}（status={data['status']}）")
    if data.get("warnings"):
        for w in data["warnings"]:
            print(f"  ⚠️ 注意：任务 #{w['task_id']}（{w['user']}）也在动: {', '.join(w['targets'])}")
    if not args.wait:
        return
    deadline = time.time() + (args.wait if isinstance(args.wait, float) else 3600)
    while time.time() < deadline:
        t = c.get(f"/tasks/{task_id}").json()
        if t["status"] in TERMINAL:
            print_task(t)
            sys.exit(0 if t["status"] in ("done", "review") else 1)
        time.sleep(1.5)
    print(f"等待超时（{args.wait}s），任务 #{task_id} 仍在 {c.get(f'/tasks/{task_id}').json()['status']} 状态",
          file=sys.stderr)
    sys.exit(3)


def cmd_stream(args, c):
    with c.stream("GET", f"/tasks/{args.id}/stream", timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            t = ev.get("t")
            if t == "status":
                print(f"[status] {ev.get('s')}")
                if ev.get("changed"):
                    for f in ev["changed"]:
                        print(f"  changed: {f}")
                if ev.get("conflicts"):
                    for cf in ev["conflicts"]:
                        print(f"  CONFLICT: {cf['file']} (#{cf['with_task']} {cf['with_user']})")
                if ev.get("s") in TERMINAL:
                    break
            elif t == "agent":
                if ev.get("kind") == "tool":
                    print(f"[tool] {ev.get('tool')} {ev.get('title') or ''}".rstrip())
                else:
                    print(f"[text] {ev.get('text','').strip()}")


def main():
    ap = argparse.ArgumentParser(prog="team-agent", description="团队中转服务 CLI")
    ap.add_argument("--server", default=os.environ.get("TEAM_AGENT_SERVER", "http://127.0.0.1:8787"))
    ap.add_argument("--token", default=os.environ.get("TEAM_AGENT_TOKEN", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("users", help="管理成员 token（需 admin token）")
    p.add_argument("action", choices=["create", "list", "delete"])
    p.add_argument("name", nargs="?", default="")

    p = sub.add_parser("run", help="提交任务")
    p.add_argument("description")
    p.add_argument("--project", default="")
    p.add_argument("--targets", default="", help="逗号分隔，如 shared/report/市场分析/2026-09,shared/data")
    p.add_argument("--priority", choices=["normal", "urgent"], default="normal")
    p.add_argument("--force", action="store_true", help="目标重叠时强制提交")
    p.add_argument("--resume", default="", help="接续任务 id")
    p.add_argument("--wait", nargs="?", const="", default=None,
                   help="阻塞等待终态（可给超时秒数）")

    p = sub.add_parser("tasks", help="任务列表")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("task", help="任务详情")
    p.add_argument("id", type=int)

    p = sub.add_parser("stream", help="SSE 实时流")
    p.add_argument("id", type=int)

    p = sub.add_parser("cancel", help="取消任务")
    p.add_argument("id", type=int)

    p = sub.add_parser("diff", help="任务变更文件")
    p.add_argument("id", type=int)

    p = sub.add_parser("confirm", help="确认 review 任务")
    p.add_argument("id", type=int)
    p.add_argument("--outcome", choices=["done", "conflict"], default="done")

    p = sub.add_parser("approve", help="审批高危命令拦截（pending_approval 任务）")
    p.add_argument("id", type=int)
    p.add_argument("--decision", choices=["approve", "deny"], required=True)

    args = ap.parse_args()
    c = client(args)

    try:
        if args.cmd == "users":
            if args.action == "create":
                if not args.name:
                    die("需要成员名")
                r = c.post("/users", json={"name": args.name})
                r.raise_for_status()
                print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            elif args.action == "list":
                r = c.get("/users")
                r.raise_for_status()
                for u in r.json()["users"]:
                    print(f"{u['name']}\t{u['token'][:12]}…")
            else:
                r = c.delete(f"/users/{args.name}")
                r.raise_for_status()
                print("deleted")
        elif args.cmd == "run":
            cmd_run(args, c)
        elif args.cmd == "tasks":
            params = {"limit": args.limit}
            if args.status:
                params["status"] = args.status
            for t in c.get("/tasks", params=params).json()["tasks"]:
                print(f"#{t['id']} [{t['status']}] {t['user']}: {t['description'][:60]}")
        elif args.cmd == "task":
            r = c.get(f"/tasks/{args.id}")
            r.raise_for_status()
            print_task(r.json())
        elif args.cmd == "stream":
            cmd_stream(args, c)
        elif args.cmd == "cancel":
            r = c.post(f"/tasks/{args.id}/cancel")
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False))
        elif args.cmd == "diff":
            r = c.get(f"/tasks/{args.id}/diff")
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        elif args.cmd == "confirm":
            r = c.post(f"/tasks/{args.id}/confirm", json={"outcome": args.outcome})
            r.raise_for_status()
            t = r.json()
            print(f"#{t['id']} -> {t['status']}")
        elif args.cmd == "approve":
            r = c.post(f"/tasks/{args.id}/approve", json={"decision": args.decision})
            r.raise_for_status()
            t = r.json()
            print(f"#{t['id']} -> {t['status']}")
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        die(f"HTTP {e.response.status_code}: {body}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
