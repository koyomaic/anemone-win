"""tests/test_plans.py —— 大脑 ```plan 标记 → relay-avatar 带凭据执行（创建/列表/删除/启停）。"""
import json
import time

import pytest

from app.main import Avatar


class FakeDws:
    def __init__(self, uids=None):
        self.uids = uids or {}
        self.sent = []
        self.reacts = []
        self.text_emotions = []

    def resolve_userid(self, open_id, sender, cache):
        return self.uids.get(open_id, "")

    def send_text(self, target, content):
        self.sent.append((target, content))
        return {"ok": True}

    def send_file(self, target, path):
        self.sent.append((target, f"[file]{path}"))
        return {"ok": True}

    def react(self, cid, mid, emoji):
        self.reacts.append(emoji)
        return {"ok": True}

    def react_text_emotion(self, cid, mid, te):
        self.text_emotions.append((mid, te))
        return {"ok": True}

    def remove_text_emotion(self, cid, mid, te):
        return {"ok": True}

    def card_create(self, target):
        return ""

    def card_update(self, biz, content, flow_status):
        return False

    def mark_read(self, cid, mid):
        return {"ok": True}

    def run(self, args, timeout=60):
        return {}


class PlanExec:
    name = "relay"

    def __init__(self):
        self.submitted = []
        self.tasks = {}
        self.plans = []
        self.created = []
        self.deleted = []
        self.patched = []

    def available(self):
        return True

    def submit(self, desc, resume_from=None, uid="", ingress="dingtalk",
                 requester=""):
        tid = len(self.submitted) + 1
        self.submitted.append({"tid": tid, "desc": desc, "uid": uid})
        self.tasks[tid] = {"id": tid, "status": "queued"}
        return {"task_id": tid, "status": "queued"}

    def poll(self, tid):
        return self.tasks.get(tid, {"_error": 404})

    def approve(self, tid, decision, uid=""):
        return {"ok": True}

    def confirm(self, tid, outcome, uid=""):
        return {"ok": True}

    def cancel(self, tid, uid=""):
        return {"ok": True}

    def create_plan(self, name, on_calendar, action, uid=""):
        self.created.append((name, on_calendar, action))
        if any(p["name"] == name for p in self.plans):
            return {"_error": 409, "_detail": "plan name exists"}
        pid = len(self.plans) + 1
        plan = {"id": pid, "name": name, "on_calendar": on_calendar,
                "action": action, "enabled": True,
                "next_run": time.time() + 3600, "last_run": None}
        self.plans.append(plan)
        return plan

    def list_plans(self, uid=""):
        return {"plans": list(self.plans)}

    def patch_plan(self, pid, fields, uid=""):
        self.patched.append((pid, fields))
        for p in self.plans:
            if p["id"] == pid:
                p.update(fields)
        return {"ok": True}

    def delete_plan(self, pid, uid=""):
        self.deleted.append(pid)
        self.plans = [p for p in self.plans if p["id"] != pid]
        return {"ok": True}


def ev(text, sid="sid200", sender="张三", n="1"):
    return {"event_type": "user_im_message_receive_o2o_all",
            "content": text, "sender": sender,
            "sender_open_dingtalk_id": sid, "message_id": f"m{n}",
            "conversation_id": f"c{n}", "event_id": f"e{n}"}


@pytest.fixture
def avatar(cfg):
    av = Avatar(cfg, dry=True)
    av.dws = FakeDws({"sid200": "200", "sid18434": "18434"})
    av.inbox.dws = av.dws
    av.exec_relay = PlanExec()
    return av


def texts(avatar):
    return [c for _, c in avatar.dws.sent]


def _run_with_result(avatar, user_msg, result):
    """走完整链路：用户消息→缓冲→提交→done(带 result)→轮询回流。返回会话。"""
    avatar.on_event(ev(user_msg, n="1"))
    s = avatar.sess.sessions["o:sid200"]
    avatar._flush(s, uid="200")
    tid = s["task_id"]
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "done", "result": result}
    avatar.dws.sent.clear()
    avatar._poll_sessions()
    return s


def test_create_marker_executed_and_stripped(avatar):
    result = ("好的，已安排每天 8 点跑经营日报。\n\n"
              "```plan\n"
              '{"op":"create","name":"经营日报","on_calendar":"*-*-* 08:00:00",'
              '"description":"生成昨日经营日报并发送给王洪彬","priority":"normal","read_only":false}\n'
              "```")
    _run_with_result(avatar, "帮我安排每天8点跑经营日报", result)
    # 执行：create_plan 被调用，参数正确
    assert len(avatar.exec_relay.created) == 1
    name, on_cal, action = avatar.exec_relay.created[0]
    assert name == "经营日报" and on_cal == "*-*-* 08:00:00"
    assert action["type"] == "task" and "经营日报" in action["description"]
    # 正文：标记块被剔除，权威确认被追加
    joined = "\n".join(texts(avatar))
    assert "```plan" not in joined and "on_calendar" not in joined
    assert "已安排每天 8 点" in joined          # 大脑原话保留
    assert "已创建定时计划「经营日报」" in joined  # 系统权威确认
    assert "下次" in joined


