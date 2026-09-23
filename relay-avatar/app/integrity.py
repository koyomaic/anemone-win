"""app/integrity.py —— 代码真相自检（P3 基础版：漂移检测+告警+固化快照）。

provision 安装时写基线 run/integrity-manifest.json（app/*.py + avatar-cli 的 sha256）。
启动时校验：被本地手改 → 固化快照 run/local-drift-<ts>/ + 审计 + 钉钉告警（一日一组节流），
只告警不阻止启动。改动应走 git 提案流回仓库（机队以 git 为真源，夜间 provision 会覆盖）。
只读加固（chmod）与 propose/publish 提案流为 P3 完整项。
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(p.read_bytes())
    except Exception:
        return ""
    return h.hexdigest()


def code_files(base: Path) -> list:
    out = []
    for p in sorted((base / "app").glob("*.py")):
        out.append(p)
    cli = base / "avatar-cli"
    if cli.exists():
        out.append(cli)
    return out


def write_manifest(base: Path, state: Path) -> Path:
    man = {str(p.relative_to(base)): _sha256(p) for p in code_files(base)}
    out = state / "run" / "integrity-manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    import json
    out.write_text(json.dumps(man, indent=1), encoding="utf-8")
    return out


def check_drift(base: Path, state: Path):
    """None=无基线（首装）；{}=一致；{rel: (want, have)}=漂移明细。"""
    import json
    mf = state / "run" / "integrity-manifest.json"
    if not mf.exists():
        return None
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None
    drift = {}
    for rel, want in man.items():
        p = base / rel
        have = _sha256(p) if p.exists() else "(deleted)"
        if have != want:
            drift[rel] = (want[:12], have[:12])
    for p in code_files(base):
        rel = str(p.relative_to(base))
        if rel not in man:
            drift[rel] = ("(absent)", _sha256(p)[:12])
    return drift


def save_snapshot(base: Path, state: Path, drift: dict) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    d = state / "run" / f"local-drift-{ts}"
    d.mkdir(parents=True, exist_ok=True)
    for rel in drift:
        src = base / rel
        if src.exists():
            dst = d / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
    return d


def drift_signature(drift: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(sorted(drift.items())).encode()).hexdigest()[:16]


def drift_summary(drift: dict, limit: int = 12) -> str:
    items = [f"{k}（{v[0]}→{v[1]}）" for k, v in list(drift.items())[:limit]]
    more = f" 等{len(drift)}项" if len(drift) > limit else ""
    return "、".join(items) + more
