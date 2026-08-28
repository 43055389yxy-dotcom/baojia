from __future__ import annotations

from app.core.config import Settings
from app.integrations.ai_gateway import AiGateway


def test_ai_gateway_uses_configured_https_proxy_without_parsing_no_proxy(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    gateway = AiGateway(Settings(_env_file=None, ai_trust_env_proxy=True))

    assert gateway._proxy_url() == "http://127.0.0.1:8080"


def test_ai_gateway_can_disable_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    gateway = AiGateway(Settings(_env_file=None, ai_trust_env_proxy=False))

    assert gateway._proxy_url() is None
