from __future__ import annotations

import json
import os

import pytest

from app import shared_knowledge
from app.config import Settings
from app.shared_knowledge import (ensure_claude_external_redaction,
                                  ensure_external_redaction, prepare_run,
                                  redaction_health)


class _Cfg:
    def __init__(self, shared_dir: str, xdg: str = "", engine: str = "opencode", **kwargs):
        self.shared_dir = shared_dir
        self.xdg_config_home = xdg
        self.workspace_root = ""
        self.engine = engine
        self.model = kwargs.pop("model", "")
        self.relay_exclude_providers = kwargs.pop("exclude", [])
        self.external_model_redaction = kwargs.pop("external_model_redaction", "off")
        self.external_proxy_url = kwargs.pop("external_proxy_url", "")
        self.external_providers = kwargs.pop("external_providers", [])
        self.external_models = kwargs.pop("external_models", [])
        self.redaction_health_required = kwargs.pop("redaction_health_required", False)
        self.redaction_health_timeout = kwargs.pop("redaction_health_timeout", 2.0)


def _base_xdg(tmp_path, providers=None, model="local/model"):
    base = tmp_path / "basecfg" / "opencode"
    base.mkdir(parents=True)
    (base / "opencode.json").write_text(json.dumps({
        "provider": providers or {
            "external": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "https://external.example/v1", "apiKey": "real-key"},
                "models": {"adv": {}},
            },
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://local.example:30131/v1", "apiKey": "local-key"},
                "models": {"27b": {}},
            },
        },
        "model": model,
    }), encoding="utf-8")
    return str(tmp_path / "basecfg")


def _workdir(tmp_path):
    wd = tmp_path / "wd"
    (wd / "home").mkdir(parents=True, exist_ok=True)
    return wd


def test_settings_from_env_redaction(monkeypatch):
    monkeypatch.setenv("RELAY_EXTERNAL_MODEL_REDACTION", "required")
    monkeypatch.setenv("RELAY_EXTERNAL_PROXY_URL", "http://127.0.0.1:8792")
    monkeypatch.setenv("RELAY_EXTERNAL_PROVIDERS", "aigateway, other ")
    monkeypatch.setenv("RELAY_EXTERNAL_MODELS", "qwen3.8-flash,qwen3.8-max")
    monkeypatch.setenv("RELAY_REDACTION_HEALTH_REQUIRED", "1")
    monkeypatch.setenv("RELAY_REDACTION_HEALTH_TIMEOUT", "3.5")
    cfg = Settings.from_env()
    assert cfg.external_model_redaction == "required"
    assert cfg.external_proxy_url == "http://127.0.0.1:8792"
    assert cfg.external_providers == ["aigateway", "other"]
    assert cfg.external_models == ["qwen3.8-flash", "qwen3.8-max"]
    assert cfg.redaction_health_required is True
    assert cfg.redaction_health_timeout == 3.5


