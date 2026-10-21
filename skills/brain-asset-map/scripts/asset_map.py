#!/usr/bin/env python3
"""asset_map —— 财务智脑「大脑资产地图」生成器（L1 只读盘点）。

只读取数、只渲染产物，不写任何资产真源（唯一落盘是 --out 产物与 index/ 下的缓存）。
数据源四路，任一路失败自动降级并在产物「体检区」如实标注：
  1) 本地目录树   shared/knowledge|skills|memory|mcp
  2) store 台账   store q --format json（PG 权威索引）
  3) jingboWiki   wiki/stats + wiki/pages（category_path 构建层级）+ wiki/graph
  4) 服务健康     relay 8787 /health、avatar 8791 /health

用法：
  asset_map.py                          # 生成 HTML 到 shared/report/asset-map/
  asset_map.py --format md              # 钉钉 Markdown 摘要（可直接发群）
  asset_map.py --format json --out -    # 结构化数据到 stdout，供二次加工
  asset_map.py --quick                  # 只出统计与顶层分类，不枚举全部页面（秒级）
  asset_map.py --deep                   # 追加 store dup / store doctor 体检（较慢）
  asset_map.py --no-cache               # 忽略缓存强制重新取数

安全：凭据仅从 shared/secrets/jingbowiki.env（600）读取，只用于当次调用；
产物渲染后做泄漏自检，命中 token/密码即拒绝输出。页面正文一律不落产物，
只出标题、路径、分类与元数据（台账 sensitivity=internal）。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

WORKSPACE = os.environ.get("RELAY_WORKSPACE", "/mnt/vol-eltaah12/workspace")
SHARED = os.path.join(WORKSPACE, "shared")
KNOW_ROOT = os.path.join(SHARED, "knowledge")
SKILL_ROOT = os.path.join(SHARED, "skills")
MEM_ROOT = os.path.join(SHARED, "memory")
MCP_ROOT = os.path.join(SHARED, "mcp")
SECRET_ENV = os.path.join(SHARED, "secrets", "jingbowiki.env")
CACHE_PATH = os.path.join(SHARED, "index", "asset-map.cache.json")
OUT_DIR = os.path.join(SHARED, "report", "asset-map")

RELAY_HEALTH = "http://127.0.0.1:8787/health"
AVATAR_HEALTH = "http://127.0.0.1:8791/health"
WIKI_PAGE_SIZE = 500
WIKI_MAX_PAGES = 20000
CACHE_TTL = 600
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "tmp", ".archive"}
STORE_LIMIT = 5000
FOUR_LIB_PARENT = "口径方法论"
FOUR_LIBS = ("知识库", "模型库", "案例库", "规则库")
FOUR_LIB_PREFIXES = tuple(f"knowledge/{FOUR_LIB_PARENT}/{lib}/" for lib in FOUR_LIBS)
GAP_STATUS = ("none", "skip", "failed")


def resolve_sync_scope(exe: str, wiki_env: dict, doctor: dict | None = None) -> dict:
    """本机 wiki 同步的有效范围。

    口径随 store 版本变化，一律按本机实际解析，不硬编码机队级范围：
      dc05a37+  --scope 默认 config → STORE_SYNC_SCOPE / JINGBOWIKI_SYNC_SCOPE
                （fourlib 别名 或 JINGBOWIKI_SYNC_PATHS 自定义前缀），缺省 all
      941aa53   --scope 默认 fourlib（该口径已被 dc05a37 撤销）
      更早      无 --scope，全量同步
    store doctor 的 wiki_sync_scope 是权威值，取到即覆盖上述推断。
    """
    res = {"scope": "all", "prefixes": [], "source": "store 无 --scope（早于 941aa53），全量同步"}
    rc, out, err = _run([exe, "sync", "--help"], 30)
    help_txt = (out or "") + (err or "")
    if rc != 0:
        res["source"] = f"store sync --help 调用失败（rc={rc}），保守按全量计"
    if rc == 0 and "--scope" in help_txt:
        env_scope = (os.environ.get("STORE_SYNC_SCOPE") or "").strip().lower()
        scope_var = "STORE_SYNC_SCOPE"
        if not env_scope:
            env_scope = (wiki_env.get("JINGBOWIKI_SYNC_SCOPE") or "").strip().lower()
            scope_var = "jingbowiki.env:JINGBOWIKI_SYNC_SCOPE"
        env_paths = (os.environ.get("JINGBOWIKI_SYNC_PATHS")
                     or wiki_env.get("JINGBOWIKI_SYNC_PATHS") or "").strip()
        if "{config" in help_txt or "config=" in help_txt:
            if env_scope == "fourlib":
                res = {"scope": "fourlib", "prefixes": list(FOUR_LIB_PREFIXES),
                       "source": f"本机 {scope_var}=fourlib（四库别名，仅本机口径）"}
            elif env_scope == "all":
                res = {"scope": "all", "prefixes": [],
                       "source": f"本机 {scope_var}=all"}
            elif env_paths:
                res = {"scope": "custom",
                       "prefixes": [p.strip() for p in env_paths.split(",") if p.strip()],
                       "source": "本机 JINGBOWIKI_SYNC_PATHS 自定义前缀"}
            else:
                res = {"scope": "all", "prefixes": [],
                       "source": "store --scope 默认 config 且本机未收窄，按全量同步"}
        else:
            res = {"scope": "fourlib", "prefixes": list(FOUR_LIB_PREFIXES),
                   "source": "store --scope 默认 fourlib（941aa53 口径，已被 dc05a37 撤销）"}
    ws = (doctor or {}).get("wiki_sync_scope")
    if isinstance(ws, dict) and ws.get("scope"):
        res = {"scope": str(ws["scope"]),
               "prefixes": [str(p) for p in (ws.get("prefixes") or [])],
               "source": "store doctor 报告的 wiki_sync_scope（权威）"}
    if res["scope"] == "all":
        res["prefixes"] = []
    return res


def path_in_scope(path, prefixes) -> bool:
    p = str(path or "")
    for pre in prefixes or ():
        if p.startswith(pre) or p.startswith("store:" + pre):
            return True
    return False


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _http(url: str, timeout: int = 30, headers: dict | None = None,
          data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"


# ---------------------------------------------------------------- 本地目录树

def scan_tree(root: str, max_depth: int = 3) -> dict:
    node = {"name": os.path.basename(root) or root, "path": root,
            "dirs": [], "files": 0, "bytes": 0, "bak": 0, "depth": 0}
    if not os.path.isdir(root):
        node["missing"] = True
        return node
    stack = [(root, node, 0)]
    while stack:
        cur, parent, depth = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: (not e.is_dir(), e.name))
        except OSError:
            parent["unreadable"] = True
            continue
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        child = {"name": e.name, "path": e.path, "dirs": [],
                                 "files": 0, "bytes": 0, "bak": 0, "depth": depth + 1}
                        parent["dirs"].append(child)
                        stack.append((e.path, child, depth + 1))
                    else:
                        parent["dirs"].append({"name": e.name, "path": e.path,
                                               "dirs": [], "files": 0, "bytes": 0, "bak": 0,
                                               "depth": depth + 1, "truncated": True})
                elif e.is_file(follow_symlinks=True):
                    parent["files"] += 1
                    parent["bytes"] += e.stat(follow_symlinks=True).st_size
                    if ".bak" in e.name:
                        parent["bak"] += 1
            except OSError:
                continue
    _rollup(node)
    return node


def _rollup(node: dict) -> tuple[int, int]:
    f, b, k = node.get("files", 0), node.get("bytes", 0), node.get("bak", 0)
    for c in node.get("dirs", []):
        cf, cb, ck = _rollup(c)
        f += cf
        b += cb
        k += ck
    node["files"], node["bytes"], node["bak"] = f, b, k
    return f, b, k


def scan_skills() -> list[dict]:
    out = []
    if not os.path.isdir(SKILL_ROOT):
        return out
    for name in sorted(os.listdir(SKILL_ROOT)):
        d = os.path.join(SKILL_ROOT, name)
        if not os.path.isdir(d):
            continue
        link = os.path.islink(d)
        target = os.path.realpath(d) if link else ""
        desc, triggers = "", ""
        sp = os.path.join(d, "SKILL.md")
        nfiles = 0
        if os.path.isfile(sp):
            try:
                with open(sp, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                m = re.search(r"^description:\s*(.+)$", txt, re.M)
                if m:
                    desc = m.group(1).strip()
                t = re.search(r"触发词[：:]\s*(.+)", txt)
                if t:
                    triggers = t.group(1).strip()[:200]
            except OSError:
                pass
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            nfiles += len([f for f in filenames if ".bak-provision-" not in f])
        out.append({"name": name, "description": desc, "triggers": triggers,
                    "files": nfiles, "symlink": link, "target": target,
                    "shared": link and not target.startswith(SKILL_ROOT),
                    "has_skill_md": os.path.isfile(sp)})
    return out


def scan_flat(root: str, suffix: str = "") -> dict:
    res = {"root": root, "exists": os.path.isdir(root), "items": []}
    if not res["exists"]:
        return res
    for fn in sorted(os.listdir(root)):
        p = os.path.join(root, fn)
        if not os.path.isfile(p) or (suffix and not fn.endswith(suffix)):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        res["items"].append({"name": fn[:-len(suffix)] if suffix and fn.endswith(suffix) else fn,
                             "file": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
    return res


# ---------------------------------------------------------------- store 台账

def store_ledger(deep: bool, errors: list) -> dict:
    exe = shutil.which("store") or "/usr/local/bin/store"
    res = {"available": False, "records": [], "by": {}, "gaps": [],
           "gaps_in": [], "gaps_out": [], "sync_scope": {}, "deep": {}}
    if not os.path.exists(exe):
        errors.append("store CLI 不可用，台账维度跳过")
        return res
    rc, out, err = _run([exe, "q", "--format", "json", "--limit", str(STORE_LIMIT)], 180)
    if rc != 0:
        errors.append(f"store q 失败 rc={rc}: {(err or out).strip()[:200]}")
        return res
    try:
        d = json.loads(out)
    except ValueError as e:
        errors.append(f"store q 输出非 JSON: {e}")
        return res
    rows = d if isinstance(d, list) else (d.get("rows") or d.get("items") or d.get("data") or [])
    res["available"] = True
    res["records"] = rows
    for f in ("kind", "domain", "status", "wiki_status", "authority", "sensitivity"):
        res["by"][f] = Counter(str(r.get(f) or "-") for r in rows).most_common()
    if deep:
        for key, cmd in (("dup", [exe, "dup"]), ("doctor", [exe, "doctor"])):
            rc2, out2, err2 = _run(cmd, 300)
            raw = (out2 or err2).strip()
            entry = {"rc": rc2}
            try:
                entry["data"] = _slim_deep(json.loads(raw))
            except ValueError:
                entry["text"] = raw[:20000]
            res["deep"][key] = entry
    doctor_data = ((res["deep"].get("doctor") or {}).get("data")) or {}
    scope = resolve_sync_scope(exe, _load_wiki_env([]), doctor_data)
    res["sync_scope"] = scope
    prefixes = scope.get("prefixes") or []
    narrowed = bool(prefixes)
    gaps = []
    for r in rows:
        if str(r.get("wiki_status") or "") not in GAP_STATUS:
            continue
        inside = (not narrowed) or path_in_scope(r.get("path"), prefixes)
        gaps.append({"id": r.get("id"), "path": r.get("path"), "domain": r.get("domain"),
                     "wiki_status": r.get("wiki_status"), "task_id": r.get("task_id"),
                     "in_scope": inside, "note": (r.get("note") or "")[:160]})
    res["gaps"] = gaps
    res["gaps_in"] = [g for g in gaps if g["in_scope"]]
    res["gaps_out"] = [g for g in gaps if not g["in_scope"]]
    return res


def _slim_deep(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, list):
            out[k] = v[:20]
            out[k + "__total"] = len(v)
        elif isinstance(v, dict):
            sub = {}
            for kk, vv in v.items():
                if isinstance(vv, list):
                    sub[kk] = vv[:20]
                    sub[kk + "__total"] = len(vv)
                else:
                    sub[kk] = vv
            out[k] = sub
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- jingboWiki

def _load_wiki_env(errors: list) -> dict:
    cfg = {}
    if not os.path.isfile(SECRET_ENV):
        errors.append(f"未找到 {SECRET_ENV}，Wiki 维度跳过")
        return cfg
    try:
        mode = os.stat(SECRET_ENV).st_mode & 0o777
        if mode != 0o600:
            errors.append(f"jingbowiki.env 权限为 {oct(mode)}，建议 600")
        with open(SECRET_ENV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as e:
        errors.append(f"读取 jingbowiki.env 失败: {e}")
    return cfg


def wiki_assets(quick: bool, errors: list) -> dict:
    res = {"available": False, "kb_id": "", "kb_name": "", "stats": {},
           "tree": {}, "top_linked": [], "type_counter": [], "pages_seen": 0,
           "graph_meta": {}, "recent": []}
    cfg = _load_wiki_env(errors)
    base, email, pwd = cfg.get("JINGBOWIKI_BASE"), cfg.get("JINGBOWIKI_EMAIL"), cfg.get("JINGBOWIKI_PASSWORD")
    kb = os.environ.get("STORE_KB_ID") or cfg.get("JINGBOWIKI_KB_ID") or ""
    res["kb_id"], res["kb_name"] = kb, cfg.get("JINGBOWIKI_KB_NAME", "")
    if not (base and email and pwd and kb):
        errors.append("jingboWiki 配置不完整（BASE/EMAIL/PASSWORD/KB_ID），Wiki 维度跳过")
        return res
    base = base.rstrip("/")
    secret_values = [v for v in (pwd, email) if v]
    try:
        _, body = _http(f"{base}/auth/login", 25, {"Content-Type": "application/json"},
                        json.dumps({"email": email, "password": pwd}).encode(), "POST")
        token = (json.loads(body) or {}).get("token") or ""
    except Exception as e:
        errors.append(f"jingboWiki 登录失败: {type(e).__name__} {str(e)[:120]}")
        return res
    if not token:
        errors.append("jingboWiki 登录未返回 token")
        return res
    secret_values.append(token)
    auth = {"Authorization": f"Bearer {token}"}

    def get(path: str, timeout: int = 60):
        return json.loads(_http(f"{base}/{path.lstrip('/')}", timeout, auth)[1])

    try:
        res["stats"] = get(f"knowledgebase/{kb}/wiki/stats", 30)
        res["available"] = True
    except Exception as e:
        errors.append(f"wiki/stats 失败: {type(e).__name__} {str(e)[:120]}")
        return res
    try:
        g = get(f"knowledgebase/{kb}/wiki/graph", 60)
        res["graph_meta"] = g.get("meta") or {}
        nodes = g.get("nodes") or []
        res["graph_nodes"] = len(nodes)
        res["graph_edges"] = len(g.get("edges") or [])
    except Exception as e:
        errors.append(f"wiki/graph 失败（不影响主视图）: {type(e).__name__} {str(e)[:100]}")
    if quick:
        res["quick"] = True
        return res

    tree: dict = {}
    types: Counter = Counter()
    linked: list = []
    seen = 0
    try:
        page = 1
        while seen < WIKI_MAX_PAGES:
            d = get(f"knowledgebase/{kb}/wiki/pages?page={page}&page_size={WIKI_PAGE_SIZE}", 90)
            pages = d.get("pages") or []
            if not pages:
                break
            for p in pages:
                seen += 1
                types[str(p.get("page_type") or "-")] += 1
                cat = p.get("category_path") or []
                if isinstance(cat, str):
                    cat = [c for c in re.split(r"[/>|]", cat) if c.strip()]
                parts = [str(c).strip() or "(未分类)" for c in cat] or ["(未分类)"]
                node = tree
                for part in parts:
                    nxt = node.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {"__pages": 0}
                        node[part] = nxt
                    node = nxt
                node["__pages"] = int(node.get("__pages") or 0) + 1
                inl = p.get("in_links") or []
                linked.append({"slug": p.get("slug"), "title": p.get("title"),
                               "type": p.get("page_type"), "in_links": len(inl) if isinstance(inl, list) else 0,
                               "depth": p.get("depth"), "updated_at": str(p.get("updated_at") or "")[:19],
                               "wiki_path": p.get("wiki_path")})
            total_pages = int(d.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
    except Exception as e:
        errors.append(f"wiki/pages 拉取中断（已取 {seen} 页）: {type(e).__name__} {str(e)[:120]}")
    res["pages_seen"] = seen
    res["type_counter"] = types.most_common()
    res["tree"] = tree
    linked.sort(key=lambda x: (-x["in_links"], x["title"] or ""))
    res["top_linked"] = linked[:20]
    res["recent"] = sorted(linked, key=lambda x: x["updated_at"], reverse=True)[:15]
    res["_secret_values"] = secret_values
    return res


def _tree_counts(node: dict) -> tuple[int, int]:
    pages = node.get("__pages", 0)
    subs = 0
    for k, v in node.items():
        if k in ("__pages", "__sub") or not isinstance(v, dict):
            continue
        p2, s2 = _tree_counts(v)
        pages += p2
        subs += 1 + s2
    node["__total"], node["__subdirs"] = pages, subs
    return pages, subs


# ---------------------------------------------------------------- 服务健康

def service_health(errors: list) -> dict:
    out = {}
    for key, url in (("relay", RELAY_HEALTH), ("avatar", AVATAR_HEALTH)):
        try:
            _, body = _http(url, 8)
            out[key] = json.loads(body)
        except Exception as e:
            out[key] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
            errors.append(f"{key} /health 不可达: {out[key]['error']}")
    return out


# ---------------------------------------------------------------- 汇总

def collect(quick: bool, deep: bool, errors: list) -> dict:
    data = {"generated_at": _now(), "host": os.uname().nodename,
            "workspace": WORKSPACE, "quick": quick, "deep": deep, "errors": errors}
    data["health"] = service_health(errors)
    t0 = time.time()
    data["knowledge"] = scan_tree(KNOW_ROOT, 3)
    data["skills"] = scan_skills()
    data["memory"] = scan_flat(MEM_ROOT, ".md")
    data["mcp"] = scan_flat(MCP_ROOT, ".json")
    data["local_scan_sec"] = round(time.time() - t0, 2)
    t0 = time.time()
    data["store"] = store_ledger(deep, errors)
    data["store_sec"] = round(time.time() - t0, 2)
    t0 = time.time()
    data["wiki"] = wiki_assets(quick, errors)
    data["wiki_sec"] = round(time.time() - t0, 2)
    data["domains"] = align_domains(data)
    return data


def align_domains(data: dict) -> list[dict]:
    local = {d["name"] for d in data["knowledge"].get("dirs", [])}
    ledger = {k for k, _ in (data["store"].get("by", {}).get("domain") or []) if k != "-"}
    wiki_top = {k for k in (data["wiki"].get("tree") or {}) if not k.startswith("__")}
    rows = []
    for name in sorted(local | ledger | wiki_top):
        rows.append({"name": name, "local": name in local,
                     "ledger": name in ledger, "wiki": name in wiki_top})
    return rows


def cache_load(ttl: int):
    try:
        st = os.stat(CACHE_PATH)
        if time.time() - st.st_mtime > ttl:
            return None
        with open(CACHE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        d["cached"] = True
        d["cache_age_sec"] = int(time.time() - st.st_mtime)
        return d
    except (OSError, ValueError):
        return None


def cache_save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        slim = {k: v for k, v in data.items() if k != "_secret_values"}
        if isinstance(slim.get("wiki"), dict):
            slim["wiki"] = {k: v for k, v in slim["wiki"].items() if k != "_secret_values"}
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(slim, fh, ensure_ascii=False)
        os.chmod(CACHE_PATH, 0o600)
    except OSError:
        pass


def leak_check(text: str, data: dict) -> list[str]:
    hits = []
    vals = list(data.get("_secret_values") or [])
    w = data.get("wiki") or {}
    vals += list(w.get("_secret_values") or [])
    cfg = _load_wiki_env([])
    vals += [cfg.get("JINGBOWIKI_PASSWORD", ""), cfg.get("JINGBOWIKI_EMAIL", "")]
    for v in {x for x in vals if x and len(x) >= 6}:
        if v in text:
            hits.append(v[:4] + "***")
    return hits


# ---------------------------------------------------------------- 渲染：HTML

CSS = """
:root{--blue:#0b3d91;--blue2:#1668dc;--ink:#1f2733;--mut:#6b7684;--line:#e3e8ef;--bg:#f5f7fa;
--ok:#12805c;--warn:#b26a00;--bad:#c0392b;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
header{background:linear-gradient(120deg,var(--blue),var(--blue2));color:#fff;
border-radius:14px;padding:26px 28px;margin-bottom:22px}
header h1{margin:0 0 6px;font-size:25px;letter-spacing:.5px}
header .meta{opacity:.9;font-size:12.5px}
h2{font-size:17px;margin:30px 0 12px;padding-left:10px;border-left:4px solid var(--blue2)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--mut);font-size:12px;margin-bottom:6px}
.card .v{font-size:23px;font-weight:650;color:var(--blue)}
.card .s{color:var(--mut);font-size:11.5px;margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#eef2f8;color:#33415c;font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:none}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;font-weight:600}
.p-ok{background:#e3f5ee;color:var(--ok)}.p-warn{background:#fdf1dc;color:var(--warn)}
.p-bad{background:#fbe6e3;color:var(--bad)}.p-mut{background:#eef1f5;color:var(--mut)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:9px 13px;margin-bottom:8px}
details>summary{cursor:pointer;font-weight:600;color:#26364f;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"▸";color:var(--blue2);margin-right:7px}
details[open]>summary:before{content:"▾"}
.sub{margin:8px 0 2px 14px;padding-left:12px;border-left:2px solid var(--line)}
.mut{color:var(--mut)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.note{background:#fffdf5;border:1px solid #f0e3b8;border-radius:10px;padding:11px 14px;
font-size:12.8px;color:#6a5a1e;margin:10px 0}
.err{background:#fff6f5;border:1px solid #f2cfca;border-radius:10px;padding:11px 14px;
font-size:12.8px;color:#8d2b1f;margin:10px 0}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
color:var(--mut);font-size:11.8px}
.bar{height:7px;background:#eef1f5;border-radius:5px;overflow:hidden;min-width:70px}
.bar>i{display:block;height:100%;background:var(--blue2)}
@media print{body{background:#fff}.wrap{max-width:none;padding:0}details{page-break-inside:avoid}
header{border-radius:0}}
"""


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def pill(kind: str, text: str) -> str:
    return f'<span class="pill p-{kind}">{e(text)}</span>'


def _deep_summary(key: str, dd: dict) -> str:
    d = dd.get("data")
    if not isinstance(d, dict):
        try:
            d = json.loads(dd.get("text") or "")
        except ValueError:
            return ""
    if not isinstance(d, dict):
        return ""

    def cnt(container: dict, k: str) -> int:
        if k + "__total" in container:
            return int(container[k + "__total"])
        v = container.get(k)
        if isinstance(v, list):
            return len(v)
        if isinstance(v, dict):
            return sum(len(x) if isinstance(x, list) else 1 for x in v.values())
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    rows = []
    if key == "doctor":
        for label, field, bad in (
                ("台账行数（PG）", "db_rows", False),
                ("磁盘文件数", "disk_files", False),
                ("磁盘孤儿（未登记台账）", "orphan_disk_count", True),
                ("台账孤儿（磁盘已无）", "orphan_db_count", True),
                ("问题总数", "problem_count", True)):
            if field not in d:
                continue
            v = d[field]
            badge = pill("bad" if v else "ok", "需处理" if v else "干净") if bad else ""
            rows.append(f'<tr><td>{e(label)}</td><td class="n">{e(v)}</td><td>{badge}</td></tr>')
        rows.append(f'<tr><td>总体结论</td><td class="n">-</td>'
                    f'<td>{pill("ok" if d.get("ok") else "bad", "ok" if d.get("ok") else "not ok")}</td></tr>')
        vio = d.get("violations") or {}
        if isinstance(vio, dict) and vio:
            parts = []
            for k, label in (("bak_in_production_dirs", "生产目录残留 .bak"),
                             ("bad_chars", "文件名非法字符")):
                if k in vio:
                    parts.append(f'{label} {cnt(vio, k)}')
            if vio.get("bad_name_bytes"):
                parts.append(f'违规命名占用 {_human(int(vio["bad_name_bytes"]))}')
            bt = vio.get("bad_name_by_top")
            if isinstance(bt, dict) and bt:
                parts.append('按顶层 ' + '、'.join(f'{k} {v}' for k, v in list(bt.items())[:4]))
            vtotal = vio.get("count")
            if vtotal is None:
                vtotal = sum(cnt(vio, k) for k in vio
                             if not k.endswith("__total") and isinstance(vio.get(k), list))
            rows.append(f'<tr><td>命名/位置违规</td><td class="n">{e(vtotal)}</td>'
                        f'<td class="mut">{e("；".join(parts))}</td></tr>')
        od = d.get("orphan_disk")
        if isinstance(od, list) and od:
            rows.append(f'<tr><td>磁盘孤儿样例</td><td class="n">{e(cnt(d, "orphan_disk"))}</td>'
                        f'<td class="mut mono">{e("；".join(str(x) for x in od[:5]))}</td></tr>')
        doms = d.get("domains") or {}
        if isinstance(doms, dict):
            empty = [k for k, v in doms.items() if not k.endswith("__total") and not v]
            if empty:
                rows.append(f'<tr><td>已定义但 0 条的业务域</td><td class="n">{len(empty)}</td>'
                            f'<td class="mut">{e("、".join(empty[:14]))}</td></tr>')
        bk = d.get("by_kind") or {}
        if isinstance(bk, dict) and bk:
            kt = "、".join(f'{k} {v.get("files")} 个/{_human(int(v.get("bytes") or 0))}'
                          for k, v in list(bk.items())[:5] if isinstance(v, dict))
            rows.append(f'<tr><td>按 kind 分布</td><td class="n">{len(bk)}</td><td class="mut">{e(kt)}</td></tr>')
        fl = d.get("four_lib")
        if isinstance(fl, dict) and fl:
            if int(fl.get("rows") or 0):
                bl = fl.get("by_lib") or {}
                rows.append(f'<tr><td>四库正本（本机同步范围专项）</td>'
                            f'<td class="n">{e(fl.get("rows"))}</td>'
                            f'<td class="mut">现行 {e(fl.get("canonical_active"))} / '
                            f'归档 {e(fl.get("canonical_archived"))} / '
                            f'旧顶层残留 {e(fl.get("stale_top_level_rows"))} / '
                            f'问题 {e(fl.get("problems"))}'
                            + ('；' + e("、".join(f'{k} {v}' for k, v in bl.items())) if bl else '')
                            + '</td></tr>')
            else:
                rows.append('<tr><td>四库正本</td><td class="n">0</td>'
                            '<td class="mut">本机无 knowledge/口径方法论/{知识库,模型库,案例库,规则库}/ 目录；'
                            '四库是其他机队成员的私有口径，本机不适用</td></tr>')
        ws = d.get("wiki_sync_scope")
        if isinstance(ws, dict) and ws:
            rows.append(f'<tr><td>本机 wiki 同步范围（doctor 权威）</td>'
                        f'<td class="n">{e(ws.get("scope"))}</td>'
                        f'<td class="mut mono">{e("、".join(ws.get("prefixes") or []) or "（全量，无前缀收窄）")}</td></tr>')
    elif key == "dup":
        n = int(d.get("duplicate_groups") or 0)
        rows.append(f'<tr><td>重复组（sha256 相同）</td><td class="n">{e(n)}</td>'
                    f'<td>{pill("ok" if not n else "warn", "无重复" if not n else "有重复")}</td></tr>')
        rows.append(f'<tr><td>可回收空间</td><td class="n">{_human(int(d.get("wasted_bytes") or 0))}</td><td></td></tr>')
    if not rows:
        return ""
    return ('<table><tr><th>指标</th><th class="n">值</th><th>说明</th></tr>'
            + "".join(rows) + '</table>')


def render_tree(node: dict, depth: int = 0) -> str:
    items = [(k, v) for k, v in node.items()
             if not k.startswith("__") and isinstance(v, dict)]
    items.sort(key=lambda kv: (-kv[1].get("__total", 0), kv[0]))
    out = []
    for name, child in items:
        total = child.get("__total", 0)
        own = child.get("__pages", 0)
        subs = len([k for k in child if not k.startswith("__") and isinstance(child[k], dict)])
        label = (f'{e(name)} <span class="mut">· {total} 页'
                 + (f'（本层 {own}）' if subs and own else '') + '</span>')
        if subs and depth < 4:
            out.append(f'<details{" open" if depth == 0 else ""}><summary>{label}</summary>'
                       f'<div class="sub">{render_tree(child, depth + 1)}</div></details>')
        else:
            out.append(f'<div class="sub" style="border:none;margin:3px 0 3px 2px">{label}</div>')
    return "".join(out) or '<div class="mut">（无分类数据）</div>'


def render_local_tree(node: dict, depth: int = 0) -> str:
    if node.get("missing"):
        return f'<div class="mut">目录不存在：{e(node.get("path"))}</div>'
    out = []
    dirs = sorted(node.get("dirs", []), key=lambda d: (-d.get("files", 0), d["name"]))
    for d in dirs:
        bak = d.get("bak") or 0
        bak_txt = f' · {pill("warn", "含 " + str(bak) + " 备份件")}' if bak else ""
        trunc = ' · <span class="pill p-mut">已截断</span>' if d.get("truncated") else ""
        label = (f'{e(d["name"])} <span class="mut">· {d.get("files", 0)} 文件 · '
                 f'{_human(d.get("bytes", 0))}</span>{bak_txt}{trunc}')
        sub = [x for x in d.get("dirs", []) if isinstance(x, dict)]
        if sub and depth < 3:
            out.append(f'<details><summary>{label}</summary>'
                       f'<div class="sub">{render_local_tree(d, depth + 1)}</div></details>')
        else:
            out.append(f'<div class="sub" style="border:none;margin:3px 0 3px 2px">{label}</div>')
    if depth == 0:
        out.append(f'<div class="mut" style="margin-top:6px">根目录直属文件 '
                   f'{node.get("files", 0) - sum(d.get("files", 0) for d in dirs)} 个 · '
                   f'合计 {_human(node.get("bytes", 0))}</div>')
    return "".join(out) or '<div class="mut">（空）</div>'


def _gap_table(gaps: list, limit: int = 200) -> str:
    if not gaps:
        return '<div class="sub mut">无。</div>'
    rows = ['<div class="sub"><table><tr><th class="n">ID</th><th>路径</th>'
            '<th>分类</th><th>状态</th><th>任务</th><th>备注</th></tr>']
    for g in gaps[:limit]:
        rows.append(f'<tr><td class="n">{e(g.get("id"))}</td>'
                    f'<td class="mono">{e(g.get("path"))}</td><td>{e(g.get("domain"))}</td>'
                    f'<td>{pill("warn", g.get("wiki_status"))}</td>'
                    f'<td>{e(g.get("task_id"))}</td><td class="mut">{e(g.get("note"))}</td></tr>')
    rows.append('</table>')
    if len(gaps) > limit:
        rows.append(f'<div class="mut">仅显示前 {limit} 条，共 {len(gaps)} 条。</div>')
    rows.append('</div>')
    return "".join(rows)


def render_html(d: dict) -> str:
    w, st, kn = d.get("wiki") or {}, d.get("store") or {}, d.get("knowledge") or {}
    stats = w.get("stats") or {}
    skills = d.get("skills") or []
    mem, mcp = d.get("memory") or {}, d.get("mcp") or {}
    relay = (d.get("health") or {}).get("relay") or {}
    errs = d.get("errors") or []
    recs = st.get("records") or []
    pbt = stats.get("pages_by_type") or {}
    shared_n = sum(1 for s in skills if s.get("shared"))

    P = []
    kinds = st.get("by", {}).get("kind") or []
    kind_txt = ("kind: " + ", ".join(f'{k} {v}' for k, v in kinds[:3])) if recs else "台账不可用"
    pbt_txt = " / ".join(f'{k} {v}' for k, v in list(pbt.items())[:4]) or "库不可达"
    kn_dirs = len([x for x in kn.get("dirs", []) if isinstance(x, dict)])
    kn_bak = kn.get("bak") or 0
    kn_txt = (f'{kn_dirs} 个分类 · {_human(kn.get("bytes", 0))}'
              + (f' · 含 {kn_bak} 备份件' if kn_bak else ''))
    cache_txt = (f' · <b>缓存数据</b>（{d.get("cache_age_sec")}s 前）' if d.get("cached") else "")
    P.append(f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>大脑资产地图 · {e(d.get("generated_at"))}</title>'
             f'<style>{CSS}</style></head><body><div class="wrap">')
    P.append(f'<header><h1>财务智脑 · 大脑资产地图</h1>'
             f'<div class="meta">生成时间 {e(d.get("generated_at"))} · 主机 {e(d.get("host"))} · '
             f'relay {e(str(relay.get("version") or "-")[:12])} · db {e(relay.get("db") or "-")} · '
             f'模式 {"quick（仅统计）" if d.get("quick") else "full"}'
             f'{" · 含深度体检" if d.get("deep") else ""}{cache_txt}</div></header>')

    if errs:
        P.append('<div class="err"><b>取数降级提示（' + str(len(errs)) + ' 项）</b><ul style="margin:6px 0 0 18px">'
                 + "".join(f'<li>{e(x)}</li>' for x in errs) + '</ul></div>')

    P.append('<h2>一、资产总览</h2><div class="cards">')
    cards = [
        ("Wiki 知识页", stats.get("total_pages", 0), pbt_txt),
        ("Wiki 链接", stats.get("total_links", 0), f'孤儿页 {stats.get("orphan_count", "-")}'),
        ("本地知识文件", kn.get("files", 0), kn_txt),
        ("store 台账", len(recs), kind_txt),
        ("技能", len(skills), f'其中系统级共享 {shared_n} 个'),
        ("语料库 memory", len(mem.get("items") or []),
         "未启用（目录不存在）" if not mem.get("exists") else "shared/memory/*.md"),
        ("MCP 配置", len(mcp.get("items") or []),
         "未配置" if not (mcp.get("items") or []) else "shared/mcp/*.json"),
        ("待解析任务", stats.get("pending_tasks", 0), "Wiki 后台解析队列"),
    ]
    for k, v, s in cards:
        P.append(f'<div class="card"><div class="k">{e(k)}</div><div class="v">{e(v)}</div>'
                 f'<div class="s">{e(s)}</div></div>')
    P.append('</div>')

    P.append('<h2>二、知识层级结构</h2>')
    P.append('<div class="note">层级来自 Wiki 页面自带的 <span class="mono">category_path</span> / '
             '<span class="mono">wiki_path</span>（服务端无 tree 端点，由页面元数据实时构建）；'
             '本地视图来自 <span class="mono">shared/knowledge/</span> 目录树。'
             '两者口径不同，对照见「四、三视图对齐」。</div>')
    if w.get("available"):
        tree = w.get("tree") or {}
        _tree_counts_wrap(tree)
        P.append(f'<details open><summary>Wiki 分类树 · 共 {e(stats.get("total_pages", 0))} 页'
                 f'（本次枚举 {e(w.get("pages_seen", 0))} 页）</summary>'
                 f'<div class="sub">{render_tree(tree)}</div></details>')
        if w.get("quick"):
            P.append('<div class="note">quick 模式未枚举页面，分类树为空；去掉 --quick 可得完整层级。</div>')
    else:
        P.append('<div class="err">Wiki 库不可达，层级视图跳过。</div>')
    P.append(f'<details><summary>本地知识目录树 · {e(kn.get("files", 0))} 文件 / '
             f'{_human(kn.get("bytes", 0))}（展开到 3 层）</summary>'
             f'<div class="sub">{render_local_tree(kn)}</div></details>')

    P.append('<h2>三、能力矩阵（技能）</h2>')
    if skills:
        P.append('<table><tr><th>技能</th><th>能力说明</th><th class="n">文件</th>'
                 '<th>来源</th></tr>')
        for s in skills:
            src = ('<span class="pill p-ok">系统级共享</span> <span class="mut mono">'
                   + e(s.get("target") or "") + '</span>') if s.get("shared") else \
                  ('<span class="pill p-mut">本地共享区</span>')
            desc = s.get("description") or ("（无 SKILL.md）" if not s.get("has_skill_md") else "（无 description）")
            trig = f'<div class="mut" style="margin-top:3px">触发词：{e(s["triggers"])}</div>' if s.get("triggers") else ""
            P.append(f'<tr><td class="mono">{e(s["name"])}</td>'
                     f'<td>{e(desc[:300])}{trig}</td>'
                     f'<td class="n">{e(s.get("files", 0))}</td><td>{src}</td></tr>')
        P.append('</table>')
    else:
        P.append('<div class="err">未发现技能。</div>')

    P.append('<h2>四、三视图对齐（本地 / 台账 / Wiki）</h2>')
    doms = d.get("domains") or []
    both = sum(1 for r in doms if sum(1 for k in ("local", "ledger", "wiki") if r[k]) == 3)
    P.append('<div class="note">本地目录用<b>业务口径</b>命名（财务制度、税务政策…），'
             'Wiki 分类由服务端<b>自动聚类</b>生成（财税与金融、组织与机构…），'
             '两套命名体系尚未建立映射，因此「三处齐备」通常偏低'
             f'（本次 {both}/{len(doms)}）。这不是数据缺失，而是<b>分类口径未统一</b>的治理缺口；'
             '台账 domain 与本地目录同源，故两者对齐率应接近 100%。</div>')
    if doms:
        P.append('<table><tr><th>分类名</th><th>本地目录</th><th>store 台账</th>'
                 '<th>Wiki 分类</th><th>状态</th></tr>')
        for r in doms:
            marks = "".join(f'<td>{pill("ok" if r[k] else "mut", "有" if r[k] else "无")}</td>'
                            for k in ("local", "ledger", "wiki"))
            n = sum(1 for k in ("local", "ledger", "wiki") if r[k])
            badge = pill("ok", "三处齐备") if n == 3 else (
                pill("warn", "两处") if n == 2 else pill("bad", "仅单处"))
            P.append(f'<tr><td>{e(r["name"])}</td>{marks}<td>{badge}</td></tr>')
        P.append('</table>')

    P.append('<h2>五、语料库 memory 与 MCP</h2>')
    if mem.get("exists") and mem.get("items"):
        P.append('<table><tr><th>语料</th><th class="n">大小</th><th>修改时间</th></tr>')
        for it in mem["items"]:
            P.append(f'<tr><td class="mono">{e(it["name"])}</td><td class="n">{_human(it["size"])}</td>'
                     f'<td>{e(datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d %H:%M"))}</td></tr>')
        P.append('</table>')
    else:
        P.append('<div class="note"><b>语料库 memory 当前为空</b>'
                 + ('（目录不存在）' if not mem.get("exists") else '（目录存在但无 .md 文件）')
                 + '。该维度用于全员 agent 的 AGENTS.md 注入，写入后下一个任务即生效；'
                   'L1 为只读盘点，如需启用请走 L2 或看板 <span class="mono">POST /dashboard/shared/memory</span>。</div>')
    if mcp.get("items"):
        P.append('<table><tr><th>MCP</th><th class="n">大小</th></tr>'
                 + "".join(f'<tr><td class="mono">{e(i["name"])}</td><td class="n">{_human(i["size"])}</td></tr>'
                           for i in mcp["items"]) + '</table>')
    else:
        P.append('<div class="note"><b>MCP 未配置</b>：'
                 '<span class="mono">shared/mcp/</span> 无 JSON，provision 体检长期 WARN 项。</div>')

    P.append('<h2>六、体检区</h2>')
    if st.get("available"):
        for f, title in (("wiki_status", "双写状态"), ("status", "台账状态"),
                         ("authority", "权威源"), ("sensitivity", "敏感级")):
            rows = st.get("by", {}).get(f) or []
            if not rows:
                continue
            P.append(f'<div style="margin:10px 0 4px"><b>{e(title)}</b></div><table>'
                     f'<tr><th>值</th><th class="n">条数</th><th style="width:40%">占比</th></tr>')
            tot = sum(c for _, c in rows) or 1
            for k, c in rows:
                badge = ('p-bad' if f == "wiki_status" and k in ("none", "skip", "failed") else
                         'p-warn' if f == "wiki_status" and k == "accepted" else 'p-ok')
                P.append(f'<tr><td><span class="pill {badge}">{e(k)}</span></td>'
                         f'<td class="n">{c}</td><td><div class="bar">'
                         f'<i style="width:{max(2, round(c * 100 / tot))}%"></i></div></td></tr>')
            P.append('</table>')
        gaps = st.get("gaps") or []
        gin = st.get("gaps_in") or []
        gout = st.get("gaps_out") or []
        sc = st.get("sync_scope") or {}
        prefixes = sc.get("prefixes") or []
        if prefixes:
            P.append('<div class="note">本机 wiki 同步范围<b>已收窄</b>：'
                     f'<span class="mono">scope={e(sc.get("scope"))}</span>，前缀 '
                     f'<span class="mono">{e("、".join(prefixes[:6]))}</span>'
                     f'（判定依据：{e(sc.get("source"))}）。因此范围外的 <b>{len(gout)} 条</b> '
                     '<span class="mono">wiki_status=none/skip</span> 属<b>策略性不同步</b>，不是故障；'
                     f'真正要处理的缺口是范围内的 <b>{len(gin)} 条</b>。'
                     '该范围是<b>每机私有配置</b>，不代表机队统一口径。</div>')
        else:
            P.append('<div class="note">本机 wiki 同步为<b>全量</b>'
                     f'（判定依据：{e(sc.get("source") or "store 无范围收窄")}），'
                     f'下列 {len(gaps)} 条 <span class="mono">wiki_status=none/skip/failed</span> '
                     '全部按缺口计。</div>')
        P.append(f'<details{" open" if gin else ""}><summary>双写缺口 · 同步范围内 '
                 f'{len(gin)} 条（需处理）</summary>{_gap_table(gin)}</details>')
        if prefixes:
            P.append(f'<details><summary>同步范围外 {len(gout)} 条（策略性不同步）</summary>'
                     f'{_gap_table(gout)}</details>')
        for key in ("dup", "doctor"):
            dd = (st.get("deep") or {}).get(key)
            if not dd:
                continue
            summ = _deep_summary(key, dd)
            if summ:
                P.append(f'<details open><summary>store {e(key)} 体检'
                         f'（rc={e(dd.get("rc"))}）</summary><div class="sub">{summ}</div></details>')
            else:
                P.append(f'<details><summary>store {e(key)}（rc={e(dd.get("rc"))}）</summary>'
                         f'<pre class="sub mono" style="white-space:pre-wrap">{e(dd.get("text"))}</pre></details>')
    else:
        P.append('<div class="err">store 台账不可用，体检区跳过。</div>')
    if w.get("available"):
        P.append(f'<div style="margin:10px 0 4px"><b>Wiki 页面类型</b></div><table>'
                 f'<tr><th>类型</th><th class="n">页数</th></tr>'
                 + "".join(f'<tr><td>{e(k)}</td><td class="n">{e(v)}</td></tr>'
                           for k, v in (w.get("type_counter") or []))
                 + '</table>')
        if w.get("top_linked"):
            P.append(f'<details><summary>被引最多的页面 · Top {len(w["top_linked"])}'
                     f'（图谱节点 {e(w.get("graph_nodes", "-"))} / 边 {e(w.get("graph_edges", "-"))}）</summary>'
                     '<div class="sub"><table><tr><th>标题</th><th>slug</th><th>类型</th>'
                     '<th class="n">入链</th><th>更新</th></tr>'
                     + "".join(f'<tr><td>{e(t.get("title"))}</td>'
                               f'<td class="mono mut">{e(t.get("slug"))}</td><td>{e(t.get("type"))}</td>'
                               f'<td class="n">{e(t.get("in_links"))}</td>'
                               f'<td class="mut">{e(t.get("updated_at"))}</td></tr>'
                               for t in w["top_linked"])
                     + '</table></div></details>')
        if w.get("recent"):
            P.append(f'<details><summary>最近更新的页面 · {len(w["recent"])} 条</summary>'
                     '<div class="sub"><table><tr><th>标题</th><th>wiki_path</th>'
                     '<th>更新</th></tr>'
                     + "".join(f'<tr><td>{e(t.get("title"))}</td>'
                               f'<td class="mono mut">{e(t.get("wiki_path"))}</td>'
                               f'<td class="mut">{e(t.get("updated_at"))}</td></tr>'
                               for t in w["recent"])
                     + '</table></div></details>')

    P.append('<footer><b>取数口径</b>：'
             f'本地目录树 <span class="mono">{e(SHARED)}/{{knowledge,skills,memory,mcp}}</span>；'
             '台账 <span class="mono">store q --format json</span>（PG 权威索引）；'
             'Wiki <span class="mono">wiki/stats + wiki/pages + wiki/graph</span>；'
             '健康 <span class="mono">127.0.0.1:8787|8791 /health</span>。'
             f'耗时：本地 {e(d.get("local_scan_sec"))}s / 台账 {e(d.get("store_sec"))}s / '
             f'Wiki {e(d.get("wiki_sec"))}s。'
             '本产物为 L1 只读盘点，不含页面正文、不含任何凭据；'
             '分类计数为实时枚举结果，与 stats 总数差异源于枚举期间的并发写入。</footer>')
    P.append('</div></body></html>')
    return "".join(P)


def _tree_counts_wrap(tree: dict) -> None:
    for k, v in list(tree.items()):
        if not k.startswith("__") and isinstance(v, dict):
            _tree_counts(v)


# ---------------------------------------------------------------- 渲染：MD / JSON

def render_md(d: dict) -> str:
    w, st, kn = d.get("wiki") or {}, d.get("store") or {}, d.get("knowledge") or {}
    stats = w.get("stats") or {}
    skills = d.get("skills") or []
    mem = d.get("memory") or {}
    recs = st.get("records") or []
    pbt = stats.get("pages_by_type") or {}
    L = [f'## 大脑资产地图 · {d.get("generated_at")}',
         f'> 主机 {d.get("host")} · L1 只读盘点 · {"quick" if d.get("quick") else "full"} 模式',
         '',
         '### 资产总览',
         f'- **Wiki 知识页**：{stats.get("total_pages", 0)} 页'
         + (f'（{" / ".join(f"{k} {v}" for k, v in list(pbt.items())[:4])}）' if pbt else '（库不可达）'),
         f'- **Wiki 链接**：{stats.get("total_links", 0)} 条，孤儿页 {stats.get("orphan_count", "-")}',
         f'- **本地知识**：{kn.get("files", 0)} 文件 / {_human(kn.get("bytes", 0))}，'
         f'{len([x for x in kn.get("dirs", []) if isinstance(x, dict)])} 个分类'
         + (f'（含 {kn.get("bak")} 个 .bak 备份件）' if kn.get("bak") else ''),
         f'- **store 台账**：{len(recs)} 条',
         f'- **技能**：{len(skills)} 个（系统级共享 {sum(1 for s in skills if s.get("shared"))}）',
         f'- **语料库 memory**：{len(mem.get("items") or [])} 条'
         + ('（未启用）' if not mem.get("exists") else ''),
         f'- **MCP**：{len((d.get("mcp") or {}).get("items") or [])} 个',
         '']
    tree = w.get("tree") or {}
    if tree:
        _tree_counts_wrap(tree)
        tops = sorted([(k, v) for k, v in tree.items()
                       if not k.startswith("__") and isinstance(v, dict)],
                      key=lambda kv: -kv[1].get("__total", 0))[:12]
        if tops:
            L += ['### Wiki 顶层分类（Top 12）']
            L += [f'- {k}：{v.get("__total", 0)} 页'
                  + (f'，{len([x for x in v if not x.startswith("__")])} 个子分类'
                     if len([x for x in v if not x.startswith("__")]) else '')
                  for k, v in tops]
            L.append('')
    doms = st.get("by", {}).get("domain") or []
    if doms:
        L += ['### 台账分类分布']
        L += [f'- {k}：{v} 条' for k, v in doms[:10]]
        L.append('')
    gaps = st.get("gaps") or []
    ws = st.get("by", {}).get("wiki_status") or []
    doms = d.get("domains") or []
    if doms:
        both = sum(1 for r in doms if sum(1 for k in ("local", "ledger", "wiki") if r[k]) == 3)
        L += ['### 三视图对齐',
              f'- 分类名共 {len(doms)} 个，三处齐备 {both} 个'
              f'（本地=业务口径、Wiki=自动聚类口径，映射未建立）',
              '']
    if ws:
        gin = st.get("gaps_in") or []
        gout = st.get("gaps_out") or []
        sc = st.get("sync_scope") or {}
        prefixes = sc.get("prefixes") or []
        L += ['### 双写状态']
        L += [f'- {k}：{v} 条' for k, v in ws]
        if prefixes:
            L.append(f'- **真缺口（同步范围内）：{len(gin)} 条**'
                     + (f'，例：{gin[0].get("path")}' if gin else ''))
            L.append(f'- 范围外 {len(gout)} 条属策略性不同步（本机 scope={sc.get("scope")}，'
                     f'前缀 {"、".join(prefixes[:3])}），非故障')
        else:
            L.append(f'- 缺口（none/skip/failed）：**{len(gaps)} 条**'
                     + (f'，例：{gaps[0].get("path")}' if gaps else ''))
            L.append(f'- 本机为全量同步（{sc.get("source") or "无范围收窄"}），上述条目均按缺口计')
        L.append('')
    L += ['### 技能清单']
    L += [f'- `{s["name"]}`' + (f'：{s["description"][:70]}' if s.get("description") else '')
          + ('（系统级共享）' if s.get("shared") else '') for s in skills]
    if d.get("errors"):
        L += ['', '### 取数降级'] + [f'- {x}' for x in d["errors"]]
    L += ['', f'---', f'完整层级树与体检明细见 HTML 版；本摘要由 brain-asset-map 技能生成（只读）。']
    return "\n".join(L)


def render_json(d: dict) -> str:
    slim = {k: v for k, v in d.items() if k != "_secret_values"}
    if isinstance(slim.get("wiki"), dict):
        slim["wiki"] = {k: v for k, v in slim["wiki"].items() if k != "_secret_values"}
    if isinstance(slim.get("store"), dict):
        slim["store"] = {k: v for k, v in slim["store"].items() if k != "records"}
        slim["store"]["record_count"] = len((d.get("store") or {}).get("records") or [])
    return json.dumps(slim, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="财务智脑大脑资产地图（L1 只读）")
    ap.add_argument("--format", choices=("html", "md", "json"), default="html")
    ap.add_argument("--out", default="", help="产物路径；- 表示 stdout")
    ap.add_argument("--quick", action="store_true", help="只出统计，不枚举全部 Wiki 页面")
    ap.add_argument("--deep", action="store_true", help="追加 store dup / doctor 体检")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL, help=f"缓存秒数，默认 {CACHE_TTL}")
    a = ap.parse_args()

    errors: list[str] = []
    data = None if a.no_cache else cache_load(a.ttl)
    if data and (data.get("quick") and not a.quick):
        data = None
    if data is None:
        data = collect(a.quick, a.deep, errors)
        data["errors"] = errors
        cache_save(data)
    else:
        data.setdefault("errors", [])

    if a.format == "html":
        text = render_html(data)
    elif a.format == "md":
        text = render_md(data)
    else:
        text = render_json(data)

    hits = leak_check(text, data)
    if hits:
        print(f"拒绝输出：产物疑似含凭据 {hits}", file=sys.stderr)
        return 3

    if a.out == "-":
        sys.stdout.write(text)
        return 0
    out = a.out
    if not out:
        os.makedirs(OUT_DIR, exist_ok=True)
        ext = {"html": "html", "md": "md", "json": "json"}[a.format]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(OUT_DIR, f"asset-map-{stamp}.{ext}")
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    latest = os.path.join(OUT_DIR, f"latest.{a.format}")
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        shutil.copyfile(out, latest)
    except OSError:
        latest = out
    print(json.dumps({"ok": True, "format": a.format, "out": out, "latest": latest,
                      "bytes": len(text.encode("utf-8")),
                      "generated_at": data.get("generated_at"),
                      "cached": bool(data.get("cached")),
                      "errors": data.get("errors") or []}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
