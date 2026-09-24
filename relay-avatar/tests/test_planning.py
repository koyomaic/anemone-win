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
