from __future__ import annotations

"""自然语言 → 定时计划（relay-avatar 侧，确定性解析，无 LLM 往返）。

两条创建路径，都在进大脑之前就地拦截建计划（大脑没机会"当场执行"）：
  1) 显式前缀「定时/计划」：如「定时 每天8点 跑经营日报」；解析不出会 create_fail 提示。
  2) 放宽触发（无前缀）：剥离礼貌前导后，句子以节奏词（每天/每周/每月/每隔/每小时/
     工作日…）开头，且能解析出完整计划（daily/weekly/monthly 等还须带钟点）才拦截；
     否则一律放行走大脑——故"统计每天的销量""分析每天8点的量"这类动词开头的一次性活
     不会被误建计划。
（大脑产 ```plan 标记的兜底路径见 main.py 的 _consume_plan_markers。）

  定时 每天8点 跑经营日报          → 创建（前缀）
  每天8点跑经营日报                → 创建（放宽）
  每周一9:30 提醒我开会            → 创建（放宽）
  计划列表 / 我的计划              → 列表
  计划删除 <name> / 删除计划 <name> → 删除
  计划停用 <name> / 计划启用 <name> → 启停

生成的 OnCalendar 交由 relay 服务端用 systemd-analyze 二次校验，非法即 422。
"""

import re

WD_CN = {"一": "Mon", "二": "Tue", "三": "Wed", "四": "Thu",
         "五": "Fri", "六": "Sat", "日": "Sun", "天": "Sun"}
WD_NUM = {"1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu",
          "5": "Fri", "6": "Sat", "7": "Sun"}
WD_GLOSS = {"Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四",
            "Fri": "周五", "Sat": "周六", "Sun": "周日"}

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_NUMCLS = r"[零〇一二两三四五六七八九十\d]"

MERIDIEM_PM = ("下午", "晚上", "傍晚", "夜里", "夜晚")
MERIDIEM_NOON = ("中午",)
MERIDIEM_AM = ("早上", "早晨", "上午", "凌晨", "清晨")

# 创建意图里"像定时"的提示词（区分 create_fail 与普通任务）
_HINT = re.compile(r"每天|每日|天天|每周|每月|每隔|工作日|每小时|定时|提醒|"
                   r"\d+\s*点|" + _NUMCLS + r"+点|\d{1,2}[:：]\d{2}")

# 放宽触发（无前缀）：先剥离礼貌/命令前导，再要求句子以"节奏词"开头，
# 且能解析出完整计划才拦截建计划；否则放行走大脑，避免误伤"统计每天的销量"。
_LEADIN = re.compile(
    r"^(?:请|帮我|帮忙|给我|麻烦|然后|就|要|想|来|现在|立刻|马上|顺便|"
    r"记得|别忘了|提醒我|通知我|叫我)+[\s,，、:：]*")
_CADENCE_ANCHOR = re.compile(
    r"^(?:每\s*[天日]|天天|每隔|每\s*小时|每\s*周|每\s*星期|每\s*礼拜|每\s*月|工作日)")

# ── 查询（list）触发：调度名词 × 枚举疑问 ──
# 为什么激进（2026-09-29）：list 是**只读、零副作用**操作，误判最多多回一句计划
# 清单；而漏判会让「现在有几个定时任务」落进大脑——#278 为此烧掉 5.5 分钟 / 32 次
# Bash 去刨 relay 路由、源码与凭据。故 list 放宽、create 保持上面的严格判据不动。
# 原先只认 4 个 fullmatch 句式（计划列表/我的计划/清单/全部计划），口语问法全漏。
_SCHED_NOUN = re.compile(r"定时任务|定时计划|计划任务|定时|调度|排期|cron", re.I)
# 强枚举疑问：与调度名词搭配即可判 list（含「有什么」这类开放式疑问）
_ENUM_HINT = re.compile(
    r"有哪些|有什么|有几个|多少个|有没有|有无|列出|列一下|列个|查一下|查查|查看|"
    r"清单|列表|全部|所有|现存|现有|盘点|几个|多少")
# 严格枚举疑问：用于裸「计划／定时」（无"任务"后缀）的短句。**刻意排除「有什么」**，
# 否则「这个计划有什么问题」会被劫持成 list。
_ENUM_STRICT = re.compile(
    r"有哪些|有几个|多少个|有没有|有无|列出|列一下|清单|列表|全部|所有|现存|现有|盘点")
