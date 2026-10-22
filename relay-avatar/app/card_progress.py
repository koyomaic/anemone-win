from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

SENSITIVE_RE = re.compile(
    r"(?i)(token|secret|passw|credential|api[_-]?key|relay\.env|device_secret|"
    r"admin_token|authorization)")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PATH_RE = re.compile(r"(?:/[\w.\-@]+)+")

TOOL_LABELS = {
    "read": "📖 读取文件",
    "write": "✍️ 写文件",
    "edit": "✏️ 修改文件",
    "multiedit": "✏️ 修改文件",
    "glob": "🔍 查找文件",
    "grep": "🔍 搜索内容",
    "ls": "📂 浏览目录",
    "webfetch": "🌐 访问网页",
    "websearch": "🔍 联网搜索",
    "task": "🤖 委派子任务",
    "bash": "🔧 执行命令",
    "shell": "🔧 执行命令",
    "execute_bash": "🔧 执行命令",
}

STAGE_RULES = [
    ("渲染报告", "报告装配"),
    ("台账", "台账登记"),
    ("自检", "质检"),
    ("质检", "质检"),
    ("委派", "专业智能体协作"),
    ("搜索内容", "资料检索"),
    ("查找文件", "资料检索"),
    ("检索文件", "资料检索"),
    ("浏览目录", "资料检索"),
    ("访问网页", "外部资料检索"),
    ("联网搜索", "外部资料检索"),
    ("读取文件", "资料/数据读取"),
    ("写文件", "交付件生成"),
    ("修改文件", "交付件修订"),
    ("运行数据处理脚本", "数据处理"),
    ("执行命令", "数据处理"),
    ("组织结论", "组织结论"),
]


def safe_text(s, limit: int = 60) -> str:
    t = CTRL_RE.sub(" ", str(s or ""))
    t = " ".join(t.split())
    if not t or SENSITIVE_RE.search(t):
        return ""
    t = PATH_RE.sub(lambda m: m.group(0).rstrip("/").split("/")[-1], t)
    return t[:limit].strip()


def safe_basename(p, limit: int = 40) -> str:
    t = str(p or "").strip().replace("\\", "/")
    if not t or SENSITIVE_RE.search(t):
        return ""
    name = CTRL_RE.sub(" ", t.rstrip("/").split("/")[-1])
    return " ".join(name.split())[:limit].strip()


def bash_label(cmd) -> tuple[str, str]:
    low = str(cmd or "").lower().strip()
    if not low:
        return "🔧 执行命令", ""
    if SENSITIVE_RE.search(low):
        return "🔧 执行系统命令", ""
    if "render_report.py" in low:
        return "📑 渲染报告", "render_report.py"
    if re.search(r"\bstore\b\s+(add|sync|doctor|link|list|search)", low):
        return "🗂 台账登记/校验", ""
    if "pytest" in low:
        return "✅ 自检", ""
    m = re.search(r"\bpython[0-9.]*\b\s+(?:-[^\s]+\s+)*([^\s;|&]+\.py)", low)
    if m:
        return "🧮 运行数据处理脚本", safe_basename(m.group(1))
    if re.search(r"(^|[\s;&|])git\s", low):
        return "🕒 版本查询", ""
    if re.search(r"(^|[\s;&|])dws\s", low):
        return "📨 钉钉资料/投递准备", ""
    if re.search(r"\b(rg|grep|find|ls|cat|head|tail|wc)\b", low):
        return "🔍 检索文件", ""
    return "🔧 执行命令", ""


def tool_label(name, inp=None, title: str = "") -> tuple[str, str]:
    inp = inp if isinstance(inp, dict) else {}
    n = str(name or "").strip()
    low = n.lower()
    if low.startswith("mcp__"):
        tool = n.split("__")[-1] or "tool"
        return f"🔌 调用 {safe_text(tool, 30)}", ""
    if low in ("bash", "shell", "execute_bash"):
        return bash_label(inp.get("command") or inp.get("cmd") or title)
    base = TOOL_LABELS.get(low, f"🔧 {safe_text(n, 30) or '工具'}")
    detail = ""
    if low in ("read", "write", "edit", "multiedit"):
        detail = safe_basename(inp.get("file_path") or inp.get("path")
                               or inp.get("filePath") or title)
    elif low in ("glob", "ls"):
        detail = safe_text(inp.get("pattern") or inp.get("path") or title, 40)
    elif low == "grep":
        detail = safe_text(inp.get("pattern") or inp.get("regex") or title, 40)
    elif low == "task":
        detail = safe_text(inp.get("description") or inp.get("prompt") or title, 30)
    elif low in ("webfetch", "websearch"):
        url = str(inp.get("url") or title or "")
        try:
            detail = safe_text(urlparse(url).hostname or url, 40)
        except Exception:
            detail = safe_text(url, 40)
    return base, detail


def tool_step(blk) -> str:
    if isinstance(blk, dict):
        name = blk.get("name") or blk.get("tool") or ""
        inp = blk.get("input") or {}
        title = blk.get("title") or ""
    else:
        name, inp, title = str(blk or ""), {}, ""
    label, detail = tool_label(name, inp, title)
    return f"{label} · {detail}" if detail else label


