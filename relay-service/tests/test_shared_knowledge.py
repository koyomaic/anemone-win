from __future__ import annotations

import json
import os

from app.shared_knowledge import (load_shared_mcp, mcp_entry_from_file,
                                 prepare_run, shared_memory_text, slug_valid,
                                 strip_jsonc, _slim_memory_body)


class _Cfg:
    def __init__(self, shared_dir: str, xdg: str = "", exclude: list | None = None):
        self.shared_dir = shared_dir
        self.xdg_config_home = xdg
        self.relay_exclude_providers = exclude or []


def test_strip_jsonc_keeps_urls_and_drops_comments():
    txt = ('{\n "baseURL": "http://x:30131/v1", // trailing comment\n'
           ' "a": 1, /* block */ "b": [1, 2,],\n}')
    d = json.loads(strip_jsonc(txt))
    assert d["baseURL"] == "http://x:30131/v1"
    assert d["a"] == 1
    assert d["b"] == [1, 2]


def test_strip_jsonc_escapes():
    txt = r'{"s": "a \"q\" and // not comment", "n": 1,}'
    d = json.loads(strip_jsonc(txt))
    assert d["s"] == 'a "q" and // not comment' and d["n"] == 1


def test_slug_valid():
    assert slug_valid("my-skill_2.X")
    assert not slug_valid("")
    assert not slug_valid("../x")
    assert not slug_valid("a b")
    assert not slug_valid("a" * 60)


def test_mcp_entry_forms():
    assert mcp_entry_from_file("a", {"a": {"type": "local"}}) == {"type": "local"}
    assert mcp_entry_from_file("b", {"type": "remote", "url": "u"}) == {"type": "remote", "url": "u"}
    assert mcp_entry_from_file("c", {"x": {"y": 1}}) == {"y": 1}
    assert mcp_entry_from_file("d", "not-a-dict") == {}


def test_load_shared_mcp(tmp_path):
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "alpha.json").write_text(
        json.dumps({"type": "local", "command": ["python3", "a.py"], "enabled": True}))
    (tmp_path / "mcp" / "beta.json").write_text(
        json.dumps({"beta": {"type": "remote", "url": "http://b"}}))
    (tmp_path / "mcp" / "bad.json").write_text("not json")
    (tmp_path / "mcp" / "ignore.txt").write_text("x")
    m = load_shared_mcp(str(tmp_path))
    assert m["alpha"]["command"] == ["python3", "a.py"]
    assert m["beta"]["url"] == "http://b"
    assert "bad" not in m and "ignore" not in m


def test_load_shared_mcp_missing_dir(tmp_path):
    assert load_shared_mcp(str(tmp_path)) == {}


def test_shared_memory_text_empty(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "empty.md").write_text("   ")
    assert shared_memory_text(str(tmp_path)) is None


# 口径与红线.md 实测 59KB，每任务整体并入 CLAUDE.md（→61.5KB≈2 万 token 起步），
# 其中附录/溯源/度量口径/修订留痕对执行无约束力却每轮重放。注入时滤掉、原文留盘按需 Read。
_MEM_SAMPLE = """# 口径与红线（强约束）

> **修订留痕（2026-10-13，批次 P-99）：** 新增 R-GV-01；条文计数 1 → 2 条。
> **来源：** 从规则库抽取，只删减不改写。

## R-GV-01 测试红线（现行）

**若** 遇到 X，**则** 必须 Y。

- **红线：** 不得 Z。
- **溯源：** 《某规范 V1.0-20261013》。
- **度量口径与收益（P-43 新增）：** 每轮省 30% token。

## 附一、抽取对应表（规则编号｜源文件）

| 规则 | 源 |
|---|---|
| R-GV-01 | x |

## 附三、版本记录

| V1.0 | 2026-09-21 | 首次建立 |
"""


def test_slim_memory_body_drops_metadata_keeps_rules():
    slim, dropped = _slim_memory_body(_MEM_SAMPLE.strip())
    assert dropped is True
    # 可执行核心逐字保留
    assert "R-GV-01" in slim and "必须 Y" in slim and "不得 Z" in slim
    # 运行时元数据删除：前言修订留痕、溯源行、度量口径行、附一/附三整节
    assert "修订留痕" not in slim
    assert "《某规范 V1.0-20261013》" not in slim      # 溯源行内容
    assert "每轮省 30% token" not in slim               # 度量口径行内容
    assert "抽取对应表" not in slim and "版本记录" not in slim
    assert "## 附" not in slim


def test_slim_memory_body_no_metadata_unchanged():
    body = "## R-X 规则\n\n**若** A **则** B。\n- **红线：** 不得 C。"
    slim, dropped = _slim_memory_body(body)
    assert dropped is False and slim == body.strip()


def test_shared_memory_text_appends_ondemand_pointer(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "口径与红线.md").write_text(_MEM_SAMPLE, encoding="utf-8")
    text = shared_memory_text(str(tmp_path))
    assert "必须 Y" in text and "不得 Z" in text          # 核心保留
    assert "修订留痕" not in text and "抽取对应表" not in text  # 元数据已滤
    # 文末指引 agent 按需 Read 原文（机队每台本地都有该文件，不新建跨机派生件）
    assert "需要完整原文时 Read" in text
    assert "shared/memory/口径与红线.md" in text


