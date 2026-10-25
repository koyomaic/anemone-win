from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import progress
from . import shared_knowledge
from .config import block_category

# 环境继承收敛（2026-09-17 加固）：服务自身机密绝不进任务环境——
# RELAY_* 前缀整体剔除（ADMIN_TOKEN / DB 连接串 / DASHBOARD_PASSWORD / TLS_DIR 等），
# 代码托管凭据按名剔除。ANTHROPIC_*（引擎鉴权必需）与常规运行变量保留。
# 与 relay.service 挂载命名空间（InaccessiblePaths=relay.env、/root/.ssh 等）双保险。
_ENV_DROP_PREFIXES = ("RELAY_",)
_ENV_DROP_EXACT = {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GITLAB_TOKEN", "NPM_TOKEN"}

# 子进程 stdout 的 StreamReader 缓冲上限。asyncio 默认 2**16=64KB，对 stream-json
# 远远不够（单行含大工具结果/长文本/内联图片时 readline() 抛 ValueError 致任务失败）。
_STREAM_LIMIT = 64 * 1024 * 1024


def _scrubbed_environ() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(_ENV_DROP_PREFIXES) and k not in _ENV_DROP_EXACT}


@dataclass
class AgentResult:
    session_id: str = ""
    text: str = ""
    tokens: int = 0
    cost: float = 0.0
    error: str | None = None
    raw_tail: str = field(default="")
    blocked_cmd: str = ""
    # 命中的**原因**（pattern 原文 / "recursive rm outside safe dirs: …" / dws 确认原因）。
    # 此前 _is_blocked 的返回值只当布尔用、原因被丢弃，于是上层无法区分「侦察类」与
    # 「删共享区/对外发送」——只能一律 pending_approval 去问人。分类靠比对已知字符串
    # 即可（O(1) 集合查找），**不新增任何正则**，故这里把已经算出来的值带上去。
    block_reason: str = ""
    # ── 长任务异步化：单段实际耗时与升级标记 ──
    # elapsed 用于审计与分段续跑决策（"这一段跑满了多久"）。
    # timed_out 用**独立布尔**而不是让上层去 startswith("timeout after")：错误串是
    # 给人看的文案，拿它当控制流一旦改字就静默失效。
    elapsed: float = 0.0
    long_flagged: bool = False
    timed_out: bool = False
    # 本段工具调用次数。**分段续跑的进展门需要它**：被砍的段拿不到 result 事件，
    # 故 tokens=0、最终文本也常常是空的（#146 第 1 段实测 files=0 text=0，其实一直在
    # 跑工具查数据）。只看文件与文本会把"正在采集数据"误判成"毫无进展"而停掉续跑。
    tool_calls: int = 0
    # 本段 JSON 解析失败的工具输入数（__unparsedToolInput 签名），管道切碎护栏的
    # 分子；分母复用 tool_calls（切碎时头+尾碎片都计入，比值趋近 100%，基线 ~0）。
    # 判据与阈值见 pipeline_shred_suspect()。
    tool_verrs: int = 0
    # 子进程退出码，原样保留不字符串化。上层要区分「被信号打断」与「agent 自己
    # 报错」时靠它——从 error 文案里正则抠数字，一改字就静默失效（与 timed_out
    # 用独立布尔而非 startswith("timeout after") 同一个理由）。
    exit_code: int = 0


def signal_name(rc: int) -> str:
    """退出码 → 信号名；非信号退出返回空串。

    兼容两种口径：asyncio 直接子进程给负数（-15），经 shell 包装给 128+signo
    （143）。#149 实测拿到的是 143。
    """
    signo = -rc if rc < 0 else (rc - 128 if rc > 128 else 0)
    if signo <= 0:
        return ""
    try:
        return signal.Signals(signo).name
    except ValueError:
        return f"signal {signo}"


def pipeline_shred_suspect(res: AgentResult, cfg) -> str:
    """工具参数解析被拒超阈值 → 疑似**平台管道缺陷**（不是模型问题）。

    2026-10-23 事故：8790 代理的流式脱敏保留区在块切换时不刷新，工具参数被切成
    「头部截断 JSON＋定长尾巴」两个同 id 碎片，双双 InputValidationError；两天
    56% 逻辑调用被拒、~2 亿 input token 白烧，而错误文案全程甩锅模型（"You
    sent … could not be parsed as JSON"），两天无人察觉方向错了。本护栏把签名
    计数翻译成平台侧真相：error 标注＋审计＋钉钉告警（alert_pipeline_shred）。

    阈值依据：__unparsedToolInput 是「解析失败」签名，基线 ~0（09-15→10-22 五周
    全机队日均 ≤4 次且多为 schema 错、不走此签名）；事故期占比 41-58%。
    min_count 防小样本误报（2 次调用 1 次被拒＝50% 不足以定罪管道）。
    返回标注文案；未达阈值返回空串。
    """
    min_count = max(1, int(getattr(cfg, "tool_verr_min_count", 5)))
    if res.tool_verrs < min_count:
        return ""
    uses = max(res.tool_calls, 1)
    ratio = res.tool_verrs / uses
    if ratio < float(getattr(cfg, "tool_verr_min_ratio", 0.25)):
        return ""
    return (f"疑似平台管道缺陷（非模型问题）：工具参数解析被拒 "
            f"{res.tool_verrs}/{uses} 次（{ratio:.0%}）——请检查 claude-proxy "
            f"流式转换/脱敏保留区，故障签名与排查口诀见知识库 #8105")


# 告警冷却锚点（进程级）。事故期每个任务都会触发判定，不冷却会把收件人打爆：
# 第一条送达真相，后续靠审计线兜底，修复后自然归零。
_last_pipeline_alert = 0.0


