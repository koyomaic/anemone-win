#!/usr/bin/env python3
"""team-agent-relay 客户端（纯标准库，无第三方依赖）。

用法:
  python3 relay.py config [set TOKEN] [--server URL] [--profile 名称]
  python3 relay.py profiles                                # 列出全部 profile
  python3 relay.py version                                 # 客户端/服务端版本对照
  python3 relay.py run "任务描述" [-t a,b] [--urgent] [--resume N] [--force]
  python3 relay.py submit "任务描述" [-t a,b] [--urgent] [--resume N] [--force]
  python3 relay.py wait <id> [--timeout 900]
  python3 relay.py status <id>
  python3 relay.py tasks [--status S] [--limit N]
  python3 relay.py stream <id> [--timeout 600]
  python3 relay.py diff <id>
  python3 relay.py upload <本地文件> <共享区相对路径>     # 上传，≤10MB
  python3 relay.py download <共享区相对路径> [本地路径]   # 下载，≤10MB
  python3 relay.py cancel <id>
  python3 relay.py confirm <id> [--outcome done|conflict]
  python3 relay.py approve <id> --decision approve|deny   # 审批 pending_approval（高危命令拦截）

所有子命令支持 --profile <名称> 选择公司/服务器配置
（缺省顺序: --profile > env TEAM_AGENT_PROFILE > 打包大脑名 PACKAGE_BRAIN > default）。
成员包由所在中转服务器 pack.sh 生成，包名 team-agent-relay-<dws认证大脑名>。

输出均为单行 JSON。退出码: 0 正常; 2 目标重叠(409); 3 token失效/已激活(401/409)。
认证: token 即一次性激活码——首次使用自动调 /auth/activate 核销 token、
绑定本设备并换回 device_secret（此后请求用 HMAC-SHA256 签名，secret 永不上网络，
±900s 时间窗+nonce 防重放）；旧服务端无激活端点时自动回落 bearer，不阻塞。
配置: 环境变量 TEAM_AGENT_SERVER/TEAM_AGENT_TOKEN/TEAM_AGENT_PROFILE 优先，
其次 ~/.team-agent/config（INI 多节，每节一组 SERVER=/TOKEN=，节名即 profile；
旧版无节平铺格式首次加载自动迁移为 [default] 并回写）。
连接: 默认 https://127.0.0.1:8788（环境服务器本机中转）。传输安全策略（1.4.1）：
仅允许 https——http:// 地址直接拒绝（除非显式 --insecure/TEAM_AGENT_INSECURE=1，
仅排障用）；TLS 校验独占固定本目录 ca.pem（不叠加系统 CA），ca.pem 缺失时
拒绝发起请求（fail-fast，不静默回退系统信任库）。历史默认地址（.23:8787 http、
.29:8787 http、.32:8787 http）首次加载时按 SERVER_MIGRATIONS 自动迁移并回写。
.29 为新能源中转，新能源成员用 --profile 新能源 --server https://10.189.51.29:8788 配置。
"""
from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import hmac
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

__version__ = "1.4.4"

# 打包时由 pack.sh 写入：本技能所代表的 dws 认证大脑名（同时作为缺省 profile 名）。
# 源模板留空 → 缺省 profile 回落 "default"。
PACKAGE_BRAIN = ""

CFG_PATH = os.path.expanduser("~/.team-agent/config")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PEM = os.path.join(SCRIPT_DIR, "ca.pem")
DEFAULT_SERVER = "https://127.0.0.1:8788"   # 源模板默认（环境服务器本机中转）；成员包由 pack.sh 写入所属大脑服务器
# 历史默认地址 → 现地址（首载自动迁移回写；各机 .N:8787 只回本机 https，不跨机迁移）
SERVER_MIGRATIONS = {
    "http://10.189.51.23:8787": "https://10.189.51.23:8788",
    "http://10.189.51.29:8787": "https://10.189.51.29:8788",
    "http://10.189.51.32:8787": "https://10.189.51.32:8788",
}
TERMINAL = {"done", "review", "conflict", "failed", "cancelled", "pending_approval"}
MAX_FILE_BYTES = 10 * 1024 * 1024  # 文件通道单文件上限 10MB


