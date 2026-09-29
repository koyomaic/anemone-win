from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field

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
        out["agent_event"] = {
            "kind": "tool",
            "tool": part.get("tool", ""),
            "title": part.get("title", ""),
        }
        if part.get("tool") == "bash":
            state = part.get("state") or {}
            inp = part.get("input") or state.get("input") or {}
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
                out["agent_event"] = {"kind": "tool", "tool": block.get("name", "")}
                if str(block.get("name", "")).lower() == "bash":
                    inp = block.get("input") or {}
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
        tok = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
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
        mcp_cfg = os.path.join(workdir, ".xdg", "claude-mcp.json")
        if os.path.isfile(mcp_cfg):
            cmd += ["--mcp-config", mcp_cfg, "--strict-mcp-config"]
        # 系统级提示词（高管工作法）：append 附加在默认系统提示词之后，
        # 文件缺失则不注入（不阻断任务）。
        sys_prompt = os.path.join(self.cfg.workspace_root, "CEO_PROMPT.md")
        if os.path.isfile(sys_prompt):
            cmd += ["--append-system-prompt-file", sys_prompt]
        return cmd

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
            state = os.path.join(self.cfg.users_root, _state_dirname(user), ".claude")
            os.makedirs(state, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = state
        return env

    async def run(self, *, task_id: int, workdir: str, prompt: str,
                  resume_session: str | None, data_dir: str | None,
                  cancel: asyncio.Event, publish,
                  exempt_cmd: str | None = None, user: str = "") -> AgentResult:
        xdg = shared_knowledge.prepare_run(workdir, self.cfg)
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
                if remaining <= 0:
                    self._kill(proc)
                    res.error = f"timeout after {self.cfg.task_timeout:.0f}s"
                    return res
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
                if parsed.get("agent_event"):
                    publish(task_id, t="agent", **parsed["agent_event"])
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
        res.raw_tail = "\n".join(raw_tail[-5:])
        if res.error is None and proc.returncode != 0:
            tail = (err or res.raw_tail)[-800:]
            res.error = f"agent exited with code {proc.returncode}: {tail}"
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
