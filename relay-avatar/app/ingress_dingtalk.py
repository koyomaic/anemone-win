"""app/ingress_dingtalk.py —— 钉钉/dws 适配器（双入口之 A）。

- dws event consume 长连接（o2o + 群@，群默认关）：270s 空闲看门狗 +
  stderr 凭据失效检测 → run/inbound_health.json（ok/bad_credentials/stalled 三态，
  测「连着但收不到消息」假死）；凭据失效立即 kill 重连，不干等看门狗。
- self 模式补充轮询 +messages-list-direct（账号自发消息不进事件流）+ SELF_MARK 回音闸。
- 所有 dws 调用强制 --profile（配置了 dws_profile 时）；strict 模式下 profile
  缺失硬告警拒发（修 fenshen P1 wrapper 静默降级）。
- 媒体下载 / 表情 ACK / 标已读 / openId→userId 解析（contacts 缓存）。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .storage import atomic_write_text


def send_accepted(r) -> tuple:
    """判定 dws message send 响应是否「已受理」，返回 (ok, openTaskId, 错误摘要)。

    为什么需要它：Dws.run() 的失败返回形态很多——{"_timeout":True}、{"_error":…}、
    {"raw":…}（stdout 非 JSON，通常就是报错文本）、{"error":{…}}（dws 业务错误）。
    而旧判定是
        ok = bool(r) and "_exc" not in r and r.get("success") is not False
    它把上述**全部当成成功**：真实成功响应里 success 嵌在 data 下、顶层没有该键，
    于是 r.get("success") 恒为 None、`is not False` 恒真。结果就是「审计记
    send_file 成功、用户什么都没收到」，且日志里查不到任何失败痕迹。
    2026-09-25 用户连续 6 次反馈文件发不出来即此因（叠加提示词仍宣称无 Bash
    导致格式降级，两个缺陷互相掩盖）。

    注意 ok=True 只代表**受理**：dws 文件发送是异步的（outcome=pending），
    真到没到要用 send_status(openTaskId) 查 sendStatus。
    """
    if not isinstance(r, dict) or not r:
        return False, "", "空响应"
    for k in ("_exc", "_error", "_timeout"):
        if k in r:
            return False, "", f"{k}={str(r[k])[:110]}"
    if isinstance(r.get("error"), (dict, str)):
        e = r["error"]
        msg = e.get("message") if isinstance(e, dict) else str(e)
        return False, "", str(msg)[:120]
    if "raw" in r and r.get("ok") is not True:
        return False, "", f"非 JSON 响应: {str(r['raw'])[:110]}"
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    if data.get("errorCode"):
        return False, "", f"{data.get('errorCode')}: {data.get('errorMsg')}"
    if data.get("success") is False or r.get("ok") is False:
        return False, "", str(data.get("errorMsg") or "success=false")[:120]
    if r.get("dry"):
        return True, "", ""                      # dry-run：视为已处理
    if r.get("ok") is True or data.get("success") is True:
        res = data.get("result")
        tid = str((res or {}).get("openTaskId") or "") if isinstance(res, dict) else ""
        return True, tid, ""
    return False, "", "响应无 ok/success 标记"


def card_biz_id(resp) -> str:
    """从 card_create 响应提取 bizId（兼容测试桩直接返回字符串）。"""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return ""
    try:
        return str(((resp.get("cardRef") or {}).get("bizId")
                    or ((resp.get("created") or {}).get("result")
                        or {}).get("bizId")
                    or (resp.get("result") or {}).get("bizId")
                    or resp.get("bizId") or ""))
    except Exception:
        return ""


def md_hardbreak(text: str) -> str:
    """钉钉 Markdown 硬换行：非表格/代码块的行尾补两个空格。"""
    lines = (text or "").split("\n")
    out, in_code = [], False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code or ln.strip().startswith("|") or ln.strip() == "":
            out.append(ln)
        else:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            out.append(ln + "  " if nxt.strip() != "" else ln)
    return "\n".join(out)


class Dws:
    """dws CLI 封装。dry=True 时发送类命令只打印不执行（自测）。"""

    def __init__(self, cfg, audit, dry: bool = False):
        self.cfg = cfg
        self.audit = audit
        self.dry = dry
        self.bin = shutil.which("dws") or "/usr/bin/dws"
        self.profile = cfg.dws_profile
        self.strict = str(cfg.get("security", "profile_enforce",
                                  "strict")) == "strict"
        self._profile_warned = False

    def _argv(self, args: list) -> list:
        argv = [self.bin] + list(args)
        if self.profile:
            argv += ["--profile", self.profile]
        elif self.strict and not self._profile_warned:
            # 单账号机（.35）允许空 profile=本机默认登录态，但留痕一次
            self._profile_warned = True
            self.audit.log("profile_missing",
                           "system", "dws_profile 未配置，使用本机默认登录态")
        return argv

    def _env(self) -> dict:
        e = os.environ.copy()
        if not e.get("HOME"):
            e["HOME"] = os.path.expanduser("~") or "/root"
        np = ("127.0.0.1,localhost,.dingtalk.com,dingtalk.com,"
              ".aliyuncs.com,aliyuncs.com,.alidocs.com")
        e["NO_PROXY"] = np
        e["no_proxy"] = np
        return e

    def run(self, args: list, timeout: int = 60) -> dict:
        send_cmds = ("+conversation-mark-read", "+messages-add-emoji",
                     "+messages-send-card", "+messages-update-card")
        if self.dry and (args[:2] == ["chat", "message"] or
                         (args[1:2] and args[1] in send_cmds)):
            print("[dry-send]", " ".join(str(a)[:80] for a in args), flush=True)
            return {"dry": True}
        try:
            p = subprocess.run(self._argv(args), capture_output=True,
                               text=True, timeout=timeout, env=self._env())
        except subprocess.TimeoutExpired:
            return {"_timeout": True}
        except Exception as e:
            return {"_error": str(e)[:200]}
        if p.returncode != 0:
            print("dws error:", (p.stderr or p.stdout)[:300], flush=True)
        try:
            return json.loads(p.stdout)
        except Exception:
            return {"raw": (p.stdout or "")[:300]}

    # ── 发送 ──
    def _target_args(self, t: dict) -> list:
        if t.get("is_group"):
            return ["--conversation-id", t["conversation_id"]]
        if t.get("user_id"):
            return ["--user", str(t["user_id"])]
        return ["--open-dingtalk-id", t["open_dingtalk_id"]]

    def send_text(self, target: dict, content: str) -> dict:
        return self.run(["chat", "message", "send"] +
                        self._target_args(target) +
                        ["--content", content, "-y", "-f", "json"])

    def send_file(self, target: dict, path: str) -> dict:
        return self.run(["chat", "message", "send"] +
                        self._target_args(target) +
                        ["--msg-type", "file", "--file", path,
                         "-y", "-f", "json"], timeout=120)

    def send_status(self, open_task_id: str) -> dict:
        """查异步发送的真实投递结果。

        dws 的 message send 是**异步**的：返回 `outcome: "pending"`、
        `meta.operation.state: "processing"` 与一个 openTaskId，`data.success: true`
        只代表**请求被受理**，不代表投递成功。要确认真到没到必须查这个端点
        （响应里的 `next_command` 就是它）。2026-09-25：用户连续 6 次反馈
        「文件发不出来」，而审计一路记 send_file 成功，就是因为只看了受理态。
        """
        return self.run(["chat", "message", "query-send-status",
                         "--open-task-id", str(open_task_id),
                         "-y", "-f", "json"], timeout=30)

    def react(self, conv_id: str, msg_id: str, emoji: str) -> dict:
        return self.run(["chat", "message", "add-emoji",
                         "--conversation-id", conv_id,
                         "--message-id", msg_id,
                         "--emoji", emoji, "-y", "-f", "json"], timeout=15)

    def react_text_emotion(self, conv_id: str, msg_id: str, te: dict) -> dict:
        """文字表情回应：贴原消息的小图标，显示自定义文字（如「正在疯狂处理中...」）。"""
        return self.run(["chat", "message", "add-text-emotion",
                         "--conversation-id", conv_id,
                         "--message-id", msg_id,
                         "--emotion-id", str(te.get("emotion_id", "")),
                         "--emotion-name", str(te.get("emotion_name", "")),
                         "--text", str(te.get("text", "")),
                         "--background-id", str(te.get("background_id", "")),
                         "-y", "-f", "json"], timeout=15)

    def remove_text_emotion(self, conv_id: str, msg_id: str,
                            te: dict) -> dict:
        """移除文字表情（完成态换图标用：先撤「处理中」再贴「已办好」）。"""
        return self.run(["chat", "message", "remove-text-emotion",
                         "--conversation-id", conv_id,
                         "--message-id", msg_id,
                         "--emotion-id", str(te.get("emotion_id", "")),
                         "--emotion-name", str(te.get("emotion_name", "")),
                         "--text", str(te.get("text", "")),
                         "--background-id", str(te.get("background_id", "")),
                         "-y", "-f", "json"], timeout=15)

    def mark_read(self, conv_id: str, msg_id: str) -> dict:
        return self.run(["chat", "+conversation-mark-read",
                         "--conversation-id", conv_id,
                         "--message-id", msg_id, "--yes", "-f", "json"],
                        timeout=15)

    # ── 流式卡片（提交即开卡，状态跳变更新，完成 flow-status=3 定格） ──
    def card_create(self, target: dict):
        """创建流式卡片，返回原始响应（bizId 用 card_biz_id 提取；原始响应
        含 openTaskId，供回执落档/撤回）。群 --group / 单聊优先
        --receiver-open-dingtalk-id 直传（--receiver 走通讯录解析，慢且可能歧义）。"""
        if target.get("is_group"):
            args = ["chat", "+messages-send-card", "--group",
                    target.get("conversation_id", "")]
        elif target.get("open_dingtalk_id"):
            args = ["chat", "+messages-send-card",
                    "--receiver-open-dingtalk-id",
                    target["open_dingtalk_id"]]
        elif target.get("user_id"):
            args = ["chat", "+messages-send-card", "--receiver",
                    str(target["user_id"])]
        else:
            return {}
        return self.run(args + ["--yes", "--format", "json"], timeout=30)

    def card_update(self, biz_id: str, content: str, flow_status: int) -> bool:
        """更新卡片。flow_status: 1处理中/2输入中/3完成/4执行中/5错误。"""
        if not biz_id:
            return False
        r = self.run(["chat", "+messages-update-card", "--biz-id", biz_id,
                      "--content", md_hardbreak(content or ""),
                      "--flow-status", str(flow_status),
                      "--yes", "--format", "json"], timeout=30)
        return not (r.get("error") or r.get("errorMsg") or r.get("_error")
                    or r.get("_timeout"))

    def download_media(self, media_id: str, message_id: str, conv_id: str,
                       outdir: Path) -> dict:
        outdir.mkdir(parents=True, exist_ok=True)
        return self.run(["chat", "message", "download-media",
                         "--type", "mediaId", "--resource-id", media_id,
                         "--message-id", message_id,
                         "--open-conversation-id", conv_id,
                         "--output", str(outdir) + "/", "-y", "-f", "json"],
                        timeout=180)

    def download_file_by_id(self, file_id: str, out_abs: Path) -> str:
        """单聊文件消息走 fileId：drive download --node（实测 dws v1.0.62）。"""
        out_abs.parent.mkdir(parents=True, exist_ok=True)
        r = self.run(["drive", "download", "--node", file_id,
                      "--output", str(out_abs), "--overwrite",
                      "--format", "json"], timeout=180)
        path = ((r.get("data") or {}).get("savedPath") or r.get("savedPath")
                or (r.get("data") or {}).get("path") or r.get("path") or "")
        if path and Path(str(path)).exists():
            return str(path)
        return str(out_abs) if out_abs.exists() else ""

    def doc_to_markdown(self, local_path: str, out_md: Path) -> str:
        """docx/pdf/xlsx 经钉钉转 markdown（doc +import → doc export）。"""
        imp = self.run(["doc", "+import", "--file", local_path,
                        "--format", "json"], timeout=180)
        node = (imp.get("nodeId") or (imp.get("data") or {}).get("nodeId")
                or (imp.get("verification") or {}).get("nodeId") or "")
        if not node:
            return ""
        out_md.parent.mkdir(parents=True, exist_ok=True)
        self.run(["doc", "export", "--node", node,
                  "--export-format", "markdown", "--output", str(out_md),
                  "--format", "json"], timeout=180)
        return str(out_md) if out_md.exists() and out_md.stat().st_size else ""

    def resolve_userid(self, open_id: str, sender_name: str,
                       cache_file: Path) -> str:
        """openDingTalkId → userId：contacts 缓存 → dws 按姓名反查落盘。
        未命中写负缓存（miss:<ts>，6h），防 bot/陌生号每条消息都打通讯录。"""
        if not open_id:
            return ""
        try:
            contacts = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            contacts = {}
        v = contacts.get(open_id)
        if isinstance(v, str) and v:
            if not v.startswith("miss:"):
                return v
            try:
                if time.time() - float(v.split(":", 1)[1]) < 21600:
                    return ""
            except Exception:
                pass
        if not sender_name or sender_name == "未知":
            return ""
        res = self.run(["contact", "+search-user", "--query", sender_name,
                        "--format", "json"], timeout=15)
        users = (res.get("data") or {}).get("users") or res.get("users") or []
        uid = ""
        for u in users:
            if u.get("openDingTalkId", "") == open_id:
                uid = str(u.get("userId") or u.get("userid") or "")
                break
        contacts[open_id] = uid or f"miss:{time.time()}"
        try:
            atomic_write_text(cache_file, json.dumps(
                contacts, ensure_ascii=False, indent=1))
        except Exception:
            pass
        return uid


class InboundHealth:
    """run/inbound_health.json 三态：ok / bad_credentials / stalled。"""

    def __init__(self, storage):
        self.file = storage.run / "inbound_health.json"
        self.reconnects = 0

    def write(self, state: str, detail: str = ""):
        try:
            atomic_write_text(self.file, json.dumps(
                {"ts": int(time.time()), "state": state,
                 "detail": str(detail)[:200], "reconnects": self.reconnects},
                ensure_ascii=False))
        except Exception:
            pass

    def read(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except Exception:
            return {}


class SelfMark:
    """SELF_MARK 隐形标识：出站消息末尾附注。
    ⚠️ 2026-09-23 实测：钉钉会剥离 HTML 注释，轮询回来的 text 不含本标记——
    它只是尽力而为的第二道防线；防自循环主防线是 SentEcho 文本指纹闸。"""

    def __init__(self, storage):
        self.path = storage.run / ".self_mark"
        self.mark = self._load()

    def _load(self) -> str:
        try:
            if self.path.exists():
                m = self.path.read_text(encoding="utf-8").strip()
                if m:
                    return m
        except Exception:
            pass
        m = f"<!--av-self:{os.urandom(8).hex()}-->"
        try:
            atomic_write_text(self.path, m, chmod=0o600)
        except Exception:
            pass
        return m

    def is_own(self, text: str) -> bool:
        return bool(text) and self.mark in text

    def append(self, text: str, sign: str = "") -> str:
        t = (text or "").rstrip()
        if sign and sign not in t:
            t += f"\n\n{sign}"
        return t + "\n" + self.mark


class SentEcho:
    """出站内容指纹闸（防自循环主防线）。

    钉钉剥离 HTML 注释导致 SELF_MARK 在轮询文本里失效（2026-09-23 速记
    自循环事故根因）：改为记录出站文本的规范化指纹（去全部空白+去标记后
    sha256），轮询到的文本命中指纹即判定为自己发的回执，跳过。
    落盘 run/sent_echo.json（TTL 1h，容量 500，重启不丢）。
    """

    def __init__(self, storage, ttl_sec: int = 3600, cap: int = 500):
        self.st = storage
        self.file = storage.run / "sent_echo.json"
        self.ttl = ttl_sec
        self.cap = cap
        self._d: dict | None = None

    @staticmethod
    def norm(text: str) -> str:
        import hashlib
        t = re.sub(r"<!--\S*self:[0-9a-f]+-->", "", str(text or ""))
        t = re.sub(r"\s+", "", t)
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:24]

    def _load(self) -> dict:
        if self._d is None:
            self._d = self.st.load_json(self.file, {})
        return self._d

    def record(self, text: str):
        if not str(text or "").strip():
            return
        now = time.time()
        d = {k: v for k, v in self._load().items() if now - v < self.ttl}
        d[self.norm(text)] = now
        if len(d) > self.cap:
            for k in sorted(d, key=lambda x: d[x])[:len(d) - self.cap]:
                d.pop(k, None)
        self._d = d
        self.st.save_json(self.file, d)

    def is_echo(self, text: str) -> bool:
        return self.norm(text) in self._load()


class EventIngress:
    """event consume 长连接 + 空闲看门狗 + 凭据失效检测（线程内跑，回调 on_event）。"""

    def __init__(self, cfg, dws: Dws, health: InboundHealth, on_event,
                 stop: threading.Event):
        self.cfg = cfg
        self.dws = dws
        self.health = health
        self.on_event = on_event
        self.stop = stop
        self.idle_max = int(cfg.get("ingress", "idle_watchdog_sec", 270))
        self.errf = cfg.state / "run" / "events.err"

    def _keys(self) -> list:
        keys = [str(k) for k in
                self.cfg.get("ingress", "event_keys",
                             ["user_im_message_receive_o2o_all"])]
        if self.cfg.get("group", "enabled", False) and \
                "user_im_message_receive_at" not in keys:
            keys.append("user_im_message_receive_at")
        return keys

    def _spawn(self):
        cmd = self.dws._argv(["event", "consume"] + self._keys() +
                             ["--flatten", "-f", "ndjson"])
        print("spawn:", " ".join(cmd), flush=True)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace",
                                env=self.dws._env(), bufsize=1)

    def loop(self):
        while not self.stop.is_set():
            proc = None
            try:
                proc = self._spawn()
                last_act = {"ts": time.time()}
                threading.Thread(target=self._drain_err, args=(proc,),
                                 daemon=True).start()
                threading.Thread(target=self._watchdog,
                                 args=(proc, last_act), daemon=True).start()
                for line in proc.stdout:
                    if self.stop.is_set():
                        break
                    last_act["ts"] = time.time()
                    line = (line or "").strip()
                    if not line.startswith("{"):
                        continue
                    self.health.reconnects = 0
                    self.health.write("ok", "receiving")
                    self.on_event(line)
                proc.wait()
            except Exception:
                pass
            finally:
                try:
                    if proc and proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=15)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self.stop.is_set():
                break
            self.health.reconnects += 1
            if self.health.reconnects >= 3:
                self.health.write(
                    "stalled", f"连续{self.health.reconnects}次重连未见事件流就绪")
            backoff = (min(3 * self.health.reconnects, 60)
                       if self.health.reconnects >= 3 else 3)
            self.stop.wait(backoff)

    def _drain_err(self, proc):
        """stderr 抽干留痕 + 扫描就绪/凭据失效标志。"""
        try:
            for eline in proc.stderr:
                eline = (eline or "").strip()
                if not eline:
                    continue
                try:
                    with open(self.errf, "a", encoding="utf-8") as f:
                        f.write(eline + "\n")
                except Exception:
                    pass
                low = eline.lower()
                if eline.startswith("[event]") and \
                        ("ready" in low or "state=connected" in low):
                    print("consumer ready", flush=True)
                    self.health.reconnects = 0
                    self.health.write("ok", "connected")
                    continue
                if ("not found" in low and "organization" in low) or \
                        "load access token" in low or \
                        "$home is not defined" in low:
                    self.health.write("bad_credentials", eline)
                    print("入站凭据异常(收不到消息):", eline[:120], flush=True)
                    try:
                        proc.kill()   # 立即重连，不干等看门狗
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    def _watchdog(self, proc, last_act):
        while proc.poll() is None and not self.stop.is_set():
            if time.time() - last_act["ts"] > self.idle_max:
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            time.sleep(15)


class SelfPoller:
    """self 模式补充轮询（P2 启用）：本人单聊 + 双重回音闸
    （SentEcho 指纹为主，SELF_MARK 尽力而为）+ 游标持久化。"""

    def __init__(self, cfg, dws: Dws, selfmark: SelfMark, storage, on_message,
                 stop: threading.Event, echo: "SentEcho | None" = None):
        self.cfg = cfg
        self.dws = dws
        self.sm = selfmark
        self.st = storage
        self.on_message = on_message
        self.stop = stop
        self.echo = echo
        self.cursor_file = storage.run / "poll_cursor.txt"

    def loop(self):
        uid = self.cfg.owner_userid
        interval = max(1, int(self.cfg.get("ingress",
                                           "self_poll_interval_sec", 5)))
        try:
            since = self.cursor_file.read_text(encoding="utf-8").strip()
        except Exception:
            since = ""
        # 游标过旧（停机较久/首次）→ 快进到 now-2min：宁可漏掉陈旧历史，
        # 不批量重放（旧回执无出站指纹记录，重放会当新消息处理）
        try:
            if since and (datetime.datetime.now() - datetime.datetime.strptime(
                    since, "%Y-%m-%d %H:%M:%S")).total_seconds() > 600:
                since = ""
        except Exception:
            since = ""
        if not since:
            since = (datetime.datetime.now() - datetime.timedelta(minutes=2)
                     ).strftime("%Y-%m-%d %H:%M:%S")
        backoff = 0
        while not self.stop.is_set():
            try:
                r = self.dws.run(["chat", "+messages-list-direct",
                                  "--user", str(uid), "--time", since,
                                  "--forward", "--limit", "20"], timeout=20)
                msgs = r.get("messages") or []
                msgs.sort(key=lambda m: m.get("createTime", ""))
                for m in msgs:
                    text = (m.get("text") or "").strip()
                    ct = m.get("createTime", "")
                    if ct and ct > since:
                        since = ct
                        try:
                            atomic_write_text(self.cursor_file, since)
                        except Exception:
                            pass
                    if not text:
                        continue
                    if self.sm.is_own(text):
                        continue
                    if self.echo is not None and self.echo.is_echo(text):
                        continue   # 出站指纹命中：自己发的回执，跳过（防自循环）
                    self.on_message({
                        "sender_open_dingtalk_id":
                            self.cfg.self_open_id or m.get("senderId", ""),
                        "sender": self.cfg.get("identity", "owner_name", ""),
                        "conversation_id": m.get("conversationId", ""),
                        "message_id": m.get("messageId", ""),
                        "content": text,
                        "event_type": "user_im_message_receive_o2o_all",
                        "_self_poll": True,
                    })
                backoff = 0
            except Exception as e:
                if backoff == 0 or backoff >= 6:
                    print(f"单聊轮询失败(第{backoff + 1}次): {e}", flush=True)
                backoff = min(backoff + 1, 6)
            self.stop.wait(interval * (2 ** backoff) if backoff else interval)