async def alert_pipeline_shred(task_id: int, user: str, note: str, cfg) -> bool:
    """管道缺陷钉钉直聊告警。与 boot.sh notify_failure 同一套种子登录态
    （临时 HOME 软链 dws-cli-seed，token 轮换发生在种子上、无复制竞态）；
    无种子时退回本机 root 登录态。带冷却（默认 1h）。返回是否实际发送。

    绝不抛异常：告警是旁路，失败只丢告警、不得影响任务收尾。
    """
    global _last_pipeline_alert
    to = (getattr(cfg, "pipeline_alert_to", "") or "").strip()
    if not to:
        return False
    now = time.time()
    cooldown = float(getattr(cfg, "pipeline_alert_cooldown_sec", 3600) or 0)
    if now - _last_pipeline_alert < cooldown:
        return False
    _last_pipeline_alert = now
    msg = (f"【relay管道预警·{socket.gethostname()}】任务 #{task_id}（{user}）："
           f"{note}；{time.strftime('%F %T')}")
    seed = getattr(cfg, "dws_seed_dir", "") or ""
    env = dict(os.environ)
    work = ""
    try:
        if seed and os.path.isdir(seed):
            work = tempfile.mkdtemp(prefix="relay-alert-")
            os.makedirs(os.path.join(work, ".local", "share"), exist_ok=True)
            os.symlink(seed, os.path.join(work, ".local", "share", "dws-cli"))
            env["HOME"] = work
        else:
            env["HOME"] = "/root"
        proc = await asyncio.create_subprocess_exec(
            "dws", "chat", "+dm", "--to", to, "--content", msg,
            "--yes", "--format", "json",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=env)
        return (await proc.wait()) == 0
    except Exception:
        return False
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)   # 只删软链，种子不受影响


def _parse_opencode(line: str) -> dict | None:
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    part = ev.get("part") or {}
    sid = ev.get("sessionID") or part.get("sessionID") or ""
    out: dict = {}
    if sid:
        out["session_id"] = sid
    t = ev.get("type")
    if t == "text" and part.get("text"):
        out["text"] = part["text"]
        out["agent_event"] = {"kind": "text", "text": part["text"][:2000]}
    elif t == "tool_use":
        state = part.get("state") or {}
        inp = part.get("input") or state.get("input") or {}
        out["agent_event"] = progress.agent_tool_event(
            part.get("tool", ""), inp, part.get("title", ""))
        if part.get("tool") == "bash":
            cmd = inp.get("command") or inp.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                out["tool_cmd"] = cmd
    elif t == "step_finish":
        tok = (part.get("tokens") or {}).get("total")
        if tok:
            out["tokens"] = int(tok)
        if part.get("cost"):
            out["cost"] = float(part["cost"])
    return out or None


def _parse_claude(line: str) -> dict | None:
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    out: dict = {}
    et = ev.get("type")
    if et == "system" and ev.get("session_id"):
        out["session_id"] = ev["session_id"]
    elif et == "assistant":
        for block in (ev.get("message") or {}).get("content", []):
            if block.get("type") == "text" and block.get("text"):
                out["text"] = out.get("text", "") + block["text"]
                out["agent_event"] = {"kind": "text", "text": block["text"][:2000]}
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                out["tool_use_n"] = out.get("tool_use_n", 0) + 1
                # 管道切碎签名：claude-code 把 JSON 解析失败的工具输入包成
                # __unparsedToolInput {"raw":…,"len":…}。2026-10-23 代理保留区
                # 事故里它成对出现（同 id 头部+定长尾巴）；正常条件下模型参数
                # 解析失败率 ~0（09-15→10-22 五周基线 0.2% 且多为 schema 错而非
                # 解析错），故此签名几乎只在平台管道缺陷时出现。
                if isinstance(inp, dict) and "__unparsedToolInput" in inp:
                    out["tool_verr_n"] = out.get("tool_verr_n", 0) + 1
                out["agent_event"] = progress.agent_tool_event(
                    block.get("name", ""), inp)
                if str(block.get("name", "")).lower() == "bash":
                    cmd = inp.get("command") or inp.get("cmd")
                    if isinstance(cmd, str) and cmd.strip():
                        out["tool_cmd"] = cmd
    elif et == "result":
        # result 事件携带完整最终文本（与流式 assistant text 重复），整体替换而非追加
        if ev.get("result"):
            out["text_final"] = ev["result"]
        if ev.get("total_cost_usd"):
            out["cost"] = float(ev["total_cost_usd"])
        if ev.get("session_id"):
            out["session_id"] = ev["session_id"]
        usage = ev.get("usage") or {}
        # cache 字段必须计入：直通模式（aigateway 原生 Anthropic 协议）下提示
        # 缓存生效，input_tokens 只剩可变尾部（实测 34K→18），大头进
        # cache_read_input_tokens。但**缓存读≠上下文变小**——resume 时那些
        # token 仍占着窗口，avatar 的续跑上下文预算闸门（chain_tokens）要的
        # 是上下文真值。翻译路径无 cache 字段，get 默认 0，向后兼容。
        tok = (int(usage.get("input_tokens") or 0)
               + int(usage.get("output_tokens") or 0)
               + int(usage.get("cache_read_input_tokens") or 0)
               + int(usage.get("cache_creation_input_tokens") or 0))
        if tok:
            out["tokens"] = tok
    return out or None


