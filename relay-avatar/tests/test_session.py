"""tests/test_session.py —— 会话状态机 + debounce + resume_from + 去重。"""
import time

from app.session import SessionManager


def make_ev(**kw):
    ev = {"conversation_id": "cid1", "message_id": "m1", "sender": "张三",
          "_uid": "200"}
    ev.update(kw)
    return ev


def test_new_session_and_persist(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:sid1", "张三", "sid1", make_ev(), False)
    assert s["state"] == "IDLE" and s["uid"] == "200"
    sm.start_buffering(s, "任务内容")
    assert s["state"] == "BUFFERING" and s["buffer"] == ["任务内容"]
    sm.save()
    sm2 = SessionManager(cfg, storage)
    assert sm2.sessions["o:sid1"]["state"] == "BUFFERING"
    assert sm2.sessions["o:sid1"]["buffer"] == ["任务内容"]


def test_dedup(cfg, storage):
    sm = SessionManager(cfg, storage)
    assert not sm.already_seen("e1")
    assert sm.already_seen("e1")
    assert not sm.already_seen("")


def test_due_flush(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    sm.start_buffering(s, "x")
    assert sm.due() == []
    s["deadline"] = time.time() - 1
    due = sm.due()
    assert len(due) == 1 and due[0][2] == "buffer"
    s["state"] = "FOLLOWUP"
    assert sm.due()[0][2] == "followup"


def test_resume_candidate(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    assert sm.resume_candidate(s) is None
    s["last_task_id"] = 42
    s["last_activity"] = time.time()
    assert sm.resume_candidate(s) == 42
    s["last_activity"] = time.time() - 99999
    assert sm.resume_candidate(s) is None


def test_cleanup_expired(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    s["last_activity"] = time.time() - 999999
    sm.cleanup_expired()
    assert "o:a" not in sm.sessions
    s2 = sm.get_or_create("o:b", "李四", "b", make_ev(), False)
    s2["buffer"] = ["x"]
    s2["last_activity"] = time.time() - 999999
    sm.cleanup_expired()
    assert "o:b" in sm.sessions   # 有缓冲不清


# ── resume 上下文预算（2026-09-28 P0-1）──
#
# 此前 resume_candidate **只有 TTL 判定**，链可无限续：claude 每轮重放整段历史。
# 实测赵兴峰 #278→#281 四任务共用一个会话（transcript 1004KB），单轮模型往返
# 7.3s→32.6s（4.5 倍），#281 轮次比 #278 少一半、耗时却 2.1 倍，13 分钟里 12 分钟
# 纯花在模型往返上（占执行时间 75%）。


def _live(sm, s, tid=42):
    """构造一个「刚跑完 tid、仍在 TTL 内」的会话。"""
    s["last_task_id"] = tid
    s["last_activity"] = time.time()
    return s


def test_resume_budget_defaults_allow_short_chains(cfg, storage):
    """默认额度下正常追问不受影响（回归保护：别把日常两轮追问也断了）。"""
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 9887, "上一轮结论")            # #279 量级
    assert sm.resume_candidate(s) == 42
    sm.note_chain(s, 25864, "又一轮")               # #280 量级，累计 35751
    assert sm.resume_candidate(s) == 42


def test_resume_depth_cap(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    for i in range(3):                               # 默认 resume_max_depth=3
        sm.note_chain(s, 100, f"r{i}")
        s["last_task_id"] = 42 + i
    assert s["chain_depth"] == 3
    assert sm.resume_candidate(s) is None
    assert sm.resume_break_reason(s).startswith("budget:depth")


def test_resume_token_cap(cfg, storage):
    """#280/#281 的真实量级：两轮就吃掉 57k tokens，第三轮触顶。
    先关掉 depth 上限以隔离 token 判定（否则 3 次 note_chain 会先撞 depth）。"""
    cfg._p.setdefault("ingress", {}).update(resume_max_depth=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 25864, "a")
    assert sm.resume_candidate(s) == 42
    sm.note_chain(s, 31332, "b")                     # 累计 57196 < 60000
    assert sm.resume_candidate(s) == 42
    sm.note_chain(s, 5000, "c")                      # 累计 62196 >= 60000
    assert sm.resume_candidate(s) is None
    assert "tokens" in sm.resume_break_reason(s)


def test_resume_budget_zero_disables_cap(cfg, storage):
    """0/负值＝不设限，给运维留整体关闭的口子（与 local_sandbox 同风格）。"""
    cfg._p.setdefault("ingress", {}).update(
        resume_max_depth=0, resume_max_tokens=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    for i in range(10):
        sm.note_chain(s, 999999, f"r{i}")
    assert s["chain_depth"] == 10
    assert sm.resume_candidate(s) == 42              # 照旧 resume


def test_resume_ttl_still_wins_over_budget(cfg, storage):
    """TTL 过期优先判为 ttl（不注入摘要），即使预算也超了。"""
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    for i in range(5):
        sm.note_chain(s, 99999, f"r{i}")
    s["last_activity"] = time.time() - 99999
    assert sm.resume_candidate(s) is None
    assert sm.resume_break_reason(s) == "ttl"


def test_reset_chain_keeps_digest(cfg, storage):
    """清零只清计数：摘要要留着给断链后注入，且会被下一轮覆盖。"""
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "旧摘要")
    sm.reset_chain(s)
    assert s["chain_depth"] == 0 and s["chain_tokens"] == 0
    assert s["chain_digest"] == "旧摘要"
    sm.note_chain(s, 50, "新摘要")
    assert s["chain_digest"] == "新摘要" and s["chain_tokens"] == 50


def test_note_chain_tolerates_bad_tokens(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, None, "x")
    sm.note_chain(s, "not-a-number", "y")
    sm.note_chain(s, -5, "z")
    assert s["chain_depth"] == 3 and s["chain_tokens"] == 0


def test_resume_break_reason_no_last_task(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = sm.get_or_create("o:a", "张三", "a", make_ev(), False)
    assert sm.resume_break_reason(s) == "none"
    assert sm.resume_candidate(s) is None


# ── resume 转写字节闸门（2026-10-23）──
#
# token 闸门量的是 relay 记的 tokens，而那个数在 claude-proxy 丢掉流式
# input_tokens 期间退化成纯输出，对图片载荷完全瞎：宋连杰一个 181s 录屏经
# 378→380→382→384→386 五任务共用一个 session，累积 15 个图片块 / 6.11MB
# base64、转写 12.66MB，chain_tokens 却只有 48387，60000 的闸门纹丝不动，
# 一句话追问（#382）也跑满 134s。字节数不依赖遥测，量的一定是会被重放的载荷。


def test_resume_transcript_bytes_cap(cfg, storage):
    """#380→#384 的真实量级：tokens 远未触顶，转写已 12.66MB → 必须断链。"""
    cfg._p.setdefault("ingress", {}).update(resume_max_depth=0,
                                            resume_max_tokens=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "宋连杰", "a", make_ev(), False))
    sm.note_chain(s, 14282, "378", transcript_bytes=1_200_000)
    assert sm.resume_candidate(s) == 42               # 1.2MB < 2MB，照旧 resume
    sm.note_chain(s, 29579, "384", transcript_bytes=12_657_332)
    assert sm.resume_candidate(s) is None
    assert "transcript" in sm.resume_break_reason(s)
    assert sm.resume_break_reason(s).startswith("budget:")   # 断链要注入摘要


def test_resume_transcript_bytes_assigns_not_accumulates(cfg, storage):
    """一条链共用同一个转写：记最新大小，不是逐轮累加（否则第二轮就虚高触顶）。"""
    cfg._p.setdefault("ingress", {}).update(resume_max_depth=0,
                                            resume_max_tokens=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "a", transcript_bytes=900_000)
    sm.note_chain(s, 100, "b", transcript_bytes=1_100_000)
    assert s["chain_bytes"] == 1_100_000              # 不是 2_000_000
    assert sm.resume_candidate(s) == 42


def test_resume_transcript_bytes_zero_keeps_previous(cfg, storage):
    """量不到（0）不该把已有判定擦掉——否则一次 glob 失手就等于关掉闸门。"""
    cfg._p.setdefault("ingress", {}).update(resume_max_depth=0,
                                            resume_max_tokens=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "a", transcript_bytes=9_000_000)
    sm.note_chain(s, 100, "b", transcript_bytes=0)
    assert s["chain_bytes"] == 9_000_000
    assert sm.resume_candidate(s) is None


def test_resume_transcript_bytes_zero_disables_cap(cfg, storage):
    """0/负值＝不设限，与 depth/tokens 两项同款运维口子。"""
    cfg._p.setdefault("ingress", {}).update(
        resume_max_depth=0, resume_max_tokens=0, resume_max_transcript_bytes=0)
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "a", transcript_bytes=999_000_000)
    assert sm.resume_candidate(s) == 42


def test_reset_chain_clears_bytes(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "旧摘要", transcript_bytes=9_000_000)
    sm.reset_chain(s)
    assert s["chain_bytes"] == 0 and s["chain_depth"] == 0
    assert s["chain_digest"] == "旧摘要"              # 摘要仍保留


def test_note_chain_tolerates_bad_transcript_bytes(cfg, storage):
    sm = SessionManager(cfg, storage)
    s = _live(sm, sm.get_or_create("o:a", "张三", "a", make_ev(), False))
    sm.note_chain(s, 100, "x", transcript_bytes=None)
    sm.note_chain(s, 100, "y", transcript_bytes="not-a-number")
    sm.note_chain(s, 100, "z", transcript_bytes=-5)
    assert s["chain_depth"] == 3
    assert int(s.get("chain_bytes") or 0) == 0