def fmt_dur(sec) -> str:
    sec = max(0, int(float(sec or 0)))
    if sec < 60:
        return f"{sec} 秒"
    return f"{sec // 60} 分 {sec % 60} 秒"


def budget_bar(elapsed, budget, width: int = 10) -> str:
    budget = float(budget or 0)
    if budget <= 0:
        return ""
    pct = min(100, max(0, int(float(elapsed or 0) * 100 / budget)))
    filled = int(width * pct / 100)
    return f"{'▓' * filled}{'░' * (width - filled)} {pct}%"


def infer_stage(actions=None, status: str = "", backend: str = "relay",
                seg: int = 0, note: str = "") -> str:
    if note:
        return safe_text(note, 60)
    if status == "queued":
        return "排队等待后台执行槽位"
    if status == "pending_approval":
        return "等待拍板"
    if status == "review":
        return "等待确认"
    for a in reversed(list(actions or [])[-5:]):
        s = str(a)
        for kw, stage in STAGE_RULES:
            if kw in s:
                return stage
    if seg > 0:
        return f"第 {seg + 1} 段接续执行"
    if backend == "local":
        return "本机预处理"
    return "后台执行中"


@dataclass
class ProgressCtx:
    avatar_name: str = ""
    hint: str = ""
    backend: str = "relay"
    status: str = "running"
    total_elapsed: float = 0.0
    seg_elapsed: float = 0.0
    budget: float = 0.0
    seg: int = 0
    max_seg: int = 0
    stage: str = ""
    actions: list = field(default_factory=list)
    note: str = ""
    controls: str = ""
    queue: dict = field(default_factory=dict)
    detail: bool = True
    mode: str = "rich"
    title: str = "正在处理…"


def _clean_actions(actions, limit: int = 3) -> list:
    out = []
    for a in list(actions or [])[-limit:]:
        t = safe_text(a, 70)
        if t:
            out.append(t)
    return out


def render(ctx: ProgressCtx) -> str:
    if str(ctx.mode or "rich").lower() != "rich":
        note = ctx.note or ctx.stage or "正在为你办"
        return (f"🍂 **{ctx.avatar_name}{ctx.title}**\n\n"
                f"📥 已收到：{ctx.hint}\n"
                f"⏳ {note}…（已用 {int(ctx.total_elapsed or 0)}s）")

    lines = [f"🍂 **{ctx.avatar_name}{ctx.title}**", ""]
    q = []
    if ctx.hint:
        q.append(f"📥 已收到：{ctx.hint}")
    if ctx.note:
        q.append(f"⏳ {ctx.note}")
    stage = ctx.stage or infer_stage(ctx.actions, ctx.status, ctx.backend,
                                     ctx.seg, "")
    if stage:
        q.append(f"🧭 当前阶段：{stage}")
    total = float(ctx.total_elapsed or 0)
    seg = float(ctx.seg_elapsed or 0)
    if total:
        if ctx.backend == "relay" and seg and abs(seg - total) >= 1:
            q.append(f"⏱ 总用时 {fmt_dur(total)} · 本段 {fmt_dur(seg)}")
        else:
            q.append(f"⏱ 已用 {fmt_dur(total)}")
    if ctx.seg > 0:
        if ctx.max_seg > 0:
            q.append(f"🧵 第 {ctx.seg + 1}/{ctx.max_seg} 段，前段产出已保留")
        else:
            q.append(f"🧵 第 {ctx.seg + 1} 段，前段产出已保留")
    if ctx.budget and ctx.budget > 0:
        bar = budget_bar(seg or total, ctx.budget)
        label = "本机预算" if ctx.backend == "local" else "本段预算"
        q.append(f"📊 {label} {fmt_dur(ctx.budget)} {bar}".rstrip())
    if ctx.status == "queued":
        pending = int((ctx.queue or {}).get("pending") or 0)
        active = int((ctx.queue or {}).get("active") or 0)
        if pending or active:
            q.append(f"🚦 后台队列：待执行 {pending} · 执行中 {active}")
    actions = _clean_actions(ctx.actions, 3)
    if actions and ctx.detail:
        q.append("最近动作：")
        for a in actions:
            q.append(f"· {a}")
    if ctx.controls:
        q.append(ctx.controls)
    return "\n".join(lines + q)


def render_summary(ctx: ProgressCtx) -> str:
    lines = [f"⏳ 仍在后台执行，总用时 {fmt_dur(ctx.total_elapsed)}。"]
    stage = ctx.stage or infer_stage(ctx.actions, ctx.status, ctx.backend,
                                     ctx.seg, "")
    if stage:
        lines.append(f"当前阶段：{stage}。")
    actions = _clean_actions(ctx.actions, 3)
    if actions and ctx.detail:
        lines.append("最近动作：" + "；".join(actions) + "。")
    if ctx.controls:
        lines.append(ctx.controls)
    return "\n".join(lines)
