#!/usr/bin/env python3
"""app/migrate.py —— im-bridge → relay-avatar 状态迁移（provision 调用，幂等）。

迁移内容：
1. bridge.env → shared/secrets/relay-avatar.env（600；补 AVATAR_DASHBOARD_TOKEN 随机生成）
2. config.json 白/黑名单与 self_open_id → policy.toml（首装从模板渲染，已有不覆盖）
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
