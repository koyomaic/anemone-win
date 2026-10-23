"""app/session.py —— 会话状态机 + debounce 聚合（自 im-bridge 演进，语义不变）。

状态：IDLE / BUFFERING / RUNNING / WAIT_APPROVAL / WAIT_REVIEW / FOLLOWUP
- BUFFERING：20s debounce 聚合多条消息为一个任务；「提交」立即执行；「取消」清缓冲
- RUNNING/WAIT_*：期间新消息进 followups；任务结束后 30s followup 窗口 resume_from 追问
- 状态落盘 state.json（原子写，重启恢复）；schema 与 im-bridge 兼容（迁移即拷文件）
"""
from __future__ import annotations

import re
import time

# 附件标记：钉钉把图片/文件/视频/语音消息渲染成这些占位文本（实测入站事件里
# 形如「[图片消息](mediaId=@lQLP…)」「[文件] xxx fileId: yyy」）。
ATTACHMENT_MARK_RE = re.compile(
    r"\[图片消息\]|\[文件\]|\[视频\]|\[语音\]|mediaId=|fileId[:：]")


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
            "review_followup_notified": False, "review_reprompt_count": 0,
            "user_started_at": 0.0,
            "key": "",
            "card_biz": "", "card_hint": "", "card_start": 0.0,
            "card_upd": 0.0, "card_backend": "", "card_task_id": None,
            "card_state": "", "card_progress": {},
            "card_real_summary_count": 0, "card_last_real_summary": 0.0,
            "local_timeout": 0.0,
            "uid": ev.get("_uid", ""),
            # 在途请求原文（relay 通路提交后留存），供跨会话重复请求去重比对。
            # local 通路用 local_desc，BUFFERING 用 buffer，故只在 relay 通路写。
            "inflight_desc": "",
            # 去重绑定进来的**同一发起人**的其它会话投递目标。需求只跑一次，
            # 结果送到他发过这条需求的每一个会话（见 main._bind_duplicate）。
            "extra_targets": [],
            "reply": {
                "is_group": is_group,
                "open_dingtalk_id": sid,
                "conversation_id": ev.get("conversation_id") or "",
                "sender": sender,
            },
        }

    def get_or_create(self, key: str, sender: str, sid: str, ev: dict,
                      is_group: bool) -> dict:
        s = self.sessions.setdefault(
            key, self.new_session(sender, sid, ev, is_group))
        s["key"] = key
        return s

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

    # ── 聚合窗（debounce）──
    #
    # 为什么分两档而不是一刀切缩短：实测 98 个会话 / 1271 对真人相邻消息，
    # **23.2% 的间隔 ≤20s**，且抽样全是真实的多条组合请求——「你好，你是谁？」
    # →5s 后补一张图、「跑着跑着京小智崩了」→5s 后补截图、「这会有空吗？」→8s
    # 后补「一起看下这个问题？」。窗口砍到 0 会把这些劈成两个任务，agent 只看到
    # 文字没看到图就作答，那是**质量事故**，不是快。
    #
    # 但含附件与纯文本的间隔分布不同（60s 内样本）：
    #   涉及图片/文件 n=100  中位 9s   p75 28s  → 20s 窗只兜住 65%
    #   纯文本         n=420  中位 17s  p75 24s  → 20s 窗兜住 54.8%，12s 兜住 ~47%
    # 所以：**缓冲里一旦出现附件标记就用长窗**（这类劈开代价最大），纯文本用短窗
    # （劈开也只是退化成 followup 追问，会 resume 上一条会话并注入摘要，可恢复）。
    # 两档都可在 policy.toml 调，设 0 即关闭该档等待。

    def _debounce_sec(self, s: dict) -> int:
        """本轮聚合窗长度：缓冲含附件标记 → 长窗；纯文本 → 短窗。"""
        buf = "\n".join(str(x) for x in (s.get("buffer") or []))
        if ATTACHMENT_MARK_RE.search(buf):
            return int(self.cfg.get("ingress", "debounce_sec", 20))
        return int(self.cfg.get("ingress", "debounce_text_sec", 12))

    def start_buffering(self, s: dict, text: str):
        s["buffer"] = [text]
        s["state"] = "BUFFERING"
        s["deadline"] = time.time() + self._debounce_sec(s)

    def extend_buffer(self, s: dict, text: str):
        """BUFFERING 期间又来一条：追加，并把窗口从这条起重新计（滑动 debounce）。

        必须走这里而不是在调用方直接改 deadline——窗口长度取决于**当前缓冲内容**
        （有没有附件），两处各算一遍迟早算岔。
        """
        s.setdefault("buffer", []).append(text)
        s["deadline"] = time.time() + self._debounce_sec(s)

    def start_followup_window(self, s: dict):
        s["state"] = "FOLLOWUP"
        s["deadline"] = time.time() + int(
            self.cfg.get("ingress", "followup_debounce_sec", 30))

    def resume_candidate(self, s: dict):
        """会话上下文有效期内、且未超上下文预算的上一任务号（resume_from 追问）。"""
        if self.resume_break_reason(s) is not None:
            return None
        return s.get("last_task_id")

    # ── resume 上下文预算 ──
    #
    # 为什么需要：resume_candidate 此前**只有 TTL 判定**，没有任何体量上限。只要
    # 用户在 session_ttl_sec 窗口内持续追问，链就无限续下去——claude 每轮都要重放
    # 整段历史。2026-09-28 实测赵兴峰 #278→#281 四任务共用一个会话（transcript
    # 1004KB、CLAUDE.md 42KB 每任务重注入），单轮模型往返从 7.3s 涨到 32.6s
    # （4.5 倍）：#281 轮次(22)比 #278(46)少一半，耗时却是 2.1 倍，13 分钟里
    # 12 分钟纯花在模型往返上，占执行时间 75%。
    #
    # 触顶后**不是裸开新会话**——那会丢掉「用赵老师工艺深色底」这类依赖上文的追问。
    # 而是断链 + 由 main._flush 注入接续摘要（上一任务 result，已剥标记、已截断）。

    def chain_over_budget(self, s: dict) -> str | None:
        """链上预算是否触顶。返回原因字符串，None＝未触顶。0/负值＝该项不设限。"""
        depth = int(s.get("chain_depth") or 0)
        max_depth = int(self.cfg.get("ingress", "resume_max_depth", 3))
        if max_depth > 0 and depth >= max_depth:
            return f"depth {depth}>={max_depth}"
        toks = int(s.get("chain_tokens") or 0)
        # 兜底默认与 config.DEFAULTS 同值（1500000）。这个口径是「任务内所有 API
        # 调用的 input+output 累加」，不是纯输出——2026-10-23 claude-proxy 补回
        # input_tokens 后抬过一档，两处必须一起改，否则漏配时会退回旧的 60000、
        # 一个普通任务跑完就断链。
        max_toks = int(self.cfg.get("ingress", "resume_max_tokens", 1500000))
        if max_toks > 0 and toks >= max_toks:
            return f"tokens {toks}>={max_toks}"
        # 转写字节闸门：token 闸门量的是 relay 记的 tokens，而那个数在
        # claude-proxy 丢掉流式 input_tokens 期间退化成纯输出，对图片载荷完全
        # 瞎（2026-10-23 录屏链 chain_tokens=48387 / 转写 12.66MB，闸门没拦）。
        # 字节数量的一定是 --resume 会整段重放的东西，且不依赖任何遥测。
        byts = int(s.get("chain_bytes") or 0)
        max_bytes = int(self.cfg.get("ingress", "resume_max_transcript_bytes",
                                     2000000))
        if max_bytes > 0 and byts >= max_bytes:
            return f"transcript {byts}>={max_bytes}"
        return None

    def resume_break_reason(self, s: dict) -> str | None:
        """不能 resume 的原因：None＝可以 resume；ttl＝话题已断；budget＝超上下文预算。

        区分两者是因为处置不同：budget 断链要注入接续摘要（用户还在同一话题上，
        只是历史太贵）；ttl 断链不注入（话题已断，塞旧上下文只会干扰新请求）。
        """
        if not s.get("last_task_id"):
            return "none"
        ttl = int(self.cfg.get("ingress", "session_ttl_sec", 1800))
        if time.time() - s.get("last_activity", 0) >= ttl:
            return "ttl"
        over = self.chain_over_budget(s)
        return f"budget:{over}" if over else None

    def note_chain(self, s: dict, tokens, digest: str = "",
                   transcript_bytes: int = 0):
        """任务完成时累加链上预算，并记下接续摘要（供断链后注入）。

        transcript_bytes 是**赋值不是累加**：一条链上所有任务共用同一个会话
        转写，最新那次的文件大小就是整条链的体量。0＝没量到，保留原值不动
        （量不到不该把已有判定擦掉）。
        """
        s["chain_depth"] = int(s.get("chain_depth") or 0) + 1
        try:
            s["chain_tokens"] = int(s.get("chain_tokens") or 0) + max(int(tokens or 0), 0)
        except (TypeError, ValueError):
            pass
        try:
            if int(transcript_bytes or 0) > 0:
                s["chain_bytes"] = int(transcript_bytes)
        except (TypeError, ValueError):
            pass
        if digest:
            s["chain_digest"] = digest

    def reset_chain(self, s: dict):
        """开新链（本轮未 resume）时清零计数。

        摘要**保留**：它正是断链后要注入的东西，且会被新任务完成时覆盖。
        """
        s["chain_depth"] = 0
        s["chain_tokens"] = 0
        s["chain_bytes"] = 0
