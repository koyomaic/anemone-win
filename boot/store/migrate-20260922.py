#!/usr/bin/env python3
"""一次性存量迁移：shared/{data,reports,knowledge} → store/ 新规范（2026-09-22）。

用户已批准「直接迁不需要审」。安全约束：
  - 每个文件移动前后各算一次 sha256，不一致即中止
  - 目标已存在即中止（不覆盖）
  - DB 更新与文件移动分两阶段，全部移动成功后才提交台账
  - 保留原 row id，从而保住 wiki_page_id / note / status 等人工标注
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import time


def load_store():
    """store CLI 无 .py 后缀（作为命令安装），spec_from_file_location 会返回 None，
    必须用 SourceFileLoader 显式指定加载器。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store")
    loader = importlib.machinery.SourceFileLoader("store_cli", p)
    spec = importlib.util.spec_from_loader("store_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


S = load_store()

WS = "/mnt/vol-eltaah12/workspace"
ROOT = os.path.join(WS, "store")

# id → (新相对路径, 覆盖字段)
# 新路径相对 store 根；归档类进 .archive/，工具包进 shared/tools/packages/
MAP: dict[int, tuple[str, dict]] = {}


def _m(i: int, path: str, **kw):
    MAP[i] = (path, kw)


# ---------- 删除（用户明确批准：wiki 与本地垃圾条目都删） ----------
DELETE = {68: "shared/knowledge/dws/file-send-11056-first-contact.md"}  # wiki 797c54ec 已删

# ---------- .archive/probe/：测试残留与空占位 ----------
_m(35, ".archive/probe/中文文件名.md", status="junk", note="内容 'cn-name-test'，中文文件名测试残留")
_m(52, ".archive/probe/test-write.md", status="junk", note="内容 3B，写入测试残留")
_m(36, ".archive/probe/录音1_逐字稿.md", status="stub",
   note="占位空稿(133B)：录音1（73.8分钟/15片/17M m4a 齐全）从未转写；2026-09-22 决定暂不补，仅登记缺口")
_m(46, ".archive/probe/rec1_transcript.md", status="stub",
   note="同 #36，英文目录副本；录音1 从未转写")

# ---------- .archive/bak/：备份与时效失效记录 ----------
_m(64, ".archive/bak/中鲁燃能组织架构.md.bak-崔成东-1044", status="archived")
_m(47, ".archive/bak/rec1_transcript.md.bak-崔成东-1427", status="archived")
_m(49, ".archive/bak/rec2_transcript.md.bak-崔成东-1427", status="archived")
_m(37, ".archive/bak/录音1_逐字稿.md.bak-崔成东-1408", status="archived")
_m(39, ".archive/bak/录音2_逐字稿.md.bak-崔成东-1408", status="archived")
_m(40, ".archive/bak/录音2_逐字稿.md.bak-崔成东-1422", status="archived")
_m(41, ".archive/bak/录音2_逐字稿.whisper旧版备份.md", status="archived",
   note="Whisper 早期版(16938B)，被 #48(20930B) 取代")
_m(42, ".archive/bak/录音2_逐字稿.whisper旧版备份.md.bak-崔成东-1422", status="archived")
_m(44, ".archive/bak/钉钉双向对话能力自检-20260915.md.bak-崔成东-1659", status="archived")
_m(70, ".archive/bak/old-login-invalidated-20260907.md", status="archived",
   wiki_status="skip", note="时效性失效记录（20260907 登录失效），非知识")

# ---------- .archive/dup/：sha256 完全相同的冗余副本 ----------
_m(28, ".archive/dup/jingbo-data-api-capability-list.md", status="archived",
   note="与 #29 中文版 sha256 全同，去重保中文")
_m(51, ".archive/dup/dedup-result-20260922.md", status="archived",
   note="与 #53 中文版 sha256 全同，去重保中文")

# ---------- shared/tools/packages/：客户端包不是经营数据 ----------
_m(2, "shared/tools/packages/jingbo-data-api-V5.zip", kind="shared", domain="工具运维",
   status="indexed", authority="local")
_m(3, "shared/tools/packages/team-agent-relay-鲁大师-1.4.3-20260917.zip", kind="shared",
   domain="工具运维", status="indexed", authority="local")

# ---------- shared/knowledge/<tag域>/：真源 jingboWiki，本地是缓存 ----------
KN = dict(kind="knowledge", authority="jingbowiki", status="indexed")
_m(58, "shared/knowledge/工具运维/agent能力清单.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(59, "shared/knowledge/工具运维/中转服务使用说明.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(66, "shared/knowledge/工具运维/dws/auto-share-renew.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(67, "shared/knowledge/工具运维/dws/device-login.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(69, "shared/knowledge/工具运维/dws/install-skills-package.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(71, "shared/knowledge/工具运维/dws/reuse-login-across-tasks.md", domain="工具运维", wiki_tag="工具运维", **KN)
_m(61, "shared/knowledge/组织制度/净头寸经营策略矩阵-摘要.md", domain="组织制度", wiki_tag="组织制度", **KN)
_m(62, "shared/knowledge/组织制度/净头寸经营策略矩阵.html", domain="组织制度", wiki_tag="组织制度",
   wiki_status="skip", note="html 原件；摘要版已进 wiki(d067fc6d)，原件留本地由 refs 关联", **KN)
_m(63, "shared/knowledge/组织制度/中鲁燃能组织架构.md", domain="组织制度", wiki_tag="组织制度", **KN)
_m(65, "shared/knowledge/口径方法论/WTI-Brent价差分析-数据取数与口径.md", domain="口径方法论",
   wiki_tag="口径方法论", **KN)
_m(60, "shared/knowledge/口径方法论/发改委成品油调价-周期与口径.md", domain="口径方法论",
   wiki_tag="口径方法论", **KN)

# ---------- data/<YYYY>/<MM>/<DD>/<sender>/ ----------
# 座谈会 20 个文件归 20102 崔成东：该事件全部 .bak 均为「.bak-崔成东-*」，可佐证其为操作人
EV = "data/2026/2026-09/2026-09-21/20102"
EVN = "20260921_20102_T0_能源局座谈会"
for n in range(1, 16):
    _m(4 + n, f"{EV}/{EVN}-录音1分片{n:02d}_v1.m4a", sender_id="20102", source="dingtalk",
       status="raw", note="录音1（73.8分钟）从未转写，见 .archive/probe/ 缺口登记")
for n in range(1, 5):
    _m(19 + n, f"{EV}/{EVN}-录音2分片{n:02d}_v1.m4a", sender_id="20102", source="dingtalk",
       status="done", note="已转写：FunASR(#38) 与 Whisper(#48) 两版并存")
_m(24, f"{EV}/{EVN}-京博石化调研材料_v1.docx", sender_id="20102", source="dingtalk", status="raw")
_m(4, "data/2026/2026-09/2026-09-21/_unknown/20260921_T0_京博石化与山东高速业务合作模式_v1.html",
   sender_id="_unknown", status="raw", note="来源待确认")
_m(1, "data/2026/2026-09/2026-09-03/_unknown/20260903_T0_hormuz船舶过海峡明细近14天_v1.xlsx",
   sender_id="_unknown", status="raw", note="日期取自 mtime；来源待确认")
_m(25, "data/2026/2026-09/2026-09-22/_unknown/20260922_T0_查重任务说明_v1.md",
   sender_id="_unknown", source="relay", status="raw")
_m(26, "data/2026/2026-09/2026-09-22/_unknown/20260922_T0_查重输入-技能清单_v1.md",
   sender_id="_unknown", source="relay", status="raw")

# ---------- report/<domain>/<period>/ ----------
RP = dict(kind="report", authority="local", status="done")
_m(38, "report/会议纪要/2026-09/20260921_T0_能源局座谈会-录音2逐字稿-funasr_v1.md",
   domain="会议纪要", variant="funasr", wiki_tag="会议纪要",
   note="FunASR/Paraformer-large + FSMN-VAD + CT-Transformer 标点 + CAM++ 说话人；中文质量优于 whisper 版",
   sensitivity="sensitive", **RP)
_m(48, "report/会议纪要/2026-09/20260921_T0_能源局座谈会-录音2逐字稿-whisper_v1.md",
   domain="会议纪要", variant="whisper", wiki_tag="会议纪要",
   note="Whisper large-v3-turbo(faster-whisper CPU int8)；同音乱码较多，与 funasr 版并存不互相取代",
   sensitivity="sensitive", **RP)
_m(309, "report/自检报告/2026-09/20260922_T0_CAA二次质检意见-封装件V2.0_v1.md",
   domain="自检报告", **RP)
_m(27, "report/市场分析/2026-09/20260915_T0_汽柴裂解价差预测_v1.md", domain="市场分析",
   wiki_tag="经营分析", **RP)
_m(29, "report/能力清单/2026-09/20260915_T0_jingbo-data-api能力清单_v1.md", domain="能力清单",
   wiki_tag="工具运维", note="英文同名副本已去重进 .archive/dup/", **RP)
_m(30, "report/市场分析/2026-09/20260917_T0_成品油价格展望_v1.md", domain="市场分析",
   wiki_tag="经营分析", **RP)
_m(31, "report/工作总结/2026-09/20260915_T0_中鲁昨日工作总结_v1.md", domain="工作总结", **RP)
_m(32, "report/经营汇报/2026-09/20260920_T0_远期预售交货计划调整汇报_v1.docx", domain="经营汇报",
   sensitivity="sensitive", wiki_status="skip", note="含交货计划，用户 2026-09-22 决定不上 wiki", **RP)
_m(50, "report/周报/2026-W37/20260910_T0_周报W37_v1.md", domain="周报", wiki_tag="经营分析", **RP)
_m(53, "report/自检报告/2026-09/20260922_T0_查重结果-鲁大师端能力对照_v1.md", domain="自检报告",
   note="英文同名副本 #51 已去重进 .archive/dup/", **RP)
_m(45, "report/调价预期/2026-09/20260915_T0_发改委成品油调价预期_v1.md", domain="调价预期",
   wiki_status="skip", note="口径已在 knowledge/口径方法论/发改委成品油调价-周期与口径.md，本篇是应用不重复上 wiki", **RP)
_m(33, "report/自检报告/2026-09/20260915_T0_近24小时活跃会话汇总_v1.html", domain="自检报告", **RP)
_m(43, "report/自检报告/2026-09/20260915_T0_钉钉双向对话能力自检_v1.md", domain="自检报告", **RP)
_m(54, "report/能力清单/2026-09/20260918_T0_鲁大师端skills清单_v1.md", domain="能力清单",
   wiki_tag="工具运维", **RP)
_m(55, "report/能力清单/2026-09/20260922_T0_鲁大师端skills清单_v1.md", domain="能力清单",
   wiki_tag="工具运维", **RP)
_m(56, "report/能力清单/2026-09/20260918_T0_鲁大师端subagents清单_v1.md", domain="能力清单",
   wiki_tag="工具运维", **RP)
_m(57, "report/能力清单/2026-09/20260922_T0_鲁大师端subagents清单_v1.md", domain="能力清单",
   wiki_tag="工具运维", **RP)
_m(34, "report/项目进度/2026-09/20260916_T0_鲁大师项目进度报告_v1.html", domain="项目进度", **RP)

# 血缘：录音2 分片 → 两版逐字稿；html 原件 → 摘要
REFS = [(i, 38, "transcribed_by") for i in range(20, 24)]
REFS += [(i, 48, "transcribed_by") for i in range(20, 24)]
REFS += [(62, 61, "summarized_by")]
REFS += [(i, 36, "transcribed_by") for i in range(5, 20)]  # 录音1 → 空占位（缺口）


def sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main() -> None:
    conn = S.connect()
    cur = conn.cursor()
    cur.execute("SELECT id,path,sha256,root FROM store_files")
    rows = {r["id"]: dict(r) for r in cur.fetchall()}
    print(f"台账 {len(rows)} 行；映射表 {len(MAP)} 条 + 删除 {len(DELETE)} 条")

    missing = [i for i in list(MAP) + list(DELETE) if i not in rows]
    uncovered = sorted(set(rows) - set(MAP) - set(DELETE))
    if missing:
        raise SystemExit(f"映射表引用了不存在的 id: {missing}")
    if uncovered:
        print(f"WARN 未覆盖 {len(uncovered)} 行（保持原状）: {uncovered}")

    # 目标路径冲突预检
    seen: dict[str, int] = {}
    for i, (np, _) in MAP.items():
        if np in seen:
            raise SystemExit(f"目标路径冲突: {np} ← id {seen[np]} 与 {i}")
        seen[np] = i

    moved, deleted, errors = [], [], []
    for i, old_rel in DELETE.items():
        src = os.path.join(WS, old_rel)
        if os.path.isfile(src):
            os.remove(src)
            deleted.append(old_rel)
        cur.execute("DELETE FROM store_files WHERE id=%s", (i,))

    # 阶段一：移动文件（逐个校验 sha256）
    for i, (np, ov) in MAP.items():
        old_rel = rows[i]["path"]
        src = os.path.join(WS, old_rel) if rows[i]["root"] == "workspace" else os.path.join(ROOT, old_rel)
        dst = os.path.join(ROOT, np)
        if not os.path.isfile(src):
            errors.append(f"id={i} 源不存在: {src}")
            continue
        if os.path.exists(dst):
            errors.append(f"id={i} 目标已存在，拒绝覆盖: {np}")
            continue
        before = sha(src)
        if rows[i]["sha256"] and before != rows[i]["sha256"]:
            errors.append(f"id={i} 移动前 sha 与台账不符: {old_rel}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        if sha(dst) != before:
            errors.append(f"id={i} 移动后 sha 变化，严重: {np}")
            continue
        moved.append((i, old_rel, np, ov, before))

    if errors:
        print(f"\n移动阶段 {len(errors)} 个错误（台账未提交）:")
        for e in errors:
            print("  ", e)
        raise SystemExit(1)

    # 阶段二：更新台账（保留 row id 与既有 wiki_page_id 等标注）
    now = time.time()
    for i, old_rel, np, ov, sh in moved:
        fn = os.path.basename(np)
        parsed = S.parse_fname(fn)
        kind = ov.get("kind") or ("archive" if np.startswith(".archive/") else rows[i]["path"].split("/")[1])
        if np.startswith(".archive/"):
            kind = "archive"
        dom = ov.get("domain", "")
        if not dom and kind == "shared" and np.startswith("shared/knowledge/"):
            dom = S.infer_domain(np, "knowledge")
        fdate = parsed.get("fdate") or rows[i].get("fdate") or ""
        cur.execute("SELECT period_fmt,sensitivity FROM store_domains WHERE domain=%s", (dom,))
        r = cur.fetchone()
        pf = r["period_fmt"] if r else "YYYY-MM"
        sens = ov.get("sensitivity") or (r["sensitivity"] if r else "internal")
        cur.execute("""UPDATE store_files SET
            path=%s, root='store', kind=%s, fname=%s, ext=%s, domain=%s, topic=%s, variant=%s,
            fdate=%s, period=%s, sender_id=%s, task_id=%s, version=%s, size=%s, sha256=%s,
            status=%s, source=%s, sensitivity=%s, authority=%s,
            wiki_tag=COALESCE(NULLIF(%s,''), wiki_tag),
            wiki_status=COALESCE(NULLIF(%s,''), wiki_status),
            archived=%s, note=COALESCE(NULLIF(%s,''), note), updated_at=%s
            WHERE id=%s""",
            (np, kind, fn, parsed.get("ext") or fn.rsplit(".", 1)[-1].lower(), dom,
             parsed.get("topic") or "", parsed.get("variant") or "",
             fdate, S.period_of(fdate, pf),
             ov.get("sender_id", parsed.get("sender_id") or ""),
             parsed.get("task_id") if parsed else None,
             parsed.get("version") or 1,
             os.path.getsize(os.path.join(ROOT, np)), sh,
             ov.get("status") or ("archived" if np.startswith(".archive/") else "indexed"),
             ov.get("source") or "manual", sens,
             ov.get("authority") or ("jingbowiki" if kind == "knowledge" else "local"),
             ov.get("wiki_tag", ""), ov.get("wiki_status", ""),
             np.startswith(".archive/"), ov.get("note", ""), now, i))

    for src_id, dst_id, rel in REFS:
        cur.execute("""INSERT INTO store_refs(src_id,dst_id,rel) VALUES(%s,%s,%s)
                       ON CONFLICT DO NOTHING""", (src_id, dst_id, rel))

    # 清理搬空的旧目录
    for d in ("shared/data", "shared/reports", "shared/knowledge"):
        for dp, dn, fn in os.walk(os.path.join(WS, d), topdown=False):
            if not dn and not fn and os.path.abspath(dp) != os.path.join(WS, d):
                os.rmdir(dp)

    cur.execute("SELECT count(*) n FROM store_files")
    total = cur.fetchone()["n"]
    cur.execute("SELECT count(*) n FROM store_refs")
    refs = cur.fetchone()["n"]
    conn.close()
    print(f"\n迁移完成：移动 {len(moved)}，删除 {len(deleted)}，台账 {total} 行，血缘 {refs} 条")
    print("删除:", deleted)


if __name__ == "__main__":
    main()
