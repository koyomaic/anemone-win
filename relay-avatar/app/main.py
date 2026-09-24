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
from app import recall
from app.audit import Audit
from app.dashboard import Dashboard
from app.exec_local import ExecLocal
from app.exec_relay import ExecRelay
from app.file_inbox import FileInbox, extract_file_from_event, find_media_ids
from app.group_watch import GroupWatch, parse_watch_cmd
from app.ingress_dingtalk import (Dws, EventIngress, InboundHealth, SelfMark,
                                  SelfPoller, SentEcho, card_biz_id,
                                  md_hardbreak)
from app.ingress_relay import RelayClient
from app.knowledge import Knowledge, parse_learn_cmd
from app.monitor import Monitor
from app.policy import Policy, sanitize_field, sanitize_injected
from app import planning
from app.session import SessionManager
from app.storage import Storage

EMOJI_RE = re.compile(r"^(\[[^\[\]]{1,12}\]\s*)+$")

# 大脑在回复里放的结构化计划标记：```plan\n{json}\n```。relay-avatar 解析并执行
# （理解交给 LLM，带凭据的创建/管理留在本进程）。解析失败则原样保留、不吞正文。
PLAN_BLOCK_RE = re.compile(r"```plan[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)


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
        self.echo = SentEcho(self.st)
        self.monitor = Monitor(cfg, self.st, self.dws, self.audit)
        self.dashboard = Dashboard(cfg, self.st, self.audit)
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.contacts = self.st.run / "contacts.json"
        self.sign = str(cfg.get("identity", "sign", "") or "")
        self._guide_seen: dict = {}
        self._guest_seen: dict = {}
        self._note_guard: dict = {}

    def _guest_silent(self, sid: str, sender: str, content):
        """名单外静默忽略：只留节流审计（同人每小时一条），绝不回话。"""
        k = sid or sender
        now = time.time()
        if now - self._guest_seen.get(k, 0) < 3600:
            return
        self._guest_seen[k] = now
        self.audit.log("guest_ignored", str(k)[:24],
                       f"{sender}: {str(content)[:60]}")

    # ── 发送 ──
    def _notify(self, target: dict, text: str, task_id=None):
        try:
            self.echo.record(text)   # 出站指纹（防轮询回音自循环，主防线）
            r = self.dws.send_text(target, md_hardbreak(
                self.selfmark.append(text, self.sign)))
            recall.record(self.st, "text",
                          target.get("user_id")
                          or target.get("open_dingtalk_id")
                          or target.get("conversation_id"), r,
                          task_id=task_id, snippet=text)
        except Exception as e:
            log("notify failed:", e)

    def _notify_session(self, s: dict, text: str):
        log(f"-> {s['reply'].get('sender')}: {text[:80]}")
        self._notify(s["reply"], text,
                     task_id=s.get("task_id") or s.get("last_task_id"))

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

    def _ack_receive(self, s: dict):
        """来件即时回执（越快越好，先贴图标再走重活）：
        文字表情「正在疯狂处理中...」，未配置/失败回落 ack_emoji。"""
        mid = s.get("last_msg_id") or ""
        cid = s.get("last_conv_id") or ""
        if not mid or not cid:
            return
        s["ack_mid"] = mid   # 本条已回执（emoji 回落也记，防 _on_message 双贴）
        te = self.cfg.section("ingress").get("ack_text_emotion") or {}
        if te.get("emotion_id") and te.get("text"):
            try:
                r = self.dws.react_text_emotion(cid, mid, te)
                if not (r.get("error") or r.get("errorMsg")):
                    s["ack_msg"] = [cid, mid]
                    return
                log("ack text-emotion failed, fallback emoji:", str(r)[:120])
            except Exception as e:
                log("ack text-emotion failed, fallback emoji:", e)
        self._react(s, "ack_emoji")

    def _swap_ack(self, s: dict, cfg_key: str):
        """终态换图标：撤「正在疯狂处理中...」，按终态贴「已办好/出错了」
        （不同表情模板=不同背景色，一眼区分）；cfg_key 空则只撤不贴。"""
        ack = s.pop("ack_msg", None)
        if not ack:
            return
        cid, mid = ack
        ack_te = self.cfg.section("ingress").get("ack_text_emotion") or {}
        try:
            if ack_te.get("emotion_id"):
                self.dws.remove_text_emotion(cid, mid, ack_te)
        except Exception as e:
            log("remove ack emotion failed:", e)
        te = (self.cfg.section("ingress").get(cfg_key) or {}) if cfg_key else {}
        if te.get("emotion_id") and te.get("text"):
            try:
                self.dws.react_text_emotion(cid, mid, te)
            except Exception as e:
                log("swap ack emotion failed:", e)

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
        # self 模式（轮询本人单聊）：审批指令 / 「学 N」升格 / 「/」前缀任务 / 无前缀速记
        if ev.get("_self_poll"):
            key = f"o:{sid}"
            s = self.sess.get_or_create(key, sender, sid, ev, False)
            self.sess.touch(s, ev)
            self._handle_self(s, str(content), uid)
            self.sess.save()
            return
        lvl = self.policy.level_of(uid, sid, sender)
        if lvl == "BLOCKED":
            return
        # 名单外默认静默忽略：不回话、不建会话（防与自动回复 bot 互刷死循环，
        # 2026-09-23「原油AI研究院」事故）；引导话术需 guest_mode="guide" 显式开启
        if lvl == "NONE" and str(self.cfg.get("permissions", "guest_mode",
                                              "silent")) == "silent":
            self._guest_silent(sid, sender, content)
            return
        self.gwatch.collect(ev)   # enabled=false 时 no-op；只收登记群
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
        # L3：文件投递/问答（不进通用会话流）
        if lvl == "L3" and not is_group:
            self._mark_read(s)
            receipt = self.inbox.receive(uid, sender, ev)
            if receipt:
                self._dm(uid or sid, receipt)
                return
            text = str(content).strip()
            if text and not self._is_noise(text):
                self._ack_receive(s)
                self._l3_qa(s, uid, text)
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
        # 即时回执前置：先贴「正在疯狂处理中...」，再做已读回标/媒体下载等重活
        quick = self._quick_text(content)
        if (s["state"] == "IDLE" and quick.strip()
                and not self._is_noise(quick)
                and not planning.parse_plan_cmd(quick)
                and not any(self._kw(quick, k) for k in
                            ("submit", "cancel", "confirm", "deny",
                             "conflict"))):
            self._ack_receive(s)
        self._mark_read(s)
        text = self._extract_text(ev, content, uid, sender)
        self._on_message(key, s, text.strip(), uid)
        self.sess.save()

    def _quick_text(self, content) -> str:
        """不下载媒体的廉价文本提取（即时回执预判噪音/关键词用）。"""
        if isinstance(content, str):
            c = content.strip()
            if c[:1] in "[{":
                try:
                    d = json.loads(c)
                    if isinstance(d, dict):
                        return str(d.get("text") or d.get("content")
                                   or d.get("title") or c)
                except Exception:
                    pass
            return c
        return str(content or "")

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
        # 引导话术同人 1 小时只发一次（申请指令不受限）——防 guide 模式下
        # 与自动回复 bot 互刷死循环
        gkey = f"dm:{uid or ev.get('sender_open_dingtalk_id') or sender}"
        now = time.time()
        if now - self._guide_seen.get(gkey, 0) < 3600:
            return ""
        self._guide_seen[gkey] = now
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

    # ── self 模式（本人单聊轮询，P2） ──
    def _handle_self(self, s: dict, text: str, uid: str):
        t = (text or "").strip()
        if not t:
            return
        # 审批指令：superadmin 经自聊审批（分身用本人账号，审批入口即自聊）
        ap = self.policy.parse_approval(t)
        if ap and self.policy.is_superadmin(uid):
            self._dm(uid, self._do_approval(ap, uid))
            return
        # 会话进行中 → 交会话流（提交/取消/门禁关键词与聚合）
        if s["state"] != "IDLE":
            self._on_message("", s, t, uid)
            return
        # 「学 N [域] [主题]」：暂存笔记审核升格（knowledge + wiki 双写 + store）
        lc = parse_learn_cmd(t)
        if lc:
            n, domain, topic = lc
            r = self.know.promote_by_index(n, domain, topic)
            if r.get("error"):
                # 带 hint（可选域清单）一并回话，否则用户不知道该怎么改
                self._notify_session(
                    s, f"⚠️ {r['error']}"
                       + (f"\n\n{r['hint']}" if r.get("hint") else ""))
                return
            wiki = ("已双写 jingboWiki（受理即成功，解析后台进行）"
                    if r.get("wiki") == "pushed"
                    else f"wiki 双写未执行（{r.get('wiki')}）")
            store = "✓" if r.get("store_ok") else \
                "✗ " + str(r.get("store_out", ""))[:80]
            self._notify_session(
                s, f"✅ 已升格入库：{r['path']}\n\nstore 登记：{store}\n{wiki}")
            self.audit.log("knowledge_promote", uid, str(r.get("path")))
            return
        # 「/」前缀 → 任务（走会话流：debounce/门禁/卡片全套，relay 后端）
        if t.startswith(("/", "／")):
            real = t.lstrip("/／").strip()
            if real:
                self._on_message("", s, real, uid)
            return
        # 门禁类关键词在 IDLE 不落笔记（与会话流语义一致）
        if any(self._kw(t, k) for k in
               ("submit", "cancel", "confirm", "deny", "conflict")):
            return
        # 防循环闸二：同文本 2 分钟内不重复落笔记（回音闸万一漏网的双保险）
        nkey = SentEcho.norm(t)
        now = time.time()
        if now - self._note_guard.get(nkey, 0) < 120:
            return
        self._note_guard[nkey] = now
        # 无前缀 → 暂存速记（低摩擦，不进台账）
        self.know.save_note(t)
        n = len(self.know.list_notes())
        self._notify_session(
            s, f"📝 已记到暂存笔记（第 {n} 条）。发「学 {n}」审核入库，"
               f"或「学 {n} 口径方法论」指定域。")

    # ── L3 文件问答（exec_local 硬隔离 + 流式卡片） ──
    def _l3_qa(self, s: dict, uid: str, text: str):
        target = {"user_id": uid} if uid else s["reply"]
        if not self.exec_local.available():
            self._notify(target, "📮 文件问答尚未在本机启用"
                                 "（policy execution.local_enabled=false），"
                                 "文件已安全存档，如需加急请联系管理员。")
            return
        biz = ""
        if self.cfg.get("ingress", "use_card", True):
            try:
                biz = card_biz_id(self.dws.card_create(s["reply"]))
            except Exception:
                biz = ""
        throttle = {"upd": 0.0}

        def on_step(step, steps):
            if not biz:
                return
            now = time.time()
            if now - throttle["upd"] < 5:
                return
            throttle["upd"] = now
            try:
                self.dws.card_update(
                    biz, f"🍂 **{self.cfg.avatar_name}正在处理…**\n\n"
                    + "\n".join(f"> {x}" for x in steps[-6:]), 1)
            except Exception:
                pass

        kb = self.know.search(text)
        r = self.exec_local.run_l3(uid, text, kb=kb, on_step=on_step)
        out = str(r.get("text") or "（没读出内容，换个问法试试？）")
        self.st.archive_round(f"l3-{uid}", "qa",
                              {"q": text[:200], "a": out[:300]})
        if biz:
            try:
                if self.dws.card_update(biz, out[:2500], 3):
                    return
            except Exception:
                pass
        self._notify(target, out)

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
                self._notify_session(s, "✅ 已放行，继续执行。")
                s["state"] = "RUNNING"
                self.st.archive_round(s["task_id"], "approve", r)
                return
            if self._kw(text, "deny"):
                r = self.exec_relay.approve(s["task_id"], "deny", uid)
                self._notify_session(s, "🚫 已拒绝，任务终止。")
                s["state"] = "IDLE"
                self._swap_ack(s, "")
                self.st.archive_round(s["task_id"], "deny", r)
                return
            self._reprompt(s)
            return
        if st == "WAIT_REVIEW" and s.get("task_id"):
            if self._kw(text, "confirm"):
                r = self.exec_relay.confirm(s["task_id"], "done", uid)
                self._notify_session(s, "✅ 已确认完成。")
                s["state"] = "IDLE"
                self.st.archive_round(s["task_id"], "confirm-done", r)
                return
            if self._kw(text, "conflict") or self._kw(text, "deny"):
                r = self.exec_relay.confirm(s["task_id"], "conflict", uid)
                self._notify_session(s, "已标记文件冲突，请人工复核。")
                s["state"] = "IDLE"
                self.st.archive_round(s["task_id"], "confirm-conflict", r)
                return
            self._reprompt(s)
            return
        if self._is_noise(text):
            self._react(s, "noise_emoji")
            return
        if st == "BUFFERING":
            if self._kw(text, "submit"):
                self._flush(s, uid=uid)
                return
            if self._kw(text, "cancel"):
                s["buffer"] = []
                s["state"] = "IDLE"
                self._swap_ack(s, "")
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
                        s, "🚫 已取消任务，补充内容一并丢弃。")
                else:
                    self._notify_session(s, "已放弃待提交的追问内容。")
                s["task_id"] = None
                s["followups"] = []
                s["buffer"] = []
                s["state"] = "IDLE"
                self._swap_ack(s, "")
                return
            s["followups"].append(text)
            if not s.get("notified_followup"):
                s["notified_followup"] = True
                self._react(s, "followup_emoji")
            return
        # IDLE
        if any(self._kw(text, k) for k in
               ("submit", "cancel", "confirm", "deny", "conflict")):
            return
        # 计划（定时任务）：以 定时/计划 前缀就地拦截，进大脑前处理（大脑不会去"当场执行"）
        pc = planning.parse_plan_cmd(text)
        if pc:
            self._handle_plan(pc, s, uid)
            return
        if s.get("ack_mid") != s.get("last_msg_id"):
            self._ack_receive(s)   # 即时回执已贴过则不重复
        self.sess.start_buffering(s, text)

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
            self._notify_session(s, f"⚠️ 提交失败了：{r['_error']} "
                                    f"{str(r.get('_detail', ''))[:120]}，稍后再试～")
            s["state"] = "IDLE"
            self._swap_ack(s, "fail_text_emotion")
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
        self._card_open(s, tid, desc)
        log(f"task #{tid} submitted (resume={resume})")

    # ── 流式卡片（fenshen 样式：叶子+粗体名+引用行；提交即开卡，跳变更新，完成定格） ──
    def _card_open(self, s: dict, tid, desc: str):
        if not self.cfg.get("ingress", "use_card", True):
            return
        try:
            resp = self.dws.card_create(s["reply"])
        except Exception as e:
            log("card create failed:", e)
            return
        biz = card_biz_id(resp)
        if not biz:
            return
        recall.record(self.st, "card",
                      s["reply"].get("open_dingtalk_id")
                      or s["reply"].get("conversation_id"), resp,
                      task_id=tid, snippet=f"card:{biz}")
        s["card_biz"] = biz
        s["card_hint"] = (desc.splitlines() or [""])[0][:40]
        s["card_start"] = time.time()
        s["card_upd"] = time.time()
        try:
            self.dws.card_update(biz, self._card_frame(
                s, "正在为你办，稍等一下～"), 1)
        except Exception as e:
            log("card first frame failed:", e)

    def _card_frame(self, s: dict, note: str) -> str:
        return (f"🍂 **{self.cfg.avatar_name}正在处理…**\n\n"
                f"> 📥 已收到：{s.get('card_hint') or ''}\n> ⏳ {note}")

    def _card_beat(self, s: dict, tid, status: str):
        """运行中进度刷新（节流 card_progress_sec，防钉钉客户端合并帧）。"""
        biz = s.get("card_biz")
        if not biz:
            return
        gap = int(self.cfg.get("ingress", "card_progress_sec", 30))
        now = time.time()
        if now - s.get("card_upd", 0) < gap:
            return
        s["card_upd"] = now
        used = int(now - s.get("card_start", now))
        word = "已排队，马上开始" if status == "queued" else "正在为你办"
        try:
            self.dws.card_update(
                biz, self._card_frame(s, f"{word}…（已用 {used}s）"), 1)
        except Exception:
            pass

    def _card_final(self, s: dict, content: str, status: int = 3) -> bool:
        biz = s.pop("card_biz", None)
        if not biz:
            return False
        try:
            return bool(self.dws.card_update(biz, content[:2500], status))
        except Exception as e:
            log("card final failed:", e)
            return False

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
                self._card_beat(s, tid, status)
                continue
            if status == "pending_approval" and s["state"] != "WAIT_APPROVAL":
                s["state"] = "WAIT_APPROVAL"
                s["gate_cmd"] = row.get("blocked_cmd") or ""
                self._notify_session(s, self._approval_text(s))
                if s.get("card_biz"):
                    cmd = (s.get("gate_cmd") or "").splitlines()
                    cmd = (cmd[0] if cmd else "")[:60]
                    try:
                        # 不销卡：放行后任务继续，卡片随后续状态刷新
                        self.dws.card_update(
                            s["card_biz"],
                            f"🍂 **{self.cfg.avatar_name}等你拍板…**\n\n"
                            f"> 🚧 门禁拦截：`{cmd}…`\n"
                            "> 回复「确认」放行 /「拒绝」终止", 4)
                    except Exception:
                        pass
                self.st.archive_round(tid, "pending_approval",
                                      row.get("blocked_cmd"))
                continue
            if status == "review" and s["state"] != "WAIT_REVIEW":
                s["state"] = "WAIT_REVIEW"
                self._send_result(s, row, prefix="👀 办完了，等你过目"
                                                 "（回复「确认」完成 /「冲突」标记文件冲突）")
                self.st.archive_round(tid, "review",
                                      {"result": (row.get("result") or "")[:500]})
                continue
            if status in ("done", "conflict", "failed", "cancelled"):
                self._finish(s, row)

    def _approval_text(self, s: dict) -> str:
        cmd = (s.get("gate_cmd") or "").splitlines()
        cmd = (cmd[0] if cmd else "")[:60]
        if cmd:
            return (f"🚧 门禁拦截了任务里的高危命令：`{cmd}…`\n\n"
                    "回复「确认」放行，「拒绝」终止。")
        return ("🚧 任务里有高危操作待审批。\n\n"
                "回复「确认」放行，「拒绝」终止。")

    def _send_result(self, s: dict, row: dict, prefix: str = ""):
        tid = row.get("id")
        result = (row.get("result") or "").strip()
        result, notes = self._consume_plan_markers(result, s, tid)
        if notes:
            result = (result + ("\n\n" if result else "")
                      + "\n\n".join(notes)).strip()
        head = prefix or f"🍂 **{self.cfg.avatar_name}办好了 ✅**"
        limit = int(self.cfg.get("ingress", "result_max_chars", 3000))
        long = len(result) > limit
        if s.get("card_biz"):
            body = head + "\n\n" + (
                result[:1500] + "\n\n…（全文见附件）" if long
                else (result or "（无文本结果）"))
            if self._card_final(s, body, 3):
                if long:
                    self._send_full_file(s, tid, result)
                return
        if long:
            self._notify_session(
                s, head + "\n\n" + result[:1500] + "\n\n…（全文见附件）")
            self._send_full_file(s, tid, result)
        else:
            self._notify_session(
                s, head + ("\n\n" + result if result else "（无文本结果）"))

    # ── 计划标记消费：大脑产出 ```plan JSON，本进程带凭据执行 ──
    def _consume_plan_markers(self, result: str, s: dict, tid):
        specs: list = []

        def _grab(m):
            try:
                obj = json.loads(m.group(1))
            except Exception:
                return m.group(0)          # 解析失败：原样保留，不吞正文
            if isinstance(obj, dict):
                specs.append(obj)
                return ""                  # 命中：从正文剔除
            return m.group(0)

        clean = PLAN_BLOCK_RE.sub(_grab, result).strip()
        if not specs or s.get("plan_applied") == tid:
            return clean, []               # 已执行过（review→done 二次回流）不再重复
        s["plan_applied"] = tid
        uid = s.get("uid") or ""
        notes = []
        for spec in specs:
            try:
                note = self._exec_plan_op(spec, uid)
            except Exception as e:
                note = f"⚠️ 计划操作异常：{e!r}"
            if note:
                notes.append(note)
        return clean, notes

    def _exec_plan_op(self, spec: dict, uid: str) -> str | None:
        op = str(spec.get("op") or "create").lower()
        if op == "list":
            return self._plan_list_note(uid)
        if op == "create":
            return self._plan_create_note(spec, uid)
        if op in ("delete", "disable", "enable"):
            return self._plan_by_name_note(op, str(spec.get("name") or ""), uid)
        return None

    def _plan_create_note(self, spec: dict, uid: str) -> str:
        on_cal = str(spec.get("on_calendar") or "").strip()
        desc = str(spec.get("description") or "").strip()
        if not on_cal or not desc:
            return "⚠️ 计划信息不完整（缺 on_calendar 或 description），未创建。"
        name = re.sub(r"[^A-Za-z0-9_.一-鿿-]", "",
                      str(spec.get("name") or "").strip() or desc[:16])[:40] or "plan"
        existing = {p.get("name") for p in
                    (self.exec_relay.list_plans(uid).get("plans") or [])}
        base, i = name, 2
        while name in existing:
            name = f"{base}-{i}"
            i += 1
        action = {"type": "task", "description": desc,
                  "priority": spec.get("priority", "normal"),
                  "read_only": bool(spec.get("read_only", False))}
        r = self.exec_relay.create_plan(name, on_cal, action, uid=uid)
        if "_error" in r:
            return (f"⚠️ 创建计划失败："
                    f"{str(r.get('_detail') or r.get('_error'))[:160]}")
        nr = r.get("next_run")
        nrs = time.strftime("%m-%d %H:%M", time.localtime(nr)) if nr else "?"
        return (f"✅ 已创建定时计划「{name}」\n"
                f"· 规则：{on_cal}\n· 下次：{nrs}\n· 内容：{desc[:60]}\n"
                f"到点自动执行。想看就说「我的计划」，想取消说「删掉计划 {name}」。")

    def _plan_list_note(self, uid: str) -> str:
        r = self.exec_relay.list_plans(uid)
        if "_error" in r:
            return f"⚠️ 读取计划失败：{r.get('_error')}"
        plans = r.get("plans") or []
        if not plans:
            return "📋 当前没有定时计划。"
        lines = ["📋 定时计划："]
        for p in plans:
            nr = p.get("next_run")
            nrs = (time.strftime("%m-%d %H:%M", time.localtime(nr))
                   if nr else "已停用")
            st = "启用" if p.get("enabled") else "停用"
            lines.append(f"· {p['name']}｜{p['on_calendar']}｜{st}｜下次 {nrs}")
        return "\n".join(lines)

    def _plan_by_name_note(self, op: str, name: str, uid: str) -> str:
        name = (name or "").strip()
        if not name:
            return "⚠️ 请说明要操作哪个计划（用计划名）。"
        r = self.exec_relay.list_plans(uid)
        if "_error" in r:
            return f"⚠️ 读取计划失败：{r.get('_error')}"
        plans = r.get("plans") or []
        cands = [p for p in plans if p["name"] == name] or \
            [p for p in plans if name in p["name"]]
        if not cands:
            return f"没找到计划「{name}」。说「我的计划」可查看全部。"
        if len(cands) > 1:
            opts = "、".join(p["name"] for p in cands[:8])
            return f"「{name}」匹配到多个：{opts}。请用完整计划名。"
        p = cands[0]
        if op == "delete":
            rr = self.exec_relay.delete_plan(p["id"], uid=uid)
            return ("🗑 已删除计划「" + p["name"] + "」。" if "_error" not in rr
                    else f"⚠️ 删除失败：{rr.get('_error')}")
        enabled = (op == "enable")
        rr = self.exec_relay.patch_plan(p["id"], {"enabled": enabled}, uid=uid)
        if "_error" in rr:
            return f"⚠️ 操作失败：{rr.get('_error')}"
        return (f"{'▶️ 已启用' if enabled else '⏸ 已停用'}计划「{p['name']}」。")

    # ── 计划命令（relay-avatar 侧确定性解析，进大脑前拦截）──
    def _handle_plan(self, pc: dict, s: dict, uid: str):
        lvl = self.policy.get_level(uid)
        if lvl not in ("super", "L1", "L2"):
            self._notify_session(s, "⚠️ 你当前权限不能创建/管理定时计划。")
            return
        cmd = pc["cmd"]
        if cmd == "create":
            spec = {"op": "create", "name": pc["name"],
                    "on_calendar": pc["on_calendar"], "description": pc["description"],
                    "priority": "normal", "read_only": False}
            note = self._plan_create_note(spec, uid)
        elif cmd == "create_fail":
            note = self._plan_fail_note(pc)
        elif cmd == "list":
            note = self._plan_list_note(uid)
        elif cmd in ("delete", "disable", "enable"):
            note = self._plan_by_name_note(cmd, pc["name"], uid)
        else:
            note = None
        if note:
            self._notify_session(s, note)

    def _plan_fail_note(self, pc: dict) -> str:
        if pc.get("reason") == "too_frequent":
            return "⏱ 频率太高了（最快支持每 5 分钟一次）。要更频繁请到 dashboard「计划」页。"
        return ("🤔 没太看懂这个定时规则。试试这样说：\n"
                "· 定时 每天8点 跑经营日报\n"
                "· 定时 每周一9:30 提醒我开会\n"
                "· 定时 每月1号9点 生成月报\n"
                "· 定时 每隔30分钟 检查服务\n"
                "（也可到 dashboard「计划」页手动创建）")

    def _send_full_file(self, s: dict, tid, result: str):
        fp = self.st.rounds / f"{tid}_result.md"
        try:
            fp.write_text(result, encoding="utf-8")
            r = self.dws.send_file(s["reply"], str(fp))
            recall.record(self.st, "file",
                          s["reply"].get("open_dingtalk_id")
                          or s["reply"].get("conversation_id"), r,
                          task_id=tid, snippet=fp.name)
        except Exception as e:
            log("send result file failed:", e)

    def _finish(self, s: dict, row: dict):
        tid = row.get("id")
        status = row.get("status")
        self.st.archive_round(tid, status, {
            "result": (row.get("result") or "")[:500],
            "error": row.get("error")})
        if status == "done":
            self._send_result(s, row)
            self._swap_ack(s, "done_text_emotion")
            s["last_task_id"] = tid
        elif status == "conflict":
            self._send_result(s, row, prefix="✅ 办好了，但检测到文件冲突，"
                                             "请人工复核。")
            self._swap_ack(s, "done_text_emotion")
            s["last_task_id"] = tid
        elif status == "failed":
            err = (row.get("error") or "未知错误")[:300]
            if not self._card_final(
                    s, f"🍂 **{self.cfg.avatar_name}没办成 ❌**\n\n> {err}", 5):
                self._notify_session(s, f"❌ 没办成：{err}")
            self._swap_ack(s, "fail_text_emotion")
        elif status == "cancelled":
            if not self._card_final(s, "🍂 **已取消 🚫**", 3):
                self._notify_session(s, "🚫 已取消。")
            self._swap_ack(s, "")
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
        if s["state"] == "WAIT_APPROVAL":
            self._notify_session(s, self._approval_text(s))
        else:
            self._notify_session(s, "👀 办完了待你确认：回复「确认」完成，"
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
                self.stop, echo=self.echo).loop, daemon=True).start()
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