# 裸「计划」歧义大（可能指业务计划书/方案），故只配短句 + 严格枚举词。
_PLAN_NOUN_LOOSE = re.compile(r"计划|定时")
_LIST_QUERY_MAXLEN = 40        # 长段落不劫持，交给大脑
_LIST_QUERY_LOOSE_MAXLEN = 12  # 裸「计划」路径的句长上限


def _is_list_query(t: str) -> bool:
    """判定「这是在问有哪些定时计划」。见上方注释：list 只读，宁可多判不可漏判。"""
    if not t or len(t) > _LIST_QUERY_MAXLEN:
        return False
    if _SCHED_NOUN.search(t) and _ENUM_HINT.search(t):
        return True
    return bool(len(t) <= _LIST_QUERY_LOOSE_MAXLEN
                and _PLAN_NOUN_LOOSE.search(t) and _ENUM_STRICT.search(t))


def cn2int(s: str) -> int | None:
    """中文/阿拉伯数字（0-59）→ int。支持 十/十五/二十/二十五/八/8。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        if a and a not in _CN:
            return None
        if b and b not in _CN:
            return None
        return (_CN[a] if a else 1) * 10 + (_CN[b] if b else 0)
    if len(s) == 1 and s in _CN:
        return _CN[s]
    return None


def _meridiem(pre: str, h: int) -> int:
    """按时间词前的时段词修正小时（12 小时制 → 24 小时制）。"""
    if any(w in pre for w in MERIDIEM_PM):
        if h < 12:
            h += 12
    elif any(w in pre for w in MERIDIEM_NOON):
        if h < 11:
            h += 12
    elif any(w in pre for w in MERIDIEM_AM):
        if h == 12:
            h = 0
    return h


def _parse_time(text: str):
    """返回 (hour, minute, span) 或 None。"""
    m = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            return _meridiem(text[:m.start()], h), mi, m.span()
    m = re.search(r"(" + _NUMCLS + r"{1,3})\s*点\s*"
                  r"(半|(" + _NUMCLS + r"{1,3})\s*分?|(\d{1,2})\s*分?)?", text)
    if m:
        h = cn2int(m.group(1))
        if h is None or h > 23:
            return None
        mi = 0
        if m.group(2) == "半":
            mi = 30
        elif m.group(3):
            mi = cn2int(m.group(3)) or 0
        elif m.group(4):
            mi = int(m.group(4))
        if mi > 59:
            return None
        return _meridiem(text[:m.start()], h), mi, m.span()
    return None


def _parse_freq(text: str):
    """返回 (kind, param, span) 或 None。"""
    m = re.search(r"每隔?\s*(" + _NUMCLS + r"{1,3})\s*分钟", text)
    if m:
        n = cn2int(m.group(1))
        if n and n >= 1:
            return "min", n, m.span()
    m = re.search(r"每隔?\s*(" + _NUMCLS + r"{1,3})\s*(?:小时|钟头)", text)
    if m:
        n = cn2int(m.group(1))
        if n and n >= 1:
            return "hour", n, m.span()
    m = re.search(r"每小时", text)
    if m:
        return "hourly", None, m.span()
    m = re.search(r"工作日|周[一二三四五1-5]到周[一二三四五1-5]|星期[一二三四五1-5]到星期[一二三四五1-5]", text)
    if m:
        return "weekdays", None, m.span()
    m = re.search(r"(?:每)?(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
    if m:
        wd = WD_CN.get(m.group(1)) or WD_NUM.get(m.group(1))
        if wd:
            return "weekly", wd, m.span()
    m = re.search(r"(?:每)?月\s*(" + _NUMCLS + r"{1,3})\s*[号日]", text)
    if m:
        n = cn2int(m.group(1))
        if n and 1 <= n <= 31:
            return "monthly", n, m.span()
    m = re.search(r"每天|每日|天天", text)
    if m:
        return "daily", None, m.span()
    return None


def _build_on_calendar(kind: str, param, h: int, m: int) -> str | None:
    hm = f"{h:02d}:{m:02d}:00"
    if kind == "min":
        return f"*:0/{param}:00" if param and param >= 5 else None
    if kind == "hour":
        return f"*-*-* 0/{param}:00:00"
    if kind == "hourly":
        return "*-*-* *:00:00"
    if kind == "weekdays":
        return f"Mon..Fri *-*-* {hm}"
    if kind == "weekly":
        return f"{param} *-*-* {hm}"
    if kind == "monthly":
        return f"*-*-{param:02d} {hm}"
    if kind == "daily":
        return f"*-*-* {hm}"
    return None


def _gloss(kind: str, param, h: int, m: int) -> str:
    hm = f"{h:02d}:{m:02d}"
    return {"daily": f"每天 {hm}", "weekly": f"{WD_GLOSS.get(param, param)} {hm}",
            "weekdays": f"工作日 {hm}", "monthly": f"每月{param}号 {hm}",
            "hourly": "每小时整点", "hour": f"每{param}小时",
            "min": f"每{param}分钟"}.get(kind, "")


def _extract_desc(text: str, spans: list) -> str:
    """从原句剔除频率/时间片段与时段词、前导客套，余下即任务描述。"""
    chars = list(text)
    for sp in spans:
        if sp:
            for i in range(sp[0], min(sp[1], len(chars))):
                chars[i] = ""
    s = "".join(chars)
    s = re.sub("|".join(MERIDIEM_PM + MERIDIEM_NOON + MERIDIEM_AM), "", s)
    s = re.sub(r"^(?:请|帮我|帮忙|给我|麻烦|然后|就|要|想|来|的)+", "", s.strip())
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = s.strip(" ，,、的然后就给我帮请麻烦:-—、")
    return s or "定时任务"


def make_name(desc: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.一-鿿-]", "", desc or "")[:16]
    return base or "plan"


def parse_schedule(text: str) -> dict | None:
    """自然语言 → {on_calendar, description, name, gloss} 或 None / {"error": ...}。"""
    t = (text or "").strip()
    if not t:
        return None
    freq = _parse_freq(t)
    if not freq:
        return None
    kind, param, fspan = freq
    need_time = kind in ("daily", "weekly", "weekdays", "monthly")
    tm = _parse_time(t)
    h = m = 0
    tspan = None
    if tm:
        h, m, tspan = tm
    if need_time and not tm:
        return None
    if kind == "min" and (not param or param < 5):
        return {"error": "too_frequent"}
    on = _build_on_calendar(kind, param, h, m)
    if not on:
        return None
    desc = _extract_desc(t, [fspan, tspan])
    return {"on_calendar": on, "description": desc,
            "name": make_name(desc), "gloss": _gloss(kind, param, h, m)}


def parse_plan_cmd(text: str) -> dict | None:
    """识别计划命令。返回 {cmd: list|create|create_fail|delete|disable|enable, ...} 或 None。"""
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"(?:计划|定时)?\s*(?:列表|清单|全部计划)", t) or t in ("我的计划", "计划列表"):
        return {"cmd": "list"}
    m = re.match(r"^(?:计划|定时)\s*(?:删除|删掉|移除)\s*(\S+)$", t) \
        or re.match(r"^(?:删除计划|删掉计划|移除计划)\s*(\S+)$", t)
    if m:
        return {"cmd": "delete", "name": m.group(1)}
    m = re.match(r"^(?:计划|定时)\s*(?:停用|暂停|关闭)\s*(\S+)$", t)
    if m:
        return {"cmd": "disable", "name": m.group(1)}
    m = re.match(r"^(?:计划|定时)\s*(?:启用|开启|恢复)\s*(\S+)$", t)
    if m:
        return {"cmd": "enable", "name": m.group(1)}
    m = re.match(r"^(?:定时|计划)[\s:：]*(.+)$", t, re.S)
    if m:
        rest = m.group(1).strip()
        sch = parse_schedule(rest)
        if sch:
            if sch.get("error"):
                return {"cmd": "create_fail", "reason": sch["error"], "raw": rest}
            return {"cmd": "create", **sch}
        if _HINT.search(rest):
            return {"cmd": "create_fail", "reason": "unparsed", "raw": rest}
        # 无定时意图：此前直接 return None 放行走大脑，于是「定时任务有哪些」这类
        # 以"定时"开头的**查询**句也一并漏给了大脑（#278 那 5.5 分钟 / 32 次 Bash
        # 就是这么来的）。改为落到下面的 list 判定；判不出仍返回 None 走普通任务。
    else:
        # 放宽触发：无前缀，但剥离礼貌前导后以节奏词开头，且能解析出完整计划才拦截；
        # 解析不出（缺钟点的"每天跑日报"、动词开头的"统计每天的销量"）一律放行走大脑。
        core = _LEADIN.sub("", t).lstrip()
        if _CADENCE_ANCHOR.match(core):
            sch = parse_schedule(core)
            if sch and not sch.get("error") and sch["description"] != "定时任务":
                return {"cmd": "create", **sch}
    # create 各路径都已优先判过，这里才做激进的 list 判定（只读、零副作用）。
    if _is_list_query(t):
        return {"cmd": "list"}
    return None