def _ssl_ctx(server: str, insecure: bool = False):
    """https 返回 SSLContext，http 返回 None（http 是否放行由 _check_transport 把关）。

    安全策略（1.4.1）：
    - ca.pem 存在 → 独占 pinning：只信任 ca.pem，不叠加系统 CA（防信任面扩大）；
    - ca.pem 缺失 → fail-fast 拒绝发起请求，不静默回退系统信任库；
    - insecure（--insecure / TEAM_AGENT_INSECURE=1）→ 显式跳过校验，仅排障用。
    """
    if not server.startswith("https://"):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if not os.path.isfile(CA_PEM):
        out({"error": True, "detail": "CA_PEM_MISSING", "path": CA_PEM,
             "hint": "ca.pem 缺失：已拒绝回退系统信任库（防校验降级）。"
                     "请将 ca.pem 补入本技能目录后重试；排障可显式 --insecure（不推荐）"})
        sys.exit(5)
    ctx.load_verify_locations(CA_PEM)
    return ctx


def _check_transport(cfg: dict) -> None:
    """传输层安全门：拒绝明文 http://（除非显式 insecure）；已知旧地址在 SERVER_MIGRATIONS 已自动升级。"""
    server = cfg.get("server", "")
    if server.startswith("http://") and not cfg.get("insecure"):
        out({"error": True, "detail": "INSECURE_TRANSPORT_REFUSED", "server": server,
             "hint": "已拒绝明文 http:// 连接（凭证与任务内容会裸奔、签名可被截获重放）。"
                     "请改用 https:// 地址；确需明文排障时显式加 --insecure 或 TEAM_AGENT_INSECURE=1"})
        sys.exit(5)


def _new_cp() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.optionxform = str  # 保留 SERVER/TOKEN 大写键
    return cp


def _write_ini(cp: configparser.ConfigParser) -> None:
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)
    os.chmod(CFG_PATH, 0o600)


def _read_ini() -> configparser.ConfigParser:
    """读多 profile 配置；旧平铺格式（无节头）自动迁移为 [default] 并回写。"""
    cp = _new_cp()
    if not os.path.isfile(CFG_PATH):
        return cp
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return cp
    try:
        cp.read_string(text)
    except configparser.MissingSectionHeaderError:
        cp = _new_cp()  # 旧平铺格式 → 迁移进 [default]
        flat = {}
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            flat[k.strip().upper()] = v.strip()
        cp.add_section("default")
        server = flat.get("SERVER", "")
        if server:
            cp.set("default", "SERVER", SERVER_MIGRATIONS.get(server, server))
        if flat.get("TOKEN"):
            cp.set("default", "TOKEN", flat["TOKEN"])
        _write_ini(cp)
        return cp
    except configparser.Error:
        return _new_cp()
    changed = False
    for name in cp.sections():  # 各节历史默认地址统一迁移
        old = cp.get(name, "SERVER", fallback="")
        if old in SERVER_MIGRATIONS:
            cp.set(name, "SERVER", SERVER_MIGRATIONS[old])
            changed = True
    if changed:
        _write_ini(cp)
    return cp


def load_cfg(profile: str = "") -> dict:
    profile = (profile or os.environ.get("TEAM_AGENT_PROFILE", "")
               or PACKAGE_BRAIN or "default")
    cfg = {
        "profile": profile,
        "server": os.environ.get("TEAM_AGENT_SERVER", ""),
        "token": os.environ.get("TEAM_AGENT_TOKEN", ""),
        "device_id": os.environ.get("TEAM_AGENT_DEVICE_ID", ""),
        "device_secret": os.environ.get("TEAM_AGENT_DEVICE_SECRET", ""),
    }
    cp = _read_ini()
    if cp.has_section(profile):
        for key in ("server", "token", "device_id", "device_secret"):
            if not cfg[key]:
                cfg[key] = cp.get(profile, key.upper(), fallback="")
    return cfg


