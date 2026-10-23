"""tests/test_planning.py —— 确定性中文定时解析器。"""
import pytest

from app import planning as P


def test_cn2int():
    assert P.cn2int("8") == 8
    assert P.cn2int("十") == 10
    assert P.cn2int("十五") == 15
    assert P.cn2int("二十") == 20
    assert P.cn2int("二十五") == 25
    assert P.cn2int("九") == 9
    assert P.cn2int("半") is None
    assert P.cn2int("") is None


@pytest.mark.parametrize("text,on_cal,desc", [
    ("每天8点 跑经营日报", "*-*-* 08:00:00", "跑经营日报"),
    ("每天早上8点 给我发经营日报", "*-*-* 08:00:00", "发经营日报"),
    ("每周一9:30 提醒我开会", "Mon *-*-* 09:30:00", "提醒我开会"),
    ("每周三下午3点半 汇报进度", "Wed *-*-* 15:30:00", "汇报进度"),
    ("每月1号9点 生成月报", "*-*-01 09:00:00", "生成月报"),
    ("工作日9点 检查服务", "Mon..Fri *-*-* 09:00:00", "检查服务"),
    ("每天晚上10点 备份", "*-*-* 22:00:00", "备份"),
    ("每天中午12点 吃饭", "*-*-* 12:00:00", "吃饭"),
    ("每隔30分钟 拉一次数据", "*:0/30:00", "拉一次数据"),
    ("每2小时 巡检", "*-*-* 0/2:00:00", "巡检"),
    ("每小时 看一眼队列", "*-*-* *:00:00", "看一眼队列"),
])
def test_parse_schedule_ok(text, on_cal, desc):
    r = P.parse_schedule(text)
    assert r and not r.get("error")
    assert r["on_calendar"] == on_cal
    assert desc in r["description"]


def test_parse_schedule_too_frequent():
    assert P.parse_schedule("每隔2分钟 刷") == {"error": "too_frequent"}


def test_parse_schedule_no_time():
    assert P.parse_schedule("每天 跑日报") is None      # daily 缺时间
    assert P.parse_schedule("随便说点什么") is None      # 无频率


@pytest.mark.parametrize("text,cmd", [
    ("计划列表", "list"),
    ("我的计划", "list"),
    ("计划 列表", "list"),
    ("计划删除 跑经营日报", "delete"),
    ("删除计划 日报", "delete"),
    ("计划停用 巡检", "disable"),
    ("计划启用 巡检", "enable"),
    ("定时 每天8点 跑日报", "create"),
    # 放宽触发：无前缀，句首节奏词 + 完整可解析
    ("每天8点跑经营日报", "create"),
    ("每周一9:30提醒我开会", "create"),
    ("每隔30分钟检查库存", "create"),
    ("工作日9点汇总数据", "create"),
    ("帮我每天8点跑日报", "create"),        # 礼貌前导剥离
    ("提醒我每天8点喝水", "create"),         # 提醒我作前导
])
def test_parse_plan_cmd(text, cmd):
    r = P.parse_plan_cmd(text)
    assert r and r["cmd"] == cmd


def test_parse_plan_cmd_broadened_fields():
    r = P.parse_plan_cmd("每天8点跑经营日报")
    assert r["on_calendar"] == "*-*-* 08:00:00"
    assert r["description"] == "跑经营日报"
    assert r["name"] == "跑经营日报"
    r2 = P.parse_plan_cmd("每周一9:30提醒我开会")
    assert r2["on_calendar"] == "Mon *-*-* 09:30:00"


def test_parse_plan_cmd_create_fail():
    assert P.parse_plan_cmd("定时 每天 跑日报")["cmd"] == "create_fail"
    assert P.parse_plan_cmd("定时 每隔2分钟 刷")["cmd"] == "create_fail"


@pytest.mark.parametrize("text", [
    "这个计划不错",          # 不以 定时/计划 触发创建（rest 无定时提示）
    "计划经济学的基本概念",
    "请帮我制定一个计划",
    "统计每天的销量",          # 普通一次性任务，绝不拦
    "查一下今天的日程",
    "你好",
    # 放宽触发后仍须放行的"像定时却非排期命令"
    "分析每天8点的成交量",      # 动词开头，每天在句中 → 不拦
    "每天跑日报",              # daily 缺钟点 → 不拦
    "看看每周的数据",          # 无具体星期/时间 → 不拦
    "周一9点开会",             # 无"每"前缀，视为一次性 → 不拦
    "每天8点",                 # 只有节奏无动作 → 不拦
])
def test_parse_plan_cmd_none(text):
    assert P.parse_plan_cmd(text) is None


def test_make_name_strips_illegal():
    assert P.make_name("跑经营日报！@#") == "跑经营日报"
    assert P.make_name("") == "plan"