# ---- dws 消息发送二次确认门控 ----
# 发送入口按 dws CLI --help 实测调研（shortcut + 原子命令两层）：
#   chat shortcut: +dm/+broadcast/+send-to-group/+messages-send(+by-bot/by-webhook/card)/
#                  +messages-batch-send-by-bot/+messages-reply/+messages-forward(+combine/topic)
#   chat 原子:     message send/send-by-bot/send-by-webhook/send-card/reply/forward/combine-forward
#   ding:          +send-personal、message send/send-personal/send-by-message
# 只读命令（auth status、contact search、chat +chat-list/+chat-messages 等）不在表内，不拦。
_DWS_SEND_PATHS = {
    ("chat", "+dm"), ("chat", "+broadcast"), ("chat", "+send-to-group"),
    ("chat", "+messages-send"), ("chat", "+messages-send-by-bot"),
    ("chat", "+messages-send-by-webhook"), ("chat", "+messages-send-card"),
    ("chat", "+messages-batch-send-by-bot"), ("chat", "+messages-reply"),
    ("chat", "+messages-forward"), ("chat", "+messages-combine-forward"),
    ("chat", "+messages-forward-topic"),
    ("chat", "message", "send"), ("chat", "message", "send-by-bot"),
    ("chat", "message", "send-by-webhook"), ("chat", "message", "send-card"),
    ("chat", "message", "reply"), ("chat", "message", "forward"),
    ("chat", "message", "combine-forward"),
    ("ding", "+send-personal"), ("ding", "message", "send"),
    ("ding", "message", "send-personal"), ("ding", "message", "send-by-message"),
}
# 单人接收者参数（值可逗号/空格分隔多值，或重复传 flag 累积）
_DWS_PERSON_FLAGS = {
    "--to", "--user", "--users", "--open-dingtalk-id", "--open-dingtalk-ids",
    "--receiver", "--receiver-open-dingtalk-id", "--user-query",
}
# 群聊/会话目标参数（webhook token 的目标即 token 所在群，一并视为群发）。
# 注意：--chat-id/--conversation-id 是"会话"别名，1:1 单聊与群聊都长 cid，
# 不能一刀切当群发——目标值是 cid 时由 _cid_is_single_chat 按会话类型二次判定。
_DWS_GROUP_FLAGS = {
    "--group", "--groups", "--groups-file", "--chat-id", "--chat-query",
    "--conversation-id", "--dest-conversation-id", "--webhook-token", "--token",
}
# 会话解析缓存：cid -> (singleChat, title)。会话类型/对方名不变，进程内一次解析长期复用。
# 解析失败不入缓存（下次重试，避免把瞬时故障固化成误判）。
_CID_INFO_CACHE: dict[str, tuple[bool, str]] = {}
# 判定一个群/会话目标值是否为"会话 cid"（形如 cidXXX==，base64 字母表 + 尾随 =）。
# 群名（--group 名/--chat-query）等不含这些特征 → 视为明确群发，直接拦截。
_CID_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
# 个别命令里同名 flag 不是发送目标：ding message send-by-message 的 --group 是源会话
_DWS_NON_TARGET_FLAGS = {("ding", "message", "send-by-message"): {"--group"}}
# 段内 cd 目标提取。仅供 _cwd_at 惰性回溯相对路径 rm 用（见 _rm_violation 注释），
# 不在每条命令的热路径上跑。`(?<![\w-])` 防误命中 `--cd`、`pushd` 之类。
_CD_RE = re.compile(r"(?<![\w-])cd\s+(\"[^\"]*\"|'[^']*'|\S+)")


def _state_dirname(user: str) -> str:
    """把用户名安全化为持久状态目录名（防路径穿越；允许中文，Linux 目录名无碍）。"""
    u = (user or "").replace("\x00", "").replace("/", "_").strip()
    if u in ("", ".", ".."):
        return "default"
    return u


# ── resume 前剥离历史图片 ──
#
# 为什么必须做：--resume 把整段历史重放给模型，图片块由 claude-proxy 转成
# image_url(data:<mime>;base64,...) 原样上送。实测 2026-10-23 宋连杰一个 181s
# 录屏，经 378→380→382→384→386 五任务共用一个 session，累积 15 个图片块、
# 6.11MB base64，转写涨到 12.66MB；每回合都要整体重算 prefill，一句话追问
# （#382）也跑满 134s。图片在它出现的那一轮已被模型看过，resume 时纯属死重。
#
# 只动两处，行数与 JSON 结构不变：
#   message.content 里的 image 块 → text 占位（这才是上送载荷本体）
#   toolUseResult.file.base64     → "<stripped>"（本地副本，占近一半体积）
# 保留 media_type / dimensions / originalSize 并写进占位文本，模型仍知道
# 「这里曾有张 2100x2349 的 jpeg」，需要时可以重新 Read 原文件。

_IMG_PLACEHOLDER = "[图片已省略以节省上下文: {meta}]"


