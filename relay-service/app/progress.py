from __future__ import annotations

import re
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


def agent_tool_event(name, inp=None, title: str = "") -> dict:
    label, detail = tool_label(name, inp, title)
    ev = {"kind": "tool", "tool": str(name or ""), "label": label,
          "title": label}
    if detail:
        ev["detail"] = detail
    return ev


def summarize_event(ev: dict) -> dict | None:
    if not isinstance(ev, dict):
        return None
    if ev.get("t") != "agent":
        return None
    eid = ev.get("id")
    if ev.get("kind") == "tool":
        label = ev.get("label")
        if not label:
            label, detail = tool_label(ev.get("tool"), ev.get("input"),
                                       ev.get("title"))
        else:
            detail = safe_text(ev.get("detail"), 40)
        out = {"id": eid, "kind": "tool", "tool": str(ev.get("tool") or ""),
               "label": safe_text(label, 60)}
        if detail:
            out["detail"] = detail
        return out
    if ev.get("kind") == "text":
        return {"id": eid, "kind": "text", "label": "✍️ 组织结论"}
    return None


def summarize_events(events, limit: int = 5) -> list:
    out = []
    for ev in events or []:
        s = summarize_event(ev)
        if s:
            out.append(s)
    return out[-max(1, min(int(limit or 5), 10)):]


def infer_stage(events, status: str = "") -> str:
    if status == "queued":
        return "排队等待后台执行槽位"
    if status == "pending_approval":
        return "等待拍板"
    if status == "review":
        return "等待确认"
    for ev in reversed(list(events or [])[-30:]):
        s = summarize_event(ev)
        if not s:
            continue
        text = str(s.get("label") or "")
        for kw, stage in STAGE_RULES:
            if kw in text:
                return stage
    if status == "running":
        return "后台执行中"
    return ""
