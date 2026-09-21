#!/usr/bin/env python3
"""relay-im-bridge: 钉钉单聊/群聊消息 -> relay 任务桥。

- dws event consume 子进程读事件流（NDJSON, --flatten）
- 每会话状态机: IDLE/BUFFERING/RUNNING/WAIT_APPROVAL/WAIT_REVIEW/FOLLOWUP
- debounce 聚合多条消息为一个任务; resume_from 续会话追问
- confirm/approve 门禁透传到钉钉; 结果 markdown 回复, 长文附件
- 状态落盘 state.json, 事件落盘 events.ndjson, 每轮归档 rounds/
仅标准库。
"""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("BRIDGE_STATE_DIR",
                           "/mnt/vol-eltaah12/workspace/im-bridge")
ENV_FILE = os.path.join(STATE_DIR, "bridge.env")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
EVENTS_FILE = os.path.join(STATE_DIR, "events.ndjson")
ROUNDS_DIR = os.path.join(STATE_DIR, "rounds")
MEDIA_DIR = os.path.join(STATE_DIR, "media")

DEFAULT_CONFIG = {
    "personal": {"mode": "whitelist", "whitelist": [], "blacklist": []},
    "group": {"enabled": False, "mode": "whitelist", "whitelist": [],
              "blacklist": []},
    "debounce_sec": 20,
    "followup_debounce_sec": 30,
    "session_ttl_sec": 1800,
    "poll_sec": 3,
    "self_open_id": "D8YACgStfKBfWDE6Jwr6sZFGR45xXK0wc",
    "keywords": {
        "submit": ["提交", "开始执行"],
        "cancel": ["取消"],
        "confirm": ["确认", "同意"],
        "deny": ["拒绝", "不同意"],
        "conflict": ["冲突"],
    },
    "result_max_chars": 3000,
    "ack_emoji": "收到",
    "submit_emoji": "赞",
    "noise_ignore": ["你好", "您好", "在吗", "在么", "hi", "hello", "嗯",
                     "嗯嗯", "好的", "好", "收到", "ok", "OK", "谢谢",
                     "多谢", "辛苦了", "测试"],
}

EMOJI_RE = re.compile(r"^(\[[^\[\]]{1,12}\]\s*)+$")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def load_env(path=ENV_FILE):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = path or os.path.join(STATE_DIR, "config.json")
    if os.path.exists(p):
        user = json.load(open(p))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


