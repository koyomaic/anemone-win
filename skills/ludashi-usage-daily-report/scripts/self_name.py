#!/usr/bin/env python3
"""解析**本机**分身对外展示名（供使用日报标题/摘要/文件名用）。

为什么需要：ludashi-usage-daily-report 是机队共享技能（provision 21b 推给全部机器），
但报告抬头不能写死「鲁大师」——非鲁大师机（如 avatar_name=智微办）显示鲁大师就错了。

判据（用户 2026-10-15 拍板：本机保留鲁大师，仅其他机用 avatar_name）：
  1. 本机 bot 自称 identity.local.json 的 selfName == "鲁大师" → 用「鲁大师」（本机）；
  2. 否则 → 用 policy.toml [identity] avatar_name（其他机各自的分身名）；
  3. 都取不到 → 兜底 selfName，再兜底「鲁大师」（保持历史行为，不让报告开天窗）。

两个真源都是**本机 local、不入库、provision 不覆盖**：identity.local.json 在
shared/skills/dingtalk-send/，policy.toml 在 avatar/；故各机解析出各自的名字。

红线：只读名字字段，绝不回显 identity.local.json 里的 openId/token 等凭据。
"""
from __future__ import annotations

import json
import os
import tomllib

LUDASHI = "鲁大师"

_WORKSPACE = os.environ.get("RELAY_WORKSPACE", "/mnt/vol-eltaah12/workspace")

# identity.local.json（dingtalk-send 技能的本机 bot 身份，含 selfName）候选路径。
# 主位置随 RELAY_WORKSPACE 走；后两条是历史/备选根，与 recipients.py 的 policy 候选对齐。
_IDENTITY_CANDIDATES = [
    os.environ.get("USAGE_IDENTITY", ""),                       # ① 显式指定（排障用）
    os.path.join(_WORKSPACE, "shared", "skills", "dingtalk-send", "identity.local.json"),
    "/mnt/vol-eltaah12/workspace/shared/skills/dingtalk-send/identity.local.json",
    "/root/fenshen-ludashi/shared/skills/dingtalk-send/identity.local.json",
]

# policy.toml（含 [identity] avatar_name）候选路径，与 recipients.py 保持一致。
_POLICY_CANDIDATES = [
    os.path.join(_WORKSPACE, "avatar", "policy.toml"),
    "/mnt/vol-eltaah12/workspace/avatar/policy.toml",
    "/root/fenshen-ludashi/avatar/policy.toml",
]


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def self_name() -> str | None:
    """本机 bot 自称（identity.local.json 的 selfName）。取不到返回 None。"""
    p = _first_existing(_IDENTITY_CANDIDATES)
    if not p:
        return None
    try:
        o = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    v = o.get("selfName") if isinstance(o, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def avatar_name() -> str | None:
    """本机分身名（policy.toml [identity] avatar_name）。取不到返回 None。"""
    p = _first_existing(_POLICY_CANDIDATES)
    if not p:
        return None
    try:
        with open(p, "rb") as fh:
            d = tomllib.load(fh)
    except Exception:
        return None
    v = (d.get("identity") or {}).get("avatar_name") if isinstance(d, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def resolve_display_name() -> str:
    """返回本机报告对外展示名。

    本机（bot 自称鲁大师）→「鲁大师」；其他机→各自 avatar_name；都缺→兜底鲁大师。
    """
    sn = self_name()
    if sn == LUDASHI:
        return LUDASHI            # 本机＝鲁大师，保留
    av = avatar_name()
    if av:
        return av                 # 其他机：用各自分身名
    return sn or LUDASHI          # 兜底，绝不开天窗


if __name__ == "__main__":
    print(resolve_display_name())
