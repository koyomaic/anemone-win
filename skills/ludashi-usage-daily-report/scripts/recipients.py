#!/usr/bin/env python3
"""解析**本机**日报收件人。

为什么需要这个：机队各机的收件人不同（用户 2026-10-12 明确指出「并不都是王洪彬和崔成东」）。
把收件人写死在计划描述或脚本里，会被夜间 provision 推给其他 7 台 → 报告发错人。
故收件人存 `config/recipients.local.json`，该文件被 .gitignore 挡掉、不入库、provision 不覆盖。

用法：
    recipients.py --kind report          # 打印日报收件人（每行一个）
    recipients.py --kind alert           # 打印失败预警收件人
    recipients.py --kind report --csv    # 逗号分隔，便于 shell 里用
    recipients.py --init                 # 从本机 policy.toml 派生并写出 local 配置
    recipients.py --init --force         # 覆盖已有 local 配置

缺省行为：local 配置不存在时，从本机 policy.toml 派生（owner_userid + superadmin），
姓名经 config/key_users.toml 的 name_by_id 反查；派生结果会自动写出 local 文件，
使配置显式化、下次可直接改。故新机器零配置也能跑，且推给的是**本机**负责人。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tomllib

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(SKILL_DIR, "config", "recipients.example.json")
ROSTER = os.path.join(SKILL_DIR, "config", "key_users.toml")
POLICY_CANDIDATES = [
    "/mnt/vol-eltaah12/workspace/avatar/policy.toml",
    "/root/fenshen-ludashi/avatar/policy.toml",
]

# 本机配置的查找顺序。
# ⚠ 主位置刻意放在 avatar/ 而**不是技能目录内**：技能目录是 provision 21b 的同步目标，
#   一次「rm -rf 安装位 + cp -r」式的全量重同步就会把本机配置抹掉（实测踩过：安装位丢了
#   local 配置 → 解析器静默回退成 policy 派生值，收件人从王洪彬+崔成东变成崔成东+毕倩，
#   报告发错人且无人察觉）。avatar/ 是 policy.toml 所在的本机配置区，永不被同步。
WORKSPACE = os.environ.get("RELAY_WORKSPACE", "/mnt/vol-eltaah12/workspace")
LOCAL_CANDIDATES = [
    os.environ.get("USAGE_RECIPIENTS", ""),                      # ① 显式指定
    os.path.join(WORKSPACE, "avatar", "usage-report-recipients.json"),  # ② 本机配置区（主）
    os.path.join(SKILL_DIR, "config", "recipients.local.json"),  # ③ 技能目录内（开发/兼容）
]
LOCAL_CANDIDATES = [p for p in LOCAL_CANDIDATES if p]
# 写出时一律写主位置
LOCAL_WRITE = os.path.join(WORKSPACE, "avatar", "usage-report-recipients.json")

# 机队惯例：部署/采集类失败预警发王洪彬（见 AGENTS.md）。仅用于 alert 缺省，
# report 缺省一律走本机 policy 派生，不套用这个。
FLEET_ALERT_DEFAULT = "王洪彬"


def local_path() -> str | None:
    """返回第一个存在的本机配置路径。"""
    for p in LOCAL_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def name_by_id() -> dict:
    """从 key_users.toml 取 id→姓名（该文件由 build_roster.py 从本机真源派生）。"""
    out = {}
    if not os.path.exists(ROSTER):
        return out
    cur = None
    for raw in open(ROSTER, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[[user]]"):
            cur = {}
            continue
        if line.startswith("["):
            cur = None
            continue
        if "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"').strip("'")
            if cur.get("id") and cur.get("name"):
                out[cur["id"]] = cur["name"]
    return out


def find_policy() -> str | None:
    for p in POLICY_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def derive_from_policy() -> tuple[list[str], list[str], list[str]]:
    """从本机 policy.toml 派生 (report 收件人, alert 收件人, 备注)。"""
    notes = []
    p = find_policy()
    if not p:
        notes.append(f"[缺档] 未找到 policy.toml（试过 {'、'.join(POLICY_CANDIDATES)}）；"
                     f"report 收件人留空，需人工填 {LOCAL_WRITE}")
        return [], [FLEET_ALERT_DEFAULT], notes
    try:
        with open(p, "rb") as fh:
            d = tomllib.load(fh)
    except Exception as exc:
        notes.append(f"[缺档] policy.toml 解析失败 {type(exc).__name__}；report 收件人留空")
        return [], [FLEET_ALERT_DEFAULT], notes

    ids: list[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "owner_userid" and isinstance(v, str) and v.strip():
                    ids.append(v.strip())
                elif k == "superadmin" and isinstance(v, list):
                    ids.extend(str(x).strip() for x in v if str(x).strip())
                else:
                    walk(v)
    walk(d)

    nmap = name_by_id()
    seen, report = set(), []
    for i in dict.fromkeys(ids):          # 去重保序
        nm = nmap.get(i)
        if not nm:
            # openId 或名单外的人：反查不到姓名，dws --to 也没法用姓名解析
            notes.append(f"[缺档] policy 里的 {i[:20]}… 在 key_users.toml 反查不到姓名"
                         f"（先跑 build_roster.py），已跳过")
            continue
        if nm not in seen:
            seen.add(nm)
            report.append(nm)
    if not report:
        notes.append("[缺档] policy 派生结果为空，report 收件人需人工填")
    notes.append(f"report 收件人由本机 {os.path.basename(p)} 的 owner_userid+superadmin 派生")
    return report, [FLEET_ALERT_DEFAULT], notes


def load_local() -> tuple[dict | None, str | None]:
    """返回 (配置, 命中的路径)。按 LOCAL_CANDIDATES 顺序取第一个存在的。"""
    path = local_path()
    if not path:
        return None, None
    try:
        d = json.load(open(path, encoding="utf-8"))
        return (d if isinstance(d, dict) else None), path
    except Exception:
        return None, path


def write_local(report: list[str], alert: list[str], notes: list[str]) -> None:
    payload = {
        "_说明": "本机日报收件人。各机不同，故不入库（.gitignore 已挡），provision 不会覆盖。",
        "_改法": "直接编辑本文件的 report / alert 两个数组，填钉钉可解析的姓名。",
        "host": socket.gethostname(),
        "report": report,
        "alert": alert,
        "_derived_notes": notes,
    }
    os.makedirs(os.path.dirname(LOCAL_WRITE), exist_ok=True)
    with open(LOCAL_WRITE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def resolve(kind: str, do_init: bool, force: bool) -> tuple[list[str], list[str]]:
    """返回 (收件人列表, 备注)。"""
    cfg, cfg_path = load_local()
    if cfg is None or force:
        report, alert, notes = derive_from_policy()
        if cfg is None:
            notes.append(f"local 配置不存在 → 已按本机 policy 派生并写出 {LOCAL_WRITE}")
        else:
            notes.append("--force：已用本机 policy 派生结果覆盖原 local 配置")
        if do_init or force or cfg is None:
            write_local(report, alert, notes)
        cfg = {"report": report, "alert": alert}
        return list(cfg.get(kind) or []), notes
    vals = cfg.get(kind)
    if not isinstance(vals, list):
        return [], [f"[缺档] {cfg_path or LOCAL_WRITE} 的 {kind} 字段不是数组"]
    out = [str(x).strip() for x in vals if str(x).strip()]
    if not out and kind == "alert":
        out = [FLEET_ALERT_DEFAULT]
    return out, []


def main() -> int:
    ap = argparse.ArgumentParser(description="解析本机日报收件人")
    ap.add_argument("--kind", choices=["report", "alert"], default="report")
    ap.add_argument("--csv", action="store_true", help="逗号分隔输出（便于 shell）")
    ap.add_argument("--init", action="store_true", help="从本机 policy.toml 派生并写出 local 配置")
    ap.add_argument("--force", action="store_true", help="覆盖已有 local 配置")
    ap.add_argument("--quiet", action="store_true", help="不往 stderr 打备注")
    a = ap.parse_args()

    names, notes = resolve(a.kind, a.init, a.force)
    if notes and not a.quiet:
        for x in notes:
            print(f"[recipients] {x}", file=sys.stderr)
    if not names:
        print(f"[recipients] {a.kind} 收件人为空——请填 {LOCAL_WRITE}", file=sys.stderr)
        return 1
    print(",".join(names) if a.csv else "\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
