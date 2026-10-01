#!/usr/bin/env python3
"""app/migrate.py —— im-bridge → relay-avatar 状态迁移（provision 调用，幂等）。

迁移内容：
1. bridge.env → shared/secrets/relay-avatar.env（600；补 AVATAR_DASHBOARD_TOKEN 随机生成）
2. config.json 白/黑名单与 self_open_id → policy.toml（首装从模板渲染，已有不覆盖）
2b. 已存在的 policy.toml：local_sandbox_net_allow 置空（沙箱出网不限制，机队统一）
3. state.json / rounds/ / events.ndjson → $WORKSPACE/avatar/（schema 兼容，直接拷贝）
4. 写 integrity 基线 manifest
旧目录只读不删（历史留痕）；重复执行安全。
"""
from __future__ import annotations

import argparse
import json
import secrets as pysecrets
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WS = Path("/mnt/vol-eltaah12/workspace")
BASE = Path(__file__).resolve().parent.parent

# 迁移 2b 写入各机 policy.toml 的标准注释（替换原「刻意不给公网」那段）。
# 与 policy.example.toml 同名键注释保持同一口径，避免机队内文档分叉。
_NET_OPEN_NOTE = (
    "# 沙箱网络白名单（IP/CIDR）。非空即施加 IPAddressDeny=any + IPAddressAllow=本列表；\n"
    "# **留空＝不施加任何 IP 规则＝出网完全不限制**（exec_local._sandbox_wrap 的 `if net:`\n"
    "# 守卫；语义由 test_sandbox_wrap_no_ip_rules_when_allowlist_empty 锁定）。\n"
    "# 2026-10-01 用户指示：沙箱访问外部网络不限制，机队统一置空。原白名单只放行 claude\n"
    "# 代理与 PG，把 oil-research-data（https://10.200.6.87/api/v1/research/query）等业务\n"
    "# 数据源堵死在沙箱内（实测 HTTP 000，宿主同目标 443 正常）；且 IPAddressAllow 无端口\n"
    "# 粒度、每接一个新数据源都要改配置+重启，逐个加白不可持续。\n"
    "# 代价：Bash 对 L2+ 开放 + 出网不限 ⇒ 沙箱内可读到的数据理论上可被外发。剩余防线为\n"
    "# local_sandbox_hide（relay.env / shared/secrets / /root 整体不可见）、ProtectSystem=\n"
    "# strict、NoNewPrivileges、CapabilityBoundingSet 清空、三层写域收窄。\n"
    "# 如需收紧：填回 127.0.0.1、10.189.21.235 及业务主机后重启 avatar（claude 代理与 PG\n"
    "# 必须在列，否则 claude 起不来、store 连不上库）。\n"
)

# 迁移 2b 写入各机 policy.toml 的标准注释（替换原「刻意不给公网／不要留空」块，
# 否则注释会与置空后的值自相矛盾）。措辞与 policy.example.toml 同名键保持一致。
_NET_OPEN_NOTE = (
    "# 沙箱网络白名单（IP/CIDR）。非空即施加 IPAddressDeny=any + IPAddressAllow=本列表；\n"
    "# **留空＝不施加任何 IP 规则＝出网完全不限制**（exec_local._sandbox_wrap 的 `if net:`\n"
    "# 守卫；语义由 test_sandbox_wrap_no_ip_rules_when_allowlist_empty 锁定）。\n"
    "# 2026-10-01 用户指示：沙箱访问外部网络不限制，机队统一置空（migrate.py 步骤 2b）。\n"
    "# 动因：原白名单只放行 claude 代理与 PG，把 oil-research-data 等业务数据源堵死在\n"
    "# 沙箱内（实测 HTTP 000）；且 IPAddressAllow 无端口粒度、每接一个新数据源都要改\n"
    "# 配置+重启，不可持续。代价与剩余防线见 policy.example.toml 同名键注释。\n"
    "# 如需收紧：填回 127.0.0.1、10.189.21.235 及业务主机后重启 avatar——claude 代理与\n"
    "# PG 必须在列，否则 claude 起不来、store 连不上库。\n"
)