def _save_section(profile: str, updates: dict, removes=()) -> None:
    cp = _read_ini()
    if not cp.has_section(profile):
        cp.add_section(profile)
    for k, v in updates.items():
        cp.set(profile, k.upper(), v)
    for k in removes:
        cp.remove_option(profile, k.upper())
    _write_ini(cp)


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def ensure_device(cfg: dict) -> None:
    """有 device_secret → 设备签名模式；仅有 token → 静默激活换设备凭证。

    token 即一次性激活码：激活成功后立即核销并回写配置（TOKEN → DEVICE_*）。
    旧服务端无 /auth/activate（404）或激活请求异常时回落 bearer 模式，不阻塞本次使用。
    """
    if cfg.get("device_secret") or not cfg.get("token"):
        return
    dev_id = cfg.get("device_id") or ("dev_" + uuid.uuid4().hex[:16])
    body = json.dumps({"token": cfg["token"], "device_id": dev_id,
                       "device_name": socket.gethostname()}).encode()
    r = urllib.request.Request(cfg["server"].rstrip("/") + "/auth/activate",
                               data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30,
                                    context=_ssl_ctx(cfg["server"],
                                                     cfg.get("insecure"))) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw[:200])
        except ValueError:
            detail = raw[:200]
        if e.code == 404:      # 旧服务端 → 继续 bearer
            return
        if e.code == 409:      # token 已被激活（一次性）
            out({"error": True, "http": 409,
                 "detail": f"token 已被激活使用（一次性）：{detail}"})
            sys.exit(3)
        if e.code == 401:
            fail(3, 401, {"detail": detail})
        return                 # 5xx 等 → 本次按 bearer 尝试，不阻塞
    except Exception:
        return                 # 网络不可达 → 让后续真实请求报错
    cfg["device_id"] = payload["device_id"]
    cfg["device_secret"] = payload["device_secret"]
    cfg["token"] = ""
    _save_section(cfg["profile"],
                  {"DEVICE_ID": cfg["device_id"],
                   "DEVICE_SECRET": cfg["device_secret"]},
                  removes=("TOKEN",))


def _auth_headers(cfg: dict, method: str, path: str, body: bytes | None) -> dict:
    """设备模式：HMAC-SHA256 请求签名（secret 永不上网络）；否则回落 bearer。"""
    if cfg.get("device_secret"):
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        digest = hashlib.sha256(body or b"").hexdigest()
        msg = f"{ts}\n{method}\n{path}\n{digest}\n{nonce}"
        sig = hmac.new(cfg["device_secret"].encode(), msg.encode(),
                       hashlib.sha256).hexdigest()
        return {"X-Device-Id": cfg["device_id"], "X-Timestamp": ts,
                "X-Nonce": nonce, "X-Signature": sig}
    if cfg.get("token"):
        return {"Authorization": "Bearer " + cfg["token"]}
    return {}


def req(method: str, cfg: dict, path: str, body=None):
    url = cfg["server"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    for k, v in _auth_headers(cfg, method, path, data).items():
        r.add_header(k, v)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60,
                                    context=_ssl_ctx(cfg["server"], cfg.get("insecure"))) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"detail": raw[:300] or str(e)}
        return e.code, payload
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        out({"error": True, "detail": f"无法连接服务器 {cfg['server']}: {e}"})
        sys.exit(1)


def fail(code: int, status: int, payload) -> None:
    out({"error": True, "http": status, "detail": payload})
    sys.exit(code)


def req_bytes(cfg: dict, path: str):
    """GET 二进制内容，返回 (status, bytes)。"""
    url = cfg["server"].rstrip("/") + path
    r = urllib.request.Request(url, method="GET")
    for k, v in _auth_headers(cfg, "GET", path, None).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=120,
                                    context=_ssl_ctx(cfg["server"], cfg.get("insecure"))) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        out({"error": True, "detail": f"无法连接服务器 {cfg['server']}: {e}"})
        sys.exit(1)


def need_cfg(cfg: dict) -> None:
    if not cfg["server"] or not (cfg["token"] or cfg["device_secret"]):
        out({"error": True, "detail": "CONFIG_MISSING",
             "profile": cfg["profile"]})
        sys.exit(4)


# ---- 命令 ----

