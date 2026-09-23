#!/usr/bin/env python3
"""app/main.py —— relay-avatar 守护入口（双 ingress 循环）。

钉钉/dws 入口（event consume + self 轮询）与 relay API 入口（avatar-cli 同源客户端）
收敛到同一策略核：权限分层/审批流/限流/注入清洗/审计只有一份实现、一份 policy.toml。
执行默认走 relay 后端（门禁透传钉钉）；local 后端仅 self/L3（P2，默认关）。
仅标准库。用法：python3 app/main.py [--inject F] [--dry-send] [--state DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as C
from app import integrity
from app.audit import Audit
from app.dashboard import Dashboard
from app.exec_local import ExecLocal
from app.exec_relay import ExecRelay
from app.file_inbox import FileInbox, extract_file_from_event, find_media_ids
from app.group_watch import GroupWatch, parse_watch_cmd
from app.ingress_dingtalk import (Dws, EventIngress, InboundHealth, SelfMark,
                                  SelfPoller, md_hardbreak)
from app.ingress_relay import RelayClient
from app.knowledge import Knowledge
from app.monitor import Monitor
from app.policy import Policy, sanitize_field, sanitize_injected
from app.session import SessionManager
from app.storage import Storage

EMOJI_RE = re.compile(r"^(\[[^\[\]]{1,12}\]\s*)+$")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


class Avatar:
    def __init__(self, cfg, inject: str = "", dry: bool = False):
        self.cfg = cfg
        self.inject = inject
        self.st = Storage(cfg)
        self.audit = Audit(cfg.state)
        self.policy = Policy(cfg, self.st, self.audit)
        self.sess = SessionManager(cfg, self.st)
        self.dws = Dws(cfg, self.audit, dry=dry)
        self.relay = RelayClient(cfg.env)
        self.exec_relay = ExecRelay(cfg, self.relay, self.st, self.audit)
        self.exec_local = ExecLocal(cfg, self.st, self.audit, self.policy)
        self.inbox = FileInbox(cfg, self.dws, self.st, self.audit)
        self.gwatch = GroupWatch(cfg, self.st, self.audit)
        self.know = Knowledge(cfg, self.st)
        self.health = InboundHealth(self.st)
        self.selfmark = SelfMark(self.st)
        self.monitor = Monitor(cfg, self.st, self.dws, self.audit)
        self.dashboard = Dashboard(cfg, self.st)
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.contacts = self.st.run / "contacts.json"
        self.sign = str(cfg.get("identity", "sign", "") or "")
        self._guide_seen: dict = {}

    # ── 发送 ──
    def _notify(self, target: dict, text: str):
        try:
            self.dws.send_text(target, md_hardbreak(
                self.selfmark.append(text, self.sign)))
        except Exception as e:
            log("notify failed:", e)

    def _notify_session(self, s: dict, text: str):
        log(f"-> {s['reply'].get('sender')}: {text[:80]}")
        self._notify(s["reply"], text)

    def _dm(self, uid: str, text: str):
        self._notify({"user_id": str(uid)}, text)

    def _react(self, s: dict, cfg_key: str):
        emoji = self.cfg.get("ingress", cfg_key)
        mid = s.get("last_msg_id") or ""
        cid = s.get("last_conv_id") or ""
        if not emoji or not mid or not cid:
            return
        try:
            self.dws.react(cid, mid, str(emoji))
        except Exception as e:
            log("react failed:", e)

    # ── 事件入口 ──
    def handle_line(self, line: str):
        self.st.append_event(line)
        try:
            ev = json.loads(line)
        except Exception:
            return
        self.on_event(ev)

    def on_event(self, ev: dict):
        et = str(ev.get("event_type") or ev.get("type") or "")
        if et.startswith("user_") and "message_receive" not in et:
            return
        content = ev.get("content")
        sid = ev.get("sender_open_dingtalk_id") or ev.get("senderId") or ""
        if content is None or not (sid or ev.get("sender")):
            return
        eid = ev.get("event_id") or ev.get("message_id")
        if eid and self.sess.already_seen(str(eid)):
            return
        sender = sanitize_field(ev.get("sender") or "", 40)
        if sid and sid == self.cfg.self_open_id and not ev.get("_self_poll"):
            return
        if self.policy.is_bot_blocked(sid):
            self.audit.log("bot_blocked", sid[:16], str(content)[:60])
            return
        is_group = ("group" in et) or et == "user_im_message_receive_at"
        if is_group and not self.cfg.get("group", "enabled", False):
            return
        if is_group:
            ev["content"] = re.sub(r"@\S+\s*", "", str(content)).strip()
            content = ev["content"]
            if not content:
                return
        uid = str(ev.get("_uid") or "")
        if not uid:
            if ev.get("_self_poll"):
                uid = self.cfg.owner_userid
            else:
                uid = self.dws.resolve_userid(sid, sender, self.contacts)
        ev["_uid"] = uid
        self.gwatch.collect(ev)   # enabled=false 时 no-op
        key = (f"g:{ev.get('conversation_id')}:{sid}" if is_group
               else f"o:{sid}")
        s = self.sess.get_or_create(key, sender, sid, ev, is_group)
        self.sess.touch(s, ev)

        # 超管审批指令（单聊，先于会话流拦截）
        if uid and self.policy.is_superadmin(uid) and not is_group:
            ap = self.policy.parse_approval(str(content))
            if ap:
                self._dm(uid, self._do_approval(ap, uid))
                return
        lvl = self.policy.level_of(uid, sid, sender)
        if lvl == "BLOCKED":
            return
        # L3：文件投递/问答（不进通用会话流）
        if lvl == "L3" and not is_group:
            self._mark_read(s)
            receipt = self.inbox.receive(uid, sender, ev)
            if receipt:
                self._dm(uid or sid, receipt)
                return
            text = str(content).strip()
            if text and not self._is_noise(text):
                self._react(s, "ack_emoji")
                self._dm(uid or sid, self.inbox.l3_qa(
                    uid, text,
                    self.exec_local if self.exec_local.available() else None))
            return
        # guest：引导申请授权（审批流）
        if lvl == "NONE":
            fb = self._handle_unauthorized(uid, sender, str(content),
                                           is_group, ev)
            if fb:
                self._notify(s["reply"], fb)
            return
        # 授权用户（super/L1/L2）：监控指令 → 会话流
        wc = parse_watch_cmd(str(content))
        if wc:
            fb = self.gwatch.handle_cmd(wc, uid,
                                        ev.get("conversation_id") or "",
                                        is_group)
            if fb:
                self._notify(s["reply"], fb)
            return
        self._mark_read(s)
        text = self._extract_text(ev, content, uid, sender)
        self._on_message(key, s, text.strip(), uid)
        self.sess.save()

    def _mark_read(self, s: dict):
        if s.get("last_conv_id") and s.get("last_msg_id"):
            try:
                self.dws.mark_read(s["last_conv_id"], s["last_msg_id"])
            except Exception as e:
                log("mark-read failed:", e)

    # ── 文本/媒体提取（媒体统一走 store 台账登记） ──
    def _extract_text(self, ev: dict, content, uid: str, sender: str) -> str:
        parts = []
        q = ev.get("quoted_message") or ev.get("quoted")
        if isinstance(q, dict):
            q = q.get("text") or str(q)
        if q:
            parts.append(f"[引用] {sanitize_injected(str(q)[:200])}")
        fi = extract_file_from_event(ev) if isinstance(content, str) \
            else {"file_id": "", "media_id": "", "file_name": ""}
        media: list = []
        if not (fi["file_id"] or fi["media_id"]):
            find_media_ids(content, media)
        if fi["file_id"] or fi["media_id"]:
            receipt = self.inbox.receive(uid, sender, ev)
            parts.append(receipt or "[附件处理失败]")
            return "\n".join(parts)
        if media:
            mid = ev.get("message_id") or ""
            cid = ev.get("conversation_id") or ""
            for m in dict.fromkeys(media):
                r = self.dws.download_media(m, mid, cid, self.st.staging)
                path = (r or {}).get("path") or (r or {}).get("output")
                if path and os.path.exists(str(path)):
                    final, _ok = self.inbox.register(Path(str(path)), uid,
                                                     sender)
                    parts.append(f"[附件] {final}")
                else:
                    parts.append(f"[附件下载失败 mediaId={str(m)[:20]}]")
            return "\n".join(parts)
        if isinstance(content, str) and content[:1] in "[{":
            try:
                d = json.loads(content)
                if isinstance(d, dict):
                    txt = d.get("text") or d.get("content") or d.get("title")
                    if txt:
                        content = str(txt)
            except Exception:
                pass
        parts.append(str(content))
        return "\n".join(parts)

    # ── 审批流 ──
    def _resolve_pending(self, target: str):
        matches = self.policy.find_pending_all(target)
        if not matches:
            return None, None, f"没找到「{target}」的待审申请（可能已处理）。"
        if len(matches) > 1:
            lines = "\n".join(f"- {i.get('name', '?')}（工号 {u}）"
                              for u, i in matches)
            return None, None, f"有多个叫「{target}」的申请，请用**工号**指定：\n{lines}"
        u, i = matches[0]
        return u, i, ""

    def _do_approval(self, ap, su_uid: str) -> str:
        kind, target = ap
        uid, info, err = self._resolve_pending(target)
        if err:
            return err
        name = info.get("name", target)
        avatar = self.cfg.avatar_name
        if kind == "deny":
            self.policy.revoke_pending(uid)
            self.audit.log("reject_access", su_uid, f"驳回 {name}({uid})")
            return f"已驳回 {name}（{uid}）的申请。"
        tier = "L3" if kind == "grant_l3" else "L2"
        note = ""
        if kind == "grant_l3" and info.get("title") != "file_inbox":
            note = f"\n⚠️ 对方申请的是**完整权限**，你只给了文件投递(L3)。如需完整权限回 `同意 {uid}`。"
        if kind == "grant" and info.get("title") == "file_inbox":
            note = f"\n⚠️ 对方只申请了**文件投递**，你给了**完整权限**(L2)。如只想给文件投递回 `同意传 {uid}`。"
        self.policy.grant(uid, name, tier=tier, by_name="管理员")
        self.audit.log("grant_access", su_uid, f"授权 {name}({uid}) tier={tier}")
        days = self.policy.grant_valid_days
        if tier == "L3":
            self._dm(uid, f"✅ 你的文件投递申请已通过～ 现在可以把文件发给我、"
                          f"并就这些文件提问了。我是「{avatar}」。")
            return f"✅ 已给 {name}（{uid}）开通**文件投递**（L3，{days}天有效），已通知本人。{note}"
        self._dm(uid, f"✅ 你的使用申请已通过，现在可以用我了～ 我是「{avatar}」。")
        return f"✅ 已开通 {name}（{uid}）的权限（L2，{days}天有效），已通知本人。{note}"

    def _handle_unauthorized(self, uid: str, sender: str, text: str,
                             is_group: bool, ev: dict) -> str:
        avatar = self.cfg.avatar_name
        owner = self.cfg.get("identity", "owner_name", "管理员")
        conv_id = ev.get("conversation_id") or ""
        if is_group:
            gkey = f"{conv_id}:{uid}"
            now = time.time()
            if now - self._guide_seen.get(gkey, 0) < 3600:
                return ""
            self._guide_seen[gkey] = now
            return (f"@{sender} 你好，我是「{avatar}」～ 你还没开通使用权限，"
                    "**私聊我发『申请授权』** 我帮你转管理员开通。")
        t = (text or "").strip().lstrip("/／").strip()
        sid = ev.get("sender_open_dingtalk_id") or ""
        if re.match(r"^(申请传文件|申请文件|传文件授权|申请上传|申请投递)", t):
            if uid:
                self.policy.record_pending(uid, sender, "file_inbox",
                                           openid=sid, conv_id=conv_id)
                self._notify_superadmins(
                    f"# 📮 文件投递申请\n**{sender}**（工号 {uid}）申请**文件投递**"
                    f"权限（只能传文件+就自己文件问答）。\n\n---\n批准回复 `同意传 {uid}`；"
                    f"给完整权限回 `同意 {uid}`；拒绝回 `拒绝 {uid}`。")
                self.audit.log("access_request", uid, "file_inbox")
                return (f"已帮你提交**文件投递**申请，管理员批准后你就能把文件发给我、"
                        f"并就这些文件提问了。\n我是「{avatar}」——{owner}团队的数字员工。")
            return "⚠️ 没能识别你的身份（工号解析失败），请稍后再试或联系管理员。"
        if re.match(r"^(申请授权|授权申请|申请开通|求授权)$|"
                    r"^(申请授权|授权申请|申请开通|求授权)\b", t) or t == "开通":
            if uid:
                self.policy.record_pending(uid, sender, "", openid=sid,
                                           conv_id=conv_id)
                self._notify_superadmins(
                    f"# 🔔 授权申请\n**{sender}**（工号 {uid}）申请使用「{avatar}」。\n\n"
                    f"---\n同意回复 `同意 {uid}`（或 `同意 {sender}`），拒绝回复 `拒绝 {uid}`。\n"
                    f"若只给文件投递权限（低权限），回 `同意传 {uid}`。")
                self.audit.log("access_request", uid, "full")
                return (f"已帮你提交授权申请，管理员审批通过后就能用我了。\n"
                        f"我是「{avatar}」——{owner}团队的数字员工，"
                        "能答疑、查知识、整理材料、协办事务。")
            return "⚠️ 没能识别你的身份（工号解析失败），请稍后再试或联系管理员。"
        expired = self.policy.dyn_expired(uid) if uid else {}
        if expired:
            was_l3 = expired.get("tier") == "L3"
            kind = "文件投递" if was_l3 else "使用"
            recmd = "申请传文件" if was_l3 else "申请授权"
            return (f"⏳ 你之前的**{kind}权限已过期**了。\n\n"
                    f"想继续用的话，**回复『{recmd}』** 我帮你转管理员续期～")
        return (f"你好👋 我是「{avatar}」，{owner}团队的数字员工，"
                "可以答疑、查知识、整理材料、协办事务。\n\n"
                "你还没有使用权限，**回复『申请授权』** 我帮你转管理员开通；"
                "若只想发文件给我整理、就发『申请传文件』。")

    def _notify_superadmins(self, text: str):
        for su in sorted(self.policy.superadmin):
            self._dm(su, text)

    # ── 会话流（自 im-bridge 演进） ──
    def _kw(self, text: str, name: str) -> bool:
        return text in self.cfg.get("keywords", name, [])

    def _is_noise(self, text: str) -> bool:
        t = text.strip()
        if not t or EMOJI_RE.match(t):
            return True
        return t in self.cfg.get("ingress", "noise_ignore", [])

    def _on_message(self, key: str, s: dict, text: str, uid: str):
        st = s["state"]
        if st == "WAIT_APPROVAL" and s.get("task_id"):
            if self._kw(text, "confirm"):
                r = self.exec_relay.approve(s["task_id"], "approve", uid)
                self._notify_session(s, f"已放行，任务 #{s['task_id']} 继续执行。")
                s["state"] = "RUNNING"
                self.st.archive_round(s["task_id"], "approve", r)
                return
            if self._kw(text, "deny"):
                r = self.exec_relay.approve(s["task_id"], "deny", uid)
                self._notify_session(s, f"已拒绝，任务 #{s['task_id']} 终止。")
                s["state"] = "IDLE"
                self.st.archive_round(s["task_id"], "deny", r)
                return
            self._reprompt(s)
            return
        if st == "WAIT_REVIEW" and s.get("task_id"):
            if self._kw(text, "confirm"):
                r = self.exec_relay.confirm(s["task_id"], "done", uid)
                self._notify_session(s, f"任务 #{s['task_id']} 已确认完成。")
                s["state"] = "IDLE"
                self.st.archive_round(s["task_id"], "confirm-done", r)
                return
            if self._kw(text, "conflict") or self._kw(text, "deny"):
                r = self.exec_relay.confirm(s["task_id"], "conflict", uid)
                self._notify_session(s, f"任务 #{s['task_id']} 已标记冲突。")
                s["state"] = "IDLE"
                self.st.archive_round(s["task_id"], "confirm-conflict", r)
                return
            self._reprompt(s)
            return
        if self._is_noise(text):
            self._react(s, "ack_emoji")
            return
        if st == "BUFFERING":
            if self._kw(text, "submit"):
                self._flush(s, uid=uid)
                return
            if self._kw(text, "cancel"):
                s["buffer"] = []
                s["state"] = "IDLE"
                self._notify_session(s, "已放弃本轮内容。")
                return
            s["buffer"].append(text)
            s["deadline"] = time.time() + int(
                self.cfg.get("ingress", "debounce_sec", 20))
            return
        if st in ("RUNNING", "WAIT_APPROVAL", "WAIT_REVIEW", "FOLLOWUP"):
            if self._kw(text, "cancel"):
                tid = s.get("task_id")
                if tid:
                    r = self.exec_relay.cancel(tid, uid)
                    self.st.archive_round(tid, "cancel-by-user", r)
                    self._notify_session(
                        s, f"已取消任务 #{tid}，补充内容一并丢弃。")
                else:
                    self._notify_session(s, "已放弃待提交的追问内容。")
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
        self.sess.start_buffering(s, text)
        self._react(s, "ack_emoji")

    def _flush(self, s: dict, followup: bool = False, uid: str = ""):
        items = s["followups"] if followup else s["buffer"]
        if not items:
            s["state"] = "IDLE"
            return
        uid = uid or s.get("uid") or ""
        if self.policy.rate_hit(uid):
            self._notify_session(
                s, "⚠️ 今日任务数已达上限，请明天再试或联系管理员。")
            s["buffer"] = []
            s["followups"] = []
            s["state"] = "IDLE"
            return
        desc = sanitize_injected("\n\n".join(items))
        resume = self.sess.resume_candidate(s)
        r = self.exec_relay.submit(desc, resume_from=resume, uid=uid)
        if "_error" in r:
            self._notify_session(s, f"提交 relay 失败：{r['_error']} "
                                    f"{str(r.get('_detail', ''))[:120]}")
            s["state"] = "IDLE"
            return
        tid = r["task_id"]
        s["task_id"] = tid
        s["uid"] = uid
        s["state"] = "RUNNING"
        s["notified_followup"] = False
        if followup:
            s["followups"] = []
        else:
            s["buffer"] = []
        self.st.archive_round(tid, "submit", {
            "desc": desc[:500], "resume_from": resume, "resp": r,
            "ingress": "dingtalk", "backend": "relay",
            "tier": self.policy.get_level(uid)})
        self._react(s, "submit_emoji")
        log(f"task #{tid} submitted (resume={resume})")

    # ── relay 任务轮询 ──
    def _poll_sessions(self):
        for key, s in list(self.sess.sessions.items()):
            if s["state"] not in ("RUNNING", "WAIT_APPROVAL", "WAIT_REVIEW"):
                continue
            tid = s.get("task_id")
            if not tid:
                s["state"] = "IDLE"
                continue
            row = self.exec_relay.poll(tid)
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
                self._notify_session(s, self._approval_text(tid, s))
                self.st.archive_round(tid, "pending_approval",
                                      row.get("blocked_cmd"))
                continue
            if status == "review" and s["state"] != "WAIT_REVIEW":
                s["state"] = "WAIT_REVIEW"
                self._send_result(s, row, prefix=f"任务 #{tid} 待人工确认。")
                self.st.archive_round(tid, "review",
                                      {"result": (row.get("result") or "")[:500]})
                continue
            if status in ("done", "conflict", "failed", "cancelled"):
                self._finish(s, row)

    def _approval_text(self, tid, s: dict) -> str:
        cmd = (s.get("gate_cmd") or "").splitlines()
        cmd = (cmd[0] if cmd else "")[:60]
        return (f"任务 #{tid} 待审批：门禁拦截命令 `{cmd}…`\n\n"
                "回复「确认」放行，「拒绝」终止。")

    def _send_result(self, s: dict, row: dict, prefix: str = ""):
        tid = row.get("id")
        result = (row.get("result") or "").strip()
        head = prefix or f"任务 #{tid} 完成。"
        limit = int(self.cfg.get("ingress", "result_max_chars", 3000))
        if len(result) > limit:
            fp = self.st.rounds / f"{tid}_result.md"
            try:
                fp.write_text(result, encoding="utf-8")
                self.dws.send_text(s["reply"], md_hardbreak(
                    self.selfmark.append(
                        head + "\n\n" + result[:1500] + "\n\n…（全文见附件）",
                        self.sign)))
                self.dws.send_file(s["reply"], str(fp))
            except Exception as e:
                log("send result failed:", e)
        else:
            self._notify_session(
                s, head + ("\n\n" + result if result else "（无文本结果）"))

    def _finish(self, s: dict, row: dict):
        tid = row.get("id")
        status = row.get("status")
        self.st.archive_round(tid, status, {
            "result": (row.get("result") or "")[:500],
            "error": row.get("error")})
        if status == "done":
            self._send_result(s, row)
            s["last_task_id"] = tid
        elif status == "conflict":
            self._send_result(s, row, prefix=f"任务 #{tid} 完成但检测到文件"
                                             "冲突，请人工复核。")
            s["last_task_id"] = tid
        elif status == "failed":
            self._notify_session(s, f"任务 #{tid} 执行失败："
                                    f"{(row.get('error') or '未知错误')[:300]}")
        elif status == "cancelled":
            self._notify_session(s, f"任务 #{tid} 已取消。")
        s["task_id"] = None
        if s.get("followups"):
            self.sess.start_followup_window(s)
            log(f"{len(s['followups'])} followup(s) queued")
        else:
            s["state"] = "IDLE"

    def _reprompt(self, s: dict):
        now = time.time()
        cooldown = int(self.cfg.get("security", "reprompt_cooldown_sec", 60))
        if now - s.get("last_reprompt", 0) < cooldown:
            return
        s["last_reprompt"] = now
        tid = s.get("task_id")
        if s["state"] == "WAIT_APPROVAL":
            self._notify_session(s, self._approval_text(tid, s))
        else:
            self._notify_session(s, f"任务 #{tid} 待人工确认：回复「确认」完成，"
                                    "「冲突」标记文件冲突。")

    # ── 定时 ──
    def _tick(self):
        for key, s, kind in self.sess.due():
            s["deadline"] = 0
            self._flush(s, followup=(kind == "followup"))
            self.sess.save()
        self.sess.cleanup_expired()
        self.gwatch.gc()

    # ── 完整性自检 ──
    def _integrity_check(self):
        base = Path(__file__).resolve().parent.parent
        try:
            drift = integrity.check_drift(base, self.cfg.state)
        except Exception as e:
            log("integrity check error:", e)
            return
        if not drift:
            return
        summary = integrity.drift_summary(drift)
        self.audit.log("integrity_drift", "system", summary)
        snap = integrity.save_snapshot(base, self.cfg.state, drift)
        sig = integrity.drift_signature(drift)
        today = time.strftime("%Y-%m-%d")
        flag = self.st.run / ".drift_alerted"
        try:
            prev = flag.read_text(encoding="utf-8").strip()
        except Exception:
            prev = ""
        if prev == f"{today}:{sig}":
            return
        to = str(self.cfg.get("monitor", "alert_to", "") or "")
        if to:
            self._dm(to, f"# 🔴 relay-avatar 代码漂移告警\n**机器**：{self.monitor.host}\n"
                         f"实例代码与入库基线不一致：{summary}\n\n"
                         f"> 手改会被夜间 provision 覆盖；有意的改动请走 git 提案流回仓库。\n"
                         f"> 改动全文已固化 {snap}")
        try:
            flag.write_text(f"{today}:{sig}", encoding="utf-8")
        except Exception:
            pass

    # ── 主循环 ──
    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        self._integrity_check()
        threading.Thread(target=self.monitor.heartbeat_loop,
                         daemon=True).start()
        threading.Thread(target=self.monitor.probe_loop, daemon=True).start()
        if self.cfg.get("monitor", "dashboard_enabled", True):
            self.dashboard.serve_thread()
        # 重启后门禁态会话重提示
        for s in self.sess.sessions.values():
            if s["state"] in ("WAIT_APPROVAL", "WAIT_REVIEW") and \
                    s.get("task_id"):
                if not s.get("gate_cmd"):
                    row = self.exec_relay.poll(s["task_id"])
                    s["gate_cmd"] = row.get("blocked_cmd") or ""
                s["last_reprompt"] = 0
                self._reprompt(s)
        if self.inject:
            self._run_inject()
            return
        threading.Thread(target=EventIngress(
            self.cfg, self.dws, self.health, self.handle_line,
            self.stop).loop, daemon=True).start()
        if self.cfg.get("ingress", "self_poll", False):
            threading.Thread(target=SelfPoller(
                self.cfg, self.dws, self.selfmark, self.st, self.on_event,
                self.stop).loop, daemon=True).start()
        last_poll = 0.0
        poll_sec = float(self.cfg.get("ingress", "poll_sec", 3))
        while not self.stop.is_set():
            try:
                line = self.q.get(timeout=0.5)
                self.handle_line(line)
            except queue.Empty:
                pass
            now = time.time()
            self._tick()
            if now - last_poll >= poll_sec:
                last_poll = now
                self._poll_sessions()
                self.sess.save()
        log("avatar stopping")
        self.monitor.stop.set()
        self.sess.save()

    def _run_inject(self):
        for line in open(self.inject, encoding="utf-8"):
            line = line.strip()
            if line:
                self.handle_line(line)
        deadline = time.time() + float(os.environ.get(
            "AVATAR_INJECT_WAIT", "180"))
        while time.time() < deadline and not self.stop.is_set():
            self._tick()
            self._poll_sessions()
            time.sleep(float(self.cfg.get("ingress", "poll_sec", 3)))
            if self.sess.sessions and all(
                    s["state"] == "IDLE" for s in self.sess.sessions.values()):
                break
        self.sess.save()
        log("inject run finished")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="", help="状态目录（默认 $WORKSPACE/avatar）")
    ap.add_argument("--policy", default="", help="policy.toml 路径")
    ap.add_argument("--inject", default="", help="从文件读事件 NDJSON（自测）")
    ap.add_argument("--dry-send", action="store_true",
                    help="不真发钉钉消息，只打印")
    args = ap.parse_args()
    state = Path(args.state) if args.state else None
    policy = Path(args.policy) if args.policy else None
    cfg = C.load(state=state, policy_path=policy)
    Avatar(cfg, inject=args.inject, dry=args.dry_send).run()


if __name__ == "__main__":
    main()
