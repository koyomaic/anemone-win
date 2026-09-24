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
                                  md_hardbreak, send_accepted)
from app.ingress_relay import RelayClient
from app.knowledge import Knowledge, parse_learn_cmd
from app.monitor import Monitor
from app.policy import (Policy, needs_relay_backend, sanitize_field,
                        sanitize_injected)
from app import planning
from app.session import SessionManager
from app.storage import Storage

EMOJI_RE = re.compile(r"^(\[[^\[\]]{1,12}\]\s*)+$")

# 大脑在回复里放的结构化计划标记：```plan\n{json}\n```。relay-avatar 解析并执行
# （理解交给 LLM，带凭据的创建/管理留在本进程）。解析失败则原样保留、不吞正文。
PLAN_BLOCK_RE = re.compile(r"```plan[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
# local 通路无 Bash，跑不了 dws，故出件后**发不出去**（2026-09-24 实测：agent 明确
# 回报"本会话工具集里没有 Bash/Shell 入口，dws CLI 无从执行"）。给它一个标记出口：
# 大脑把要发的文件写成 ```send_file``` 块，由 avatar（本就持有 dws）代投。
# 这样既补上投递能力，又不必放开 Bash（avatar 服务无沙箱，放开等于无限制 root）。
SEND_BLOCK_RE = re.compile(r"```send_file[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
SEND_MAX_FILES = 5            # 单次结果最多代投几件，防刷
# 代删标记。local 无 Bash、Write/Edit 也删不了文件，故"应用户请求清理草稿"同样
# 只能由 avatar 代做。**可删根目录单独配（security.local_delete_dirs），比可写更窄**：
# 只给 shared/work/（草稿区，按定义就是临时的），shared/report/ 是正式交付区、
# 无 baseline 快照、删了不可追溯，一律严禁。
DELETE_BLOCK_RE = re.compile(r"```delete_file[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
DELETE_MAX_FILES = 10


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
        if st in ("RUNNING", "WAIT_APPROVAL", "WAIT_REVIEW", "FOLLOWUP",
                  "LOCAL"):
            if self._kw(text, "cancel"):
                if st == "LOCAL":
                    # local 通路无 task_id、claude 子进程不可中途撤回；若在此重置
                    # 会话态，_drain_local 会跳过收尾，卡片将永远停在「处理中」
                    self._notify_session(
                        s, "🚫 本机只读通路的任务不支持中途取消，稍候即可；"
                           "补充内容我会一并处理。")
                    return
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

    def _flush(self, s: dict, followup: bool = False, uid: str = "",
               force_relay: bool = False):
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
        # backend=local（只读编排，可委派 subagent）优先；未启用/越权/被拒则升级
        # relay 门禁（升级不降级）。force_relay 供 escalate 回落用，防再次进 local
        # 造成死循环。
        if not force_relay and self._try_local_task(s, uid, desc, followup):
            return
        resume = self.sess.resume_candidate(s)
        r = self.exec_relay.submit(desc + self._relay_delivery_rule(s),
                                   resume_from=resume, uid=uid)
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

    # ── local 后端团队任务（只读编排，可委派 subagent） ──
    def _try_local_task(self, s: dict, uid: str, desc: str,
                        followup: bool = False) -> bool:
        """backend=local 的团队任务通路：后台线程执行 + 流式卡片。

        返回 True=已由本通路接管（调用方直接 return）；False=未启用/越权/已在跑，
        调用方升级 relay 门禁（升级不降级）。

        为什么必须后台线程：主循环单线程串行（handle_line → _tick →
        _poll_sessions），同步跑 claude 最长会阻塞 reply_timeout_sec（默认 600s），
        期间所有入站消息与**在途 relay 任务的轮询**一起冻结。
        为什么收尾回主循环：会话态 s 与主循环共享，worker 只写 s["local_result"]，
        由 _drain_local 在主循环内收尾，避免并发改会话态。
        为什么用独立 LOCAL 态：_poll_sessions 见到 RUNNING 且无 task_id 会立刻
        重置 IDLE，而 local 通路没有 relay 的 task_id；LOCAL 也不在 due() 的
        BUFFERING/FOLLOWUP 之列，不会被误 flush。
        代价：local 通路不支持门禁审批/冲突检测/中途取消，需要这些请保持
        backend=relay。
        """
        cons = self.policy.constraints(uid)
        if cons.get("backend") != "local" or not self.exec_local.available():
            return False
        # 前置判定：需要 **relay 独有能力**（门禁审批、dws 外发凭据、root DSN 任意
        # SQL、沙箱可写范围外的系统操作、联网检索）的任务，local 结构性做不成，
        # 直接放行走 relay 门禁，不要让它进 local 空转数分钟再 escalate
        # （#204 实测白烧 10 分 02 秒）。
        # 注：阶段二起 local 沙箱内已有 Bash，「出 pptx/docx/xlsx、跑 python」**不再**
        # 是升级理由（shared/venv 带 python-pptx/python-docx，沙箱内可执行）。
        if needs_relay_backend(desc):
            log(f"local skipped for {uid}: needs relay backend")
            return False
        if s.get("state") == "LOCAL":
            return False                       # 已有本机任务在跑，不并发
        biz = ""
        if self.cfg.get("ingress", "use_card", True):
            try:
                biz = card_biz_id(self.dws.card_create(s["reply"]))
            except Exception:
                biz = ""
        if followup:
            s["followups"] = []
        else:
            s["buffer"] = []
        s["uid"] = uid
        s["task_id"] = None
        s["state"] = "LOCAL"
        s["local_result"] = None
        # 重启恢复元数据。local 通路没有 relay task_id，进程一退 daemon 线程即
        # 消失，仅凭 state="LOCAL" 无法区分「正在跑」与「上一进程遗留」。落任务
        # 原文 + 起跑时刻 + 硬截止，供 _reap_local（启动清理／超时兜底）改投 relay。
        s["local_desc"] = desc
        s["local_started"] = time.time()
        s["local_deadline"] = (s["local_started"]
                               + float(self.cfg.get("claude",
                                                    "reply_timeout_sec", 600))
                               + 60)
        s["notified_followup"] = False
        self._swap_ack(s, "")
        # 影子记录：让 local 任务在 relay 任务列表/看板可见（否则无 task_id，
        # 出事故只能翻 journal+audit+state.json 三处对账）。建记录失败**不阻断**
        # local 执行——降级回今天的无记录行为，可见性不该成为可用性前提。
        rec = {}
        try:
            rec = self.exec_relay.create_local_record(desc, uid)
        except Exception as e:
            log("local shadow record failed:", repr(e)[:120])
        if "_error" not in rec and rec.get("task_id"):
            s["task_id"] = rec["task_id"]
        else:
            s["task_id"] = None
        self.st.archive_round(f"local-{uid}", "submit", {
            "desc": desc[:500], "backend": "local",
            "shadow_task_id": s.get("task_id"),
            "tier": self.policy.get_level(uid),
            "sandbox": cons.get("sandbox", ""),
            "write_dirs": list(cons.get("write_dirs") or []),
            "add_dirs": list(cons.get("add_dirs") or [])})
        threading.Thread(target=self._local_worker,
                         args=(s, uid, desc, biz), daemon=True).start()
        log(f"local task started for {uid} (tier={cons['tier']}"
            + (f", shadow=#{s['task_id']})" if s.get("task_id") else ", no shadow)"))
        return True

    def _local_worker(self, s: dict, uid: str, desc: str, biz: str):
        """后台执行 local 任务；只写 s["local_result"]，不碰其他会话态。"""
        throttle = {"upd": 0.0}
        gap = float(self.cfg.get("ingress", "card_progress_sec", 30))

        def on_step(step, steps):
            if not biz:
                return
            now = time.time()
            if now - throttle["upd"] < gap:
                return
            throttle["upd"] = now
            try:
                self.dws.card_update(
                    biz, f"🍂 **{self.cfg.avatar_name}正在处理…**\n\n"
                    + "\n".join(f"> {x}" for x in steps[-6:]), 1)
            except Exception:
                pass

        try:
            kb = self.know.search(desc)
            r = self.exec_local.run_task(uid, desc, kb=kb, on_step=on_step)
        except Exception as e:
            log("local task error:", e)
            r = {"text": "", "escalate": True, "error": str(e)[:200]}
        s["local_result"] = {"r": r, "biz": biz, "uid": uid, "desc": desc}

    def _drain_local(self):
        """主循环侧收尾 LOCAL 态：出结果→定格卡片/回话；被拒→升级 relay 门禁。"""
        for key, s in list(self.sess.sessions.items()):
            if s.get("state") != "LOCAL":
                continue
            res = s.get("local_result")
            if not res:
                # 超时兜底：worker 线程若异常退出或被卡死，local_result 永远为空。
                # 无限等待会让会话砖化（_try_local_task 见 LOCAL 即拒绝新任务），
                # 故超过硬截止判失败并改投 relay。
                dl = float(s.get("local_deadline") or 0)
                if dl and time.time() > dl:
                    self._reap_local(key, s, "timeout")
                continue                       # 仍在跑
            s["local_result"] = None
            r, biz, uid = res["r"], res["biz"], res["uid"]
            desc, out = res["desc"], str(r.get("text") or "").strip()
            if r.get("escalate") or not out:
                s["state"] = "IDLE"
                s["buffer"] = [desc]
                why = str(r.get("error") or "")[:60]
                log(f"local task escalate→relay for {uid}"
                    + (f" ({why})" if why else ""))
                # 必须在 _flush 之前回报：_flush 会把 s["task_id"] 覆盖成新 relay
                # 任务号，晚了就找不到影子记录、留一条永久 running 的僵尸行。
                self._report_local(s, "cancelled", uid=uid,
                                   error="升级 relay 门禁"
                                         + (f"：{why}" if why else ""))
                self._flush(s, uid=uid, force_relay=True)
                continue
            s["state"] = "IDLE"
            # 代投文件（local 无 Bash 跑不了 dws，由 avatar 代投）。必须在清元数据
            # 与回报之前做：去重键要取影子 task_id，且回报/回话都应是净化后的正文。
            dedupe = s.get("task_id") or f"local-{s.get('local_started')}"
            out, notes = self._consume_send_markers(out, s, dedupe, uid)
            out, dnotes = self._consume_delete_markers(out, s, dedupe, uid)
            notes = notes + dnotes
            if notes:
                out = (out + ("\n\n" if out else "") + "\n".join(notes)).strip()
            # 成功收尾即清恢复元数据：local_reap_count 若只增不减，累计 2 次后
            # （哪怕中间都成功）会永久禁用自动重投。
            s["local_desc"] = ""
            s["local_started"] = 0
            s["local_deadline"] = 0
            s["local_reap_count"] = 0
            self._report_local(s, "done", uid=uid, result=out)
            self.st.archive_round(f"local-{uid}", "done",
                                  {"backend": "local", "chars": len(out),
                                   "out": out[:500]})
            done = False
            if biz:
                try:
                    done = bool(self.dws.card_update(biz, out[:2500], 3))
                except Exception:
                    done = False
            if not done:
                self._notify_session(s, out)
            log(f"local task done for {uid} ({len(out)} chars)")
            if s.get("followups"):
                s["state"] = "FOLLOWUP"        # 交 _tick 的 due() 正常 flush
                s["deadline"] = time.time() + 1

    def _report_local(self, s: dict, status: str, uid: str = "",
                      result: str = "", error: str = ""):
        """回报 local 影子记录终态并摘掉 task_id。

        无影子记录（建记录时 relay 不可达）则静默跳过——可见性是增益，
        不能反过来成为 local 通路的可用性前提。
        回报失败只记日志不抛：relay 侧有 sweep_stale 兜底（超 local_stale_minutes
        未回报即判 failed），不会因为一次网络抖动就把会话态搞坏。
        """
        tid = s.get("task_id")
        if not tid:
            return
        s["task_id"] = None
        try:
            r = self.exec_relay.report_local_status(tid, status, uid=uid,
                                                    result=result, error=error)
            if "_error" in r:
                log(f"local status report #{tid} -> {r.get('_error')}")
        except Exception as e:
            log(f"local status report #{tid} failed:", repr(e)[:120])

    def _reap_local(self, key: str, s: dict, reason: str):
        """清理已死的 LOCAL 态会话：改投 relay（持久、可轮询、跨重启存活）+ 如实回执。

        为什么必须清理：local 任务跑在 daemon 线程里，进程一退线程即消失，而
        state="LOCAL" 已落盘。不清理则该会话**永久砖化**——_drain_local 永远等不到
        local_result，_try_local_task 又因 state=="LOCAL" 拒绝新任务，用户既收不到
        结果也发不出下一条，且不会收到任何失败回执（2026-09-24 事故：11:03 重启
        打断 10:58 的任务，用户等待 40+ 分钟无任何下文）。

        reason: restart（启动清理上一进程遗留）| timeout（同进程内超硬截止）。
        """
        desc = str(s.get("local_desc") or "").strip()
        uid = s.get("uid") or ""
        n = int(s.get("local_reap_count") or 0)
        why = ("服务重启" if reason == "restart" else "执行超时")
        s["state"] = "IDLE"
        s["local_result"] = None
        s["local_deadline"] = 0
        s["local_reap_count"] = n + 1
        # 影子记录标 failed。必须在 _flush 之前——_flush 会覆盖 s["task_id"]。
        # 三个分支（无 desc／重投护栏／正常改投）都要报，故放在分支之前。
        self._report_local(s, "failed", uid=uid,
                           error=f"local 任务中断（{why}），未产出结果")
        self.audit.log("local_reaped", uid,
                       f"conv={key} reason={reason} resubmit={n + 1} "
                       f"started={s.get('local_started')}",
                       backend="local")
        log(f"reaping stale LOCAL session {key} (reason={reason})")
        if not desc:
            self._notify_session(
                s, f"⚠️ 上一条任务因{why}中断，且未留存任务内容，"
                   f"麻烦你再发一次。")
            return
        if n >= 2:
            # 反复中断（如崩溃循环）不再自动重投，避免任务风暴；交回人工。
            self._notify_session(
                s, f"⚠️ 上一条任务因{why}中断，且已自动重投 2 次仍未跑成，"
                   f"先不再自动重试。请稍后重发，或联系管理员。")
            return
        self._notify_session(
            s, f"⚠️ 上一条任务因{why}中断，未产出结果。已自动改投持久通道重跑，"
               f"跑完主动回你。")
        s["buffer"] = [desc]
        self._flush(s, uid=uid, force_relay=True)

    def _reap_stale_local(self):
        """启动时清理上一进程遗留的 LOCAL 态会话（见 _reap_local）。"""
        for key, s in list(self.sess.sessions.items()):
            if s.get("state") == "LOCAL":
                self._reap_local(key, s, "restart")
        self.sess.save()

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

    def _relay_delivery_rule(self, s: dict) -> str:
        """附在 relay 任务描述末尾的投递铁律：告知发起人是谁 + 禁止自行发送。

        为什么必须显式写进描述：relay 任务的 CLAUDE.md **不含发起人身份**（#242 的
        CLAUDE.md 里搜不到任何收件人口径），而 shared/skills/dingtalk-send/SKILL.md
        明写「本技能里的"我/本人"一律指**当前登录 dws 的账号**」+「**免确认直发**
        — 发个人/本人是标准动作，不要反问确认或问发到哪」。本机 dws 登录账号就是
        机器人自己（identity.local.json: selfName=鲁大师, selfUserId=37505774），
        于是用户说「发我钉钉」被 agent 如实执行成**发给机器人自己**：
        query-send-status 真返回 SUCCESS，发起人什么都收不到，agent 还会反过来
        猜「可能是客户端会话列表未刷新」（2026-09-25 #242/#243 事故）。

        agent 没做错任何一步——是「本人」的语义在分身场景下根本是错的，且它没有
        别的投递通道可用。故这里同时给出口径与替代路径（```send_file``` 标记）。
        """
        r = s.get("reply") or {}
        who = str(r.get("sender") or "").strip() or "发起人"
        conv = "群聊" if r.get("is_group") else "单聊"
        return (
            f"\n\n---\n【投递铁律｜由分身代投，禁止自行发送】\n"
            f"本任务由「{who}」通过钉钉分身（{conv}）发起，结果会回到该会话。\n"
            f"· **不要自己跑 dws 或任何钉钉发送命令**。本机 dws 登录账号是机器人"
            f"「{self.cfg.avatar_name}」，dingtalk-send 技能里的\"我/本人\"指的是它、"
            f"不是发起人；自行直发会把文件发给机器人自己，发起人收不到，而 "
            f"sendStatus 仍返回 SUCCESS（已发生过事故）。\n"
            f"· 要交付文件：把件放在本任务工作区的 output/ 或 work/ 下，然后在"
            f"**结果正文**里用围栏块声明，由分身代投给发起会话（收件人系统锁定、"
            f"标记里写什么都改不了）：\n"
            f"  ```send_file\n"
            f'  {{"path": "<绝对路径，必须在本任务 output/ 或 work/ 内>"}}\n'
            f"  ```\n"
            f"· 一次最多 5 件；用户要几种格式就声明几个块，不要合并成一个。"
            f"标记块不会展示给用户，正文不必重复解释。\n"
            f"· 做不到的格式要**如实说明做不了及原因**，可给替代件但必须讲清"
            f"「你要的 X 没做到、给的是 Y」，严禁静默降级改名糊弄。")

    def _send_result(self, s: dict, row: dict, prefix: str = ""):
        tid = row.get("id")
        uid = str(s.get("uid") or "")
        result = (row.get("result") or "").strip()
        result, notes = self._consume_plan_markers(result, s, tid)
        # relay 通路同样消费 ```send_file``` 标记。**必须接上**：此前只有 local
        # 通路（_drain_local）消费，relay 任务没有标记代投这条路可走，agent 只能
        # 自己跑 dws 发送——而本机 dws 登录账号就是机器人「鲁大师」，
        # shared/skills/dingtalk-send/SKILL.md 又明写「本技能里的"我/本人"一律指
        # **当前登录 dws 的账号**」且「免确认直发…不要反问确认或问发到哪」，于是
        # 用户说「发我钉钉」被如实执行成**发给机器人自己**：query-send-status 真的
        # 返回 SUCCESS，发起人却什么都收不到，agent 还会反过来猜「客户端未刷新」。
        # （2026-09-25 任务 #242/#243，用户自 20:36 起连续 6 次反馈文件收不到。）
        # 收件人必须由系统锁定为发起会话——这正是标记代投存在的理由。
        # 作用域额外给本任务 workdir 下的 output/ 与 work/（relay 出件落在那里）。
        result, sn = self._consume_send_markers(
            result, s, tid, uid, extra_roots=self._task_artifact_roots(row))
        notes += sn
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

    @staticmethod
    def _grab_marker_specs(result: str, rx) -> tuple:
        """抽出围栏标记里的 JSON 规格，返回 (净化正文, specs)。

        解析失败的块**原样保留在正文里**，不吞——让用户看见模型到底写了什么，
        而不是静默丢掉一段话（沿用 _consume_plan_markers 的处置）。
        """
        specs: list = []

        def _grab(m):
            try:
                obj = json.loads(m.group(1))
            except Exception:
                return m.group(0)
            if isinstance(obj, dict):
                specs.append(obj)
                return ""
            return m.group(0)

        return rx.sub(_grab, result).strip(), specs

    @staticmethod
    def _scoped_path(raw: str, roots: list) -> tuple:
        """把标记里的路径归一化并校验是否落在 roots 内，返回 (realpath, 越界?)。

        必须先 abspath 再 realpath：前者收掉 `../` 穿越，后者解析符号链接——
        否则在可写目录里放一个指向 /mnt/.../relay.env 的软链就能把任意文件带出去。
        """
        p = os.path.realpath(os.path.abspath(str(raw or "").strip()))
        ok = any(p == r or p.startswith(r + os.sep) for r in roots)
        return p, (not ok)

    def _roots(self, key: str) -> list:
        return [os.path.realpath(str(x)) for x in
                (self.cfg.section("security").get(key) or [])]

    def _task_artifact_roots(self, row: dict) -> list:
        """relay 任务的出件目录：workdir 下的 output/ 与 work/。

        **只给这两个子目录、不给整个 workdir**——workdir 里还有 .baseline.json、
        .claude/（会话数据）、.xdg/、CLAUDE.md（注入的上下文与知识片段），
        这些都不该被代投出去。
        """
        w = str((row or {}).get("workdir") or "").strip()
        if not w:
            return []
        out = []
        for sub in ("output", "work"):
            p = os.path.realpath(os.path.join(w, sub))
            if os.path.isdir(p):
                out.append(p)
        return out

    def _consume_send_markers(self, result: str, s: dict, tid, uid: str,
                              extra_roots: list | None = None):
        """解析 ```send_file``` 标记并由 avatar 代投文件，返回 (净化正文, 附注)。

        为什么由 avatar 代投而不是给 local 开 Bash：relay-avatar.service **没有任何
        沙箱**（无 ProtectSystem／InaccessiblePaths，与 relay.service 不同），local
        子进程一旦拿到 Bash 就是无限制 root——能读 relay.env 取 admin token、
        rm -rf shared/、systemctl stop relay。而 `Bash(python3 *)` 这类作用域规则是
        假边界（`python3 -c "import shutil…"` 一句话绕过）。故投递能力放在持有 dws
        的 avatar 这一层，local 只负责把件写到作用域目录里。

        五道护栏（缺一不可，标记内容来自模型输出、可被用户输入间接注入）：
        ① 路径必须 realpath 落在 security.local_write_dirs 内——即"只能发它本来
           就有权写的东西"，堵住 `../../relay.env` 与符号链接逃逸；
        ② 收件人**恒为发起会话** s["reply"]，标记里写什么都不能改投递目标，
           杜绝把内部件外发给第三方；
        ③ 必须是已存在的普通文件，且不超过 media_max_size_mb；
        ④ 单次结果最多 SEND_MAX_FILES 件，防刷；
        ⑤ 按 tid 去重，review→done 二次回流不重复发。
        """
        clean, specs = self._grab_marker_specs(result, SEND_BLOCK_RE)
        if not specs or s.get("send_applied") == tid:
            return clean, []
        s["send_applied"] = tid
        roots = self._roots("local_write_dirs") + list(extra_roots or [])
        maxb = int(self.cfg.get("security", "media_max_size_mb", 20)) * 1024 * 1024
        notes: list = []
        sent = 0
        for spec in specs:
            raw = str(spec.get("path") or "").strip()
            if not raw:
                continue
            if sent >= SEND_MAX_FILES:
                notes.append(f"⚠️ 超过单次代投上限 {SEND_MAX_FILES} 件，"
                             f"其余未发（{os.path.basename(raw)}）")
                continue
            p, out = self._scoped_path(raw, roots)
            if out:
                self.audit.log("send_refused", uid, f"越界路径 {p[:120]}",
                               backend="local", task_id=tid)
                notes.append(f"⚠️ 拒绝代投：`{os.path.basename(raw)}` 不在可写作用域内")
                continue
            if not os.path.isfile(p):
                notes.append(f"⚠️ 代投失败：`{os.path.basename(raw)}` 不是已存在的文件")
                continue
            if os.path.getsize(p) > maxb:
                notes.append(f"⚠️ 代投失败：`{os.path.basename(raw)}` 超过 "
                             f"{maxb // 1048576}MB 上限")
                continue
            try:
                r = self.dws.send_file(s["reply"], p)
            except Exception as e:
                r = {"_exc": repr(e)[:120]}
            ok, otid, err = send_accepted(r)
            # **受理 ≠ 到达**：dws 文件发送是异步的（outcome=pending、
            # state=processing），data.success=true 只代表请求被受理。必须查
            # sendStatus 才知道真到没到——2026-09-25 用户连续 6 次反馈「文件
            # 发不出来」而审计一路记成功，就是只看受理态 + 判定过松叠加的结果。
            delivered = None
            if ok and otid:
                for _ in range(4):
                    time.sleep(2)
                    try:
                        sr = self.dws.send_status(otid)
                    except Exception:
                        break
                    res = sr.get("result") if isinstance(sr, dict) else None
                    stt = res.get("sendStatus") if isinstance(res, dict) else None
                    if stt:
                        delivered = (str(stt).upper() == "SUCCESS")
                        if not delivered:
                            err = f"sendStatus={stt}"
                        break
            good = ok and delivered is not False
            tail = (f" 投递={delivered}" if delivered is not None
                    else (" 受理未确认" if ok else f" err={err[:90]}"))
            self.audit.log("send_file" if good else "send_failed", uid,
                           f"{p[:120]} ({os.path.getsize(p)}B){tail}",
                           backend="local", task_id=tid)
            sent += 1 if good else 0
            if good:
                notes.append(f"📎 已发文件：`{os.path.basename(p)}`"
                             + ("" if delivered else "（已受理，投递状态未确认）"))
            else:
                notes.append(f"⚠️ 文件发送失败：`{os.path.basename(p)}` {err[:80]}")
        return clean, notes

    def _consume_delete_markers(self, result: str, s: dict, tid, uid: str):
        """解析 ```delete_file``` 标记并由 avatar 代删，返回 (净化正文, 附注)。

        为什么需要它：local 无 Bash，Write/Edit 也只能建/改不能删，所以"应用户请求
        清理草稿"这件事 agent 自己**根本做不到**（2026-09-24 王洪彬「删除这个
        helloworld」被铁律挡下，其实是能力缺失不只是策略禁止）。

        与代投同源但**作用域更窄**：可删根目录取 security.local_delete_dirs，
        只应配 shared/work/（草稿区）。shared/report/ 是正式交付区、local 又不做
        baseline 快照，删了不可追溯，故不在可删范围内——要删走 relay 门禁。

        护栏与 _consume_send_markers 同源：路径 realpath 必须在可删根内（堵 `../`
        与符号链接逃逸）、只删普通文件（**不递归删目录**，避免一次误删整棵子树）、
        单次上限、按 tid 去重、逐条审计（delete_file / delete_refused / delete_failed）。
        """
        clean, specs = self._grab_marker_specs(result, DELETE_BLOCK_RE)
        if not specs or s.get("delete_applied") == tid:
            return clean, []
        s["delete_applied"] = tid
        roots = self._roots("local_delete_dirs")
        notes: list = []
        if not roots:
            for spec in specs:
                notes.append("⚠️ 本机未开放代删（security.local_delete_dirs 为空）")
                break
            return clean, notes
        done = 0
        for spec in specs:
            raw = str(spec.get("path") or "").strip()
            if not raw:
                continue
            if done >= DELETE_MAX_FILES:
                notes.append(f"⚠️ 超过单次代删上限 {DELETE_MAX_FILES} 件，"
                             f"其余未删（{os.path.basename(raw)}）")
                continue
            p, out = self._scoped_path(raw, roots)
            if out:
                self.audit.log("delete_refused", uid, f"越界路径 {p[:120]}",
                               backend="local", task_id=tid)
                notes.append(f"⚠️ 拒绝删除：`{os.path.basename(raw)}` 不在可删作用域内"
                             f"（仅草稿区可删，正式交付区请走 relay 门禁）")
                continue
            if os.path.isdir(p):
                self.audit.log("delete_refused", uid, f"目录不删 {p[:120]}",
                               backend="local", task_id=tid)
                notes.append(f"⚠️ 拒绝删除：`{os.path.basename(raw)}` 是目录"
                             f"（只删单个文件，不递归）")
                continue
            if not os.path.isfile(p):
                notes.append(f"⚠️ 无需删除：`{os.path.basename(raw)}` 不存在")
                continue
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                self.audit.log("delete_file", uid, f"{p[:120]} ({sz}B)",
                               backend="local", task_id=tid)
                done += 1
                notes.append(f"🗑️ 已删除：`{os.path.basename(p)}`")
            except Exception as e:
                self.audit.log("delete_failed", uid,
                               f"{p[:120]} {e!r}"[:160], backend="local",
                               task_id=tid)
                notes.append(f"⚠️ 删除失败：`{os.path.basename(p)}` {str(e)[:80]}")
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
        self._drain_local()      # 先收尾本机任务，escalate 的可在本轮直接转 relay
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
        # 重启后清理 LOCAL 态遗留会话：改投 relay + 回执（否则会话永久砖化）
        self._reap_stale_local()
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