def cmd_config(args, cfg):
    profile = cfg["profile"]
    if args.set_token:
        cp = _read_ini()
        server = (args.server
                  or (cp.get(profile, "SERVER", fallback="")
                      if cp.has_section(profile) else "")
                  or DEFAULT_SERVER)
        server = SERVER_MIGRATIONS.get(server, server)
        _check_transport({"server": server, "insecure": cfg.get("insecure")})
        # 先内存试激活，成功/回落才落盘；409（已核销）/401 直接退出，现有配置零改动
        trial = {"profile": profile, "server": server, "token": args.set_token,
                 "device_id": cfg.get("device_id", ""), "device_secret": ""}
        ensure_device(trial)
        if trial.get("device_secret"):
            _save_section(profile, {"SERVER": server})  # DEVICE_* 已由激活写盘
            activated = True
        else:
            # 旧服务端/暂不可达：存 TOKEN（首跑自动重试激活），作废旧设备凭证
            _save_section(profile, {"SERVER": server, "TOKEN": args.set_token},
                          removes=("DEVICE_SECRET",))
            activated = False
        out({"saved": True, "profile": profile, "server": server,
             "activated": activated, "path": CFG_PATH})
        return
    out({
        "configured": bool(cfg["server"]
                           and (cfg["token"] or cfg["device_secret"])),
        "profile": profile,
        "server": cfg["server"] or None,
        "auth": ("device" if cfg.get("device_secret")
                 else "bearer" if cfg.get("token") else None),
        "path": CFG_PATH,
    })


def cmd_profiles(args, cfg):
    cp = _read_ini()
    items = []
    for name in cp.sections():
        tok = cp.get(name, "TOKEN", fallback="")
        items.append({
            "name": name,
            "server": cp.get(name, "SERVER", fallback="") or None,
            "token": (tok[:6] + "…") if tok else None,
            "active": name == cfg["profile"],
        })
    out({"profiles": items, "active": cfg["profile"], "path": CFG_PATH})


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def cmd_version(args, cfg):
    server = cfg["server"] or DEFAULT_SERVER
    _check_transport({"server": server, "insecure": cfg.get("insecure")})
    result = {"client": __version__, "profile": cfg["profile"],
              "brain": PACKAGE_BRAIN or None,
              "auth": ("device" if cfg.get("device_secret")
                       else "bearer" if cfg.get("token") else None),
              "server": None}
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/health",
                                    timeout=10,
                                    context=_ssl_ctx(server,
                                                     cfg.get("insecure"))) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace"))
        minv = health.get("min_client_version") or ""
        result["server"] = {
            "url": server,
            "version": health.get("version"),
            "min_client_version": minv or None,
        }
        result["upgrade_needed"] = bool(
            minv and _ver_tuple(__version__) < _ver_tuple(minv))
        if result["upgrade_needed"]:
            result["hint"] = (f"客户端 {__version__} 低于服务端要求 "
                              f"{minv}，请更新 team-agent-relay 技能")
    except Exception as e:  # 服务端不可达不阻塞版本查询
        result["detail"] = f"服务端不可达: {e}"
    out(result)


def _submit(cfg, description, targets, urgent, resume_from, force):
    body = {
        "description": description,
        "targets": [t.strip() for t in targets.split(",") if t.strip()],
        "priority": "urgent" if urgent else "normal",
        "force": force,
    }
    if resume_from is not None:
        body["resume_from"] = resume_from
    status, payload = req("POST", cfg, "/tasks", body)
    if status == 202:
        out(payload)
        return payload["task_id"]
    fail(2 if status == 409 else 1, status, payload)


def cmd_submit(args, cfg):
    _submit(cfg, args.description, args.targets, args.urgent,
            args.resume, args.force)


def cmd_wait(args, cfg):
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        status, payload = req("GET", cfg, f"/tasks/{args.id}")
        if status == 401:
            fail(3, status, payload)
        last = payload
        if payload.get("status") in TERMINAL:
            out(payload)
            return
        time.sleep(1.5)
    out({"error": True, "detail": "wait timeout", "last": last})
    sys.exit(1)


def cmd_run(args, cfg):
    tid = _submit(cfg, args.description, args.targets, args.urgent,
                  args.resume, args.force)
    out({"submitted": tid})
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        status, payload = req("GET", cfg, f"/tasks/{tid}")
        if status == 401:
            fail(3, status, payload)
        last = payload
        if payload.get("status") in TERMINAL:
            out(payload)
            return
        time.sleep(1.5)
    out({"error": True, "detail": "wait timeout", "last": last})
    sys.exit(1)


