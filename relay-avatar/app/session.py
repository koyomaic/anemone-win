"""app/session.py —— 会话状态机 + debounce 聚合（自 im-bridge 演进，语义不变）。

状态：IDLE / BUFFERING / RUNNING / WAIT_APPROVAL / WAIT_REVIEW / FOLLOWUP
- BUFFERING：20s debounce 聚合多条消息为一个任务；「提交」立即执行；「取消」清缓冲
- RUNNING/WAIT_*：期间新消息进 followups；任务结束后 30s followup 窗口 resume_from 追问
- 状态落盘 state.json（原子写，重启恢复）；schema 与 im-bridge 兼容（迁移即拷文件）
"""
from __future__ import annotations

import time


class SessionManager:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.st = storage
        self.sessions: dict = {}
        self.seen: list = []
        self.seen_set: set = set()
        self._load()

    # ── 持久化 ──
    def _file(self):
        return self.st.state / "state.json"

    def _load(self):
        s = self.st.load_json(self._file(), {})
        self.sessions = s.get("sessions", {}) or {}
        self.seen = (s.get("seen", []) or [])[-5000:]
        self.seen_set = set(self.seen)

    def save(self):
        self.st.save_json(self._file(),
                          {"sessions": self.sessions,
                           "seen": self.seen[-5000:]})

    # ── 去重 ──
    def already_seen(self, eid: str) -> bool:
        if not eid:
            return False
        if eid in self.seen_set:
            return True
        self.seen.append(eid)
        self.seen_set.add(eid)
        if len(self.seen) > 5000:
            self.seen = self.seen[-5000:]
            self.seen_set = set(self.seen)
        return False

    # ── 会话 ──
    def new_session(self, sender: str, sid: str, ev: dict, is_group: bool) -> dict:
        return {
            "state": "IDLE", "buffer": [], "followups": [],
            "task_id": None, "last_task_id": None, "deadline": 0,
            "notified_followup": False, "last_activity": time.time(),
            "uid": ev.get("_uid", ""),
            "reply": {
                "is_group": is_group,
                "open_dingtalk_id": sid,
                "conversation_id": ev.get("conversation_id") or "",
                "sender": sender,
            },
        }

    def get_or_create(self, key: str, sender: str, sid: str, ev: dict,
                      is_group: bool) -> dict:
        return self.sessions.setdefault(
            key, self.new_session(sender, sid, ev, is_group))

    def touch(self, s: dict, ev: dict):
        s["reply"]["sender"] = ev.get("sender") or s["reply"].get("sender", "")
        s["last_activity"] = time.time()
        s["last_msg_id"] = ev.get("message_id") or ""
        s["last_conv_id"] = ev.get("conversation_id") or ""

    def due(self, now: float | None = None):
        """到期待 flush 的会话：[(key, s, 'buffer'|'followup'), ...]"""
        now = now or time.time()
        out = []
        for key, s in list(self.sessions.items()):
            if not s.get("deadline") or now < s["deadline"]:
                continue
            if s["state"] == "BUFFERING":
                out.append((key, s, "buffer"))
            elif s["state"] == "FOLLOWUP":
                out.append((key, s, "followup"))
        return out

    def cleanup_expired(self, now: float | None = None):
        now = now or time.time()
        ttl = int(self.cfg.get("ingress", "session_ttl_sec", 1800)) * 2
        for key in [k for k, s in self.sessions.items()
                    if s["state"] == "IDLE" and not s.get("buffer")
                    and now - s.get("last_activity", 0) > ttl]:
            del self.sessions[key]

    def start_buffering(self, s: dict, text: str):
        s["buffer"] = [text]
        s["state"] = "BUFFERING"
        s["deadline"] = time.time() + int(
            self.cfg.get("ingress", "debounce_sec", 20))

    def start_followup_window(self, s: dict):
        s["state"] = "FOLLOWUP"
        s["deadline"] = time.time() + int(
            self.cfg.get("ingress", "followup_debounce_sec", 30))

    def resume_candidate(self, s: dict):
        """会话上下文有效期内的上一任务号（resume_from 追问）。"""
        last = s.get("last_task_id")
        ttl = int(self.cfg.get("ingress", "session_ttl_sec", 1800))
        if last and time.time() - s.get("last_activity", 0) < ttl:
            return last
        return None
