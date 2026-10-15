from __future__ import annotations

import json
import os
import re
import shutil

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")


def slug_valid(name: str) -> bool:
    return bool(SKILL_NAME_RE.match(name or ""))


def strip_jsonc(text: str) -> str:
    """去掉 // 与 /* */ 注释（感知字符串，不伤 URL），并清理 } ] 前的尾逗号。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def load_jsonc(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.loads(strip_jsonc(f.read()))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def mcp_entry_from_file(name: str, obj: object) -> dict:
    """shared/mcp/<name>.json 的内容：条目本身（{"type":"local",...}）
    或 {name: 条目} / {任意单键: 条目} 包装形式。"""
    if not isinstance(obj, dict):
        return {}
    if set(obj) == {name}:
        v = obj[name]
        return v if isinstance(v, dict) else {}
    if len(obj) == 1:
        v = next(iter(obj.values()))
        return v if isinstance(v, dict) else {}
    return obj


def load_shared_mcp(shared_dir: str) -> dict:
    """读取 shared/mcp/*.json → {server_name: mcp条目}。"""
    root = os.path.join(shared_dir, "mcp")
    out: dict = {}
    if not os.path.isdir(root):
        return out
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".json"):
            continue
        name = fn[:-5]
        try:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError):
            continue
        entry = mcp_entry_from_file(name, obj)
        if entry:
            out[name] = entry
    return out


# 注入 CLAUDE.md/AGENTS.md 时从团队记忆滤掉的「运行时元数据」：附录／溯源／度量口径。
# 它们对 agent 执行任务无约束力，却每轮随上下文重放——口径与红线.md 59KB 里光附录
# （附一抽取对应表／附二续补登记／附三版本记录）就占 13KB，实测每任务 CLAUDE.md
# 61.5KB≈2 万 token 起步（含只读复核这类 600-token 小任务）。只删确定无约束力的部分：
# `## 附*` 整节、`溯源:`／`度量口径` 行；`若→则`／红线／EFF 纪律逐字保留。
# 完整原文仍在 shared/memory/，agent 需要时按文末指引 Read。
#
# 为什么在「注入时过滤」而不是把附录拆成单独文件：shared/memory/ 不经 store 索引、
# 也不随 git 传播（仅 dashboard 逐机上传，见 relay-service main.py 的 shared_memory_upload），
# 机队每台各存一份本地卷。拆出新持久文件就得逐机重新上传、还可能与 store/wiki 血缘打架；
# 而过滤逻辑是 CODE，走 relay-dev→boot.sh 天然全机队一致生效，原文保持单一权威件不动。
_MEM_DROP_SECTION_RE = re.compile(r"^##\s*附")
_MEM_DROP_LINE_RE = re.compile(r"^\s*[-*·]?\s*\**\s*(溯源|度量口径)")


def _slim_memory_body(body: str) -> tuple[str, bool]:
    """滤掉记忆正文里的运行时元数据，返回 (瘦身后正文, 是否有删减)。

    删三类（均无任务约束力）：①第一个 `## ` 规则节之前的 `>` 前言块引用
    （版本线／来源／日期／修订留痕等变更日志）；②`## 附*` 整节（抽取对应表／
    续补登记／版本记录）；③行首 `溯源:`／`度量口径` 行。规则节内的 `>` 块引用
    保留（可能是示例），`若→则`／红线／EFF 纪律逐字不动。
    """
    out: list[str] = []
    dropping = False        # 处于被删的 ## 附* 节内
    seen_section = False    # 是否已到第一个 ## 规则节（其前的 > 块是前言元数据）
    dropped = False
    for ln in body.split("\n"):
        if ln.startswith("## "):
            seen_section = True
            dropping = bool(_MEM_DROP_SECTION_RE.match(ln))
            if dropping:
                dropped = True
                continue
        if dropping:
            dropped = True
            continue
        if not seen_section and ln.lstrip().startswith(">"):
            dropped = True
            continue
        if _MEM_DROP_LINE_RE.match(ln):
            dropped = True
            continue
        out.append(ln)
    slim = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return slim, dropped


def shared_memory_text(shared_dir: str) -> str | None:
    """拼接 shared/memory/*.md（滤掉附录/溯源/度量等运行时元数据）；为空返回 None。

    瘦身理由与「为何在注入时过滤而非拆文件」见 _MEM_DROP_SECTION_RE 注释。被滤掉的
    完整原文仍在 shared/memory/（机队每台本地都有），文末附一行指引让 agent 按需 Read，
    故无需新建任何要跨机同步的派生文件。
    """
    root = os.path.join(shared_dir, "memory")
    if not os.path.isdir(root):
        return None
    parts: list[str] = []
    slimmed: list[str] = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".md"):
            continue
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                body = f.read().strip()
        except OSError:
            continue
        if not body:
            continue
        slim, dropped = _slim_memory_body(body)
        if not slim:
            continue
        parts.append(f"## [{fn}]\n{slim}")
        if dropped:
            slimmed.append(fn)
    if not parts:
        return None
    text = ("# 团队共享记忆（来自 shared/memory/，全员 agent 自动加载，上传即生效）\n\n"
            + "\n\n".join(parts))
    if slimmed:
        text += ("\n\n---\n（注：以上为可执行核心；"
                 + "、".join(f"`shared/memory/{fn}`" for fn in slimmed)
                 + " 的附录／溯源／度量口径等元数据为省上下文已略去，"
                 "需要完整原文时 Read 对应文件。）")
    return text


def _sync_symlink(dst: str, src: str) -> None:
    if os.path.islink(dst):
        if os.readlink(dst) == src:
            return
        os.unlink(dst)
    elif os.path.exists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst, ignore_errors=True)
        try:
            os.unlink(dst)
        except OSError:
            pass
    if os.path.exists(src):
        os.symlink(src, dst)


def _symlink_dws_seed(workdir: str, shared_dir: str) -> None:
    """把任务 HOME 的 dws 凭证目录软链到共享种子，所有会话共用同一份活凭证。

    单一登录态 lineage：dws 刷新 token 会轮换 refresh token，软链让轮换原地
    发生在种子上（服务器 dws-renew 定时器从种子续期并回写种子），杜绝复制
    副本分叉后互相失效、被迫人工浏览器重新授权。

    - 种子不存在（shared/secrets/dws-cli-seed/dws-cli/）则静默跳过，不影响任务
    - dst 已存在（旧复制模式的真实目录或旧软链）则删除后重建软链；
      任务目录是临时的，无需备份
    """
    seed = os.path.join(shared_dir, "secrets", "dws-cli-seed", "dws-cli")
    if not os.path.isdir(seed):
        return
    share = os.path.join(workdir, "home", ".local", "share")
    dst = os.path.join(share, "dws-cli")
    try:
        os.makedirs(share, exist_ok=True)
        if os.path.islink(dst) or os.path.exists(dst):
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.unlink(dst)
        os.symlink(os.path.abspath(seed), dst)
    except OSError:
        pass


def _claude_mcp_entry(entry: dict) -> dict:
    """opencode MCP 条目 → claude .mcp.json 条目。

    local:  {"type":"local","command":[bin,arg...],"environment":{...}}
            → {"command":bin,"args":[arg...],"env":{...}}
    remote: {"type":"remote","url":...} → {"type":"http","url":...}
    无法识别返回 {}（跳过该服务器）。
    """
    if not isinstance(entry, dict):
        return {}
    et = str(entry.get("type") or "").lower()
    cmd = entry.get("command")
    if et == "remote" or (not cmd and entry.get("url")):
        url = entry.get("url")
        return {"type": "http", "url": str(url)} if url else {}
    if isinstance(cmd, str):
        cmd = [cmd]
    if not isinstance(cmd, list) or not cmd:
        return {}
    out: dict = {"command": str(cmd[0]), "args": [str(a) for a in cmd[1:]]}
    env = entry.get("environment") or entry.get("env")
    if isinstance(env, dict) and env:
        out["env"] = {str(k): str(v) for k, v in env.items()}
    return out


def _prepare_claude(workdir: str, cfg) -> str:
    """claude 引擎的共享知识注入，返回 XDG 目录（与 opencode 分支同契约）。

    - shared/skills/<name>（含 SKILL.md 者）→ workdir/.claude/skills/<name> 软链
      （项目级技能，claude 在 cwd 自动发现）
    - shared/mcp/*.json（opencode 条目格式）→ workdir/.xdg/claude-mcp.json
      （claude mcpServers 格式，executor 以 --mcp-config 显式加载）
    - shared/memory/*.md → workdir/CLAUDE.md（claude 项目记忆，cwd 自动加载）
    - 鉴权/模型不经文件：由 relay.env 的 ANTHROPIC_* 走进程 env 透传；
      会话态由 executor 设 CLAUDE_CONFIG_DIR 持久化（不在 workdir，躲开终态清理）
    """
    xdg = os.path.join(workdir, ".xdg")
    os.makedirs(xdg, exist_ok=True)
    os.makedirs(os.path.join(workdir, "home"), exist_ok=True)

    skills_root = os.path.join(cfg.shared_dir, "skills")
    if os.path.isdir(skills_root):
        dst_root = os.path.join(workdir, ".claude", "skills")
        os.makedirs(dst_root, exist_ok=True)
        for name in sorted(os.listdir(skills_root)):
            src = os.path.join(skills_root, name)
            if os.path.isdir(src) and os.path.isfile(os.path.join(src, "SKILL.md")):
                _sync_symlink(os.path.join(dst_root, name), src)

    servers: dict = {}
    for name, entry in load_shared_mcp(cfg.shared_dir).items():
        conv = _claude_mcp_entry(entry)
        if conv:
            servers[name] = conv
    if servers:
        with open(os.path.join(xdg, "claude-mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f, ensure_ascii=False, indent=2)

    _symlink_dws_seed(workdir, cfg.shared_dir)

    # 团队约定（workspace/AGENTS.md：dws 用法/共享区规则/拦截边界）+ 团队记忆
    # 合并进 CLAUDE.md，claude 在 cwd 自动加载——与 opencode 引擎的 AGENTS.md 约定对齐。
    parts = []
    agents = os.path.join(cfg.workspace_root, "AGENTS.md") \
        if cfg.workspace_root else ""
    if agents and os.path.isfile(agents):
        with open(agents, encoding="utf-8") as f:
            parts.append(f.read().rstrip() + "\n")
    mem = shared_memory_text(cfg.shared_dir)
    if mem:
        parts.append(mem.rstrip() + "\n")
    if parts:
        with open(os.path.join(workdir, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
    return xdg


def prepare_run(workdir: str, cfg) -> str:
    """为本次运行生成隔离的 XDG 配置目录（共享知识注入点），返回新 XDG_CONFIG_HOME。

    - opencode.json = 全局基线配置 + skills.paths 追加 shared/skills + mcp 合并 shared/mcp
    - 全局配置目录里的 node_modules/package*.json 等以符号链接复用（不重装依赖）
    - shared/memory/*.md 拼成 AGENTS.md 写入 workdir 与 home 全局位（双保险加载）
    - dws 凭证种子软链进任务 HOME（全会话共用一份活凭证；种子缺失则跳过）
    """
    if getattr(cfg, "engine", "opencode") == "claude":
        return _prepare_claude(workdir, cfg)
    base_xdg = cfg.xdg_config_home or os.path.expanduser("~/.config")
    base_pkg = os.path.join(base_xdg, "opencode")
    xdg = os.path.join(workdir, ".xdg")
    pkg = os.path.join(xdg, "opencode")
    os.makedirs(pkg, exist_ok=True)

    base_cfg: dict = {}
    for fn in ("opencode.json", "opencode.jsonc"):
        p = os.path.join(base_pkg, fn)
        if os.path.isfile(p):
            base_cfg = load_jsonc(p)
            break

    # 剔除本地专用 provider（如本地才用的模型），但默认模型所在的 provider 永不剔除
    exclude = {str(p).strip() for p in getattr(cfg, "relay_exclude_providers", []) if str(p).strip()}
    default_model = str(base_cfg.get("model") or "")
    if "/" in default_model:
        exclude.discard(default_model.split("/", 1)[0])
    if exclude:
        provs = base_cfg.get("provider")
        if isinstance(provs, dict):
            for name in list(provs.keys()):
                if name in exclude:
                    del provs[name]

    skills = dict(base_cfg.get("skills") or {})
    paths = skills.get("paths")
    if not isinstance(paths, list):
        paths = []
    skills_root = os.path.join(cfg.shared_dir, "skills")
    if skills_root not in paths:
        paths.append(skills_root)
    skills["paths"] = paths
    base_cfg["skills"] = skills

    mcp = dict(base_cfg.get("mcp") or {})
    mcp.update(load_shared_mcp(cfg.shared_dir))
    if mcp:
        base_cfg["mcp"] = mcp

    with open(os.path.join(pkg, "opencode.json"), "w", encoding="utf-8") as f:
        json.dump(base_cfg, f, ensure_ascii=False, indent=2)

    if os.path.isdir(base_pkg):
        for entry in os.listdir(base_pkg):
            if entry in ("opencode.json", "opencode.jsonc"):
                continue
            _sync_symlink(os.path.join(pkg, entry), os.path.join(base_pkg, entry))

    _symlink_dws_seed(workdir, cfg.shared_dir)

    mem = shared_memory_text(cfg.shared_dir)
    if mem:
        for p in (os.path.join(workdir, "AGENTS.md"),
                  os.path.join(workdir, "home", ".config", "opencode", "AGENTS.md")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(mem + "\n")
    return xdg
