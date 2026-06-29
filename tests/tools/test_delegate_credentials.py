from __future__ import annotations

from tools import delegate_tool


def test_delegation_base_url_with_provider_resolves_provider_key(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_resolve_runtime_provider(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "ollama-cloud",
            "base_url": "https://ollama.com/v1",
            "api_key": "ollama-key",
            "api_mode": "chat_completions",
            "model": "glm-5.2",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    resolved = delegate_tool._resolve_delegation_credentials(
        {
            "provider": "ollama-cloud",
            "model": "glm-5.2",
            "base_url": "https://ollama.com/v1",
            "api_key": "",
        },
        parent_agent=object(),
    )

    assert calls == [
        {
            "requested": "ollama-cloud",
            "explicit_api_key": None,
            "explicit_base_url": "https://ollama.com/v1",
            "target_model": "glm-5.2",
        }
    ]
    assert resolved["provider"] == "ollama-cloud"
    assert resolved["base_url"] == "https://ollama.com/v1"
    assert resolved["api_key"] == "ollama-key"
    assert resolved["api_mode"] == "chat_completions"
    assert resolved["model"] == "glm-5.2"


def test_delegation_base_url_without_provider_keeps_parent_key_inheritance():
    resolved = delegate_tool._resolve_delegation_credentials(
        {
            "model": "local-model",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
        },
        parent_agent=object(),
    )

    assert resolved == {
        "model": "local-model",
        "provider": "custom",
        "base_url": "http://localhost:11434/v1",
        "api_key": None,
        "api_mode": "chat_completions",
    }


def test_delegation_base_url_provider_honors_explicit_api_mode(monkeypatch):
    def fake_resolve_runtime_provider(**_kwargs):
        return {
            "provider": "ollama-cloud",
            "base_url": "https://ollama.com/v1",
            "api_key": "ollama-key",
            "api_mode": "chat_completions",
            "model": "glm-5.2",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    resolved = delegate_tool._resolve_delegation_credentials(
        {
            "provider": "ollama-cloud",
            "model": "glm-5.2",
            "base_url": "https://ollama.com/v1",
            "api_key": "",
            "api_mode": "codex_responses",
        },
        parent_agent=object(),
    )

    assert resolved["api_key"] == "ollama-key"
    assert resolved["api_mode"] == "codex_responses"
