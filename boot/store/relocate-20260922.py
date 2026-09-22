#!/usr/bin/env python3
"""store 根重定位：workspace/store/ → workspace/shared/（2026-09-22，方案 A）。

起因：实测 relay 的 _snap 用 os.walk 默认不跟随符号链接，原计划的「旧路径 symlink
兼容层」会让 shared/knowledge 等整个从基线快照里消失，静默破坏冲突检测。改为让
store 根与 relay 的 shared_dir 同址，RELAY_SHARED / provision.sh / relay.env 全不动。

同时收编两处此前漏盘的存量：
  - shared/shared/（嵌套误写目录）里 2 份真实报告 + 1 个 zip
  - shared/{probe_dir,20260921能源局座谈会,tmp,mcp} 下的测试残留与 .bak
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import time


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


HERE = os.path.dirname(os.path.abspath(__file__))
S = _load("store_cli", os.path.join(HERE, "store"))

WS = "/mnt/vol-eltaah12/workspace"
OLD = os.path.join(WS, "store")
NEW = os.path.join(WS, "shared")

# 整目录搬迁：src 相对 OLD，dst 相对 NEW
DIR_MOVES = [
    ("data", "data"),
    ("report", "report"),
    ("index", "index"),
    ("work", "work"),
    (".archive", ".archive"),
    ("shared/knowledge", "knowledge"),
    ("shared/templates", "templates"),
    ("shared/skills-proposed", "skills-proposed"),
    ("shared/tools/packages", "tools/packages"),
]
# 搬完即删的空壳（shared/mcp、shared/memory 在 NEW 下已存在且为真身）
DROP_EMPTY = ["shared/mcp", "shared/memory", "shared/tools", "shared"]

# 漏盘存量：(源绝对路径, 目标相对 NEW, 覆盖字段)
EXTRA = [
    ("shared/shared/reports/20260922/Zhao/全面质检与审计报告-CAA-封装件V2.0-技能外置-20260922.md",
     "report/自检报告/2026-09/20260922_T0_CAA全面质检与审计报告-封装件V2.0_v1.md",
     dict(kind="report", domain="自检报告", status="done", source="relay",
          note="原被误写进嵌套目录 shared/shared/reports/，重定位时收编")),
    ("shared/shared/reports/20260922/Zhao/技能外置重构报告-V1.5-20260922.md",
     "report/项目进度/2026-09/20260922_T0_技能外置重构报告V1.5_v1.md",
     dict(kind="report", domain="项目进度", status="done", source="relay",
          note="原被误写进嵌套目录 shared/shared/reports/，重定位时收编")),
    ("shared/shared/skills/product-capability.zip",
     "tools/packages/product-capability.zip",
     dict(kind="shared", domain="工具运维", status="indexed",
          note="原在嵌套目录 shared/shared/skills/")),
    ("shared/probe_dir/probe.txt", ".archive/probe/probe.txt",
     dict(kind="archive", status="junk", note="probe_dir 连通性测试残留")),
    ("shared/20260921能源局座谈会/probe_cn.txt", ".archive/probe/probe_cn.txt",
     dict(kind="archive", status="junk", note="中文名写入测试残留")),
    ("shared/tmp/crack-hormuz-report.md.bak-马忠建-1734",
     ".archive/bak/crack-hormuz-report.md.bak-马忠建-1734",
     dict(kind="archive", status="archived", note="马忠建 1734 备份")),
    ("shared/tmp/crack-hormuz-index.md.bak-马忠建-1734",
     ".archive/bak/crack-hormuz-index.md.bak-马忠建-1734",
     dict(kind="archive", status="archived", note="马忠建 1734 备份")),
    ("shared/mcp/up-test-验证报告.md.bak-王洪彬-1440",
     ".archive/bak/up-test-验证报告.md.bak-王洪彬-1440",
     dict(kind="archive", status="archived", note="王洪彬 1440 备份")),
]
# 搬空后删除的旧目录
RMDIR = ["shared/shared/reports/20260922/Zhao", "shared/shared/reports/20260922",
         "shared/shared/reports", "shared/shared/skills", "shared/shared",
         "shared/probe_dir", "shared/20260921能源局座谈会", "shared/reports"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main():
    conn = S.connect()
    cur = conn.cursor()

    # 迁移前全量 sha 基线，用于事后核对一个字节都没丢
    before = {}
    for dp, dn, fn in os.walk(OLD):
        dn[:] = [d for d in dn if not S._is_runtime(dp, d)]
        for f in fn:
            p = os.path.join(dp, f)
            before[sha(p)] = os.path.relpath(p, OLD)
    print(f"store/ 迁移前 {len(before)} 个唯一 sha")

    # 1) 整目录搬迁
    for src, dst in DIR_MOVES:
        s, d = os.path.join(OLD, src), os.path.join(NEW, dst)
        if not os.path.isdir(s):
            print(f"  跳过（源不存在）: {src}")
            continue
        if os.path.isdir(d):  # 目标已存在且应为空，否则中止
            if os.listdir(d):
                raise SystemExit(f"目标非空，拒绝合并: {d}")
            os.rmdir(d)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(s, d)
        print(f"  {src} → {dst}")

    # 2) 漏盘存量收编（逐个校验 sha）
    added = []
    for rel_src, rel_dst, ov in EXTRA:
        s, d = os.path.join(WS, rel_src), os.path.join(NEW, rel_dst)
        if not os.path.isfile(s):
            print(f"  WARN 源不存在，跳过: {rel_src}")
            continue
        if os.path.exists(d):
            raise SystemExit(f"目标已存在，拒绝覆盖: {rel_dst}")
        h = sha(s)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(s, d)
        if sha(d) != h:
            raise SystemExit(f"移动后 sha 变化，严重: {rel_dst}")
        added.append((rel_dst, ov, h))
        print(f"  收编 {rel_src} → {rel_dst}")

    # 3) 删空壳
    for rel in DROP_EMPTY:
        p = os.path.join(OLD, rel)
        if os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)
    # 自底向上清空目录树：shared/reports 下还有多层已搬空的子目录，逐个 rmdir 会失败
    for rel in RMDIR:
        p = os.path.join(WS, rel)
        if not os.path.isdir(p):
            continue
        for dp, dn, fn in os.walk(p, topdown=False):
            if not dn and not fn:
                os.rmdir(dp)
        if not os.path.exists(p):
            print(f"  删空目录树 {rel}")
        else:
            print(f"  WARN {rel} 非空，保留：{os.listdir(p)[:5]}")
    if os.path.isdir(OLD) and not os.listdir(OLD):
        os.rmdir(OLD)
        print("  已删除空的 store/ 壳")

    # 4) 台账路径重写：去掉 shared/ 前缀（新根就是 shared/）
    cur.execute("SELECT count(*) n FROM store_files WHERE path LIKE 'shared/%'")
    n = cur.fetchone()["n"]
    cur.execute("""UPDATE store_files SET path=substring(path from 8), updated_at=%s
                   WHERE path LIKE 'shared/%%'""", (time.time(),))
    print(f"\n台账路径重写：{n} 行去掉 'shared/' 前缀")

    # 5) 新收编文件入库
    now = time.time()
    for rel_dst, ov, h in added:
        p = os.path.join(NEW, rel_dst)
        st = os.stat(p)
        fn = os.path.basename(rel_dst)
        parsed = S.parse_fname(fn)
        ext = parsed.get("ext") or fn.rsplit(".", 1)[-1].lower()
        fdate = parsed.get("fdate") or time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
        dom = ov.get("domain", "")
        cur.execute("SELECT period_fmt,sensitivity FROM store_domains WHERE domain=%s", (dom,))
        r = cur.fetchone()
        pf = r["period_fmt"] if r else "YYYY-MM"
        sens = ov.get("sensitivity") or (r["sensitivity"] if r else "internal")
        kind = ov.get("kind", "report")
        cur.execute("""INSERT INTO store_files(kind,path,fname,ext,domain,topic,variant,fdate,period,
                         sender_id,task_id,version,size,sha256,status,source,sensitivity,authority,
                         wiki_status,archived,note,root,created_at,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(path) DO UPDATE SET size=EXCLUDED.size, sha256=EXCLUDED.sha256,
                         status=EXCLUDED.status, note=EXCLUDED.note, updated_at=EXCLUDED.updated_at""",
                    (kind, rel_dst, fn, ext, dom, parsed.get("topic") or "",
                     parsed.get("variant") or "", fdate, S.period_of(fdate, pf),
                     parsed.get("sender_id") or "", parsed.get("task_id") if parsed else None,
                     parsed.get("version") or 1, st.st_size, h,
                     ov.get("status", "indexed"), ov.get("source", "manual"), sens,
                     ov.get("authority") or ("jingbowiki" if kind == "knowledge" else "local"),
                     "none", kind == "archive", ov.get("note", ""), "store", now, now))

    # 6) 事后核对：迁移前的每个 sha 都必须仍在新根下找得到
    after = set()
    for dp, dn, fn in os.walk(NEW):
        dn[:] = [d for d in dn if not S._is_runtime(dp, d)]
        for f in fn:
            after.add(sha(os.path.join(dp, f)))
    lost = {h: p for h, p in before.items() if h not in after}
    cur.execute("SELECT count(*) n FROM store_files")
    total = cur.fetchone()["n"]
    conn.close()
    print(f"\n新根 {NEW} 下唯一 sha: {len(after)}")
    print(f"丢失的 sha: {len(lost)}" + (f" → {lost}" if lost else "（零丢失）"))
    print(f"台账总行数: {total}（新收编 {len(added)}）")
    if lost:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