def test_list_marker(avatar):
    """标记路径：agent 在结果正文出 {"op":"list"} → 分身回传清单、标记块剔除。

    用户消息刻意**不含**计划/定时字样：2026-09-29 起「我有哪些计划」这类口语查询
    已被 planning._is_list_query 在进大脑前确定性拦截（见 test_planning.py），不再
    走到这里。本用例守的是 agent 主动出标记那条路。
    """
    avatar.exec_relay.plans.append({"id": 1, "name": "日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "帮我看看", '查一下～\n```plan\n{"op":"list"}\n```')
    joined = "\n".join(texts(avatar))
    assert "定时计划" in joined and "日报" in joined
    assert "```plan" not in joined


def test_colloquial_list_query_intercepted_before_brain(avatar):
    """口语查询「我有哪些计划」必须**在进大脑前**就地答复。

    这是本次优化的核心：#278 为「现在有几个定时任务？」烧掉 5.5 分钟 / 32 次 Bash
    去刨 relay 路由与凭据。确定性拦截后 0 次 LLM 往返、0 次 Bash。
    """
    avatar.exec_relay.plans.append({"id": 1, "name": "日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    avatar.dws.sent.clear()
    avatar.on_event(ev("我有哪些计划", n="7"))
    joined = "\n".join(texts(avatar))
    assert "定时计划" in joined and "日报" in joined
    assert not avatar.exec_relay.submitted, "查询句竟被提交成了任务（白烧一次大脑往返）"


def test_delete_marker_by_name(avatar):
    avatar.exec_relay.plans.append({"id": 7, "name": "晨会提醒", "on_calendar": "Mon..Fri *-*-* 09:30:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "删掉晨会提醒",
                     '好\n```plan\n{"op":"delete","name":"晨会"}\n```')
    assert avatar.exec_relay.deleted == [7]
    assert "已删除计划「晨会提醒」" in "\n".join(texts(avatar))


def test_disable_marker(avatar):
    avatar.exec_relay.plans.append({"id": 3, "name": "巡检", "on_calendar": "*-*-* *:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "停用巡检", '```plan\n{"op":"disable","name":"巡检"}\n```')
    assert avatar.exec_relay.patched == [(3, {"enabled": False})]
    assert "已停用计划「巡检」" in "\n".join(texts(avatar))


def test_name_dedupe(avatar):
    avatar.exec_relay.plans.append({"id": 1, "name": "经营日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    _run_with_result(avatar, "再来个经营日报",
                     '```plan\n{"op":"create","name":"经营日报","on_calendar":"*-*-* 09:00:00","description":"日报"}\n```')
    # 重名 → 自动加后缀 -2
    assert avatar.exec_relay.created[-1][0] == "经营日报-2"


def test_malformed_marker_passthrough(avatar):
    result = "正文\n```plan\n{not valid json}\n```"
    _run_with_result(avatar, "随便", result)
    assert not avatar.exec_relay.created
    joined = "\n".join(texts(avatar))
    assert "{not valid json}" in joined   # 解析失败原样保留，不吞


def test_no_marker_normal_flow(avatar):
    _run_with_result(avatar, "查个东西", "普通结果，没有计划")
    assert not avatar.exec_relay.created
    assert "普通结果" in "\n".join(texts(avatar))


def test_marker_executed_once_per_task(avatar):
    """review→done 二次回流同一 result：只执行一次（plan_applied 守卫）。"""
    avatar.on_event(ev("帮我安排每天8点跑日报", n="1"))
    s = avatar.sess.sessions["o:sid200"]
    avatar._flush(s, uid="200")
    tid = s["task_id"]
    result = ('```plan\n{"op":"create","name":"日报","on_calendar":"*-*-* 08:00:00","description":"日报"}\n```')
    # 第一次：review
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "review", "result": result}
    avatar._poll_sessions()
    assert len(avatar.exec_relay.created) == 1
    # 第二次：done（同一 result 再回流）
    avatar.exec_relay.tasks[tid] = {"id": tid, "status": "done", "result": result}
    avatar._poll_sessions()
    assert len(avatar.exec_relay.created) == 1   # 不重复创建


# ── 主路径：定时/计划 前缀在进大脑前就地拦截，绝不提交任务 ──

def test_direct_create_does_not_reach_brain(avatar):
    avatar.on_event(ev("定时 每天8点 跑经营日报", n="1"))
    # 建了计划
    assert len(avatar.exec_relay.created) == 1
    name, on_cal, action = avatar.exec_relay.created[0]
    assert name == "跑经营日报" and on_cal == "*-*-* 08:00:00"
    assert action["type"] == "task" and "经营日报" in action["description"]
    # 关键：没有提交任何大脑任务（不会被"当场执行"）
    assert avatar.exec_relay.submitted == []
    # 会话仍 IDLE（计划命令不进缓冲流）
    assert avatar.sess.sessions["o:sid200"]["state"] == "IDLE"
    joined = "\n".join(texts(avatar))
    assert "已创建定时计划「跑经营日报」" in joined and "下次" in joined


def test_broadened_create_intercepts_before_brain(avatar):
    """放宽触发：无前缀但句首节奏词+完整钟点 → 进大脑前拦截建计划，绝不提交任务。"""
    avatar.on_event(ev("每天8点跑经营日报", n="6"))
    assert len(avatar.exec_relay.created) == 1
    name, on_cal, action = avatar.exec_relay.created[0]
    assert name == "跑经营日报" and on_cal == "*-*-* 08:00:00"
    assert action["type"] == "task" and "经营日报" in action["description"]
    assert avatar.exec_relay.submitted == []
    assert avatar.sess.sessions["o:sid200"]["state"] == "IDLE"
    assert "已创建定时计划「跑经营日报」" in "\n".join(texts(avatar))


def test_direct_list(avatar):
    avatar.exec_relay.plans.append({"id": 1, "name": "日报", "on_calendar": "*-*-* 08:00:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    avatar.on_event(ev("计划列表", n="2"))
    assert avatar.exec_relay.submitted == []
    joined = "\n".join(texts(avatar))
    assert "定时计划" in joined and "日报" in joined


def test_direct_delete(avatar):
    avatar.exec_relay.plans.append({"id": 9, "name": "晨会提醒", "on_calendar": "Mon..Fri *-*-* 09:30:00",
                                    "enabled": True, "next_run": time.time() + 3600})
    avatar.on_event(ev("计划删除 晨会", n="3"))
    assert avatar.exec_relay.deleted == [9]
    assert avatar.exec_relay.submitted == []
    assert "已删除计划「晨会提醒」" in "\n".join(texts(avatar))


def test_direct_create_fail_guides(avatar):
    avatar.on_event(ev("定时 每天 跑日报", n="4"))   # 缺时间 → create_fail
    assert not avatar.exec_relay.created
    assert avatar.exec_relay.submitted == []
    assert "没太看懂" in "\n".join(texts(avatar))


def test_normal_task_still_reaches_brain(avatar):
    """非计划消息照常走会话流（解析器不误拦）。"""
    avatar.on_event(ev("统计每天的销量", n="5"))
    avatar._flush(avatar.sess.sessions["o:sid200"], uid="200")
    assert len(avatar.exec_relay.submitted) == 1
    assert not avatar.exec_relay.created


def test_plan_permission_gate(avatar):
    """_handle_plan 纵深防御：非 super/L1/L2 拒绝（正常路由里 L3/NONE 到不了 IDLE）。"""
    s = {"reply": {}, "uid": "999"}
    avatar._handle_plan({"cmd": "create", "name": "x", "on_calendar": "*-*-* 08:00:00",
                         "description": "y"}, s, uid="999")
    assert not avatar.exec_relay.created
    assert "权限" in "\n".join(texts(avatar))


def test_exec_relay_plan_audit_real(cfg):
    """回归：真实 ExecRelay + 真实 Audit（此前用假 exec 漏掉了 audit.log 非法 kwarg 崩溃）。"""
    from app.audit import Audit
    from app.exec_relay import ExecRelay
    from app.storage import Storage

    class StubRelay:
        configured = True

        def create_plan(self, n, c, a):
            return {"id": 1, "name": n, "on_calendar": c, "enabled": True, "next_run": 0}

        def list_plans(self):
            return {"plans": []}

        def patch_plan(self, pid, f):
            return {"ok": True}

        def delete_plan(self, pid):
            return {"ok": True}

    ex = ExecRelay(cfg, StubRelay(), Storage(cfg), Audit(cfg.state))
    ex.create_plan("x", "*-*-* 08:00:00", {"type": "task", "description": "d"}, uid="18434")
    ex.patch_plan(1, {"enabled": False}, uid="18434")
    ex.delete_plan(1, uid="18434")
    lines = (cfg.state / "audit" / "audit.log").read_text("utf-8")
    assert "plan_create" in lines and "plan_update" in lines and "plan_delete" in lines


# ── 标记路径的权限门禁（2026-09-25 补）──
# 此前只有 _handle_plan（用户直接下计划命令）有门禁，而 ```plan``` 标记路径
# （_consume_plan_markers → _exec_plan_op → _plan_create_note）**完全不看 uid**，
# 只校验 on_calendar/description 非空与重名。ecc3650 又把触发条件放宽到「无前缀但
# 句首节奏词+完整钟点」，于是任何能让 agent 吐出标记的输入都能建每日自动执行的
# 任务——计划的 description 会成为后续每天的任务提示词，是持久化注入的落点。

def _spec(name="x", desc="y"):
    return {"op": "create", "name": name, "on_calendar": "*-*-* 08:00:00",
            "description": desc}


def test_marker_plan_gate_denies_l3(avatar):
    """L3（文件投递用户，任何能发文件的人都落这档）不得经标记建计划。"""
    note = avatar._exec_plan_op(_spec(), "300")
    assert not avatar.exec_relay.created
    assert "权限" in note


def test_marker_plan_gate_denies_guest_and_blocked(avatar):
    for uid in ("555", "999"):                 # NONE / BLOCKED
        avatar.exec_relay.created.clear()
        note = avatar._exec_plan_op(_spec(), uid)
        assert not avatar.exec_relay.created, uid
        assert "权限" in note, uid


def test_marker_plan_gate_allows_team_tiers(avatar):
    """super/L1/L2 照常放行——不能把既有能力一起关掉（现有 3 个每日计划即 L2 所建）。"""
    for uid in ("18434", "100", "200"):        # super / L1 / L2
        avatar.exec_relay.created.clear()
        avatar.exec_relay.plans.clear()
        note = avatar._exec_plan_op(_spec(name=f"p{uid}"), uid)
        assert avatar.exec_relay.created, f"{uid} 应放行"
        assert "已创建定时计划" in note


def test_marker_plan_gate_covers_mutating_ops_only(avatar):
    """delete/disable/enable 同样受门禁；list 只读且按 uid 限定，不设门禁。"""
    for op in ("delete", "disable", "enable"):
        note = avatar._exec_plan_op({"op": op, "name": "x"}, "300")
        assert "权限" in note, op
    assert avatar._exec_plan_op({"op": "list"}, "300") is not None


def test_plan_create_capped(avatar, cfg):
    """数量上限：每个计划到点都用分身凭据起真实任务，无上限即无限刷配额。"""
    cfg._p.setdefault("security", {}).update(plan_max_per_user=2)
    avatar.exec_relay.plans.extend([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    note = avatar._exec_plan_op(_spec(name="c"), "200")
    assert not avatar.exec_relay.created
    assert "上限" in note


def test_plan_ops_are_audited(avatar, cfg):
    """plan 操作此前**完全无审计**：建了/删了谁都不知道，出问题无从追溯。"""
    avatar._exec_plan_op(_spec(name="audited"), "200")
    avatar._exec_plan_op(_spec(), "300")                 # 被拒的也要留痕
    lines = (cfg.state / "audit" / "audit.log").read_text("utf-8")
    assert "plan_op" in lines and "audited" in lines
    assert "plan_denied" in lines


# ── 占位符防护（2026-09-28 补，2026-09-29 收窄判据）──
#
# _relay_marker_rules 与 workspace/AGENTS.md 曾含字面示例
# {"name": "<计划名>", "description": "<到点要执行的任务描述>"}。agent 若把示例
# 原样回显进结果，消费侧会照单建出一个每天到点跑占位符描述的垃圾计划——而它
# **真会起任务、真会烧配额**。
#
# 但原判据 `<[^<>\n]{0,40}>`（含任意尖括号即拒）**误杀合法模板变量**：拿生产在跑的
# 4 条计划描述回测，`<YYYY-MM>`／`<YYYYMMDD>`／`<当日期次>` 全部命中 → 4 条计划
# 今天重建会被全部拒建；#288（毕倩 09-28 17:47 的 D-17 防 missing 计划）两个合法
# create 已因此被误杀。故判据收窄为「整段就是占位符」或「命中已知示例词」，
# 文档示例本身也改成实义值（见 test_marker_rules_examples_use_real_values）。


def test_plan_create_rejects_placeholder_description(avatar, cfg):
    note = avatar._exec_plan_op(
        _spec(name="计划名", desc="<到点要执行的任务描述>"), "200")
    assert not avatar.exec_relay.created
    assert "占位符" in note and "未创建" in note


def test_plan_create_rejects_placeholder_name(avatar, cfg):
    note = avatar._exec_plan_op(
        _spec(name="<计划名>", desc="生成昨日经营日报"), "200")
    assert not avatar.exec_relay.created
    assert "占位符" in note


def test_plan_create_placeholder_rejection_is_audited(avatar, cfg):
    avatar._exec_plan_op(_spec(desc="<到点要执行的任务描述>"), "200")
    lines = (cfg.state / "audit" / "audit.log").read_text("utf-8")
    assert "plan_denied" in lines and "占位符" in lines


def test_plan_create_real_values_still_pass(avatar, cfg):
    """不能误伤：实义描述照建。含尖括号的合法业务文本（如 <html>）也不该被拦，
    但占位符形态 `<中文短词>` 必须拦——故这里只验正常路径不受影响。"""
    note = avatar._exec_plan_op(
        _spec(name="每日成品油预测", desc="调 B0 智能体出成品油价格预测报告，落盘 data/Daily"),
        "200")
    assert len(avatar.exec_relay.created) == 1
    assert "已创建定时计划「每日成品油预测」" in note


def test_marker_rules_examples_use_real_values(avatar):
    """自证一致：_relay_marker_rules 里给 agent 照抄的 create 示例必须**已填实义值**。

    2026-09-29 反转了原断言。原来示例写的是 {"name": "<计划名>", "description":
    "<到点要执行的任务描述>"}，靠占位符防护兜底；但那条防护的正则
    `<[^<>\\n]{0,40}>` 同时误杀了合法模板变量（见下方生产计划回归用例）。既然防护
    收窄了，文档示例就不该再依赖它——直接给实义值，agent 照抄也不会造出垃圾计划。
    """
    import json as _json
    from app.main import PLAN_BLOCK_RE, _PLACEHOLDER_WHOLE_RE, _PLACEHOLDER_KNOWN_RE
    s = {"reply": {"is_group": True, "conversation_id": "cg", "sender": "赵兴峰"}}
    seen = 0
    for blk in PLAN_BLOCK_RE.findall(avatar._relay_marker_rules(s)):
        sp = _json.loads(blk)
        if sp.get("op") != "create":
            continue
        seen += 1
        nm, ds = str(sp.get("name") or ""), str(sp.get("description") or "")
        assert nm and ds, "示例字段为空"
        for v in (nm, ds):
            assert not _PLACEHOLDER_WHOLE_RE.match(v), f"示例整段仍是占位符：{v!r}"
            assert not _PLACEHOLDER_KNOWN_RE.search(v), f"示例含已知占位符词：{v!r}"
    assert seen >= 1, "文案里没有 create 示例，agent 无从照抄"



# ── 占位符判据收窄的回归（2026-09-29）──────────────────────────────────
#
# 原判据「含任意尖括号即拒」误杀合法模板变量。下面用**生产在跑的 4 条计划的真实
# 描述**做「必须放行」用例——这条测试缺失正是当初漏掉回归的原因。

_PROD_PLAN_DESCS = [
    ("每日原油价格预测调取",
     "由 CA0 舒小瀚智能体编排执行：经 crude-ai-research-institute 技能（G-30，"
     "R-GV-16 唯一入口）调取 AI 原油研究院当日原油价格预测报告（D-16 预测价）；"
     "输出 HTML 单文件自包含报告，落 shared/report/市场分析/<YYYY-MM>/，命名 "
     "<YYYYMMDD>_T0_原油价格预测_v1.html；落盘后 store add 登记。"),
    ("每日成品油价格预测调取",
     "经 b0 只读通道取 D-17 预测，落 shared/report/市场分析/<YYYY-MM>/，命名 "
     "<YYYYMMDD>_T1_成品油价格预测_v1.html。"),
    ("每日市场行情研究报告",
     "生成当日市场行情研究报告（第 <当日期次> 期），落 shared/report/市场分析/"
     "<YYYY-MM>/<YYYYMMDD>_市场行情研究报告.md。"),
    ("b0-refined-forecast-0735",
     "运行 b0 产品端价格预测智能体：落盘 data/Daily/product-price-forcasting/"
     "（命名 D-17-成品油价格预测表-<YYYYMMDD>.json / D-17-成品油价格预测数据报告-"
     "<YYYYMMDD>.md），data_status 三态逐条标注。"),
]


@pytest.mark.parametrize("name,desc", _PROD_PLAN_DESCS)
def test_plan_create_allows_production_template_vars(avatar, cfg, name, desc):
    """生产在跑的 4 条计划描述必须能重建。原正则会把它们全部拒建。"""
    note = avatar._exec_plan_op(_spec(name=name, desc=desc), "200")
    assert len(avatar.exec_relay.created) == 1, f"被误杀：{note}"
    assert "已创建定时计划" in note


def test_plan_create_allows_the_two_wrongly_denied_from_288(avatar, cfg):
    """#288（毕倩 09-28 17:47）被误杀的两个 D-17 计划，判据收窄后必须放行。"""
    for nm, ds in [
        ("b0-refined-forecast-0700",
         "工作日 07:00 只读取当日 D-17 落 shared/data/Daily/product-price-forcasting/，"
         "DT-B0-01 json ＋ DT-B0-02 md，data_status 三态逐条标注。"),
        ("d17-readiness-gate",
         "07:15 校验 as-of=当日，缺失则按 Z-AA-09 出《数据未就绪单》催 P-01，"
         "不顶替不估算。"),
    ]:
        note = avatar._exec_plan_op(_spec(name=nm, desc=ds), "200")
        assert "已创建定时计划" in note, f"{nm} 仍被误杀：{note}"
    assert len(avatar.exec_relay.created) == 2


@pytest.mark.parametrize("name,desc", [
    ("<计划名>", "<到点要执行的任务描述>"),   # 整段回显文档示例
    ("<计划名>", "每天8点跑日报"),             # 仅 name 是占位符
    ("我的计划", "<到点要执行的任务描述>"),      # 仅 desc 是占位符
    ("<名称>", "<描述>"),
    ("x", "<...>"),
    ("x", "<…>"),
    ("", "<计划名>"),
    ("每日报告", "生成 <任务描述> 并落盘"),      # 已知示例词嵌在实义文本里也拦
])
def test_plan_create_still_rejects_true_placeholder_echo(avatar, cfg, name, desc):
    """收窄不等于放开：真回显文档示例仍必须拒建（它会真起任务、真烧配额）。"""
    note = avatar._exec_plan_op(_spec(name=name, desc=desc), "200")
    assert not avatar.exec_relay.created
    assert "占位符" in note and "未创建" in note


# ── 归属止血：受保护计划 + 破坏性操作须精确命中完整名（2026-09-29）────────
#
# relay 侧 `_plan_owner_ok` 比 principal["name"] == plan["owner_name"]，而经分身建的
# 计划 owner_name **全是分身 token 主体名（鲁大师）**，该检查形同虚设；`_plan_gate`
# 又只校验 tier 不校验归属。于是任何 L2 说一句「删掉计划 原油」就会唯一子串命中
# 「每日原油价格预测调取」并直接删掉，07:50 的日报从此静默消失。
# 用户已定**不动 relay schema**（不加 owner_uid），故在分身侧用名单 + 精确名止血。

_PROT = ["每日原油价格预测调取", "每日成品油价格预测调取", "每日市场行情研究报告"]


def _seed_protected(avatar, cfg):
    cfg._p.setdefault("security", {}).update(plan_protected=list(_PROT))
    avatar.exec_relay.plans.extend(
        [{"id": i + 1, "name": n, "on_calendar": "*-*-* 07:50:00",
          "enabled": True, "next_run": time.time() + 3600}
         for i, n in enumerate(_PROT)])


def test_protected_plan_delete_denied_for_l2(avatar, cfg):
    """L2（whitelist=200）不得删核心业务计划。"""
    _seed_protected(avatar, cfg)
    note = avatar._exec_plan_op(
        {"op": "delete", "name": "每日原油价格预测调取"}, "200")
    assert not avatar.exec_relay.deleted
    assert "受保护" in note and "超管" in note


def test_protected_plan_disable_denied_for_l1(avatar, cfg):
    """L1（admins=100）同样不得停用——停用等于让日报静默消失。"""
    _seed_protected(avatar, cfg)
    note = avatar._exec_plan_op(
        {"op": "disable", "name": "每日市场行情研究报告"}, "100")
    assert not avatar.exec_relay.patched
    assert "受保护" in note


def test_protected_plan_denial_is_audited(avatar, cfg):
    _seed_protected(avatar, cfg)
    avatar._exec_plan_op({"op": "delete", "name": "每日原油价格预测调取"}, "200")
    lines = (cfg.state / "audit" / "audit.log").read_text("utf-8")
    assert "plan_denied" in lines and "受保护计划" in lines


def test_protected_plan_superadmin_can_delete(avatar, cfg):
    """超管（18434）不受名单限制——否则名单变成谁都动不了的死配置。"""
    _seed_protected(avatar, cfg)
    note = avatar._exec_plan_op(
        {"op": "delete", "name": "每日原油价格预测调取"}, "18434")
    assert avatar.exec_relay.deleted == [1]
    assert "已删除" in note


def test_protected_plan_enable_allowed_for_l2(avatar, cfg):
    """启用是无害的（恢复日报），不该被名单挡住。"""
    _seed_protected(avatar, cfg)
    avatar.exec_relay.plans[0]["enabled"] = False
    note = avatar._exec_plan_op(
        {"op": "enable", "name": "每日原油价格预测调取"}, "200")
    assert avatar.exec_relay.patched and "已启用" in note


def test_unprotected_plan_still_deletable_by_l2(avatar, cfg):
    """名单外的计划（用户自建的）照旧可删，不能把止血做成全面锁死。"""
    _seed_protected(avatar, cfg)
    avatar.exec_relay.plans.append(
        {"id": 99, "name": "我的临时提醒", "on_calendar": "*-*-* 09:00:00",
         "enabled": True, "next_run": time.time() + 3600})
    note = avatar._exec_plan_op({"op": "delete", "name": "我的临时提醒"}, "200")
    assert 99 in avatar.exec_relay.deleted and "已删除" in note


@pytest.mark.parametrize("op", ["delete", "disable"])
def test_destructive_op_on_protected_requires_exact_name(avatar, cfg, op):
    """受保护计划的破坏性操作必须精确命中完整名——**超管也不例外**。此前子串
    fallback 对删改同样生效：「删掉计划 原油」会唯一子串命中核心业务计划并直接执行。"""
    _seed_protected(avatar, cfg)
    note = avatar._exec_plan_op({"op": op, "name": "原油"}, "18434")   # 超管
    assert not avatar.exec_relay.deleted and not avatar.exec_relay.patched
    assert "不是完整计划名" in note and "每日原油价格预测调取" in note


def test_destructive_op_on_protected_exact_name_by_super(avatar, cfg):
    """超管给全名 → 放行（否则名单变成谁都动不了的死配置）。"""
    _seed_protected(avatar, cfg)
    note = avatar._exec_plan_op(
        {"op": "delete", "name": "每日原油价格预测调取"}, "18434")
    assert avatar.exec_relay.deleted == [1] and "已删除" in note


@pytest.mark.parametrize("op", ["delete", "disable"])
def test_unprotected_plan_tolerates_substring(avatar, cfg, op):
    """普通（非受保护）计划保留子串便利：口语「删掉晨会」→「晨会提醒」。
    精确名要求只加在受保护计划上，否则日常删改体验被无谓拖慢。"""
    avatar.exec_relay.plans.append(
        {"id": 7, "name": "晨会提醒", "on_calendar": "Mon..Fri *-*-* 09:30:00",
         "enabled": True, "next_run": time.time() + 3600})
    note = avatar._exec_plan_op({"op": op, "name": "晨会"}, "200")
    assert "不是完整计划名" not in note
    if op == "delete":
        assert avatar.exec_relay.deleted == [7]
    else:
        assert avatar.exec_relay.patched


def test_enable_tolerates_substring(avatar, cfg):
    """启用无害，保留子串便利（口语「启用 原油」）。"""
    _seed_protected(avatar, cfg)
    avatar.exec_relay.plans[0]["enabled"] = False
    note = avatar._exec_plan_op({"op": "enable", "name": "原油"}, "200")
    assert avatar.exec_relay.patched and "已启用" in note


def test_list_note_marks_protected(avatar, cfg):
    """清单要能看出哪些是核心业务计划（🔒），否则用户不知道动不得。"""
    _seed_protected(avatar, cfg)
    note = avatar._plan_list_note("200")
    assert "🔒" in note and "仅超管可删改" in note
    body = [ln for ln in note.splitlines() if ln.startswith("·")]
    assert len(body) == len(_PROT)
    assert sum("🔒" in ln for ln in body) == len(_PROT)


def test_group_plan_denial_goes_to_dm_not_group(avatar, cfg):
    """群聊里的权限拒绝**走私聊**：文案含「当前身份 `L3` 无权限」，发群里等于
    当众宣布该成员没权限。"""
    s = {"reply": {"is_group": True, "conversation_id": "cg", "sender": "外人",
                   "open_dingtalk_id": "sid300"},
         "uid": "300", "state": "IDLE"}
    avatar.dws.sent.clear()
    avatar._handle_plan({"cmd": "create", "name": "x",
                         "on_calendar": "*-*-* 08:00:00",
                         "description": "跑日报"}, s, "300")
    assert not avatar.exec_relay.created
    assert len(avatar.dws.sent) == 1
    target, content = avatar.dws.sent[0]
    assert target.get("user_id") == "300", "拒绝未走私聊"
    assert "conversation_id" not in target, "拒绝发进了群"
    assert "权限" in content


def test_group_plan_success_goes_to_group(avatar, cfg):
    """成功回执仍发群：全群可见＝天然审计，他人可当场异议。"""
    s = {"reply": {"is_group": True, "conversation_id": "cg", "sender": "张三",
                   "open_dingtalk_id": "sid200"},
         "uid": "200", "state": "IDLE"}
    avatar.dws.sent.clear()
    avatar._handle_plan({"cmd": "create", "name": "群里的日报",
                         "on_calendar": "*-*-* 08:00:00",
                         "description": "生成当日经营日报并落盘 shared/report/"},
                        s, "200")
    assert len(avatar.exec_relay.created) == 1
    target, content = avatar.dws.sent[0]
    assert target.get("conversation_id") == "cg", "成功回执没发回群"
    assert "已创建定时计划" in content


# ── 身份回落与定级口径统一（2026-09-29）──
#
# 起因：赵兴峰按 **openId** 入白名单，而他的 userId 三条途径都拿不到（实测
# contact +search-user 只回 openDingTalkId、aisearch person 返回空、
# chat +chat-members-get 只回 nick+openId——外部/跨组织联系人），且
# resolve_userid 每 6h 负缓存过期后重试、永远同样失败。uid 恒为空造成三处静默故障：
#   ① _plan_gate 用 get_level("")→NONE，他明明在白名单里却建不了计划，拒绝文案
#      还叫他"找超管加白名单"（他已在），两边都查不出原因
#   ② rate_hit 按 str(uid) 计桶 → 所有解析失败的人**共用一个配额桶**
#      （实测生产 rate.json 里 '' 桶当天已计 4 次）
#   ③ 审计 actor="" 无归属
#
# 修法：uid 解析不出时**回落用 openId**——它是钉钉真实、稳定、全局唯一的标识符，
# 不是凭空造的；白名单本就认 openId（_static_level）；真实 userId 是纯数字串、
# openId 是 34 位 base64 串，**永不撞号**。并把所有定级统一到 level_of。
# 发送侧的归位见 test_main_routing.py 的 _norm_target 组。

ZHAO_SID = "DJTDWXe5Omkca6fEkz0vxWVZZG4c4SxTc"   # 赵兴峰真实 openId


def _zhao(avatar):
    """把赵兴峰的 openId 加进白名单（模拟生产 policy.toml 的登记方式）。"""
    avatar.policy.whitelist.add(ZHAO_SID)
    return avatar


def test_uid_falls_back_to_openid(avatar):
    """通讯录反查不到 userId → uid 回落为 openId（不再是空串）。"""
    _zhao(avatar)
    assert avatar.dws.resolve_userid(ZHAO_SID, "赵兴峰", avatar.contacts) == ""
    avatar.on_event(ev("你好", sid=ZHAO_SID, sender="赵兴峰", n="70"))
    s = avatar.sess.sessions[f"o:{ZHAO_SID}"]
    assert s["uid"] == ZHAO_SID, "uid 未回落到 openId"


def test_openid_whitelisted_user_passes_plan_gate(avatar):
    """核心修复：按 openId 入白名单的人能过计划门禁。

    两种入参都要放行：uid 为空但 sid 命中名单（回落前的形态），以及 uid 已是
    openId（回落后的形态）——后者说明回落与 level_of 两条修法各自独立生效。
    """
    _zhao(avatar)
    assert avatar.policy.get_level("") == "NONE"            # 旧口径：必拒
    assert avatar.policy.level_of("", ZHAO_SID, "赵兴峰") == "L2"
    assert avatar._plan_gate("", "create", ZHAO_SID, "赵兴峰") is None
    assert avatar._plan_gate(ZHAO_SID, "create") is None


def test_openid_user_end_to_end_plan_create(avatar):
    """端到端：他在单聊里说「定时 每天8点 跑经营日报」→ 计划真建出来。"""
    _zhao(avatar)
    avatar.dws.sent.clear()
    avatar.on_event(ev("定时 每天8点 跑经营日报", sid=ZHAO_SID,
                       sender="赵兴峰", n="71"))
    assert len(avatar.exec_relay.created) == 1, "计划未创建"
    _, on_cal, _ = avatar.exec_relay.created[0]
    assert on_cal == "*-*-* 08:00:00"
    assert any("已创建定时计划" in c for _, c in avatar.dws.sent)


def test_openid_user_marker_path_also_works(avatar):
    """标记路径（agent 出 ```plan``` 块）同样要认 openId 身份。"""
    _zhao(avatar)
    s = {"reply": {"is_group": True, "conversation_id": "cg",
                   "sender": "赵兴峰", "open_dingtalk_id": ZHAO_SID},
         "uid": ZHAO_SID, "state": "IDLE"}
    note = avatar._exec_plan_op(
        {"op": "create", "name": "群里的日报", "on_calendar": "*-*-* 09:00:00",
         "description": "生成当日经营日报并落盘 shared/report/"},
        ZHAO_SID, ZHAO_SID, "赵兴峰")
    assert len(avatar.exec_relay.created) == 1, note
    assert "已创建定时计划" in note


def test_openid_user_rate_limit_own_bucket(avatar, cfg):
    """限流独立计桶：此前所有 uid 解析失败的人共用 '' 一个桶。

    rate_hit 在 _flush 里触发（不在 on_event），且 `uid = uid or s.get("uid")`，
    故不显式传 uid 时会话里回落后的 openId 会自然生效。
    """
    _zhao(avatar)
    avatar.on_event(ev("跑个任务", sid=ZHAO_SID, sender="赵兴峰", n="72"))
    s = avatar.sess.sessions[f"o:{ZHAO_SID}"]
    assert s["uid"] == ZHAO_SID
    avatar._flush(s)
    d = json.loads((cfg.state / "run" / "rate.json").read_text("utf-8"))
    counts = d.get("counts") or {}
    assert ZHAO_SID in counts, f"未按 openId 独立计桶：{counts}"
    assert "" not in counts, f"仍有空 uid 桶：{counts}"


def test_openid_user_plan_ops_are_attributed(avatar, cfg):
    """审计要能认出是谁：actor 记 openId，detail 带显示名（光看 base64 认不出人）。"""
    _zhao(avatar)
    avatar._exec_plan_op({"op": "create", "name": "赵的日报",
                          "on_calendar": "*-*-* 08:00:00",
                          "description": "生成当日经营日报并落盘 shared/report/"},
                         ZHAO_SID, ZHAO_SID, "赵兴峰")
    lines = (cfg.state / "audit" / "audit.log").read_text("utf-8")
    assert ZHAO_SID in lines, "审计未记 openId"
    assert "赵兴峰" in lines, "审计未带显示名，无法人对账"


def test_openid_superadmin_can_touch_protected_plan(avatar, cfg):
    """按 openId 入 superadmin 名单的人也要被认出来。

    受保护计划判定此前用 is_superadmin(uid)，uid 为空即漏判成非超管；改用
    level_of(uid, sid, sender) 后与 _plan_gate 同源。
    """
    avatar.policy.superadmin.add(ZHAO_SID)
    assert avatar.policy.level_of("", ZHAO_SID, "赵兴峰") == "super"
    cfg._p.setdefault("security", {})["plan_protected"] = ["每日原油价格预测调取"]
    avatar.exec_relay.plans.append(
        {"id": 1, "name": "每日原油价格预测调取", "on_calendar": "*-*-* 07:50:00",
         "enabled": True, "next_run": time.time() + 3600})
    note = avatar._plan_by_name_note("delete", "每日原油价格预测调取",
                                     "", ZHAO_SID, "赵兴峰")
    assert avatar.exec_relay.deleted == [1], note


def test_guest_still_denied_after_fallback(avatar):
    """回落不得放宽边界：名单外的人 uid 变成 openId 后仍是 NONE，照样拒。"""
    stranger = "DxxxxSTRANGERxxxx"
    assert avatar.policy.level_of(stranger, stranger, "路人") == "NONE"
    assert avatar._plan_gate(stranger, "create", stranger, "路人") is not None
    avatar.on_event(ev("定时 每天8点 跑日报", sid=stranger, sender="路人", n="73"))
    assert not avatar.exec_relay.created


def test_blocked_openid_still_blocked_after_fallback(avatar):
    """拉黑优先级最高：openId 在黑名单里，回落后不得因为形态变化而漏判。"""
    bad = "DblockedOpenId0000000000000000000"
    avatar.policy.blacklist.add(bad)
    assert avatar.policy.level_of(bad, bad, "坏人") == "BLOCKED"
    assert avatar._plan_gate(bad, "create", bad, "坏人") is not None