class Relay:
    def __init__(self, env):
        self.base = env["RELAY_BASE"].rstrip("/")
        self.dev = env["RELAY_DEVICE_ID"]
        self.secret = env["RELAY_DEVICE_SECRET"].encode()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        digest = hashlib.sha256(data).hexdigest()
        msg = f"{ts}\n{method}\n{path}\n{digest}\n{nonce}"
        sig = hmac.new(self.secret, msg.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.base + path, data=data or None,
                                     method=method, headers={
                                         "X-Device-Id": self.dev,
                                         "X-Timestamp": ts,
                                         "X-Nonce": nonce,
                                         "X-Signature": sig,
                                         "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_error": e.code, "_detail": e.read().decode()[:300]}

    def create_task(self, description, resume_from=None):
        body = {"description": description, "priority": "normal",
                "project": "im-bridge"}
        if resume_from:
            body["resume_from"] = resume_from
        return self._req("POST", "/tasks", body)

    def get_task(self, tid):
        return self._req("GET", f"/tasks/{tid}")

    def approve(self, tid, decision):
        return self._req("POST", f"/tasks/{tid}/approve",
                         {"decision": decision})

    def confirm(self, tid, outcome):
        return self._req("POST", f"/tasks/{tid}/confirm",
                         {"outcome": outcome})

    def cancel(self, tid):
        return self._req("POST", f"/tasks/{tid}/cancel")


class Dws:
    """dws CLI 封装（阻塞子进程）。dry=True 时只打印不执行发送。"""

    def __init__(self, dry=False):
        self.dry = dry
        self.bin = shutil.which("dws") or "/usr/bin/dws"

    def _run(self, args, timeout=60):
        if self.dry and (args[:2] == ["chat", "message"] or
                         args[1:2] == ["+conversation-mark-read"]):
            log("[dry-send]", " ".join(str(a)[:80] for a in args))
            return {"dry": True}
        p = subprocess.run([self.bin] + args, capture_output=True,
                           text=True, timeout=timeout)
        if p.returncode != 0:
            log("dws error:", (p.stderr or p.stdout)[:300])
        try:
            return json.loads(p.stdout)
        except Exception:
            return {"raw": p.stdout[:300]}

    def _target_args(self, t):
        if t.get("is_group"):
            return ["--conversation-id", t["conversation_id"]]
        return ["--open-dingtalk-id", t["open_dingtalk_id"]]

    def send_text(self, target, content):
        return self._run(["chat", "message", "send"] +
                         self._target_args(target) +
                         ["--content", content, "-y", "-f", "json"])

    def send_file(self, target, path):
        return self._run(["chat", "message", "send"] +
                         self._target_args(target) +
                         ["--msg-type", "file", "--file", path,
                          "-y", "-f", "json"])

    def react(self, conv_id, msg_id, emoji):
        return self._run(["chat", "message", "add-emoji",
                          "--conversation-id", conv_id,
                          "--message-id", msg_id,
                          "--emoji", emoji, "-y", "-f", "json"])

    def mark_read(self, conv_id, msg_id):
        return self._run(["chat", "+conversation-mark-read",
                          "--conversation-id", conv_id,
                          "--message-id", msg_id, "--yes", "-f", "json"])

    def download_media(self, media_id, message_id, conv_id, outdir):
        os.makedirs(outdir, exist_ok=True)
        r = self._run(["chat", "message", "download-media",
                       "--type", "mediaId", "--resource-id", media_id,
                       "--message-id", message_id,
                       "--open-conversation-id", conv_id,
                       "--output", outdir + "/", "-y", "-f", "json"],
                      timeout=120)
        return r


def find_media_ids(obj, out):
    """递归找 mediaId/downloadCode 类字段。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("mediaId", "downloadCode", "pictureDownloadCode") \
                    and isinstance(v, str) and v:
                out.append(v)
            else:
                find_media_ids(v, out)
    elif isinstance(obj, list):
        for i in obj:
            find_media_ids(i, out)
    elif isinstance(obj, str) and obj[:1] in "[{":
        try:
            find_media_ids(json.loads(obj), out)
        except Exception:
            pass


class Bridge:
    def __init__(self, cfg, relay, dws, inject=None):
        self.cfg = cfg
        self.relay = relay
        self.dws = dws
        self.inject = inject
        self.q = queue.Queue()
        self.sessions = {}
        self.seen = []
        self.seen_set = set()
        self.stop = False
        self._load_state()

    # ---- 持久化 ----
    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                s = json.load(open(STATE_FILE))
                self.sessions = s.get("sessions", {})
                self.seen = s.get("seen", [])[-5000:]
                self.seen_set = set(self.seen)
            except Exception as e:
                log("state load failed:", e)

    def _save_state(self):
        tmp = STATE_FILE + ".tmp"
        json.dump({"sessions": self.sessions, "seen": self.seen[-5000:]},
                  open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)

    def _archive(self, tid, kind, payload):
        os.makedirs(ROUNDS_DIR, exist_ok=True)
        with open(os.path.join(ROUNDS_DIR, f"{tid}.jsonl"), "a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind,
                                "data": payload}, ensure_ascii=False) + "\n")

    # ---- 事件源 ----
    def _spawn_consumer(self):
        keys = ["user_im_message_receive_o2o_all"]
        if self.cfg["group"].get("enabled"):
            keys.append("user_im_message_receive_group_all")
        cmd = [shutil.which("dws") or "/usr/bin/dws", "event", "consume"] + \
            keys + ["--flatten", "-f", "ndjson"]
        log("spawn:", " ".join(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    def _reader(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.q.put(line)
        log("consumer stdout closed")

    def _event_loop(self):
        backoff = 2
        while not self.stop:
            proc = self._spawn_consumer()
            t = threading.Thread(target=self._reader, args=(proc,),
                                 daemon=True)
            t.start()
            err_tail = []

            def _err():
                for l in proc.stderr:
                    err_tail.append(l.strip())
                    if "ready" in l:
                        log("consumer ready")
            threading.Thread(target=_err, daemon=True).start()
            while proc.poll() is None and not self.stop:
                time.sleep(1)
            if self.stop:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except Exception:
                    proc.kill()
                break
            log(f"consumer exited rc={proc.returncode}, retry in {backoff}s",
                " | ".join(err_tail[-6:])[:600])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---- 过滤 ----
    def _allowed(self, scope, sender, sender_id):
        c = self.cfg[scope]
        mode = c.get("mode", "whitelist")
        for ident in c.get("whitelist" if mode == "whitelist" else
                           "blacklist", []):
            hit = ident in (sender, sender_id)
            if mode == "whitelist" and hit:
                return True
            if mode == "blacklist" and hit:
                return False
        return mode == "blacklist"

    # ---- 事件处理 ----
    def handle_line(self, line):
        with open(EVENTS_FILE, "a") as f:
            f.write(line + "\n")
        try:
            ev = json.loads(line)
        except Exception:
            return
        et = str(ev.get("event_type") or ev.get("type") or "")
        if et.startswith("user_") and "message_receive" not in et:
            return
        if not (ev.get("content") is not None and
                (ev.get("sender_open_dingtalk_id") or ev.get("sender"))):
            return
        eid = ev.get("event_id") or ev.get("message_id")
        if eid:
            if eid in self.seen_set:
                return
            self.seen.append(eid)
            self.seen_set.add(eid)
        sender = ev.get("sender") or ""
        sid = ev.get("sender_open_dingtalk_id") or ""
        content = ev.get("content")
        if content is None:
            return
        if sid and sid == self.cfg["self_open_id"]:
            return
        is_group = "group" in str(et)
        if is_group:
            if not self.cfg["group"].get("enabled"):
                return
            scope = "group"
        else:
            scope = "personal"
        if not self._allowed(scope, sender, sid):
            log(f"filtered ({scope}): {sender}")
            return
        key = (f"g:{ev.get('conversation_id')}:{sid}" if is_group
               else f"o:{sid}")
        s = self.sessions.setdefault(key, self._new_session(sender, sid,
                                                            ev, is_group))
        s["reply"]["sender"] = sender
        s["last_activity"] = time.time()
        s["last_msg_id"] = ev.get("message_id") or ""
        s["last_conv_id"] = ev.get("conversation_id") or ""
        if s["last_conv_id"] and s["last_msg_id"]:
            try:
                self.dws.mark_read(s["last_conv_id"], s["last_msg_id"])
            except Exception as e:
                log("mark-read failed:", e)
        text = self._extract_text(ev, content)
        self._on_message(key, s, text.strip())
        self._save_state()

    def _new_session(self, sender, sid, ev, is_group):
        return {
            "state": "IDLE", "buffer": [], "followups": [],
            "task_id": None, "last_task_id": None, "deadline": 0,
            "notified_followup": False, "last_activity": time.time(),
            "reply": {
                "is_group": is_group,
                "open_dingtalk_id": sid,
                "conversation_id": ev.get("conversation_id") or "",
                "sender": sender,
            },
        }

    def _extract_text(self, ev, content):
        """文本直出; 媒体消息尝试下载并把本地路径拼进文本。"""
        media = []
        find_media_ids(content, media)
        if not media and isinstance(content, str) and content[:1] in "[{":
            try:
                d = json.loads(content)
                if isinstance(d, dict):
                    txt = d.get("text") or d.get("content") or d.get("title")
                    find_media_ids(d, media)
                    if txt and not media:
                        content = str(txt)
            except Exception:
                pass
        parts = [] if media else [str(content)]
        if media:
            mid = ev.get("message_id") or ""
            cid = ev.get("conversation_id") or ""
            for m in dict.fromkeys(media):
                r = self.dws.download_media(m, mid, cid, MEDIA_DIR)
                path = (r or {}).get("path") or (r or {}).get("output")
                if path and os.path.exists(str(path)):
                    parts.append(f"[附件] {path}")
                else:
                    parts.append(f"[附件下载失败 mediaId={m[:20]}]")
        q = ev.get("quoted_message")
        if q:
            parts.insert(0, f"[引用] {str(q)[:200]}")
        return "\n".join(parts)

    def _kw(self, text, name):
        return text in self.cfg["keywords"].get(name, [])

    def _is_noise(self, text):
        t = text.strip()
        if not t:
            return True
        if EMOJI_RE.match(t):
            return True
        return t in self.cfg.get("noise_ignore", [])

    def _on_message(self, key, s, text):
        st = s["state"]
        if st == "WAIT_APPROVAL" and s.get("task_id"):
            if self._kw(text, "confirm"):
                r = self.relay.approve(s["task_id"], "approve")
                self._notify(s, f"已放行，任务 #{s['task_id']} 继续执行。")
                s["state"] = "RUNNING"
                self._archive(s["task_id"], "approve", r)
                return
            if self._kw(text, "deny"):
                r = self.relay.approve(s["task_id"], "deny")
                self._notify(s, f"已拒绝，任务 #{s['task_id']} 终止。")
                s["state"] = "IDLE"
                self._archive(s["task_id"], "deny", r)
                return
            self._reprompt(s)
            return
        if st == "WAIT_REVIEW" and s.get("task_id"):
            if self._kw(text, "confirm"):
                r = self.relay.confirm(s["task_id"], "done")
                self._notify(s, f"任务 #{s['task_id']} 已确认完成。")
                s["state"] = "IDLE"
                self._archive(s["task_id"], "confirm-done", r)
                return
            if self._kw(text, "conflict") or self._kw(text, "deny"):
                r = self.relay.confirm(s["task_id"], "conflict")
                self._notify(s, f"任务 #{s['task_id']} 已标记冲突。")
                s["state"] = "IDLE"
                self._archive(s["task_id"], "confirm-conflict", r)
                return
            self._reprompt(s)
            return
        if self._is_noise(text):
            log(f"noise ignored ({st}): {text[:30]!r}")
            self._react(s, "ack_emoji")
            return
        if st == "BUFFERING":
            if self._kw(text, "submit"):
                self._flush(s)
                return
            if self._kw(text, "cancel"):
                s["buffer"] = []
                s["state"] = "IDLE"
                self._notify(s, "已放弃本轮内容。")
                return
            s["buffer"].append(text)
            s["deadline"] = time.time() + self.cfg["debounce_sec"]
            return
        if st in ("RUNNING", "WAIT_APPROVAL", "WAIT_REVIEW", "FOLLOWUP"):
            if self._kw(text, "cancel"):
                tid = s.get("task_id")
                if tid:
                    r = self.relay.cancel(tid)
                    self._archive(tid, "cancel-by-user", r)
                    self._notify(s, f"已取消任务 #{tid}，补充内容一并丢弃。")
                else:
                    self._notify(s, "已放弃待提交的追问内容。")
                s["task_id"] = None
                s["followups"] = []
                s["buffer"] = []
                s["state"] = "IDLE"
                return
            s["followups"].append(text)
            if not s.get("notified_followup"):
                s["notified_followup"] = True
                self._react(s, "ack_emoji")
            return
        # IDLE
        if any(self._kw(text, k) for k in
               ("submit", "cancel", "confirm", "deny", "conflict")):
            return
        s["buffer"] = [text]
        s["state"] = "BUFFERING"
        s["deadline"] = time.time() + self.cfg["debounce_sec"]
        self._react(s, "ack_emoji")

    # ---- 提交/轮询 ----
    def _flush(self, s, followup=False):
        items = s["followups"] if followup else s["buffer"]
        if not items:
            s["state"] = "IDLE"
            return
        desc = "\n\n".join(items)
        resume = None
        if s.get("last_task_id") and \
                time.time() - s["last_activity"] < self.cfg["session_ttl_sec"]:
            resume = s["last_task_id"]
        r = self.relay.create_task(desc, resume_from=resume)
        if "_error" in r:
            self._notify(s, f"提交 relay 失败：{r['_error']} "
                            f"{r.get('_detail', '')[:120]}")
            s["state"] = "IDLE"
            return
        tid = r["task_id"]
        s["task_id"] = tid
        s["state"] = "RUNNING"
        s["notified_followup"] = False
        if followup:
            s["followups"] = []
        else:
            s["buffer"] = []
        self._archive(tid, "submit", {"desc": desc, "resume_from": resume,
                                      "resp": r})
        self._react(s, "submit_emoji")
        log(f"task #{tid} submitted (resume={resume})")

    def _poll_sessions(self):
        for key, s in list(self.sessions.items()):
            if s["state"] not in ("RUNNING", "WAIT_APPROVAL", "WAIT_REVIEW"):
                continue
            tid = s.get("task_id")
            if not tid:
                s["state"] = "IDLE"
                continue
            row = self.relay.get_task(tid)
            if "_error" in row:
                log(f"poll #{tid} error {row['_error']}")
                continue
            status = row.get("status")
            if status in ("queued", "running"):
                if s["state"] != "RUNNING":
                    s["state"] = "RUNNING"
                continue
            if status == "pending_approval" and s["state"] != "WAIT_APPROVAL":
                s["state"] = "WAIT_APPROVAL"
                s["gate_cmd"] = row.get("blocked_cmd") or ""
                cmd = (row.get("blocked_cmd") or "").splitlines()
                cmd = (cmd[0] if cmd else "")[:60]
                self._notify(s, f"任务 #{tid} 待审批：门禁拦截命令 `{cmd}…`\n\n"
                                "回复「确认」放行，「拒绝」终止。")
                self._archive(tid, "pending_approval", row.get("blocked_cmd"))
                continue
            if status == "review" and s["state"] != "WAIT_REVIEW":
                s["state"] = "WAIT_REVIEW"
                self._send_result(s, row, prefix=f"任务 #{tid} 待人工确认。")
                self._archive(tid, "review", {"result": row.get("result")})
                continue
            if status in ("done", "conflict", "failed", "cancelled"):
                self._finish(s, row)
                continue

    def _send_result(self, s, row, prefix=""):
        tid = row.get("id")
        result = (row.get("result") or "").strip()
        head = prefix or f"任务 #{tid} 完成。"
        limit = self.cfg["result_max_chars"]
        if len(result) > limit:
            os.makedirs(ROUNDS_DIR, exist_ok=True)
            fp = os.path.join(ROUNDS_DIR, f"{tid}_result.md")
            open(fp, "w").write(result)
            self.dws.send_text(s["reply"],
                               head + "\n\n" + result[:1500] +
                               "\n\n…（全文见附件）")
            self.dws.send_file(s["reply"], fp)
        else:
            self.dws.send_text(s["reply"],
                               head + ("\n\n" + result if result else
                                        "（无文本结果）"))

    def _finish(self, s, row):
        tid = row.get("id")
        status = row.get("status")
        self._archive(tid, status, {"result": row.get("result"),
                                    "error": row.get("error")})
        if status == "done":
            self._send_result(s, row)
            s["last_task_id"] = tid
        elif status == "conflict":
            self._send_result(s, row, prefix=f"任务 #{tid} 完成但检测到文件"
                                             "冲突，请人工复核。")
            s["last_task_id"] = tid
        elif status == "failed":
            self._notify(s, f"任务 #{tid} 执行失败："
                            f"{(row.get('error') or '未知错误')[:300]}")
        elif status == "cancelled":
            self._notify(s, f"任务 #{tid} 已取消。")
        s["task_id"] = None
        if s["followups"]:
            s["state"] = "FOLLOWUP"
            s["deadline"] = time.time() + self.cfg["followup_debounce_sec"]
            log(f"{len(s['followups'])} followup(s) queued, flush in "
                f"{self.cfg['followup_debounce_sec']}s")
        else:
            s["state"] = "IDLE"

    def _reprompt(self, s):
        now = time.time()
        if now - s.get("last_reprompt", 0) < 60:
            return
        s["last_reprompt"] = now
        tid = s.get("task_id")
        if s["state"] == "WAIT_APPROVAL":
            cmd = (s.get("gate_cmd") or "").splitlines()
            cmd = (cmd[0] if cmd else "")[:60]
            self._notify(s, f"任务 #{tid} 待审批：门禁拦截命令 `{cmd}…`\n\n"
                            "回复「确认」放行，「拒绝」终止。")
        else:
            self._notify(s, f"任务 #{tid} 待人工确认：回复「确认」完成，"
                            "「冲突」标记文件冲突。")

    def _notify(self, s, text):
        log(f"-> {s['reply'].get('sender')}: {text[:80]}")
        try:
            self.dws.send_text(s["reply"], text)
        except Exception as e:
            log("notify failed:", e)

    def _react(self, s, cfg_key):
        emoji = self.cfg.get(cfg_key)
        mid = s.get("last_msg_id") or ""
        cid = s.get("last_conv_id") or ""
        if not emoji or not mid or not cid:
            log("react skipped: missing emoji/msg/conv id")
            return
        log(f"react [{emoji}] -> msg {mid[:24]}")
        try:
            self.dws.react(cid, mid, emoji)
        except Exception as e:
            log("react failed:", e)

    # ---- 定时 ----
    def _tick(self):
        now = time.time()
        for key, s in list(self.sessions.items()):
            if s["state"] == "BUFFERING" and s["deadline"] and \
                    now >= s["deadline"]:
                s["deadline"] = 0
                self._flush(s)
                self._save_state()
            elif s["state"] == "FOLLOWUP" and s["deadline"] and \
                    now >= s["deadline"]:
                s["deadline"] = 0
                self._flush(s, followup=True)
                self._save_state()
        # 过期空会话清理
        for key in [k for k, s in self.sessions.items()
                    if s["state"] == "IDLE" and not s["buffer"] and
                    now - s["last_activity"] > self.cfg["session_ttl_sec"] * 2]:
            del self.sessions[key]

    # ---- 主循环 ----
    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        os.makedirs(ROUNDS_DIR, exist_ok=True)
        os.makedirs(MEDIA_DIR, exist_ok=True)
        for s in self.sessions.values():
            if s["state"] in ("WAIT_APPROVAL", "WAIT_REVIEW") and \
                    s.get("task_id"):
                if not s.get("gate_cmd"):
                    row = self.relay.get_task(s["task_id"])
                    s["gate_cmd"] = row.get("blocked_cmd") or ""
                s["last_reprompt"] = 0
                self._reprompt(s)
        if self.inject:
            for line in open(self.inject):
                line = line.strip()
                if line:
                    self.handle_line(line)
            deadline = time.time() + float(os.environ.get(
                "BRIDGE_INJECT_WAIT", "180"))
            while time.time() < deadline and not self.stop:
                self._tick()
                self._poll_sessions()
                time.sleep(self.cfg["poll_sec"])
                if all(s["state"] == "IDLE" for s in self.sessions.values()) \
                        and self.sessions:
                    break
            self._save_state()
            log("inject run finished")
            return
        threading.Thread(target=self._event_loop, daemon=True).start()
        last_poll = 0
        while not self.stop:
            try:
                line = self.q.get(timeout=0.5)
                self.handle_line(line)
            except queue.Empty:
                pass
            now = time.time()
            self._tick()
            if now - last_poll >= self.cfg["poll_sec"]:
                last_poll = now
                self._poll_sessions()
                self._save_state()
        log("bridge stopping")
        self._save_state()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--inject", help="从文件读事件 NDJSON（自测）")
    ap.add_argument("--dry-send", action="store_true",
                    help="不真发钉钉消息，只打印")
    args = ap.parse_args()
    cfg = load_config(args.config)
    env = load_env()
    bridge = Bridge(cfg, Relay(env), Dws(dry=args.dry_send),
                    inject=args.inject)
    bridge.run()


if __name__ == "__main__":
    main()