def cmd_status(args, cfg):
    status, payload = req("GET", cfg, f"/tasks/{args.id}")
    if status == 401:
        fail(3, status, payload)
    if status == 404:
        fail(1, status, payload)
    out(payload)


def cmd_tasks(args, cfg):
    path = "/tasks"
    q = []
    if args.status:
        q.append(f"status={args.status}")
    q.append(f"limit={args.limit}")
    status, payload = req("GET", cfg, path + "?" + "&".join(q))
    if status == 401:
        fail(3, status, payload)
    out(payload)


def cmd_stream(args, cfg):
    path = f"/tasks/{args.id}/stream"
    r = urllib.request.Request(cfg["server"].rstrip("/") + path)
    for k, v in _auth_headers(cfg, "GET", path, None).items():
        r.add_header(k, v)
    deadline = time.time() + args.timeout
    try:
        resp = urllib.request.urlopen(r, timeout=60,
                                      context=_ssl_ctx(cfg["server"],
                                                       cfg.get("insecure")))
    except urllib.error.HTTPError as e:
        fail(3 if e.code == 401 else 1, e.code, {"detail": str(e)})
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        out({"error": True, "detail": f"无法连接服务器 {cfg['server']}: {e}"})
        sys.exit(1)
    with resp:
        for raw in resp:
            if time.time() > deadline:
                out({"error": True, "detail": "stream timeout"})
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            out(ev)
            if ev.get("t") == "status" and ev.get("s") in TERMINAL:
                return


def cmd_diff(args, cfg):
    status, payload = req("GET", cfg, f"/tasks/{args.id}/diff")
    if status == 401:
        fail(3, status, payload)
    out(payload)