def migrate(ws: Path, dry: bool = False) -> dict:
    from app import integrity
    old = ws / "im-bridge"
    state = ws / "avatar"
    secrets_dir = ws / "shared" / "secrets"
    env_dst = secrets_dir / "relay-avatar.env"
    report = {"state_dir": str(state), "env": str(env_dst), "steps": []}

    for d in (state, state / "run", state / "run" / "staging",
              state / "rounds", state / "audit"):
        d.mkdir(parents=True, exist_ok=True)
    try:
        state.chmod(0o700)
    except Exception:
        pass

    # 1. env 凭据
    if not env_dst.exists():
        old_env = old / "bridge.env"
        lines = []
        if old_env.exists():
            lines = [l.rstrip("\n") for l in open(old_env, encoding="utf-8")
                     if l.strip() and not l.strip().startswith("#")]
            report["steps"].append("env: 迁移自 bridge.env")
        have = {l.split("=", 1)[0] for l in lines if "=" in l}
        if "AVATAR_DASHBOARD_TOKEN" not in have:
            lines.append(f"AVATAR_DASHBOARD_TOKEN={pysecrets.token_hex(16)}")
        if "AVATAR_DASHBOARD_PASSWORD" not in have:
            lines.append(f"AVATAR_DASHBOARD_PASSWORD={pysecrets.token_hex(8)}")
        header = ["# relay-avatar 凭据（migrate.py 生成，600，不进 git）"]
        if not dry:
            secrets_dir.mkdir(parents=True, exist_ok=True)
            env_dst.write_text("\n".join(header + lines) + "\n",
                               encoding="utf-8")
            env_dst.chmod(0o600)
        report["steps"].append(f"env: 写入 {env_dst}")
    else:
        report["steps"].append("env: 已存在，跳过")

    # 2. policy.toml
    policy = state / "policy.toml"
    if not policy.exists():
        text = (BASE / "policy.example.toml").read_text(encoding="utf-8")
        cfgj = {}
        if (old / "config.json").exists():
            try:
                cfgj = json.loads((old / "config.json").read_text("utf-8"))
            except Exception:
                cfgj = {}
        per = cfgj.get("personal", {})
        wl = per.get("whitelist", [])
        bl = per.get("blacklist", [])
        sid = cfgj.get("self_open_id", "")
        if wl:
            text = text.replace("whitelist = []",
                                "whitelist = " + json.dumps(wl), 1)
        if bl:
            text = text.replace("blacklist = []",
                                "blacklist = " + json.dumps(bl), 1)
        if sid:
            import re
            text = re.sub(r'self_open_id = "[^"]*"',
                          f'self_open_id = "{sid}"', text, count=1)
        if not dry:
            policy.write_text(text, encoding="utf-8")
        report["steps"].append(
            f"policy: 从模板渲染（whitelist={len(wl)} blacklist={len(bl)}）")
    else:
        report["steps"].append("policy: 已存在，跳过")

    # 2b. 沙箱出网白名单置空（2026-10-01 用户指示：沙箱访问外部网络不限制）。
    # 为什么必须有这一步：provision.sh 只 `cp -f policy.example.toml` 到 avatar
    # 目录，**从不覆盖各机 live $AVA_STATE/policy.toml**（上面第 2 步对已存在的
    # 文件也是"已存在，跳过"），故改模板对已装机队完全无效。本步骤是唯一能自动
    # 传播到全机队的载体——provision 每次都跑 migrate.py；且改 app/ 本身构成代码
    # 漂移，provision 会随之自动 restart avatar，新配置当场生效。
    # 幂等：已是 [] 则不写盘、不动 mtime、不覆盖首次备份。
    if policy.exists():
        import re
        ptext = policy.read_text(encoding="utf-8")
        m = re.search(r"^local_sandbox_net_allow\s*=\s*\[[^\]]*\]", ptext, re.M)
        if not m:
            report["steps"].append("policy: 无 local_sandbox_net_allow 键，跳过")
        else:
            items = re.findall(r"[\"']([^\"']+)[\"']", m.group(0))
            if not items:
                report["steps"].append(
                    "policy: local_sandbox_net_allow 已为空，跳过")
            else:
                # 连同紧邻上方的连续注释行一起替换：各机原注释写着「刻意不给公网」
                # 「**不要**留空，那等于放开全部外发」，只改值会留下与 [] 自相矛盾
                # 的说明，误导下一个读配置的人。原件已备份到 .bak-netopen。
                head = ptext[:m.start()].split("\n")
                if head and head[-1] == "":
                    head.pop()          # m 匹配在行首，故 head 以换行结尾
                i = len(head)
                while i > 0 and head[i - 1].lstrip().startswith("#"):
                    i -= 1
                new_text = ("\n".join(head[:i]) + ("\n" if i else "")
                            + _NET_OPEN_NOTE + "local_sandbox_net_allow = []"
                            + ptext[m.end():])
                if not dry:
                    bak = Path(str(policy) + ".bak-netopen")
                    if not bak.exists():
                        shutil.copy2(str(policy), str(bak))
                    policy.write_text(new_text, encoding="utf-8")
                report["steps"].append(
                    f"policy: local_sandbox_net_allow 置空（原 {len(items)} 项："
                    f"{','.join(items)}）→ 沙箱出网不再限制")

    # 3. 运行态
    if not (state / "state.json").exists() and (old / "state.json").exists():
        if not dry:
            shutil.copy2(str(old / "state.json"), str(state / "state.json"))
        report["steps"].append("state.json: 已迁移")
    old_rounds = old / "rounds"
    if old_rounds.is_dir():
        n = 0
        for p in old_rounds.iterdir():
            dst = state / "rounds" / p.name
            if p.is_file() and not dst.exists():
                if not dry:
                    shutil.copy2(str(p), str(dst))
                n += 1
        if n:
            report["steps"].append(f"rounds: 迁移 {n} 个文件")
    if not (state / "events.ndjson").exists() and \
            (old / "events.ndjson").exists():
        if not dry:
            shutil.copy2(str(old / "events.ndjson"),
                         str(state / "events.ndjson"))
        report["steps"].append("events.ndjson: 已迁移（legacy，30天后清）")

    # 4. integrity 基线
    if not dry:
        integrity.write_manifest(BASE, state)
    report["steps"].append("integrity: 基线 manifest 已写")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(WS))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    r = migrate(Path(args.workspace), dry=args.dry)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