def _find_transcript(state_dir: str, session_id: str) -> str | None:
    """在 CLAUDE_CONFIG_DIR/projects/*/<sid>.jsonl 里定位会话转写。

    不自己拼 projects 下的子目录名：那是 claude-code 按它自己看到的 cwd 编码的，
    而 cwd 可能是符号链接解析后的真实路径（本机 /mnt/vol-eltaah12 →
    /mnt/vol-71srnryd），拼错就静默失效。按 session_id glob 最稳。
    """
    root = os.path.join(state_dir, "projects")
    if not session_id or not os.path.isdir(root):
        return None
    # 防路径穿越：session_id 会拼进 glob 模式，含分隔符/上跳的一律不认
    # （与 _state_dirname 同一套口径）。
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        return None
    hits = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def strip_transcript_images(path: str) -> tuple[int, int]:
    """把转写里的 base64 图片载荷换成文本占位。返回 (剥离块数, 省下字节数)。

    原地原子改写；首次剥离时留一份 .bak-imgstrip 滚动备份（同名覆盖，故每个
    会话最多多占一份），便于回滚。无图片时零改动、不产生备份。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return 0, 0
    before = sum(len(x.encode("utf-8")) for x in lines)
    blocks = 0
    out: list[str] = []
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip() or ('"image"' not in s and '"base64"' not in s):
            out.append(line)
            continue
        try:
            o = json.loads(s)
        except ValueError:
            out.append(line)  # 坏行原样保留，绝不因剥离弄坏转写
            continue
        hit = 0
        meta: dict = {}
        tur = o.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("type") == "image":
            f_ = tur.get("file")
            if isinstance(f_, dict):
                meta = {"media_type": f_.get("type"),
                        "dimensions": f_.get("dimensions") or {},
                        "originalSize": f_.get("originalSize")}
                b64 = f_.get("base64")
                if isinstance(b64, str) and len(b64) > 200:
                    f_["base64"] = "<stripped>"
                    hit += 1

        def _ph(m: dict) -> dict:
            bits = [str(m.get("media_type") or "image")]
            d = m.get("dimensions") or {}
            if d.get("originalWidth"):
                bits.append(f"{d['originalWidth']}x{d['originalHeight']}")
            if m.get("originalSize"):
                bits.append(f"{m['originalSize'] / 1000:.0f}KB")
            return {"type": "text",
                    "text": _IMG_PLACEHOLDER.format(meta=" ".join(bits))}

        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for i, b in enumerate(m["content"]):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "image":
                    src = b.get("source") or {}
                    mm = dict(meta)
                    mm.setdefault("media_type", src.get("media_type"))
                    m["content"][i] = _ph(mm)
                    hit += 1
                    continue
                inner = b.get("content")
                if not isinstance(inner, list):
                    continue
                for j, x in enumerate(inner):
                    if isinstance(x, dict) and x.get("type") == "image":
                        src = x.get("source") or {}
                        mm = dict(meta)
                        mm.setdefault("media_type", src.get("media_type"))
                        inner[j] = _ph(mm)
                        hit += 1
        if not hit:
            out.append(line)  # 本行无图片：原样保留，不重排 JSON
            continue
        blocks += hit
        out.append(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n")
    if not blocks:
        return 0, 0
    body = "".join(out)
    saved = before - len(body.encode("utf-8"))
    bak = path + ".bak-imgstrip"
    try:
        if not os.path.exists(bak):
            with open(bak, "w", encoding="utf-8") as f:
                f.writelines(lines)
        tmp = path + ".tmp-imgstrip"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        return 0, 0
    return blocks, max(saved, 0)



class Executor:
    """以无头方式驱动 agent CLI（opencode / claude），逐行解析 JSON 事件流。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def _bin(self) -> str:
        if self.cfg.agent_bin:
            return self.cfg.agent_bin
        return "opencode" if self.cfg.engine == "opencode" else "claude"

    def build_cmd(self, workdir: str, prompt: str, resume_session: str | None) -> list[str]:
        if self.cfg.engine == "opencode":
            cmd = [self._bin(), "run", "--format", "json", "--auto", "--dir", workdir]
            if self.cfg.model:
                cmd += ["-m", self.cfg.model]
            if resume_session:
                cmd += ["-s", resume_session]
            cmd.append(prompt)
            return cmd
        # claude：headless 无审批交互，工具放行统一由本执行器的流式门控兜底
        # （blocked_patterns / dws 二次确认 / rm 白名单），语义对齐 opencode --auto。
        cmd = [self._bin(), "-p", prompt, "--output-format", "stream-json",
               "--verbose", "--dangerously-skip-permissions"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if resume_session:
            cmd += ["--resume", resume_session]
        # 裁掉用不到的内置工具：工具定义占固定上下文的大头（实测 26 工具时
        # 21539 token 里 19788 是 claude-code 自带的系统提示+工具定义），每轮
        # LLM 往返都要重新 prefill 一遍。禁用清单与取舍依据见 config.py 的
        # DEFAULT_CLAUDE_DISALLOWED_TOOLS。
        disallowed = [str(t).strip() for t in (self.cfg.claude_disallowed_tools or []) if str(t).strip()]
        if disallowed:
            # 逗号连成单个 argv，与 avatar exec_local 的既有写法一致
            cmd += ["--disallowedTools", ",".join(disallowed)]
        mcp_cfg = os.path.join(workdir, ".xdg", "claude-mcp.json")
        if os.path.isfile(mcp_cfg):
            cmd += ["--mcp-config", mcp_cfg, "--strict-mcp-config"]
        # 系统级提示词（高管工作法）：append 附加在默认系统提示词之后，
        # 文件缺失则不注入（不阻断任务）。
        sys_prompt = os.path.join(self.cfg.workspace_root, "CEO_PROMPT.md")
        if os.path.isfile(sys_prompt):
            cmd += ["--append-system-prompt-file", sys_prompt]
        return cmd

    def _claude_state_dir(self, user: str) -> str:
        """claude 会话/信任态的持久目录（= CLAUDE_CONFIG_DIR）。

        放 users/<user>/.claude 而非 workdir/home：终态清理会删 home/，
        跨任务 --resume 就断了。build_env 注入环境变量、_strip_resume_images
        定位转写，两处必须同源，故收敛成一个方法。
        """
        return os.path.join(self.cfg.users_root, _state_dirname(user), ".claude")

    def _strip_resume_images(self, resume_session: str, user: str,
                             task_id: int, publish) -> None:
        """resume 前把历史图片载荷换成文本占位（详见 strip_transcript_images）。

        失败一律静默放过：剥离只是优化，绝不能因为它让任务跑不起来。
        """
        try:
            path = _find_transcript(self._claude_state_dir(user), resume_session)
            if not path:
                return
            blocks, saved = strip_transcript_images(path)
            if blocks:
                publish(task_id, t="imgstrip", blocks=blocks,
                        saved_kb=round(saved / 1024))
        except Exception:
            pass

    def build_env(self, workdir: str, xdg_config: str | None = None,
                  user: str = "") -> dict:
        env = _scrubbed_environ()
        env["HOME"] = os.path.join(workdir, "home")
        env["XDG_CONFIG_HOME"] = (xdg_config or self.cfg.xdg_config_home
                                  or os.path.expanduser("~/.config"))
        env["XDG_DATA_HOME"] = os.path.join(workdir, "data")
        if user:
            # 任务归属用户（终端身份）：供下游技能按发起人归属外部调用
            # （如 crude-ai-research-institute 以 HERMES_SESSION_MAP 映射一卡通号）。
            # 显式注入不受 _scrubbed_environ 的 RELAY_* 收敛影响（收敛只滤服务继承环境）。
            env["RELAY_TASK_USER"] = user
        venv_bin = os.path.join(self.cfg.shared_venv
                                or os.path.join(self.cfg.shared_dir, "venv"), "bin")
        if os.path.isdir(venv_bin):
            # 任务级 pip/python 落点：共享 venv 位于可写区（workspace/shared/venv），
            # 裸 pip install 直接可用；系统/服务 venv 在沙箱下只读，装包碰不到中转运行时。
            env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        if self.cfg.engine == "claude":
            # root 下 claude 拒绝 --dangerously-skip-permissions，须声明沙箱环境；
            # 任务 HOME 本就物理隔离（workdir/home），语义相符。
            env["IS_SANDBOX"] = "1"
            # 会话/信任态放持久目录（users/<user>/.claude），躲开终态清理删 home/，
            # 跨任务 --resume 才可用；鉴权与模型走 relay.env 的 ANTHROPIC_* 透传。
            state = self._claude_state_dir(user)
            os.makedirs(state, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = state
            # 单轮 LLM 调用保护。task_timeout 只管整个任务，管不到单轮：实测
            # 20/145 轮是「停顿轮」（输出 <2000 字符却 >60s），吃掉 LLM 总时间
            # 的 35%，最坏一轮单次 API 调用挂 269s 只产出 1150 字符。claude-code
            # 的 API_TIMEOUT_MS 默认 600s，这种停顿完全在阈值内、不触发重试。
            # 取值依据见 config.py 的 api_timeout_ms 注释（合法轮最长 133.5s，
            # 设 120s 会误杀，故取 180s 留 35% 余量）。0/负值＝不设，沿用默认。
            if self.cfg.api_timeout_ms > 0:
                env["API_TIMEOUT_MS"] = str(int(self.cfg.api_timeout_ms))
            if self.cfg.api_first_byte_timeout_ms > 0:
                # 专管「连上了但迟迟不吐首字节」，claude-code 对该原因有独立重试
                # 上限（不占会话预算），内部钳位 [10s, 1800s]。
                env["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"] = str(
                    int(self.cfg.api_first_byte_timeout_ms))
            if self.cfg.api_max_retries >= 0:
                # 名字必须是 CLAUDE_CODE_MAX_RETRIES：MAX_RETRIES 在 claude 二进制
                # 里 0 命中（只作为前者后缀存在），传了等于没传。钳位上限 15。
                env["CLAUDE_CODE_MAX_RETRIES"] = str(
                    min(int(self.cfg.api_max_retries), 15))
        return env

    async def run(self, *, task_id: int, workdir: str, prompt: str,
                  resume_session: str | None, data_dir: str | None,
                  cancel: asyncio.Event, publish,
                  exempt_cmd: str | None = None, user: str = "",
                  on_long=None) -> AgentResult:
        xdg = shared_knowledge.prepare_run(workdir, self.cfg)
        if resume_session and self.cfg.engine == "claude":
            # 必须在 build_cmd 之前：--resume 一旦带上，claude-code 就会把整段
            # 历史（含图片 base64）重放上送。
            self._strip_resume_images(resume_session, user, task_id, publish)
        cmd = self.build_cmd(workdir, prompt, resume_session)
        env = self.build_env(workdir, xdg, user)
        if data_dir:
            env["XDG_DATA_HOME"] = data_dir
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
            start_new_session=True,
            # stream-json 单行可远超 asyncio 默认 64KB(2**16) 缓冲：大工具结果、
            # 长文本块、内联图片都会撑爆，readline() 直接抛
            # ValueError('Separator is found, but chunk is longer than limit')，
            # 任务以 internal error 失败（实测任务 #195）。放宽到 64MB。
            limit=_STREAM_LIMIT,
        )
        parse = _parse_opencode if self.cfg.engine == "opencode" else _parse_claude
        res = AgentResult()
        deadline = time.monotonic() + self.cfg.task_timeout
        stderr_task = asyncio.create_task(proc.stderr.read())
        raw_tail: list[str] = []
        try:
            while True:
                if cancel.is_set():
                    self._kill(proc)
                    res.error = "cancelled"
                    return res
                remaining = deadline - time.monotonic()
                # elapsed 每轮刷新（1 次减法），保证任何 return 路径上都带着真实耗时。
                res.elapsed = self.cfg.task_timeout - remaining
                if remaining <= 0:
                    self._kill(proc)
                    res.error = f"timeout after {self.cfg.task_timeout:.0f}s"
                    res.timed_out = True
                    return res
                # ── 长任务升级判据（运行时，不是提交时预测）──
                # 依据：P(>15m｜已跑到 10m 仍在跑)=75% 精确率、**100% 召回**（历史上
                # 没有任何长任务在 10m 前结束过），只打扰 14.8% 的任务。提交时静态特征
                # 精确率仅 16.9%，要抓 91.7% 召回得标记 60% 的任务，不可用。
                # 热路径成本：每轮 1 次浮点比较 + 1 次 bool 判断，相对同轮 readline()
                # 的 IO 等待（上限 1s）占比 <0.001%；每任务只触发一次回调。
                if (not res.long_flagged
                        and res.elapsed >= self.cfg.long_task_threshold_sec > 0):
                    res.long_flagged = True
                    publish(task_id, t="long", elapsed=round(res.elapsed, 1))
                    if on_long:
                        try:
                            on_long(task_id, res.elapsed)
                        except Exception:
                            pass        # 判据失败绝不能拖垮任务本身
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(1.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                raw_tail.append(text)
                if len(raw_tail) > 20:
                    raw_tail.pop(0)
                parsed = parse(text)
                if not parsed:
                    continue
                if parsed.get("session_id"):
                    res.session_id = parsed["session_id"]
                if parsed.get("text_final"):
                    res.text = parsed["text_final"]
                else:
                    res.text += parsed.get("text", "")
                res.tokens += parsed.get("tokens", 0)
                res.cost += parsed.get("cost", 0.0)
                res.tool_verrs += parsed.get("tool_verr_n", 0)
                aev = parsed.get("agent_event")
                if aev:
                    if aev.get("kind") == "tool":
                        res.tool_calls += 1   # 进展门的活动量信号（见 AgentResult 注释）
                    publish(task_id, t="agent", **aev)
                cmd = parsed.get("tool_cmd")
                if cmd:
                    # 单次调用，原因与判定同源（此前原因被丢弃，见 AgentResult.block_reason）
                    reason = self._is_blocked(cmd, workdir, env)
                    if reason and not self._exempt(cmd, exempt_cmd):
                        self._kill(proc)
                        res.blocked_cmd = cmd
                        res.block_reason = reason
                        # 类别编码进 error：`blocked:<cat>: <cmd>`。**向后兼容**——
                        # 仍以 "blocked:" 开头，故 run_task 的判定与既有测试都不用改；
                        # 分身侧按 ":" 切三段即可拿到类别，用业务语言转述给超管，
                        # 而不必把 shell 原文甩给业务用户。不新增 DB 列（不迁移）。
                        res.error = f"blocked:{block_category(reason)}: {cmd[:200]}"
                        return res
        finally:
            if proc.returncode is None:
                self._kill(proc)
            await proc.wait()
            err = (await stderr_task).decode("utf-8", "replace")
            # 放在 finally 里：timeout/cancelled/blocked 都是**提前 return**，
            # 写在下面会让这些路径拿不到退出码，上层就无法识别"被信号打断"。
            res.exit_code = int(proc.returncode or 0)
        res.raw_tail = "\n".join(raw_tail[-5:])
        if res.error is None and proc.returncode != 0:
            tail = (err or res.raw_tail)[-800:]
            # 信号终止时先点名信号。#149 实测：rc=143（SIGTERM，部署重启所杀），
            # 但 stderr 末行恰好是无关的 [claude-code:unrecognized_model]，被顶成
            # 错误主因，排查直接跑偏。tail 仍保留（可能藏着真因），只是不再打头阵。
            sig = signal_name(proc.returncode)
            head = f"agent exited with code {proc.returncode}"
            if sig:
                head += f"（{sig} 终止，非 agent 自身报错）"
            res.error = f"{head}: {tail}"
        return res

    def _is_blocked(self, cmd: str, workdir: str = "",
                    dws_env: dict | None = None) -> str | None:
        low = cmd.lower()
        for p in self.cfg.blocked_patterns:
            if not p:
                continue
            try:
                if re.search(p, low):
                    return p
            except re.error:
                if p.lower() in low:  # 非法正则退化为子串
                    return p
        confirm = self._confirm_violation(cmd, workdir, dws_env)
        if confirm:
            return confirm
        return self._rm_violation(cmd, workdir)

    # ---- dws 消息发送：敏感接收人 / 多接收人 / 群聊目标 → 需人工二次确认 ----
    # 命中后走既有 blocked → pending_approval → approve → resume(exempt) 流程，无新机制。

    @staticmethod
    def _dws_send_target(segment: str) -> tuple[tuple[str, ...], list[str], list[str]] | None:
        """解析命令段。若是 dws 发送命令，返回 (子命令路径, 单人接收 tokens, 群/会话 tokens)。

        非发送命令（含 dws 的只读命令、路径里带 dws 字的文件操作）返回 None。
        """
        try:
            tokens = shlex.split(segment)
        except ValueError:  # 引号不闭合等退化为空白切分，取值时再手动去引号
            tokens = segment.split()
        for i, t in enumerate(tokens):
            if t != "dws" and not t.endswith("/dws"):
                # bash -c "dws chat +dm ..." 之类嵌套：对含空白的内层命令串递归解析
                if "dws" in t and re.search(r"\s", t):
                    nested = Executor._dws_send_target(t)
                    if nested:
                        return nested
                continue
            path: list[str] = []
            j = i + 1
            while j < len(tokens) and not tokens[j].startswith("-"):
                path.append(tokens[j])
                j += 1
            key = tuple(path)
            if key not in _DWS_SEND_PATHS:
                continue
            skip = _DWS_NON_TARGET_FLAGS.get(key, set())
            persons: list[str] = []
            groups: list[str] = []
            while j < len(tokens):
                t = tokens[j]
                j += 1
                if not t.startswith("--"):
                    continue
                name, eq, val = t.partition("=")
                if name in skip:
                    continue
                is_person, is_group = name in _DWS_PERSON_FLAGS, name in _DWS_GROUP_FLAGS
                if not is_person and not is_group:
                    continue
                if not eq:
                    if j < len(tokens) and not tokens[j].startswith("-"):
                        val = tokens[j]
                        j += 1
                    else:
                        continue
                for v in re.split(r"[,\s]+", val.strip("\"'")):
                    if v:
                        (persons if is_person else groups).append(v)
            return key, persons, groups
        return None

    def _confirm_violation(self, cmd: str, workdir: str = "",
                           dws_env: dict | None = None) -> str | None:
        for segment in re.split(r"[;&|\n]+", cmd):
            if "dws" not in segment:
                continue
            parsed = self._dws_send_target(segment)
            if not parsed:
                continue
            key, persons, groups = parsed
            target = " ".join(key)
            for r in persons + groups:  # 整 token 精确相等，"1" 不会命中 "10"/"37505774"
                if r in self.cfg.confirm_recipients:
                    return f"dws {target} 接收人命中敏感对象 '{r}'，需二次确认"
            if len(persons) >= 2:
                return (f"dws {target} 多接收人（{len(persons)} 人: "
                        f"{','.join(persons[:5])}），需二次确认")
            if not groups:
                continue
            # 会话 cid 二次判定：dws 里 1:1 单聊与群聊都用 --chat-id/--conversation-id 传 cid，
            # 须按会话类型区分——单聊放行、群聊拦截（见 _cid_is_single_chat）。
            # 群名/群参数（--group 名、--chat-query、--groups 多群、--webhook-token 等）
            # 语义明确是群，直接视为群发拦截，不查类型。
            cid_vals = [g for g in groups if _CID_RE.match(g) and "=" in g]
            non_cid = [g for g in groups if g not in cid_vals]
            if non_cid:
                return f"dws {target} 目标为群聊（{','.join(non_cid[:3])}），需二次确认"
            all_single = all(self._cid_is_single_chat(c, dws_env) for c in cid_vals)
            if all_single:
                continue  # 1:1 单聊：放行（敏感接收人仍由上面精确匹配拦截）
            return f"dws {target} 目标为群聊会话（{','.join(cid_vals[:3])}），需二次确认"
        return None

    def _cid_is_single_chat(self, cid: str, dws_env: dict | None) -> bool:
        """判定会话 cid 是否为 1:1 单聊（True）。群聊/解析失败返回 False（保守，拦截）。

        通过 `dws chat +conversation-info --group <cid>` 读 singleChat 字段（只读、幂等）。
        复用任务自身运行环境（HOME/XDG/凭证）调用 dws——与任务发送用同一身份；
        进程内按 cid 缓存复用（cid 即会话唯一标识，类型是其固有属性）。
        """
        if cid in _CID_INFO_CACHE:
            return _CID_INFO_CACHE[cid][0]
        info = self._dws_conversation_info(cid, dws_env)
        if info is None:
            return False  # 解析失败：放行失败=拦截（fail-closed）
        single, title = info
        _CID_INFO_CACHE[cid] = (single, title)
        return single

    def _dws_conversation_info(self, cid: str,
                               dws_env: dict | None) -> tuple[bool, str] | None:
        """调用 dws 取会话类型。返回 (singleChat, title)；失败/非预期返回 None。"""
        env = dict(dws_env) if dws_env else dict(os.environ)
        try:
            p = subprocess.run(
                ["dws", "chat", "+conversation-info", "--group", cid, "--format", "json"],
                capture_output=True, text=True, timeout=20, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        try:
            data = json.loads(p.stdout)
        except (ValueError, AttributeError):
            return None
        ci = ((data or {}).get("result") or {}).get("conversationInfo")
        if not isinstance(ci, dict) or "singleChat" not in ci:
            return None
        return (bool(ci.get("singleChat")), str(ci.get("title") or ""))

    # ---- 递归 rm 按路径判断：rm_safe_prefixes 与任务自身 HOME 沙箱内放行，其余拦截 ----

    @staticmethod
    def _rm_targets(segment: str) -> tuple[bool, list[str]]:
        """从命令段中提取 rm 的参数。返回 (是否递归, 目标路径列表)。"""
        m = re.search(r"(?<![\w-])rm\b\s*([^\n;&|]*)", segment)
        if not m:
            return False, []
        recursive, paths = False, []
        for a in m.group(1).split():
            if a == "--recursive":
                recursive = True
            elif a.startswith("--"):
                continue
            elif a.startswith("-") and len(a) > 1:
                if "r" in a[1:].lower():
                    recursive = True
            else:
                paths.append(a)
        return recursive, paths

    @staticmethod
    def _resolve_rm_path(p: str, home: str) -> str:
        """去引号、把开头的 ~/$HOME/${HOME} 展开为任务真实 HOME，再规范化（防 .. 逃逸）。"""
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        if home:
            if p in ("~", "$HOME", "${HOME}"):
                p = home
            elif p.startswith("~/"):
                p = home + p[1:]
            elif p.startswith("$HOME/"):
                p = home + p[len("$HOME"):]
            elif p.startswith("${HOME}/"):
                p = home + p[len("${HOME}"):]
        return os.path.normpath(p) if p.startswith("/") else p

    def _rm_path_safe(self, p: str, home: str = "", workdir: str = "") -> bool:
        p = self._resolve_rm_path(p, home)
        if not p.startswith("/"):  # 相对路径/无法展开的变量：无法验证，拦截
            return False
        for root in self.cfg.rm_safe_prefixes:
            if p == root or p.startswith(root + "/"):
                return True
        if home and (p == home or p.startswith(home + "/")):
            return True  # 任务自身 HOME 沙箱（物理隔离），触不到共享区/系统目录
        # 任务自身 workdir 树（output/ work/ data/ 等）：删自己的中间件只伤本任务，
        # 触不到 shared/ 与系统目录。此前只放行 <workdir>/home，于是「把产物 mv 到
        # 共享区后 rm -rf 自己的 work/shared 收尾」这种正常清理也要去问人（#259）。
        # _resolve_rm_path 已 normpath，`workdir/../../shared` 会先被折叠成 shared/
        # 从而不匹配本前缀，故 .. 逃逸挡得住。
        if workdir and (p == workdir or p.startswith(workdir + "/")):
            return True
        return False

    def _rm_violation(self, cmd: str, workdir: str = "") -> str | None:
        home = os.path.join(workdir, "home") if workdir else ""
        segments = re.split(r"[;&|\n]+", cmd)
        for idx, segment in enumerate(segments):
            recursive, paths = self._rm_targets(segment)
            if not recursive:
                continue
            for p in paths:
                if self._rm_path_safe(p, home, workdir):
                    continue
                # 惰性 cd 回溯：**只有**走到这里（按绝对路径判不安全）且目标是相对
                # 路径时才付费扫描前缀段。绝大多数命令在上一行就 continue 了，
                # 故热路径零额外开销。修 #121：`cd /tmp && rm -rf syncpkg` 里
                # /tmp 本属安全前缀，只因相对路径没解析就被拦。
                #
                # 但含 `$`／反引号／引号的目标**一律不做 cd 解析、直接拦**：这类值
                # 静态展开不了，拼进 cwd 会得到 `<workdir>/"$HOME"/x` 这种以
                # workdir 开头的字面路径，被"自身 workdir 树安全"规则误放行
                # （2026-09-29 对抗测试实测到的洞）。
                if not any(c in p for c in "$`\"'") \
                        and not p.startswith(("/", "~")):
                    cwd = self._cwd_at(segments, idx, workdir)
                    if cwd and self._rm_path_safe(
                            os.path.join(cwd, p), home, workdir):
                        continue
                return f"recursive rm outside safe dirs: {p}"
        return None

    @staticmethod
    def _cwd_at(segments: list[str], idx: int, workdir: str) -> str:
        """回溯 segments[:idx] 里最后一次 cd 的目标。仅相对路径 rm 时调用（惰性）。

        返回 ""＝cwd 不可知，调用方必须拦截。**含 `$`／反引号的 cd 目标一律视为
        不可知**：`KEY=/…/shared; cd "$KEY"; rm -rf reports` 里 $KEY 静态展开不了，
        若把字面量 "$KEY" 拼进 workdir，会得到 `<workdir>/$KEY/reports`——它以
        workdir 开头，于是被"自身 workdir 树安全"规则放行，而真实目标是
        `shared/reports`。这是 2026-09-29 改 Tier 1 时实测踩到的洞（#217/#222）。
        """
        cwd = workdir or ""
        for seg in segments[:idx]:
            m = _CD_RE.search(seg)
            if not m:
                continue
            t = m.group(1).strip("\"'")
            if not t or "$" in t or "`" in t:
                return ""          # 不可静态展开 → cwd 不可知
            cwd = t if t.startswith("/") else os.path.normpath(
                os.path.join(cwd or ".", t))
        return cwd

    @staticmethod
    def _send_signatures(cmd: str) -> set[str]:
        """提取命令里各 dws 发送段的签名（发送路径 + 目标值）。

        用于审批豁免匹配：agent resume 重发同一条发送时可能重排格式
        （加 --yes、改 | head N），整串相等会漏判；按"发送目标"签名匹配更稳。
        """
        sigs: set[str] = set()
        for segment in re.split(r"[;&|\n]+", cmd):
            if "dws" not in segment:
                continue
            parsed = Executor._dws_send_target(segment)
            if not parsed:
                continue
            key, persons, groups = parsed
            path = " ".join(key)
            for v in persons:
                sigs.add(f"{path} {v}")
            for v in groups:
                sigs.add(f"{path} {v}")
        return sigs

    @staticmethod
    def _exempt(cmd: str, exempt_cmd: str | None) -> bool:
        if not exempt_cmd:
            return False
        c, e = cmd.strip().lower(), exempt_cmd.strip().lower()
        cs, es = Executor._send_signatures(cmd), Executor._send_signatures(exempt_cmd)
        if cs and es:  # dws 发送：按发送目标签名匹配（容忍 resume 重排格式）
            return bool(cs & es)
        return c == e or c.startswith(e)  # 非 dws 拦截（rm/mkfs 等）：整串匹配兜底

    @staticmethod
    def _tree_pids(root_pid: int) -> list[int]:
        """root_pid 及其全部后代（agent 的工具子进程可能自成进程组，须整树杀）。"""
        children: dict[int, list[int]] = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat", "rb") as f:
                    data = f.read().decode("ascii", "replace")
                ppid = int(data.rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(d))
        out: list[int] = []
        stack = [root_pid]
        while stack:
            p = stack.pop()
            out.append(p)
            stack.extend(children.get(p, []))
        return out

    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        pids = self._tree_pids(proc.pid)
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        async def _escalate() -> None:
            await asyncio.sleep(5)
            for p in pids:
                try:
                    os.kill(p, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        try:
            asyncio.get_running_loop().create_task(_escalate())
        except RuntimeError:
            pass