# ── 查询（list）触发激进扩充（2026-09-29）──
#
# 为什么激进：list 是**只读、零副作用**操作，误判最多多回一句计划清单；而漏判会让
# 「现在有几个定时任务」落进大脑——#278 为此烧掉 5.5 分钟 / 32 次 Bash 去刨 relay
# 路由、源码与凭据。原先只认 4 个 fullmatch 句式，口语问法全漏。
# create 路径的判据（节奏词 + 完整钟点）保持严格不动，且**优先于** list 判定。

@pytest.mark.parametrize("text", [
    # 原有 4 句式（不得回归）
    "计划列表", "我的计划", "计划清单", "全部计划",
    # 口语查询：调度名词 × 枚举疑问
    "有哪些定时任务", "定时任务有几个", "现在有什么定时任务", "查一下定时任务",
    "定时任务列表", "定时任务有哪些", "有没有定时任务", "定时计划有多少个",
    "现有的定时计划", "盘点一下定时任务", "查看调度", "排期有哪些", "cron 列表",
    # 裸「计划」短句（严格枚举词）
    "所有计划", "计划有哪些", "有多少个计划", "列出全部计划",
])
def test_parse_plan_cmd_list_queries(text):
    assert (P.parse_plan_cmd(text) or {}).get("cmd") == "list", text


@pytest.mark.parametrize("text", [
    "这个计划有什么问题",        # 裸「计划」+「有什么」→ 严格枚举词刻意排除，不得劫持
    "帮我看看这份计划书",
    "写一份营销计划",
    "分析一下定时任务的执行日志为什么失败",   # 长句 + 无枚举词
    "查一下今天的日程",          # 日程≠计划
    "统计每天的销量",
    "把报告发我",
])
def test_parse_plan_cmd_list_does_not_hijack_business_text(text):
    assert (P.parse_plan_cmd(text) or {}).get("cmd") != "list", text


@pytest.mark.parametrize("text", [
    "定时 每天8点 跑经营日报",
    "每天9点提醒我开会",
    "定时 每天8点 列出所有报表",     # 含枚举词，但 create 前缀优先
    "工作日7点取D-17",
    "定时 每周一9:30 提醒我开会",
])
def test_parse_plan_cmd_create_wins_over_list(text):
    """create 各路径必须优先于激进的 list 判定，否则排期请求会被降级成查清单。"""
    assert (P.parse_plan_cmd(text) or {}).get("cmd") == "create", text


def test_list_query_maxlen_guards_long_paragraphs():
    """长段落不劫持，交给大脑（大脑现在有正确的 AGENTS.md 口径）。"""
    long = "定时任务" + "的" * 60 + "有哪些"
    assert len(long) > P._LIST_QUERY_MAXLEN
    assert P._is_list_query(long) is False


@pytest.mark.parametrize("text,name", [
    ("绑定计划 每日数据更新跟踪回执 到本群", "每日数据更新跟踪回执"),
    ("把计划「每日数据更新跟踪回执」绑定到本群", "每日数据更新跟踪回执"),
    ("计划 每日数据更新跟踪回执 投递到本群", "每日数据更新跟踪回执"),
    ("把任务 每日数据上传提醒 发送到本群", "每日数据上传提醒"),
])
def test_parse_plan_cmd_bind(text, name):
    r = P.parse_plan_cmd(text)
    assert r and r["cmd"] == "bind" and r["name"] == name


@pytest.mark.parametrize("text,name", [
    ("解绑计划 每日数据更新跟踪回执", "每日数据更新跟踪回执"),
    ("取消绑定 每日数据更新跟踪回执", "每日数据更新跟踪回执"),
    ("把计划「每日数据上传提醒」取消投递", "每日数据上传提醒"),
])
def test_parse_plan_cmd_unbind(text, name):
    r = P.parse_plan_cmd(text)
    assert r and r["cmd"] == "unbind" and r["name"] == name


def test_parse_plan_cmd_update_time_and_desc():
    r = P.parse_plan_cmd("修改计划 每日数据上传提醒 每天9点")
    assert r == {"cmd": "update", "name": "每日数据上传提醒",
                 "on_calendar": "*-*-* 09:00:00", "gloss": "每天 09:00"}
    r = P.parse_plan_cmd("修改定时任务 每日数据上传提醒 描述：检查当日数据并输出回执")
    assert r["cmd"] == "update" and r["name"] == "每日数据上传提醒"
    assert r["description"] == "检查当日数据并输出回执"
    r = P.parse_plan_cmd("调整计划「每日数据上传提醒」每天09:30 提醒上传数据")
    assert r["on_calendar"] == "*-*-* 09:30:00"
    assert r["description"] == "提醒上传数据"


def test_direct_create_desc_and_name_are_cleaned():
    r = P.parse_plan_cmd("定时任务：每日3点，生成鲁大师用户日度使用分析报告")
    assert r["cmd"] == "create"
    assert r["description"] == "生成鲁大师用户日度使用分析报告"
    assert r["name"] == "生成鲁大师用户日度使用分析报告"
