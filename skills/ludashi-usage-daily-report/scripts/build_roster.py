#!/usr/bin/env python3
"""从**本机**授权真源派生重点用户名单 → config/key_users.toml。

为什么必须派生而不能入库固定：机队各机的 policy.toml 完全不同
（本机 owner_userid=20102/whitelist 5 人；机队模板 policy.example.toml 是
owner_userid=18434/whitelist=[]）。把某一台的名单提交进 git，夜间 provision
会把它强加给其他 7 台 → 活跃率、静默追踪全错。

故：**名单成员＝运行时从本机派生**；key_users.toml 只是派生结果的缓存
（含 dws 反查来的 org/dept/title，避免每次跑报告都打通讯录）。

用法：
    build_roster.py                 # 派生 + dws 反查 + 写 config/key_users.toml
    build_roster.py --dry-run       # 只看派生结果，不写文件
    build_roster.py --no-dws        # 跳过通讯录反查（离线/无网时用缓存里的旧值）
    build_roster.py --include-historical   # 把 audit 里的历史活跃发起人也纳入名单

成员出处（source 字段，逐人可追溯）：
    policy_owner       policy.toml 的 owner_userid
    superadmin         policy.toml 的 superadmin
    static_whitelist   policy.toml 的 whitelist
    file_uploader      policy.toml 的 file_uploaders（L3）
    dynamic_grant      run/whitelist_dynamic.json 且未过期
    historical_active  audit.log 里出现过的真实发起人（需 --include-historical）

红线：不回显凭据；企业邮箱不写入文件（对报告无用，属 PII）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
AVATAR = "/mnt/vol-eltaah12/workspace/avatar"
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(SKILL_DIR, "config", "key_users.toml")
DWS_CANDIDATES = ["/root/fenshen-ludashi/bin/dws", "/opt/team/bin/dws", "dws"]

# 明显是机器人/服务号的名字，不纳入"重点用户"（人）
BOT_NAME_HINTS = ("大脑", "智能体", "机器人", "助手", "bot", "Bot", "服务", "商旅",
                  "运营", "采购", "系统", "admin", "canary", "鲁大师")


def find_dws() -> str | None:
    for c in DWS_CANDIDATES:
        p = shutil.which(c) if "/" not in c else (c if os.access(c, os.X_OK) else None)
        if p:
            return p
    return None


def dws_profile(dws: str) -> str | None:
    """从本机 dws 认证态动态取 profile（各机 corp_id/user_id 不同，不能写死）。"""
    try:
        r = subprocess.run([dws, "auth", "status"], capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout)
        if j.get("corp_id") and j.get("user_id"):
            return f"{j['corp_id']}:{j['user_id']}"
    except Exception:
        pass
    return None


def parse_policy(path: str) -> dict:
    """读本机 policy.toml 的授权名单。"""
    out = {"owner": "", "superadmin": [], "whitelist": [], "file_uploaders": [],
           "blacklist": [], "gap": None}
    if not os.path.exists(path):
        out["gap"] = f"policy.toml 不存在: {path}"
        return out
    try:
        with open(path, "rb") as fh:
            d = tomllib.load(fh)
    except Exception as exc:
        out["gap"] = f"policy.toml 解析失败: {type(exc).__name__}"
        return out

    def walk(obj):
        """policy.toml 分节，递归找目标键。"""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "owner_userid" and isinstance(v, str):
                    out["owner"] = v.strip()
                elif k in ("superadmin", "whitelist", "file_uploaders", "blacklist"):
                    if isinstance(v, list):
                        out[k] = [str(x).strip() for x in v if str(x).strip()]
                else:
                    walk(v)
    walk(d)
    return out


def parse_dynamic(path: str) -> dict:
    """读动态授权，剔除已过期的（grant_valid_days=90 会到期）。"""
    out, now = {}, dt.datetime.now(TZ).timestamp()
    if not os.path.exists(path):
        return out
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(d, dict):
        return out
    for uid, v in d.items():
        if not isinstance(v, dict):
            continue
        exp = v.get("expires")
        if exp and float(exp) < now:
            continue  # 已过期，不算在册重点用户
        out[str(uid).strip()] = {
            "name": v.get("name", ""), "tier": v.get("tier", ""),
            "by": v.get("by", ""), "expires": exp,
        }
    return out


def names_from_events() -> dict:
    """events-*.ndjson → {openId 或 userId: 姓名}。

    openId 用户（如跨组织的赵兴峰）通讯录反查不到，这是本机唯一权威姓名源。
    """
    m: dict[str, str] = {}
    for f in sorted(glob.glob(os.path.join(AVATAR, "events-*.ndjson"))):
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            nm = (r.get("sender") or "").strip()
            oid = (r.get("sender_open_dingtalk_id") or "").strip()
            if nm and oid:
                m[oid] = nm
    return m


def names_from_contacts_cache() -> dict:
    """run/contacts.json → {openId: userId}，用于把 openId 归一到 userId。"""
    p = os.path.join(AVATAR, "run", "contacts.json")
    out = {}
    if not os.path.exists(p):
        return out
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return out
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, str) and v and not v.startswith("miss:"):
                out[k] = v
            elif isinstance(v, dict) and v.get("user_id"):
                out[k] = str(v["user_id"])
    return out


def actors_from_audit() -> list[str]:
    """audit.log 里出现过的真实发起人（actor），按出现次数降序。"""
    import collections
    c = collections.Counter()
    p = os.path.join(AVATAR, "audit", "audit.log")
    if not os.path.exists(p):
        return []
    for line in open(p, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("action") not in ("task_submit", "local_shadow", "local_exec"):
            continue
        a = (r.get("actor") or "").strip()
        if a:
            c[a] += 1
    return [a for a, _ in c.most_common()]


def pick_dept(depts: list[dict], prefer: str = "") -> tuple[str, int]:
    """从多部门里**确定性**挑一个。

    ⚠ 通讯录 API 返回的 depts 顺序是随机的：邹雄(40714) 有 34 个部门，
      连查两次 depts[0] 分别是「KG董事局成员」和「乐安家族管理委员会成员」。
      直接取 depts[0] 会让报告每天显示不同部门 → 必须确定性选择。

    规则：① 旧缓存的部门若仍在当前列表里 → 沿用（跨次稳定，不因 API 抖动而变）
          ② 否则取 deptPathName 最短者（＝层级最高的主部门），同级按名排序
    返回 (部门名, 部门总数)。
    """
    if not depts:
        return "", 0
    names = {(d.get("deptName") or "") for d in depts}
    if prefer and prefer in names:
        return prefer, len(depts)
    ranked = sorted(depts, key=lambda d: (len(d.get("deptPathName") or ""),
                                          d.get("deptName") or ""))
    return (ranked[0].get("deptName") or ""), len(depts)


def dws_lookup(dws: str, profile: str | None, ids: list[str]) -> dict:
    """批量通讯录反查 → {userId: {name,org,dept,title,dept_count}}。只查数字 id。"""
    ids = [i for i in ids if i.isdigit()]
    if not ids or not dws:
        return {}
    cmd = [dws, "contact", "user", "get", "--ids", ",".join(ids), "--format", "json"]
    if profile:
        cmd += ["--profile", profile]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        j = json.loads(r.stdout)
    except Exception:
        return {}
    out = {}
    for row in (j.get("result") or []):
        m = row.get("orgEmployeeModel") or {}
        uid = str(m.get("orgUserId") or m.get("jobNumber") or "").strip()
        if not uid:
            continue
        depts = m.get("depts") or []
        out[uid] = {
            "name": m.get("orgUserName") or "",
            "org": m.get("orgName") or "",
            "depts": depts,          # 全量留着，由 pick_dept 确定性挑
            "title": m.get("orgTitle") or "",
        }
    return out


def load_existing(path: str) -> dict:
    """读旧 key_users.toml → {id: 字段字典}，用于保留人工 note 与部门选定结果。"""
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    cur = None
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[[user]]"):
            # ⚠ 必须先落盘上一块再重置。曾因直接 cur={} 丢掉除最后一人外的全部记录，
            #   导致部门缓存偏好失效（舒瀚的 HSRN高管层 被重新随机选成 ZLRN高管层）。
            if cur is not None and cur.get("id"):
                out[cur["id"]] = cur
            cur = {}
            continue
        if line.startswith("["):          # [[service_account]] 或其他段 → 结束当前 user
            if cur is not None and cur.get("id"):
                out[cur["id"]] = cur
            cur = None
            continue
        if "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"').strip("'")
    if cur is not None and cur.get("id"):
        out[cur["id"]] = cur
    return out


def is_bot(name: str) -> bool:
    return any(h in (name or "") for h in BOT_NAME_HINTS)


def build(args) -> tuple[list[dict], list[str]]:
    notes = []
    pol = parse_policy(args.policy)
    if pol.get("gap"):
        notes.append(pol["gap"])
    dyn = parse_dynamic(args.dynamic)
    ev = names_from_events()
    oid2uid = names_from_contacts_cache()
    old = load_existing(args.out)

    # ① 收集成员：id → (source, tier)
    members: dict[str, tuple[str, str]] = {}
    if pol["owner"]:
        members[pol["owner"]] = ("policy_owner", "owner")
    for i in pol["superadmin"]:
        members.setdefault(i, ("superadmin", "superadmin"))
    for i in pol["whitelist"]:
        members.setdefault(i, ("static_whitelist", "L2"))
    for i in pol["file_uploaders"]:
        members.setdefault(i, ("file_uploader", "L3"))
    for uid, v in dyn.items():
        members.setdefault(uid, ("dynamic_grant", v.get("tier") or ""))
    if args.include_historical:
        for a in actors_from_audit():
            if a in members or is_bot(ev.get(a, "")):
                continue
            members.setdefault(a, ("historical_active", ""))

    # ② 姓名：通讯录反查（数字 id）＋ events（openId）＋ 旧缓存兜底
    info = {} if args.no_dws else dws_lookup(find_dws(), args.profile or dws_profile(find_dws() or ""),
                                             list(members))
    if args.no_dws:
        notes.append("[缺档] --no-dws：跳过通讯录反查，org/dept/title 沿用旧缓存")
    if not info and not args.no_dws:
        notes.append("[缺档] 通讯录反查无返回（dws 未登录/无网/profile 不符），org/dept/title 沿用旧缓存")

    rows = []
    for uid, (src, tier) in members.items():
        d = info.get(uid) or {}
        o = old.get(uid) or {}
        dy = dyn.get(uid) or {}
        # 姓名回退链：通讯录 → 动态授权表 → events(openId) → 旧缓存 → id 占位。
        # 动态授权表这一环不可省：靳光传(12223) 通讯录返回全空、events 里也没有他，
        # 只有 whitelist_dynamic.json 存着名字，漏掉就会退化成用 id 当姓名。
        name = (d.get("name") or dy.get("name") or ev.get(uid) or o.get("name") or "")
        if not name and uid in oid2uid:  # openId → userId → 姓名
            name = ev.get(uid) or info.get(oid2uid[uid], {}).get("name") or o.get("name") or ""
        if not name:
            name = uid
            notes.append(f"[缺档] {uid[:20]}… 姓名无法解析（通讯录/动态授权/events 均无），暂用 id 占位")
        elif not d.get("name") and dy.get("name"):
            notes.append(f"[缺档] {name}({uid}) 通讯录反查无返回（离职/无权限/id 变更），"
                         f"姓名取自 whitelist_dynamic.json，org/dept/title 缺失")
        # 部门：旧缓存优先（跨次稳定），否则确定性挑；多部门记数以便报告注明
        dept, dcnt = pick_dept(d.get("depts") or [], prefer=o.get("dept") or "")
        if not dept:
            dept, dcnt = (o.get("dept") or ""), int(o.get("dept_count") or 0)
        if dcnt > 1:
            notes.append(f"[多部门] {name}({uid}) 有 {dcnt} 个部门，取「{dept}」"
                         f"（通讯录返回顺序随机，已按缓存优先+路径最短确定性选定）")
        rows.append({
            "id": uid, "name": name,
            "org": d.get("org") or o.get("org") or "",
            "dept": dept, "dept_count": dcnt,
            "title": d.get("title") or o.get("title") or "",
            "tier": tier or o.get("tier") or "",
            "source": src,
            "note": o.get("note") or "",
        })
    # owner 置顶，其余按 source 优先级再按姓名
    prio = {"policy_owner": 0, "superadmin": 1, "static_whitelist": 2,
            "file_uploader": 3, "dynamic_grant": 4, "historical_active": 5}
    rows.sort(key=lambda r: (prio.get(r["source"], 9), r["name"]))
    return rows, notes


def write_toml(rows: list[dict], notes: list[str], path: str, svc: list[tuple[str, str]]) -> None:
    host = os.environ.get("AVATAR_NAME") or ""
    L = [
        "# 鲁大师重点用户名单 —— 由 build_roster.py 从**本机**授权真源派生，勿手改成员。",
        "#",
        "# 为什么不入库固定：机队各机 policy.toml 完全不同（本机 owner/whitelist 与",
        "# 机队模板 policy.example.toml 就不一样）。固定名单会被夜间 provision 强加给",
        "# 其他机器 → 活跃率与静默追踪全错。故成员运行时派生，本文件只是缓存",
        "# （存 dws 反查来的 org/dept/title，省得每次跑报告都打通讯录）。",
        "#",
        f"# 生成时间：{dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"# 本机人数：{len(rows)}（报告按实际人数算活跃率 N/{len(rows)}，不写死任何数字）",
        "#",
        "# 重新派生：python3 scripts/build_roster.py [--include-historical] [--no-dws]",
        "# 人工可改的只有 note 字段与 [[service_account]] 段，重跑会保留。",
        "",
    ]
    if notes:
        L.append("# 本次派生备注：")
        for x in notes:
            L.append(f"#   - {x}")
        L.append("")
    L.append("# 服务账号：有 token 但不是自然人，归 cron/api_server 口径，不参与用户级排名。")
    L.append("# 注：分身本体名各机不同（provision 29b 让 avatar_name 跟随本机 dws 认证名），")
    L.append("#     故 usage_collect.py 还会自动把 PG plans.owner_name 并入服务账号。")
    for name, why in svc:
        L += ["[[service_account]]", f'name = "{name}"', f'note = "{why}"', ""]
    for r in rows:
        L += ["[[user]]"]
        for k in ("id", "name", "org", "dept", "dept_count", "title", "tier", "source", "note"):
            v = str(r.get(k) or "").replace('"', "'")
            L.append(f'{k} = "{v}"')
        L.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(L))


DEFAULT_SVC = [
    ("中鲁运营", "API token 账号；跑云端打包/清理等运维任务，非人工提问"),
    ("__admin__", "管理接口调用"),
    ("canary", "健康探针"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="从本机授权真源派生重点用户名单")
    ap.add_argument("--policy", default=os.path.join(AVATAR, "policy.toml"))
    ap.add_argument("--dynamic", default=os.path.join(AVATAR, "run", "whitelist_dynamic.json"))
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--profile", help="dws profile（默认从 dws auth status 动态取）")
    ap.add_argument("--no-dws", action="store_true", help="跳过通讯录反查")
    ap.add_argument("--include-historical", action="store_true",
                    help="把 audit 里的历史活跃发起人也纳入名单")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows, notes = build(args)
    # 保留旧文件里的服务账号段（人工维护），并入默认项
    old_svc = []
    if os.path.exists(args.out):
        cur = None
        for raw in open(args.out, encoding="utf-8", errors="replace"):
            s = raw.strip()
            if s.startswith("[[service_account]]"):
                cur = {}
                old_svc.append(cur)
                continue
            if s.startswith("["):
                cur = None
                continue
            if "=" in s and cur is not None:
                k, v = s.split("=", 1)
                cur[k.strip()] = v.strip().strip('"').strip("'")
    have = {s.get("name") for s in old_svc}
    svc = [(s.get("name"), s.get("note", "")) for s in old_svc if s.get("name")]
    for nm, why in DEFAULT_SVC:
        if nm not in have:
            svc.append((nm, why))

    print(f"派生出重点用户 {len(rows)} 人：")
    for r in rows:
        print(f"  {r['id'][:22]:24} {r['name']:8} {r['source']:18} "
              f"{r['tier']:10} {r['org']}/{r['dept'] or '-'}")
    if notes:
        print("\n备注：")
        for x in notes:
            print("  -", x)
    print(f"\n服务账号 {len(svc)} 个：{', '.join(n for n, _ in svc)}")

    if args.dry_run:
        print("\n[--dry-run] 未写文件")
        return 0
    write_toml(rows, notes, args.out, svc)
    print(f"\n写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