def cmd_upload(args, cfg):
    if not os.path.isfile(args.local):
        out({"error": True, "detail": f"本地文件不存在: {args.local}"})
        sys.exit(1)
    size = os.path.getsize(args.local)
    if size > MAX_FILE_BYTES:
        out({"error": True, "detail": f"文件大小 {size} 字节超过 10MB 上限"})
        sys.exit(1)
    with open(args.local, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    status, payload = req("POST", cfg, "/files",
                          {"path": args.path, "content_b64": b64})
    if status == 401:
        fail(3, status, payload)
    if status == 201:
        out(payload)
        return
    fail(1, status, payload)


def cmd_download(args, cfg):
    status, data = req_bytes(cfg, "/files?path=" + urllib.parse.quote(args.path))
    if status == 401:
        fail(3, status, {"detail": "invalid token"})
    if status != 200:
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            payload = {"detail": data[:300].decode("utf-8", "replace")}
        fail(1, status, payload)
    local = args.local or os.path.basename(args.path.rstrip("/")) or "download"
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    out({"ok": True, "saved": local, "size": len(data)})


def cmd_cancel(args, cfg):
    status, payload = req("POST", cfg, f"/tasks/{args.id}/cancel")
    if status == 401:
        fail(3, status, payload)
    out(payload)


def cmd_confirm(args, cfg):
    status, payload = req("POST", cfg, f"/tasks/{args.id}/confirm",
                          {"outcome": args.outcome})
    if status == 401:
        fail(3, status, payload)
    out(payload)


def cmd_approve(args, cfg):
    status, payload = req("POST", cfg, f"/tasks/{args.id}/approve",
                          {"decision": args.decision})
    if status == 401:
        fail(3, status, payload)
    out(payload)


def _plan_locate(cfg, args):
    """位置参数 id 或 --name → (plan_id, plan|None)。零命中/多命中都算失败。"""
    if getattr(args, "id", None):
        return int(args.id), None
    nm = (getattr(args, "name", "") or "").strip()
    if not nm:
        out({"error": True, "detail": "需要计划 id（位置参数）或 --name"})
        sys.exit(2)
    status, payload = req("GET", cfg, "/plans")
    if status == 401:
        fail(3, status, payload)
    plans = payload.get("plans") or []
    hits = ([p for p in plans if p.get("name") == nm]
            or [p for p in plans if nm in (p.get("name") or "")])
    if len(hits) != 1:
        out({"error": True,
             "detail": "PLAN_NOT_FOUND" if not hits else "NAME_NOT_UNIQUE",
             "candidates": [{"id": p.get("id"), "name": p.get("name"),
                             "owner_name": p.get("owner_name")} for p in hits]})
        sys.exit(1)
    return int(hits[0]["id"]), hits[0]


def cmd_plan(args, cfg):
    """定时计划管理。服务端 /plans 只要求已认证主体（设备签名即可，不需 admin），
    owner_name 自动记调用者；非 admin 只能改/删自己的计划。

    注意：计划到点触发的任务仍在沙箱内跑，钉钉群发等高危命令照样走审批门禁——
    需要"到点自动发消息"的计划，投递要交给分身或宿主机侧执行。
    """
    op = args.op
    if op == "list":
        status, payload = req("GET", cfg, "/plans")
        if status == 401:
            fail(3, status, payload)
        plans = payload.get("plans") or []
        if args.owner:
            plans = [p for p in plans if p.get("owner_name") == args.owner]
        out({"plans": [{k: p.get(k) for k in
                        ("id", "name", "owner_name", "on_calendar", "enabled",
                         "next_run", "last_run", "last_task_id")}
                       for p in plans]})
        return

    if op == "create":
        missing = [f for f in ("name", "on_calendar", "description")
                   if not (getattr(args, f, "") or "").strip()]
        if missing:
            out({"error": True, "detail": "create 缺少必填项", "missing": missing,
                 "hint": 'plan create --name 每日X --on-calendar "*-*-* 08:30:00" '
                         '--description "到点要跑的任务描述"'})
            sys.exit(2)
        action = {"type": "task", "description": args.description,
                  "priority": args.priority, "read_only": args.read_only,
                  "targets": [t.strip() for t in args.targets.split(",")
                              if t.strip()]}
        if args.project:
            action["project"] = args.project
        body = {"name": args.name.strip(),
                "on_calendar": args.on_calendar.strip(),
                "action": action, "enabled": not args.disabled}
        status, payload = req("POST", cfg, "/plans", body)
        if status == 201:
            out(payload)
            return
        fail(3 if status == 401 else 1, status, payload)

    pid, _ = _plan_locate(cfg, args)
    if op == "get":
        status, payload = req("GET", cfg, f"/plans/{pid}")
        if status == 401:
            fail(3, status, payload)
        if status != 200:
            fail(1, status, payload)
        out(payload)
        return
    if op == "delete":
        status, payload = req("DELETE", cfg, f"/plans/{pid}")
        if status == 401:
            fail(3, status, payload)
        if status != 200:
            fail(1, status, payload)
        out(payload)
        return

    patch = {}
    if op in ("enable", "disable"):
        patch["enabled"] = (op == "enable")
    else:  # update：只提交显式给出的字段；action 类字段先取现值再合并
        if args.name:
            patch["name"] = args.name.strip()
        if args.on_calendar:
            patch["on_calendar"] = args.on_calendar.strip()
        if args.description or args.targets or args.project or args.read_only:
            st, cur = req("GET", cfg, f"/plans/{pid}")
            if st == 401:
                fail(3, st, cur)
            if st != 200:
                fail(1, st, cur)
            act = dict(cur.get("action") or {})
            act["type"] = "task"
            if not act.get("description"):
                act["description"] = args.description or ""
            if args.description:
                act["description"] = args.description
            if args.targets:
                act["targets"] = [t.strip() for t in args.targets.split(",")
                                  if t.strip()]
            if args.project:
                act["project"] = args.project
            if args.read_only:
                act["read_only"] = True
            patch["action"] = act
        if not patch:
            out({"error": True, "detail": "update 未提供任何要改的字段",
                 "hint": "可用 --name/--on-calendar/--description/--targets/"
                         "--project/--read-only"})
            sys.exit(2)
    status, payload = req("PATCH", cfg, f"/plans/{pid}", patch)
    if status == 401:
        fail(3, status, payload)
    if status != 200:
        fail(1, status, payload)
    out(payload)


def main() -> None:
    ap = argparse.ArgumentParser(prog="relay", description="团队中转服务客户端")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default="",
                        help="配置 profile 名（缺省 default；env TEAM_AGENT_PROFILE）")
    common.add_argument("--insecure", action="store_true",
                        help="允许明文 http:// 与跳过 TLS 校验（仅排障；env TEAM_AGENT_INSECURE=1）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("config", parents=[common])
    p.add_argument("set_token", nargs="?", default="", help="写入 token")
    p.add_argument("--server", default="")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("profiles", parents=[common],
                       help="列出全部 profile（token 打码）")
    p.set_defaults(fn=cmd_profiles)

    p = sub.add_parser("version", parents=[common],
                       help="客户端/服务端版本对照")
    p.set_defaults(fn=cmd_version)

    def add_task_args(p, waitable=False):
        p.add_argument("-t", "--targets", default="",
                       help="逗号分隔，如 shared/report/市场分析/2026-09")
        p.add_argument("--urgent", action="store_true")
        p.add_argument("--resume", type=int, default=None)
        p.add_argument("--force", action="store_true")
        if waitable:
            p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("run", parents=[common], help="提交并等待终态（一步到位，最常用）")
    p.add_argument("description")
    add_task_args(p, waitable=True)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("submit", parents=[common], help="仅提交，返回 task_id")
    p.add_argument("description")
    add_task_args(p)
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("wait", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("status", parents=[common])
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("tasks", parents=[common])
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("stream", parents=[common], help="SSE 实时流，逐行 JSON 到终态")
    p.add_argument("id", type=int)
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_stream)

    p = sub.add_parser("diff", parents=[common])
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("upload", parents=[common], help="上传本地文件到共享区（单文件 ≤10MB）")
    p.add_argument("local", help="本地文件路径")
    p.add_argument("path", help="共享区相对路径，如 data/9月.xlsx")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("download", parents=[common], help="从共享区下载文件（单文件 ≤10MB）")
    p.add_argument("path", help="共享区相对路径")
    p.add_argument("local", nargs="?", default="", help="保存到本地路径（默认文件名）")
    p.set_defaults(fn=cmd_download)

    p = sub.add_parser("cancel", parents=[common])
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("confirm", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("--outcome", choices=["done", "conflict"], default="done")
    p.set_defaults(fn=cmd_confirm)

    p = sub.add_parser("approve", parents=[common],
                       help="审批 pending_approval（高危命令拦截）：放行或拒绝")
    p.add_argument("id", type=int)
    p.add_argument("--decision", choices=["approve", "deny"], required=True,
                   help="approve=放行该命令续跑; deny=置 failed")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("plan", parents=[common],
                       help="定时计划：list/create/get/enable/disable/update/delete")
    p.add_argument("op", choices=["list", "create", "get", "enable",
                                  "disable", "update", "delete"])
    p.add_argument("id", nargs="?", type=int, default=None,
                   help="计划 id（get/enable/disable/update/delete；也可用 --name 定位）")
    p.add_argument("--name", default="",
                   help="create：计划名（1-64 个字母/数字/中文/._-）；其他 op：按名定位")
    p.add_argument("--on-calendar", default="",
                   help='systemd OnCalendar 表达式，如 "*-*-* 08:30:00"')
    p.add_argument("--description", default="",
                   help="create：到点要跑的任务描述（必填）；update：改描述")
    p.add_argument("--priority", choices=["normal", "urgent"], default="normal")
    p.add_argument("--targets", default="", help="逗号分隔的共享区目标目录")
    p.add_argument("--project", default="")
    p.add_argument("--read-only", action="store_true",
                   help="只读任务（update 时只能置真，不能取消）")
    p.add_argument("--disabled", action="store_true", help="create：建好即停用")
    p.add_argument("--owner", default="", help="list：按 owner_name 过滤")
    p.set_defaults(fn=cmd_plan)

    args = ap.parse_args()
    cfg = load_cfg(args.profile)
    cfg["insecure"] = (bool(getattr(args, "insecure", False))
                       or os.environ.get("TEAM_AGENT_INSECURE", "") == "1")
    if args.cmd not in ("config", "profiles", "version"):
        need_cfg(cfg)
        _check_transport(cfg)
        ensure_device(cfg)  # 旧配置首跑静默激活（一次性 token → 设备凭证）
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