def test_prepare_run_rewrites_external_provider_only(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    cfg = _Cfg(
        str(sh),
        xdg=_base_xdg(tmp_path),
        external_model_redaction="required",
        external_proxy_url="http://127.0.0.1:8792",
        external_providers=["external"],
    )
    xdg = prepare_run(str(_workdir(tmp_path)), cfg)
    out = json.load(open(os.path.join(xdg, "opencode", "opencode.json"), encoding="utf-8"))
    assert out["provider"]["external"]["options"]["baseURL"] == "http://127.0.0.1:8792/v1"
    assert out["provider"]["external"]["options"]["apiKey"] == shared_knowledge.REDACTION_DUMMY_API_KEY
    assert out["provider"]["local"]["options"]["baseURL"] == "http://local.example:30131/v1"
    assert out["provider"]["local"]["options"]["apiKey"] == "local-key"


def test_prepare_run_rewrites_default_external_provider(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    cfg = _Cfg(
        str(sh),
        xdg=_base_xdg(tmp_path, model="external/adv"),
        external_model_redaction="required",
        external_proxy_url="http://127.0.0.1:8792",
        external_providers=["external"],
    )
    xdg = prepare_run(str(_workdir(tmp_path)), cfg)
    out = json.load(open(os.path.join(xdg, "opencode", "opencode.json"), encoding="utf-8"))
    assert out["model"] == "external/adv"
    assert out["provider"]["external"]["options"]["baseURL"] == "http://127.0.0.1:8792/v1"


def test_required_redaction_needs_proxy_and_providers(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    base = _base_xdg(tmp_path)
    with pytest.raises(RuntimeError, match="RELAY_EXTERNAL_PROXY_URL"):
        prepare_run(str(_workdir(tmp_path)), _Cfg(str(sh), xdg=base, external_model_redaction="required"))
    with pytest.raises(RuntimeError, match="RELAY_EXTERNAL_PROVIDERS"):
        prepare_run(str(_workdir(tmp_path)), _Cfg(
            str(sh), xdg=base, external_model_redaction="required",
            external_proxy_url="http://127.0.0.1:8792"))


def test_health_required_blocks_when_proxy_unhealthy(monkeypatch, tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    calls = []

    def fake_health(cfg):
        calls.append(cfg)
        raise RuntimeError("外部模型脱敏代理不可达")

    monkeypatch.setattr(shared_knowledge, "redaction_health", fake_health)
    cfg = _Cfg(
        str(sh),
        xdg=_base_xdg(tmp_path),
        external_model_redaction="required",
        external_proxy_url="http://127.0.0.1:8792",
        external_providers=["external"],
        redaction_health_required=True,
    )
    with pytest.raises(RuntimeError, match="不可达"):
        prepare_run(str(_workdir(tmp_path)), cfg)
    assert calls


def test_redaction_health_validates_strict_ready(monkeypatch):
    class Resp:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        assert req.full_url == "http://127.0.0.1:8792/healthz"
        return Resp({"ok": True, "redaction": {"mode": "strict", "ready": True}})

    monkeypatch.setattr(shared_knowledge.urllib.request, "urlopen", fake_urlopen)
    cfg = _Cfg("", external_model_redaction="required", external_proxy_url="http://127.0.0.1:8792")
    assert redaction_health(cfg)["ok"] is True

    def bad_urlopen(req, timeout=None):
        return Resp({"ok": True, "redaction": {"mode": "redact-only", "ready": True}})

    monkeypatch.setattr(shared_knowledge.urllib.request, "urlopen", bad_urlopen)
    with pytest.raises(RuntimeError, match="strict ready"):
        redaction_health(cfg)


def test_claude_guard_checks_external_model(monkeypatch):
    calls = []
    monkeypatch.setattr(shared_knowledge, "redaction_health", lambda cfg: calls.append(cfg))
    cfg = _Cfg(
        "",
        engine="claude",
        model="qwen3.8-flash",
        external_model_redaction="required",
        external_proxy_url="http://127.0.0.1:8792",
        external_models=["qwen3.8-flash"],
    )
    ensure_claude_external_redaction(cfg)
    assert calls

    calls.clear()
    cfg.model = "local-27b"
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    ensure_claude_external_redaction(cfg)
    assert not calls

    calls.clear()
    cfg.redaction_health_required = True
    ensure_claude_external_redaction(cfg)
    assert calls


def test_off_mode_does_not_touch_providers(tmp_path):
    sh = tmp_path / "shared"
    sh.mkdir()
    cfg = _Cfg(str(sh), xdg=_base_xdg(tmp_path))
    xdg = prepare_run(str(_workdir(tmp_path)), cfg)
    out = json.load(open(os.path.join(xdg, "opencode", "opencode.json"), encoding="utf-8"))
    assert out["provider"]["external"]["options"]["baseURL"] == "https://external.example/v1"
    assert out["provider"]["external"]["options"]["apiKey"] == "real-key"