def test_prepare_run_injects_shared(tmp_path):
    sh = tmp_path / "shared"
    (sh / "skills" / "myskill").mkdir(parents=True)
    (sh / "skills" / "myskill" / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: test skill\n---\nbody\n")
    (sh / "mcp").mkdir()
    (sh / "mcp" / "ping.json").write_text(
        json.dumps({"type": "local", "command": ["python3", "s.py"], "enabled": True}))
    (sh / "memory").mkdir()
    (sh / "memory" / "note.md").write_text("RULE-MARKER-42")
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)

    xdg = prepare_run(str(workdir), _Cfg(str(sh)))
    cfg_path = os.path.join(xdg, "opencode", "opencode.json")
    assert os.path.isfile(cfg_path)
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    assert str(sh / "skills") in cfg["skills"]["paths"]
    assert cfg["mcp"]["ping"]["command"] == ["python3", "s.py"]

    agents = (workdir / "AGENTS.md").read_text(encoding="utf-8")
    assert "RULE-MARKER-42" in agents
    assert (workdir / "home" / ".config" / "opencode" / "AGENTS.md").is_file()

    # 幂等：重复调用不报错，内容一致
    xdg2 = prepare_run(str(workdir), _Cfg(str(sh)))
    assert xdg2 == xdg
    assert json.load(open(cfg_path, encoding="utf-8"))["mcp"]["ping"]["command"] == ["python3", "s.py"]


def test_prepare_run_no_memory_no_agents_md(tmp_path):
    sh = tmp_path / "shared"
    (sh / "skills").mkdir(parents=True)
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)
    prepare_run(str(workdir), _Cfg(str(sh)))
    assert not (workdir / "AGENTS.md").exists()


def test_dws_seed_symlinked_into_home(tmp_path):
    sh = tmp_path / "shared"
    seed = sh / "secrets" / "dws-cli-seed" / "dws-cli"
    seed.mkdir(parents=True)
    (seed / "dek").write_text("seed-dek")
    (seed / "auth-token.enc").write_text("seed-token")
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)
    prepare_run(str(workdir), _Cfg(str(sh)))
    dst = workdir / "home" / ".local" / "share" / "dws-cli"
    assert dst.is_symlink()
    assert os.readlink(str(dst)) == os.path.abspath(str(seed))
    # 透过软链读到的是种子本体（同一份活凭证，非副本）
    assert (dst / "dek").read_text() == "seed-dek"
    assert (dst / "auth-token.enc").read_text() == "seed-token"
    # 幂等：重复调用（续跑）时旧软链被重建，仍指向种子
    prepare_run(str(workdir), _Cfg(str(sh)))
    assert dst.is_symlink()
    assert os.readlink(str(dst)) == os.path.abspath(str(seed))


def test_dws_seed_replaces_stale_copy_dir(tmp_path):
    sh = tmp_path / "shared"
    seed = sh / "secrets" / "dws-cli-seed" / "dws-cli"
    seed.mkdir(parents=True)
    (seed / "dek").write_text("seed-dek")
    workdir = tmp_path / "wd"
    local = workdir / "home" / ".local" / "share" / "dws-cli"
    local.mkdir(parents=True)
    (local / "dek").write_text("stale-copy-from-old-mode")
    prepare_run(str(workdir), _Cfg(str(sh)))
    # 旧复制模式残留的真实目录被替换为指向种子的软链
    assert local.is_symlink()
    assert os.readlink(str(local)) == os.path.abspath(str(seed))
    assert (local / "dek").read_text() == "seed-dek"


def test_dws_seed_missing_noop(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir(parents=True)
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)
    prepare_run(str(workdir), _Cfg(str(sh)))
    assert not (workdir / "home" / ".local").exists()


def _base_xdg_with_providers(tmp_path):
    base = tmp_path / "basecfg" / "opencode"
    base.mkdir(parents=True)
    (base / "opencode.json").write_text(json.dumps({
        "provider": {
            "prod": {"npm": "x", "models": {"old": {}}},
            "local-only": {"npm": "x", "models": {"new": {}}},
        },
        "model": "prod/old",
    }))
    return str(tmp_path / "basecfg")


def test_prepare_run_excludes_local_only_provider(tmp_path):
    sh = tmp_path / "shared"
    (sh / "skills").mkdir(parents=True)
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)
    xdg = prepare_run(str(workdir),
                      _Cfg(str(sh), xdg=_base_xdg_with_providers(tmp_path),
                           exclude=["local-only"]))
    cfg = json.load(open(os.path.join(xdg, "opencode", "opencode.json"),
                         encoding="utf-8"))
    assert "local-only" not in cfg["provider"]
    assert "prod" in cfg["provider"]
    assert cfg["model"] == "prod/old"


def test_prepare_run_exclude_never_removes_default_model_provider(tmp_path):
    sh = tmp_path / "shared"
    (sh / "skills").mkdir(parents=True)
    workdir = tmp_path / "wd"
    (workdir / "home").mkdir(parents=True)
    xdg = prepare_run(str(workdir),
                      _Cfg(str(sh), xdg=_base_xdg_with_providers(tmp_path),
                           exclude=["prod", "local-only"]))
    cfg = json.load(open(os.path.join(xdg, "opencode", "opencode.json"),
                         encoding="utf-8"))
    assert "prod" in cfg["provider"]      # 默认模型所在 provider 保留
    assert "local-only" not in cfg["provider"]
